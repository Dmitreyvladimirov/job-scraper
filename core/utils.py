import ipaddress
import re
import socket
import time
import logging
import requests
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

logger = logging.getLogger(__name__)


class _HTMLStripper(HTMLParser):
    def __init__(self):
        super().__init__()
        self._parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self._parts.append(data)

    def get_text(self) -> str:
        return " ".join(self._parts)


def strip_html(text: str) -> str:
    """Remove HTML tags and decode entities from job board descriptions."""
    if not text:
        return text
    stripper = _HTMLStripper()
    try:
        stripper.feed(text)
        result = stripper.get_text()
    except Exception:
        result = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", result).strip()


_ALLOWED_SCHEMES = ("http", "https")


def _is_safe_host(hostname: str) -> bool:
    """SSRF guard: reject hosts that resolve to loopback/link-local/private/reserved
    addresses (RFC1918, ULA, 169.254.0.0/16, 0.0.0.0, etc.) — scraped URLs (Telegram
    messages, aggregator listings) are untrusted input. Blocks by IP, not by name, so
    it can't be bypassed with a hostname that merely *contains* a safe-looking substring."""
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if ip.is_loopback or ip.is_link_local or ip.is_private or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            return False
    return True


def _validate_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    return parsed.scheme in _ALLOWED_SCHEMES and bool(parsed.hostname) and _is_safe_host(parsed.hostname)


def _url_host_matches(url: str, hosts: tuple[str, ...]) -> bool:
    """True if url's actual hostname is one of `hosts` (or a subdomain of one) — unlike a
    raw substring check, this can't be fooled by e.g. 'evil.com/remoteworldwide.net' or
    'remoteworldwide.net.evil.com'."""
    try:
        hostname = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    return any(hostname == h or hostname.endswith("." + h) for h in hosts)


def _safe_get(url: str, **kwargs):
    """requests.get() with an SSRF guard: validates the host before the initial request,
    then follows redirects manually (allow_redirects=False) re-validating the target host
    on every hop, instead of trusting requests to follow a redirect chain unchecked."""
    kwargs.pop("allow_redirects", None)
    for _ in range(5):
        if not _validate_url(url):
            raise ValueError(f"Blocked unsafe URL: {url}")
        resp = requests.get(url, allow_redirects=False, **kwargs)
        if resp.is_redirect or resp.is_permanent_redirect:
            location = resp.headers.get("Location")
            if not location:
                return resp
            url = urljoin(url, location)
            continue
        return resp
    raise ValueError(f"Too many redirects for {url}")


_PLATFORM_HOSTS = ("remoteok.com", "arbeitnow.com", "weworkremotely.com", "jobgether.com", "jobicy.com")

# Sites whose own listing page already embeds the real apply link in server-rendered HTML —
# no Greenhouse/Lever/Ashby slug-guessing needed, just read the anchor. Higher-confidence than
# find_apply_url() since it's not a name-based guess, and works for any ATS the company uses.
_DIRECT_ANCHOR_HOSTS = ("remoteworldwide.net",)

_COMPANY_SUFFIXES = re.compile(
    r"\b(inc|ltd|llc|gmbh|sas|bv|ag|corp|co|oy|ab|as|sa|plc|pte|pty|srl|sl)\b\.?",
    re.IGNORECASE,
)


def normalize_job_key(company: str, title: str) -> tuple[str, str]:
    """Normalize (company, title) for cross-source deduplication."""
    def _norm(text: str) -> str:
        text = _COMPANY_SUFFIXES.sub("", text.lower())
        return re.sub(r"[^a-z0-9 ]", " ", text).split()

    return (" ".join(_norm(company)), " ".join(_norm(title)))


def _slugify(company: str) -> list[str]:
    base = company.lower().strip()
    variants = [
        re.sub(r"[^a-z0-9]+", "-", base).strip("-"),
        re.sub(r"[^a-z0-9]+", "", base),
    ]
    # also strip common suffixes
    cleaned = re.sub(r"\b(inc|llc|ltd|co|corp|group)\b", "", variants[0]).strip("-")
    if cleaned not in variants:
        variants.append(cleaned)
    return list(dict.fromkeys(variants))  # deduplicate, preserve order


def _title_match(a: str, b: str) -> bool:
    words = [w for w in a.lower().split() if len(w) > 3]
    return sum(1 for w in words if w in b.lower()) >= min(2, len(words))


