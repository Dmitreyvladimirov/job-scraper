import re
import time
import logging
import requests
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


_PLATFORM_HOSTS = ("remoteok.com", "arbeitnow.com")


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


def find_apply_url(company: str, title: str) -> str | None:
    """Try Greenhouse → Lever → Ashby to get a direct apply URL."""
    for slug in _slugify(company):
        # Greenhouse
        try:
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


def enrich_url(job: dict) -> None:
    """If job URL is a job-platform page, try to find the company's direct apply URL."""
    if not any(h in job.get("url", "") for h in _PLATFORM_HOSTS):
        return
    direct = find_apply_url(job.get("company", ""), job.get("title", ""))
    if direct:
        job["apply_url"] = direct
        logger.info(f"URL enriched: {job['company']} → {direct}")


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
