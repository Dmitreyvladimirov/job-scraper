import os
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass  # dotenv not available (e.g. on Railway where env vars are set directly)

NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "")
NOTION_DATABASE_ID = "f71f92e0-c976-4cf2-bb56-8063b5cea681"

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# Telegram User API — for reading job channels (get from https://my.telegram.org)
TELEGRAM_API_ID = os.environ.get("TELEGRAM_API_ID", "")
TELEGRAM_API_HASH = os.environ.get("TELEGRAM_API_HASH", "")

# Comma-separated list of channel usernames or links to monitor for job postings
# Example: "@zarubezhom_jobs,@remocate,@productjobgo"
_raw_channels = os.environ.get("TELEGRAM_JOB_CHANNELS", "")
TELEGRAM_JOB_CHANNELS: list[str] = [
    c.strip() for c in _raw_channels.split(",") if c.strip()
]


def validate_secrets() -> None:
    """Call once at startup to fail fast on missing secrets."""
    missing = [k for k in (
        "NOTION_TOKEN", "OPENAI_API_KEY", "TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID",
    ) if not os.environ.get(k)]
    if missing:
        raise EnvironmentError(f"Missing required env vars: {', '.join(missing)}")

ATS_THRESHOLD = 60
COMPANY_COOLDOWN_DAYS = 90  # warn if applied to same company within this period
MAX_GPT_CALLS_PER_RUN = 40  # cap LLM calls per run to control costs
MAX_JOB_AGE_DAYS = 14       # skip vacancies older than this; 0 = disabled

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