def _company_match(query: str, candidate: str) -> bool:
    """True if candidate company name is close enough to query (prevents e.g. 'Insider' → 'Business Insider')."""
    q = re.sub(r"[^a-z0-9 ]", "", query.lower()).split()
    c = re.sub(r"[^a-z0-9 ]", "", candidate.lower()).split()
    if not q or not c:
        return False
    shared = sum(1 for w in q if w in c)
    # All query words must appear in candidate; extra words allowed ≤ half of query length
    return shared == len(q) and (len(c) - shared) <= max(0, len(q) // 2)


def _page_title(url: str) -> str:
    """Best-effort <title> text of a job-board page — used as a weak company-name signal
    for ATS platforms (Lever, Ashby) whose posting APIs don't return an org name field."""
    try:
        r = _safe_get(url, timeout=5, headers=_GENERIC_HEADERS)
        if r.status_code == 200:
            m = re.search(r"<title>(.*?)</title>", r.text, re.I | re.S)
            if m:
                return unescape(m.group(1)).strip()
    except Exception:
        pass
    return ""


def find_apply_url(company: str, title: str) -> str | None:
    """Try Greenhouse → Lever → Ashby to get a direct apply URL."""
    for slug in _slugify(company):
        # Greenhouse
        try:
            meta = requests.get(
                f"https://boards-api.greenhouse.io/v1/boards/{slug}",
                timeout=5,
            )
            if meta.status_code == 200 and _company_match(company, meta.json().get("name", "")):
                r = requests.get(
                    f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
                    timeout=5,
                )
                if r.status_code == 200:
                    for j in r.json().get("jobs", []):
                        if _title_match(title, j.get("title", "")):
                            return j.get("absolute_url")
        except Exception:
            pass

        # Lever — API has no company-name field, so verify against the board page's <title>
        # (reliably just the bare company name, e.g. "Contentsquare") before trusting a match.
        try:
            r = requests.get(
                f"https://api.lever.co/v0/postings/{slug}",
                params={"mode": "json"},
                timeout=5,
            )
            if r.status_code == 200:
                match = next((p for p in r.json() if _title_match(title, p.get("text", ""))), None)
                if match:
                    lever_name = _page_title(f"https://jobs.lever.co/{slug}")
                    if lever_name and _company_match(company, lever_name):
                        return match.get("hostedUrl")
        except Exception:
            pass

        # Ashby — GraphQL schema has no queryable org-name field either; same <title> check,
        # stripping the "... Jobs" suffix Ashby's board pages typically use. Some org boards
        # render client-side and give an empty/generic title — in that case we skip rather
        # than trust an unverified match, at the cost of some false negatives.
        try:
            r = requests.post(
                "https://jobs.ashbyhq.com/api/non-user-graphql",
                json={
                    "operationName": "ApiJobBoardWithTeams",
                    "variables": {"organizationHostedJobsPageName": slug},
                    "query": (
                        "query ApiJobBoardWithTeams($organizationHostedJobsPageName: String!) {"
                        "  jobBoard: jobBoardWithTeams(organizationHostedJobsPageName: $organizationHostedJobsPageName) {"
                        "    jobPostings { id title } } }"
                    ),
                },
                timeout=5,
            )
            if r.status_code == 200:
                postings = (
                    r.json()
                    .get("data", {})
                    .get("jobBoard", {})
                    .get("jobPostings") or []
                )
                match = next((p for p in postings if _title_match(title, p.get("title", ""))), None)
                if match:
                    ashby_name = re.sub(r"\s+Jobs$", "", _page_title(f"https://jobs.ashbyhq.com/{slug}"), flags=re.I).strip()
                    if ashby_name and _company_match(company, ashby_name):
                        return f"https://jobs.ashbyhq.com/{slug}/{match['id']}"
        except Exception:
            pass

    return None


def fetch_jd_from_url(url: str) -> str:
    """Fetch full job description from a direct Greenhouse / Lever / Ashby URL."""
    if not url:
        return ""
    try:
        if "jobs.lever.co" in url:
            parts = url.rstrip("/").split("/")
            company, uuid = parts[-2], parts[-1]
            r = requests.get(f"https://api.lever.co/v0/postings/{company}/{uuid}", timeout=8)
            if r.status_code == 200:
                data = r.json()
                return strip_html(data.get("descriptionPlain") or data.get("description") or "")

        elif "boards.greenhouse.io" in url or "boards-api.greenhouse.io" in url:
            parts = url.rstrip("/").split("/")
            job_id, company = parts[-1], parts[-3]
            r = requests.get(
                f"https://boards-api.greenhouse.io/v1/boards/{company}/jobs/{job_id}",
                timeout=8,
            )
            if r.status_code == 200:
                # Greenhouse returns HTML-entity-encoded content — unescape before stripping tags
                return strip_html(unescape(r.json().get("content") or ""))

        elif "jobs.ashbyhq.com" in url:
            parts = url.rstrip("/").split("/")
            company, job_id = parts[-2], parts[-1]
            r = requests.post(
                "https://api.ashbyhq.com/posting-api/graphql",
                json={
                    "operationName": "ApiJobPosting",
                    "query": (
                        "query ApiJobPosting($organizationHostedJobsPageName: String!, $jobPostingId: String!) {"
                        "  jobPosting(organizationHostedJobsPageName: $organizationHostedJobsPageName,"
                        "             jobPostingId: $jobPostingId) {"
                        "    descriptionSections { descriptionHtml } } }"
                    ),
                    "variables": {"organizationHostedJobsPageName": company, "jobPostingId": job_id},
                },
                timeout=8,
            )
            if r.status_code == 200:
                sections = (
                    r.json().get("data", {}).get("jobPosting", {}).get("descriptionSections") or []
                )
                return strip_html(" ".join(s.get("descriptionHtml", "") for s in sections))
    except Exception as e:
        logger.debug(f"fetch_jd_from_url failed for {url}: {e}")
    return ""


def _extract_direct_anchor(url: str) -> str | None:
    """Read the real 'Apply Now' target straight out of server-rendered HTML — used for
    aggregators (currently remoteworldwide.net) that embed the true apply href directly,
    rather than requiring a Greenhouse/Lever/Ashby name-guess."""
    try:
        r = _safe_get(url, timeout=8, headers=_GENERIC_HEADERS)
        if r.status_code != 200:
            return None
        idx = r.text.find("Apply Now")
        if idx == -1:
            return None
        last_a = r.text.rfind("<a ", 0, idx)
        if last_a == -1:
            return None
        m = re.search(r'href="([^"]+)"', r.text[last_a:idx])
        if m:
            return m.group(1)
    except Exception as e:
        logger.debug(f"_extract_direct_anchor failed for {url}: {e}")
    return None


def enrich_url(job: dict) -> None:
    """If job URL is a job-platform page, try to find the company's direct apply URL."""
    url = job.get("url", "")

    if _url_host_matches(url, _DIRECT_ANCHOR_HOSTS):
        direct = _extract_direct_anchor(url)
        if direct:
            job["apply_url"] = direct
            logger.info(f"URL enriched (direct anchor): {job['company']} → {direct}")
        return

    if not _url_host_matches(url, _PLATFORM_HOSTS):
        return
    direct = find_apply_url(job.get("company", ""), job.get("title", ""))
    if direct:
        job["apply_url"] = direct
        logger.info(f"URL enriched: {job['company']} → {direct}")


_GENERIC_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; job-scraper/1.0)"}

