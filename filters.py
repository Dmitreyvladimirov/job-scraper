from datetime import date
from config import PM_ROLE_KEYWORDS, ISRAEL_KEYWORDS, REMOTE_KEYWORDS, EXCLUDE_LOCATION_PATTERNS, MAX_JOB_AGE_DAYS


def passes_role_filter(job: dict) -> bool:
    title = job.get("title", "").lower()
    return any(kw in title for kw in PM_ROLE_KEYWORDS)


def passes_date_filter(job: dict) -> bool:
    """Return False if job is older than MAX_JOB_AGE_DAYS. Missing date → pass."""
    if not MAX_JOB_AGE_DAYS:
        return True
    published = job.get("published", "")
    if not published:
        return True
    try:
        age = (date.today() - date.fromisoformat(published)).days
        return age <= MAX_JOB_AGE_DAYS
    except ValueError:
        return True


def passes_location_filter(job: dict) -> bool:
    location = job.get("location", "").lower()

    if any(pat in location for pat in EXCLUDE_LOCATION_PATTERNS):
        return False

    # Empty = assume worldwide remote
    if not location.strip():
        return True

    if any(kw in location for kw in REMOTE_KEYWORDS):
        return True

    if any(kw in location for kw in ISRAEL_KEYWORDS):
        return True

    return False
