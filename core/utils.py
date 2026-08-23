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
    return fetch_posting(url).get("description", "")


def fetch_posting(url: str) -> dict:
    """Description PLUS the title the ATS already knows, for a direct Greenhouse /
    Lever / Ashby URL.

    The scraper only ever needed the text, and fetch_jd_from_url() above stays its
    unchanged entry point. The intake path (intake.py) also needs a title, and all
    three posting APIs return one in the same response - re-fetching the page
    afterwards just to scrape <title> would be slower and less accurate.

    Company is deliberately NOT returned: none of the three APIs carries a real
    company name, only the board slug ("thecompany" for Acme Inc.), so intake
    extracts the true name from the JD text instead of trusting a slug.

    "location" is present only for Ashby, whose board endpoint states it outright.
    It is worth threading through: location is 15 of the 100 scoring points, and an
    authoritative value beats one the model infers from prose.

    Returns {} for a non-platform URL or any failure - callers fall back to
    fetch_url_generic().
    """
    if not url:
        return {}
    try:
        if "jobs.lever.co" in url:
            parts = url.rstrip("/").split("/")
            company, uuid = parts[-2], parts[-1]
            r = requests.get(f"https://api.lever.co/v0/postings/{company}/{uuid}", timeout=8)
            if r.status_code == 200:
                data = r.json()
                return {
                    "description": strip_html(
                        data.get("descriptionPlain") or data.get("description") or ""),
                    "title": (data.get("text") or "").strip(),
                }

        elif "boards.greenhouse.io" in url or "boards-api.greenhouse.io" in url:
            parts = url.rstrip("/").split("/")
            job_id, company = parts[-1], parts[-3]
            r = requests.get(
                f"https://boards-api.greenhouse.io/v1/boards/{company}/jobs/{job_id}",
                timeout=8,
            )
            if r.status_code == 200:
                data = r.json()
                return {
                    # Greenhouse returns HTML-entity-encoded content - unescape before stripping tags
                    "description": strip_html(unescape(data.get("content") or "")),
                    "title": (data.get("title") or "").strip(),
                }

        elif "jobs.ashbyhq.com" in url:
            # Ashby's posting-api/graphql endpoint started answering 401 to
            # unauthenticated callers (confirmed 2026-08-23 on a live posting, with
            # and without the `title` field, so it is the endpoint and not the
            # query). The documented public board endpoint still works and is
            # strictly better: it returns descriptionPlain already as text, plus the
            # title and location the GraphQL query never carried. One extra board
            # fetch per posting is the cost; boards are small (16 jobs here).
            parts = url.rstrip("/").split("/")
            company, job_id = parts[-2], parts[-1]
            r = requests.get(
                f"https://api.ashbyhq.com/posting-api/job-board/{company}", timeout=10)
            if r.status_code == 200:
                for posting in r.json().get("jobs", []):
                    if posting.get("id") != job_id:
                        continue
                    body = posting.get("descriptionPlain") or posting.get("descriptionHtml") or ""
                    return {
                        "description": strip_html(body),
                        "title": (posting.get("title") or "").strip(),
                        "location": (posting.get("location") or "").strip(),
                    }
    except Exception as e:
        logger.debug(f"fetch_posting failed for {url}: {e}")
    return {}


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


def _fetch_via_scrapingbee(url: str, render_js: bool = True) -> str:
    """Fetch a page through ScrapingBee (bypasses Cloudflare, renders JS) — costs
    credits, so only used as a fallback when the free find_apply_url() name-guess
    comes up empty. Requires SCRAPINGBEE_API_KEY; returns "" if unset or on failure."""
    from config import SCRAPINGBEE_API_KEY
    if not SCRAPINGBEE_API_KEY:
        return ""
    try:
        r = requests.get(
            "https://app.scrapingbee.com/api/v1/",
            params={"api_key": SCRAPINGBEE_API_KEY, "url": url, "render_js": "true" if render_js else "false"},
            timeout=90,
        )
        if r.status_code == 200:
            return r.text
    except Exception as e:
        logger.debug(f"_fetch_via_scrapingbee failed for {url}: {e}")
    return ""


def _extract_jobgether_apply_url(html: str) -> str | None:
    """Jobgether embeds the real apply URL directly in the rendered page as
    data-apply-url="..." on its <apply-button> element."""
    m = re.search(r'data-apply-url="([^"]+)"', html)
    return unescape(m.group(1)) if m else None


def _extract_jobicy_apply_url(html: str) -> str | None:
    """Jobicy resolves the real apply URL client-side via a POST to /signals.php with a
    per-page-load nonce (deliberately not present as a plain href, even after JS render —
    the button lives in a closed shadow DOM). Replay that same call with plain requests;
    the nonce/action/post_id triple is still readable in the rendered page's inline script."""
    m = re.search(r"var asteroid=\{'action':'([^']+)','nonce':'([^']+)','post_id':(\d+)", html)
    if not m:
        return None
    action, nonce, post_id = m.groups()
    try:
        r = requests.post(
            "https://jobicy.com/signals.php",
            data={"action": action, "nonce": nonce, "post_id": post_id, "increment_clicks": "true"},
            headers={**_GENERIC_HEADERS, "X-Jobicy-Ajax": "true"},
            timeout=15,
        )
        if r.status_code == 200:
            return r.json().get("url")
    except Exception as e:
        logger.debug(f"_extract_jobicy_apply_url failed: {e}")
    return None


def _fetch_via_unlocker(job_url: str) -> str | None:
    """Fallback for Jobgether/Jobicy when find_apply_url()'s Greenhouse/Lever/Ashby
    name-guess finds nothing — fetches the real page (Cloudflare-bypassed / JS-rendered)
    and reads the site's own authoritative apply link, so it works no matter which ATS
    the company actually uses. Costs ScrapingBee credits; skipped entirely if unset."""
    host = (urlparse(job_url).hostname or "").lower()
    if "jobgether.com" not in host and "jobicy.com" not in host:
        return None
    html = _fetch_via_scrapingbee(job_url)
    if not html:
        return None
    if "jobgether.com" in host:
        return _extract_jobgether_apply_url(html)
    return _extract_jobicy_apply_url(html)


def enrich_url(job: dict) -> None:
    """If job URL is a job-platform page, try to find the company's direct apply URL."""
    url = job.get("url", "")

    # A source that already supplied a direct apply link (Jobgether pages embed
    # their own applyUrl since 2026-08-19) must win: find_apply_url() matches by
    # a weak 2-word title overlap and can land on a DIFFERENT position at the
    # same company (live bug: Deliveroo/Smartsheet/Vanta cards pointed at the
    # wrong postings).
    if job.get("apply_url") and job["apply_url"] != url:
        return

    if _url_host_matches(url, _DIRECT_ANCHOR_HOSTS):
        direct = _extract_direct_anchor(url)
        if direct:
            job["apply_url"] = direct
            logger.info(f"URL enriched (direct anchor): {job['company']} → {direct}")
        return

    if not _url_host_matches(url, _PLATFORM_HOSTS):
        return
    direct = find_apply_url(job.get("company", ""), job.get("title", ""))
    if not direct:
        direct = _fetch_via_unlocker(url)
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