# Tags whose full content (including children) we drop before text extraction
_DROP_TAGS = re.compile(
    r"<(script|style|nav|header|footer|aside|noscript)(\s[^>]*)?>.*?</\1>",
    re.DOTALL | re.IGNORECASE,
)


def fetch_url_generic(url: str, max_chars: int = 6000) -> str:
    """Fetch an arbitrary web page and return its readable text content.

    Used as a fallback for job pages not served by Greenhouse / Lever / Ashby APIs.
    Works well for server-rendered pages; may return sparse content for JS-only SPAs.
    """
    if not url:
        return ""
    try:
        resp = _safe_get(url, headers=_GENERIC_HEADERS, timeout=12)
        resp.raise_for_status()
        content_type = resp.headers.get("content-type", "")
        if "text/html" not in content_type and "text/plain" not in content_type:
            return ""
        html = _DROP_TAGS.sub(" ", resp.text)
        text = strip_html(unescape(html))
        return text[:max_chars]
    except Exception as e:
        logger.debug(f"fetch_url_generic failed for {url}: {e}")
        return ""


def retry(fn, retries: int = 3, backoff: float = 2.0):
    """Call fn(), retry on exception with exponential backoff."""
    for attempt in range(retries):
        try:
            return fn()
        except Exception as e:
            if attempt == retries - 1:
                raise
            wait = backoff ** attempt
            logger.warning(f"Attempt {attempt + 1}/{retries} failed: {e}. Retry in {wait:.0f}s...")
            time.sleep(wait)
