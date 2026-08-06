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

# ScrapingBee — optional. Used to fetch Jobgether/Jobicy pages that block plain requests
# (Cloudflare) or need JS execution (Jobicy's apply button), so we can pull the real
# application URL instead of an aggregator link (TASK-027 follow-up). Not required for
# the scraper to run — enrich_url() falls back to the aggregator link when unset.
SCRAPINGBEE_API_KEY = os.environ.get("SCRAPINGBEE_API_KEY", "")


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

# Review UI (SPEC_FRONTEND.md v1.2) — kanban funnel states, in order. "rejected" is
# reachable from any state (not part of the linear order) and is handled separately
# in db.is_valid_transition().
CURRENT_STATUSES = ["found", "applied", "recruiter_reply", "screen", "interview", "offer", "rejected"]
FUNNEL_ORDER = ["found", "applied", "recruiter_reply", "screen", "interview", "offer"]

# Kanban/Card Review domain tags (design_handoff_review_ui Turn 4b) — one oklch hue per
# domain from ats.py's fixed taxonomy, same lightness/chroma so they read as one family.
# AI/ML keeps the existing accent-gold tag-accent class instead of a hue entry — it's
# the primary domain, already styled. Anything outside the taxonomy (or a domain that's
# never set) falls back to a plain neutral tag with no color.
DOMAIN_COLORS = {
    "EdTech": "250",
    "FinTech": "155",
    "Cybersecurity": "310",
    "B2B SaaS": "195",
    "Data/Analytics": "340",
    "Growth/Consumer": "55",
    "HealthTech": "25",
}

# Rejection reasons — 6 categories (FRONTEND_DESIGN_BRIEF.md). geo_restricted_auto is
# set programmatically by the existing ResumeBuilder pipeline script, never through the
# user-facing rejection form.
REJECTION_REASONS = [
    "low_score_after_review",
    "remote_one_country",
    "not_remote_at_all",
    "inactive_closed",
    "bad_in_general",
    "geo_restricted_auto",
]
REJECTION_REASON_LABELS = {
    "low_score_after_review": "Плохо подошла по скорингу после проверки",
    "remote_one_country": "Ремоут, но только в одной стране",
    "not_remote_at_all": "Вакансия не удалённая вообще",
    "inactive_closed": "Вакансия неактивна / закрыта",
    "bad_in_general": "Вакансия плохая в принципе",
    "geo_restricted_auto": "GEO_RESTRICTED (авто-LLM)",
}
# Reasons selectable through the user-facing rejection form — excludes geo_restricted_auto.
USER_REJECTION_REASONS = [r for r in REJECTION_REASONS if r != "geo_restricted_auto"]


def is_valid_transition(old_status: str, new_status: str) -> bool:
    """Kanban status-transition rule: forward any distance through FUNNEL_ORDER
    (skipping stages is fine), backward exactly one step (drag-and-drop correction —
    further undo isn't in scope), reject from anywhere, un-reject back into any
    funnel stage (a rejection can happen from any stage, so there's no single
    "one step back" target to restore). old_status=None means the job never reached
    'qualified' and never entered the funnel — it must not be reachable through this
    endpoint at all, not even straight to 'rejected'."""
    if old_status is None:
        return False
    if old_status == "rejected":
        return new_status in FUNNEL_ORDER
    if new_status == "rejected":
        return True
    if old_status not in FUNNEL_ORDER or new_status not in FUNNEL_ORDER:
        return False
    old_idx = FUNNEL_ORDER.index(old_status)
    new_idx = FUNNEL_ORDER.index(new_status)
    return new_idx > old_idx or new_idx == old_idx - 1


def validate_status_change(old_status: str, new_status: str, rejection_reason: str | None) -> str | None:
    """Pure validation for a kanban status change — no DB access, so it's unit-testable
    in isolation (db.update_job_status() calls this, then does the actual write).
    Returns an error message if invalid, or None if the change is allowed."""
    if not is_valid_transition(old_status, new_status):
        return f"Invalid transition {old_status} -> {new_status}"
    if new_status == "rejected":
        if rejection_reason not in REJECTION_REASONS:
            return "rejection_reason is required and must be one of the known categories"
        if rejection_reason == "geo_restricted_auto":
            return "geo_restricted_auto cannot be set through this endpoint"
    return None
