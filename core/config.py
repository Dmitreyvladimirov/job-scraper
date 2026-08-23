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

# Cloud scoring rollout (resumebuilder-cloud /v1/score). Three states:
#   ""       — off, ats.analyze() decides everything (default)
#   "shadow" — ats.analyze() still decides; the cloud is called in parallel and its
#              result is only recorded, so the two scorers can be compared on real
#              runs before anything depends on the cloud one
#   "1"      — cloud decides; ats.py stays in the tree as the fallback to roll back to
USE_CLOUD_SCORING = os.environ.get("USE_CLOUD_SCORING", "")


def validate_secrets() -> None:
    """Call once at startup to fail fast on missing secrets."""
    missing = [k for k in (
        "NOTION_TOKEN", "OPENAI_API_KEY", "TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID",
    ) if not os.environ.get(k)]
    if missing:
        raise EnvironmentError(f"Missing required env vars: {', '.join(missing)}")

# 70 is calibrated to the cloud scorer's scale (cutover 2026-08-17): on Dimitry's
# hand-labeled set it matches his verdicts 80% at 70 vs 44% for the local scorer at 60.
ATS_THRESHOLD = 70
COMPANY_COOLDOWN_DAYS = 90  # warn if applied to same company within this period

# Release 2 (2026-08-19): batch resume auto-generation at the end of each run.
# Gates are about quality, not budget (a generation costs ~$0.005): only cloud-scored
# cards still in the review queue, with a real location signal and a substantial JD.
# >=80 (inclusive) confirmed by Dimitry 2026-08-17.
RESUME_AUTOGEN_MIN_SCORE = 80
RESUME_AUTOGEN_MIN_JD_CHARS = 500
RESUME_AUTOGEN_CAP_PER_RUN = 5
# A company that REJECTED Dimitry after engaging (company_rejected /
# prior_bad_interview) within this window gets no automatic resume spend for its
# new postings — cards still get scored and queued, the Generate button still
# works; only the automatic batch skips them (Dimitry, 2026-08-19, the Payoneer
# case: rejected at HM stage in March, 4 new postings polled in August).
COMPANY_REJECTION_COOLDOWN_DAYS = 180

# Mail agent (2026-08-22). Taxonomy derived from labelling ~80 real emails from
# Dimitry's inbox, NOT guessed — the initial 4-category hypothesis missed five of
# these. Each label maps to a TARGET stage (not "one step forward"): a single
# interview process sends 4+ emails (invite → confirmed → calendar → reminder),
# and stepping forward on each would fly a card to offer in two days.
MAIL_CLASSES = {
    # label:            (target_status, rejection_reason, needs_confirmation)
    "acknowledgement":  (None,          None,                False),  # note only; card is already applied
    "interview_invite": ("recruiter_reply", None,            False),  # recruiter reached out
    "interview_scheduled": ("screen",   None,                False),  # slot booked
    "interview_reminder": (None,        None,                False),  # note only — stage already recorded
    "calendar_invite":  (None,          None,                False),  # note only, same reason
    "rejection":        ("rejected",    "company_rejected",  True),   # NEVER auto: triggers the 180-day company cooldown
    "position_on_hold": ("rejected",    "inactive_closed",   True),   # the ROLE froze — not a verdict on him
    "action_required":  (None,          None,                True),   # e.g. "verify your email to complete the application"
    "noise":            (None,          None,                False),  # verification codes, feedback surveys, newsletters
}

# v1 policy (Dimitry, 2026-08-22): everything goes through confirmation. The
# tuple's needs_confirmation stays as documentation of what would be safe to
# automate later, once accuracy is measured on real traffic.
MAIL_AUTO_APPLY = False
MAIL_MAX_PER_RUN = 50
MAIL_LOOKBACK_DAYS = 7
# How the sweep finds job mail. Set 2026-08-23 after measuring the alternatives on
# the real mailbox: the `jobhunt` label never existed, and all four labels Dimitry
# does have (עבודה, עבודהה, אבודה, "Работа / поиск") returned ZERO messages in the
# last 30 days - they are historical, nothing applies them to incoming mail. A
# sender-based query over the ATS hosts found 27 messages in 7 days and 160 in 90,
# every one of them genuinely job-related. It also needs no filter discipline to
# keep working, and it never sends personal mail to the model.
#
# Empty = mail_agent.gmail_query() builds it from mail_agent._NEUTRAL_ATS_HOSTS, so
# the sender list has exactly one definition. Set the env var to override entirely
# (must contain a {days} placeholder).
MAIL_GMAIL_QUERY = os.environ.get("MAIL_GMAIL_QUERY", "")

# Folded into the generated query alongside the ATS senders: dead today, but the
# moment Dimitry labels a recruiter mail by hand it gets picked up, and a recruiter
# writing from a personal address is exactly what the sender list cannot catch.
MAIL_LABELS = ["עבודה"]
MAIL_MODEL = "claude-haiku-4-5-20251001"

