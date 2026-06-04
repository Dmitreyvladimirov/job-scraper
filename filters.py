from config import PM_ROLE_KEYWORDS, ISRAEL_KEYWORDS, REMOTE_KEYWORDS, EXCLUDE_LOCATION_PATTERNS


def passes_role_filter(job: dict) -> bool:
    title = job.get("title", "").lower()
    return any(kw in title for kw in PM_ROLE_KEYWORDS)


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
