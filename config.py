import os

NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "")
NOTION_DATABASE_ID = "f71f92e0-c976-4cf2-bb56-8063b5cea681"

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")


def validate_secrets() -> None:
    """Call once at startup to fail fast on missing secrets."""
    missing = [k for k in ("NOTION_TOKEN", "ANTHROPIC_API_KEY", "TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID")
               if not os.environ.get(k)]
    if missing:
        raise EnvironmentError(f"Missing required env vars: {', '.join(missing)}")

ATS_THRESHOLD = 60

PM_ROLE_KEYWORDS = [
    "product manager",
    "head of product",
    "product lead",
    "chief product officer",
    "cpo",
    "group pm",
    "principal pm",
    "vp product",
    "vp of product",
    "director of product",
    "product director",
    "product owner",
]

ISRAEL_KEYWORDS = [
    "israel", "tel aviv", "tlv", "herzliya", "ra'anana", "raanana",
    "petah tikva", "haifa", "netanya",
]

REMOTE_KEYWORDS = [
    "remote", "worldwide", "anywhere", "global",
    "work from anywhere", "wfa", "distributed",
]

# Locations that explicitly restrict to regions other than Israel
EXCLUDE_LOCATION_PATTERNS = [
    "us only", "usa only", "united states only", "north america only",
    "europe only", "eu only", "uk only", "australia only", "canada only",
    "latam only", "latin america only", "apac only",
]