# Resume-generation gates (Release 1, 2026-08-17). Lived in dashboard.py until the
# intake path needed the same numbers - the bot, the HTTP endpoint and the dashboard
# button must refuse identically, or the same card generates from one surface and is
# blocked from another.
RESUME_MIN_JD_CHARS = 200

# Intake (2026-08-23): the external entry point - a vacancy Dimitry sends by hand
# from the phone (Telegram bot) or from any outside tool (POST /api/intake),
# instead of one a scraper run found. Both transports share core/intake.py.
#
# Below this many characters a "job description" is a listing stub or a login wall
# (LinkedIn serves one to every logged-out fetch), and scoring it produces the
# short-JD inflation the local scorer was retired for. Intake refuses instead and
# asks for the pasted text. 400 sits below the shortest real JD seen in the corpus
# (~900 chars) and above every stub.
INTAKE_MIN_JD_CHARS = 400

# Title/company/location are read out of the JD text by Haiku: an ATS API gives a
# title but only a board slug for the company, an aggregator page gives neither,
# and a pasted text gives nothing structured at all. One call, ~$0.0005.
INTAKE_META_MODEL = "claude-haiku-4-5-20251001"

# Shared secret in the webhook path AND in Telegram's X-Telegram-Bot-Api-Secret-Token
# header. Unset = the webhook route answers 503 and the bot simply does not work;
# it is never allowed to fall open, since this route is unauthenticated by nature.
TELEGRAM_WEBHOOK_SECRET = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")

# Phase 4 (2026-08-19): the single storage truncation limit for job descriptions.
# The cloud scorer receives the full text BEFORE this truncation and applies its
# own prompt-side limit — this constant only bounds what Postgres keeps (and thus
# what Re-score / resume generation read back). Raised 8000 -> 20000 the same day:
# 6.9% of JDs (127 qualified in 30 days) were hitting the old cap, which was a
# token-cost guard for the retired local scorer, not a storage concern. 20000
# still guards against pathological scraped pages.
DESCRIPTION_MAX_CHARS = 20000
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

# Rejection reasons — geo_restricted_auto is set programmatically by the existing
# ResumeBuilder pipeline script; "old" is set by bulk maintenance scripts (Notion status
# sync, stale-backlog cleanup) reconciling cards that were never individually reviewed —
# neither is selectable through the user-facing rejection form, both are excluded from
# USER_REJECTION_REASONS below. location_mismatch merges the former remote_one_country /
# not_remote_at_all pair (existing rows migrated in Postgres, 2026-08-14); low_salary
# added the same day — both per Dimitry's decision after the scoring-quality labeling
# session showed location/language issues dominate his real rejection reasons.
REJECTION_REASONS = [
    "low_score_after_review",
    "location_mismatch",
    "low_salary",
    "inactive_closed",
    "bad_in_general",
    "dislike_company",
    "prior_bad_interview",
    "company_rejected",
    "no_response",
    "duplicate",
    "old",
    "geo_restricted_auto",
]
REJECTION_REASON_LABELS = {
    "low_score_after_review": "Low score after manual review",
    "location_mismatch": "Location / remote mismatch",
    "low_salary": "Low salary",
    "inactive_closed": "Inactive / closed posting",
    "bad_in_general": "Bad fit in general",
    # Company-level verdicts by Dimitry (2026-08-18): the company itself is the
    # problem, not this posting. Reasons only — no auto-blocklist of future
    # postings (offered, declined for now).
    "dislike_company": "Don't like the company",
    "prior_bad_interview": "Failed interview with them before",
    # The company's decision, not Dimitry's: applied but not advanced past some
    # selection stage (added 2026-08-17). The kanban already records WHICH stage via
    # status_log's rejected-from tracking; this reason records WHY the card died.
    "company_rejected": "Company rejected — not advanced",
    # Ghosting is not the same failure as an explicit rejection after interviews —
    # split for honest funnel analytics (2026-08-19, Notion wave import).
    "no_response": "No response after applying",
    # Set by the merge-duplicate action (2026-08-19) and selectable manually: two
    # live cards turned out to be the same vacancy; this one is the twin.
    "duplicate": "Duplicate card",
    "old": "Old — bulk-rejected, never individually reviewed",
    "geo_restricted_auto": "Geo-restricted (auto, LLM-flagged)",
}
# Reasons selectable through the user-facing rejection form — excludes the two
# system-only reasons (geo_restricted_auto, old).
USER_REJECTION_REASONS = [r for r in REJECTION_REASONS if r not in ("geo_restricted_auto", "old")]


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
        if rejection_reason in ("geo_restricted_auto", "old"):
            return f"{rejection_reason} cannot be set through this endpoint"
    return None
