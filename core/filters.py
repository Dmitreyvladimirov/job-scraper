import re
from datetime import date
from config import PM_ROLE_KEYWORDS, ISRAEL_KEYWORDS, REMOTE_KEYWORDS, EXCLUDE_LOCATION_PATTERNS, MAX_JOB_AGE_DAYS

# German job posting markers — exclusively used in German HR conventions
_DE_MARKERS = re.compile(
    r"\(m/w/d\)|\(w/m/d\)|\(f/m/d\)|\(d/w/m\)|\bm/w/d\b|:in\b",
    re.IGNORECASE,
)

# Stop words distinctive to blocked languages — unlikely to appear in English/Spanish/Russian text
_BLOCKED_STOPWORDS = {
    # German
    "und", "oder", "für", "nicht", "nach", "wird", "sind", "dem", "des",
    "zum", "zur", "können", "werden", "haben", "durch", "sowie", "beim",
    # French
    "pour", "dans", "avec", "vous", "nous", "sont", "notre", "votre",
    "cette", "aussi", "comme", "dont", "donc", "leurs",
    # Dutch
    "zijn", "worden", "heeft", "kunnen", "maar", "jouw", "onze", "naar",
}


def _requires_russian(text: str) -> bool:
    return "russian" in text.lower() or "русск" in text.lower()


def passes_language_filter(job: dict) -> bool:
    """Allow English, Spanish, Russian. Block German, French, Dutch, and similar.
    Exception: non-English jobs that explicitly require Russian language proficiency."""
    title = job.get("title", "")
    desc = (job.get("description") or "")[:600]
    combined = f"{title} {desc}"

    # Exception: job requires Russian → always pass
    if _requires_russian(combined):
        return True

    # Hard block: German HR markers (m/w/d, :in suffix)
    if _DE_MARKERS.search(combined):
        return False

    # Soft block: 3+ distinctive non-English stop words
    words = set(re.findall(r"\b[a-z]+\b", combined.lower()))
    return sum(1 for w in words if w in _BLOCKED_STOPWORDS) < 3


_RUSSIA_CITIES = re.compile(
    r"\b("
    r"москв[аеуи]?|moscow|московск\w*|г\.?\s*москва|"
    r"санкт.?петербург|питер|петербург|спб|saint.?petersburg|"
    r"новосибирск|екатеринбург|казань|краснодар|"
    r"ростов.на.дону|ростов|нижний.новгород|уфа|самара|"
    r"омск|воронеж|пермь|волгоград|красноярск|челябинск|"
    r"тюмень|кемерово|томск|иркутск|барнаул|ставрополь|тула"
    r")\b",
    re.IGNORECASE,
)

_RUSSIA_KEYWORDS = re.compile(
    r"\b(россия|russia|российск\w*|рф|r\.?f\.?\b|в\s+россию|из\s+России)\b",
    re.IGNORECASE,
)


def is_russia_based(job: dict) -> bool:
    """Return True if the job is likely office-based in Russia or from a Russian company."""
    fields = [
        job.get("location", ""),
        job.get("company", ""),
        (job.get("_message_text") or "")[:800],
        (job.get("description") or "")[:800],
    ]
    combined = " ".join(fields)
    return bool(_RUSSIA_CITIES.search(combined) or _RUSSIA_KEYWORDS.search(combined))


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
