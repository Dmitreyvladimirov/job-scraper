import re
import time
import logging
import requests
from html import unescape
from html.parser import HTMLParser

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


_PLATFORM_HOSTS = ("remoteok.com", "arbeitnow.com", "weworkremotely.com")

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

        # Lever
        try:
            r = requests.get(
                f"https://api.lever.co/v0/postings/{slug}",
                params={"mode": "json"},
                timeout=5,
            )
            if r.status_code == 200:
                for p in r.json():
                    if _title_match(title, p.get("text", "")):
                        return p.get("hostedUrl")
        except Exception:
            pass

        # Ashby
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
                for p in postings:
                    if _title_match(title, p.get("title", "")):
                        return f"https://jobs.ashbyhq.com/{slug}/{p['id']}"
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


def enrich_url(job: dict) -> None:
    """If job URL is a job-platform page, try to find the company's direct apply URL."""
    if not any(h in job.get("url", "").lower() for h in _PLATFORM_HOSTS):
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
        resp = requests.get(url, headers=_GENERIC_HEADERS, timeout=12, allow_redirects=True)
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
