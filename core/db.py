import json
import logging
import os
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras
from config import CURRENT_STATUSES, DESCRIPTION_MAX_CHARS, FUNNEL_ORDER
from utils import normalize_job_key

logger = logging.getLogger(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL", "")


def _conn():
    if not DATABASE_URL:
        raise EnvironmentError("DATABASE_URL is not set")
    return psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor, connect_timeout=10)


def init_db() -> None:
    conn = _conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS runs (
                        id                 SERIAL PRIMARY KEY,
                        started_at         TEXT NOT NULL,
                        finished_at        TEXT,
                        total_fetched      INTEGER DEFAULT 0,
                        qualified          INTEGER DEFAULT 0,
                        rejected_low_score INTEGER DEFAULT 0,
                        filtered_role      INTEGER DEFAULT 0,
                        filtered_language  INTEGER DEFAULT 0,
                        filtered_location  INTEGER DEFAULT 0,
                        filtered_stale     INTEGER DEFAULT 0,
                        filtered_dedup     INTEGER DEFAULT 0,
                        filtered_gpt_limit INTEGER DEFAULT 0,
                        gpt_calls          INTEGER DEFAULT 0,
                        sources_json       TEXT
                    )
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS jobs (
                        id          SERIAL PRIMARY KEY,
                        run_id      INTEGER NOT NULL REFERENCES runs(id),
                        url         TEXT,
                        apply_url   TEXT,
                        title       TEXT,
                        company     TEXT,
                        source      TEXT,
                        published   TEXT,
                        description TEXT,
                        ats_score   INTEGER,
                        domain      TEXT,
                        why_not     TEXT,
                        outcome     TEXT,
                        logged_at   TIMESTAMP DEFAULT NOW()
                    )
                """)
                cur.execute("CREATE INDEX IF NOT EXISTS idx_jobs_run_id ON jobs(run_id)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_jobs_outcome ON jobs(outcome)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_runs_started ON runs(started_at)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_jobs_url ON jobs(url)")

                # Review UI (SPEC_FRONTEND.md v1.2) — additive, idempotent.
                cur.execute("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS why_apply TEXT")
                cur.execute("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS matched_keywords JSONB")
                cur.execute("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS missed_keywords JSONB")
                cur.execute("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS current_status TEXT")
                # current_status must only be set for outcome='qualified' rows (the ones that
                # actually enter the review funnel) — log_job() sets it explicitly per-insert.
                # A DEFAULT 'found' at the column level briefly existed here and defaulted every
                # pre-existing row (all outcomes) to 'found' on ADD COLUMN, and every future insert
                # regardless of outcome, since log_job() didn't pass the column explicitly. Drop
                # any lingering default and correct historical rows; safe to re-run (no-op once fixed).
                cur.execute("ALTER TABLE jobs ALTER COLUMN current_status DROP DEFAULT")
                cur.execute("UPDATE jobs SET current_status = NULL WHERE current_status = 'found' AND outcome != 'qualified'")
                # Mirror-image reconciliation: scripts/migrate_sqlite_to_pg.py and
                # scripts/import_notion_csv.py insert 'qualified' rows directly, without going
                # through log_job(), so they don't set current_status either — since the column
                # has no DB-level default anymore, those rows would otherwise silently never
                # appear on /review or /kanban. Idempotent — no-op once every qualified row has
                # a status.
                cur.execute("UPDATE jobs SET current_status = 'found' WHERE outcome = 'qualified' AND current_status IS NULL")
                cur.execute("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS rejection_reason TEXT")
                cur.execute("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS penalty_reason TEXT")
                # ATS sub-score breakdown — discovered missing during Review UI implementation:
                # only the total ats_score was ever persisted, but Card Review (1a/1c) shows the
                # per-axis breakdown the scorer already computes in ATSResult. NULL for pre-v1.2
                # rows -> template hides the breakdown block (same degraded-state pattern as
                # penalty_reason/why_apply for pre-v1.1 rows).
                cur.execute("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS role_score INTEGER")
                cur.execute("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS domain_score INTEGER")
                cur.execute("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS domain_value_score INTEGER")
                cur.execute("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS domain_exp_score INTEGER")
                cur.execute("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS keyword_score INTEGER")
                cur.execute("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS location_score INTEGER")
                cur.execute("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS location_reason TEXT")
                cur.execute("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS salary TEXT")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_jobs_current_status ON jobs(current_status)")
                # Card Review / Kanban show "found" and "applied" dates — logged_at already
                # covers "found"; applied_at is set by update_job_status() the first time a
                # job's current_status becomes 'applied'.
                cur.execute("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS applied_at TIMESTAMP")

                # Cloud scoring (USE_CLOUD_SCORING) — additive, idempotent.
                # pipeline_run_id correlates a row with the same vacancy's records in the
                # resumebuilder-cloud DB; indexed because that join is the whole point of it.
                cur.execute("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS pipeline_run_id TEXT")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_jobs_pipeline_run_id ON jobs(pipeline_run_id)")
                cur.execute("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS location TEXT")
                # 'local' | 'cloud' — which scorer produced ats_score on this row.
                cur.execute("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS scoring_source TEXT")
                # Shadow mode only: the cloud's verdict, recorded beside the local one that
                # actually decided. shadow_payload keeps the full ScoreResult so the
                # comparison can look at sub-scores and reasons, not just the total.
                cur.execute("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS shadow_score INTEGER")
                cur.execute("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS shadow_payload JSONB")

                # Release 1 resume features: generation_run_id in the resume service's
                # resume.generation_run/resume.artifact tables (same Postgres since the
                # 2026-08-14 DB merge). NULL = no resume generated for this vacancy yet —
                # also Release 2's batch-generation guard (resume_run_id IS NULL).
                cur.execute("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS resume_run_id INTEGER")

                cur.execute("""
                    CREATE TABLE IF NOT EXISTS status_log (
                        id         SERIAL PRIMARY KEY,
                        job_id     INTEGER NOT NULL REFERENCES jobs(id),
                        old_status TEXT,
                        new_status TEXT NOT NULL,
                        changed_at TIMESTAMP DEFAULT NOW(),
                        source     TEXT DEFAULT 'manual'
                    )
                """)
                cur.execute("CREATE INDEX IF NOT EXISTS idx_status_log_job_id ON status_log(job_id)")

                cur.execute("""
                    CREATE TABLE IF NOT EXISTS sources_config (
                        source     TEXT PRIMARY KEY,
                        enabled    BOOLEAN DEFAULT TRUE,
                        updated_at TIMESTAMP DEFAULT NOW()
                    )
                """)

                # company_direct source (2026-08-19): curated target companies whose
                # ATS boards are polled directly. Rows are never deleted — dead
                # companies get status='dead' (QA requirement from the 2026-07 plan).
                # UNIQUE(ats, slug), not name: names change after M&A, and one company
                # can briefly run two live boards during a migration.
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS target_companies (
                        id                   SERIAL PRIMARY KEY,
                        name                 TEXT NOT NULL,
                        ats                  TEXT NOT NULL,
                        slug                 TEXT NOT NULL,
                        status               TEXT NOT NULL DEFAULT 'pending',
                        layer                INTEGER NOT NULL DEFAULT 2,
                        region               TEXT,
                        provenance           TEXT,
                        notes                TEXT,
                        added_at             TIMESTAMP DEFAULT NOW(),
                        verified_at          TIMESTAMP,
                        last_checked_at      TIMESTAMP,
                        last_ok_at           TIMESTAMP,
                        last_posting_count   INTEGER,
                        consecutive_failures INTEGER NOT NULL DEFAULT 0,
                        zero_streak          INTEGER NOT NULL DEFAULT 0,
                        last_error           TEXT,
                        UNIQUE (ats, slug)
                    )
                """)
                cur.execute("CREATE INDEX IF NOT EXISTS idx_target_companies_status ON target_companies(status, layer)")

                # Mail agent (2026-08-22). One row per processed email; UNIQUE on the
                # RFC822 Message-ID is the whole idempotency story — the agent WILL
                # re-read the same message (bookmark loss, retry, overlapping
                # newer_than window) and a replay must be a no-op. job_id NULL means
                # "could not match to a card" — the email is still recorded, never
                # dropped. 'action' is the lifecycle: pending (awaiting Dimitry) →
                # confirmed/dismissed, or applied (auto) / ignored (noise).
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS mail_event (
                        id              SERIAL PRIMARY KEY,
                        message_id      TEXT NOT NULL UNIQUE,
                        thread_id       TEXT,
                        received_at     TIMESTAMP,
                        from_addr       TEXT,
                        subject         TEXT,
                        excerpt         TEXT,
                        job_id          INTEGER REFERENCES jobs(id),
                        company_hint    TEXT,
                        title_hint      TEXT,
                        classification  TEXT,
                        confidence      REAL,
                        proposed_status TEXT,
                        proposed_reason TEXT,
                        action          TEXT NOT NULL DEFAULT 'pending',
                        resolved_at     TIMESTAMP,
                        created_at      TIMESTAMP DEFAULT NOW()
                    )
                """)
                cur.execute("CREATE INDEX IF NOT EXISTS idx_mail_event_action ON mail_event(action)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_mail_event_job_id ON mail_event(job_id)")

                # Google credentials live in a ROW, not env (architect's one concession
                # to the multi-user future): the difference between "add a user" and
                # "rewrite the agent". user_id NULL = the installation owner (Dimitry).
                # Token is Fernet-encrypted; the key stays in env.
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS google_credential (
                        id              SERIAL PRIMARY KEY,
                        user_id         INTEGER,
                        email           TEXT NOT NULL,
                        refresh_token   BYTEA NOT NULL,
                        scopes          TEXT,
                        created_at      TIMESTAMP DEFAULT NOW(),
                        last_refresh_at TIMESTAMP,
                        last_error      TEXT,
                        UNIQUE (email)
                    )
                """)

                # Free-form notes on a vacancy card (2026-08-17) — recruiter names,
                # interview impressions, salary quotes. Plain text, newest first.
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS job_comments (
                        id         SERIAL PRIMARY KEY,
                        job_id     INTEGER NOT NULL REFERENCES jobs(id),
                        body       TEXT NOT NULL,
                        created_at TIMESTAMP DEFAULT NOW()
                    )
                """)
                cur.execute("CREATE INDEX IF NOT EXISTS idx_job_comments_job_id ON job_comments(job_id)")
    finally:
        conn.close()
    logger.info("DB: initialised (Postgres)")


def start_run(total_fetched: int, source_counts: dict) -> int:
    now = datetime.now(timezone.utc).isoformat()
    conn = _conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO runs (started_at, total_fetched, sources_json) VALUES (%s, %s, %s) RETURNING id",
                    (now, total_fetched, json.dumps(source_counts)),
                )
                run_id = cur.fetchone()["id"]
    finally:
        conn.close()
    logger.info(f"DB: run #{run_id} started")
    return run_id


def finish_run(run_id: int, counts: dict, gpt_calls: int) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn = _conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE runs SET
                        finished_at        = %s,
                        qualified          = %s,
                        rejected_low_score = %s,
                        filtered_role      = %s,
                        filtered_language  = %s,
                        filtered_location  = %s,
                        filtered_stale     = %s,
                        filtered_dedup     = %s,
                        filtered_gpt_limit = %s,
                        gpt_calls          = %s
                    WHERE id = %s
                """, (
                    now,
                    counts.get("qualified", 0),
                    counts.get("score", 0),
                    counts.get("role", 0),
                    counts.get("language", 0),
                    counts.get("location", 0),
                    counts.get("stale", 0),
                    counts.get("dedup", 0),
                    counts.get("gpt_limit", 0),
                    gpt_calls,
                    run_id,
                ))
    finally:
        conn.close()
    logger.info(f"DB: run #{run_id} finished")


def log_job(
    run_id: int,
    job: dict,
    outcome: str,
    ats_score: int | None = None,
    domain: str | None = None,
    why_not: str | None = None,
    why_apply: str | None = None,
    matched_keywords: list[str] | None = None,
    missed_keywords: list[str] | None = None,
    penalty_reason: str | None = None,
    role_score: int | None = None,
    domain_score: int | None = None,
    domain_value_score: int | None = None,
    domain_exp_score: int | None = None,
    keyword_score: int | None = None,
    location_score: int | None = None,
    location_reason: str | None = None,
    pipeline_run_id: str | None = None,
    scoring_source: str | None = None,
    shadow_score: int | None = None,
    shadow_payload: dict | None = None,
) -> int:
    """why_apply/keywords/penalty_reason/score-breakdown are new (SPEC_FRONTEND.md v1.2,
    Review UI) — optional/backward-compatible params, wired from scraper.py's low_score
    and qualified call sites (ats_error/role/language/etc. calls stay as before, no
    ATSResult available at those points). salary is read straight off `job` (every
    source already produces it) rather than needing a separate kwarg at each call site.
    location is read the same way, so it lands on filtered rows too, not just scored ones.

    pipeline_run_id/scoring_source/shadow_* are the cloud-scoring additions
    (USE_CLOUD_SCORING) — same optional/backward-compatible shape."""

    def _no_nul(value):
        # Postgres TEXT cannot hold NUL (0x00) and psycopg2 raises before the
        # INSERT even runs — one dirty scraped description then kills the whole
        # run (live crash 2026-08-18, a Choicy posting). Strip rather than reject:
        # the byte carries no meaning in job text.
        return value.replace("\x00", "") if isinstance(value, str) else value

    job = {k: _no_nul(v) for k, v in job.items()}
    why_not, why_apply, penalty_reason, location_reason = (
        _no_nul(why_not), _no_nul(why_apply), _no_nul(penalty_reason), _no_nul(location_reason))
    desc = (job.get("description") or "")[:DESCRIPTION_MAX_CHARS]
    salary = job.get("salary") or None
    location = job.get("location") or None
    # Only 'qualified' jobs enter the review funnel — every other outcome (role/language/
    # location/stale/dedup/gpt_limit/low_score/ats_error) is an auto-filtered row that should
    # never appear on /review or /kanban.
    current_status = "found" if outcome == "qualified" else None
    conn = _conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO jobs
                       (run_id, url, apply_url, title, company, source, published,
                        description, ats_score, domain, why_not, outcome, current_status,
                        why_apply, matched_keywords, missed_keywords, penalty_reason,
                        role_score, domain_score, domain_value_score, domain_exp_score,
                        keyword_score, location_score, location_reason, salary,
                        location, pipeline_run_id, scoring_source, shadow_score, shadow_payload)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                               %s,%s,%s,%s,%s)
                       RETURNING id""",
                    (
                        run_id,
                        job.get("url"),
                        job.get("apply_url"),
                        job.get("title"),
                        job.get("company"),
                        job.get("source"),
                        job.get("published"),
                        desc,
                        ats_score,
                        domain,
                        why_not,
                        outcome,
                        current_status,
                        why_apply,
                        json.dumps(matched_keywords) if matched_keywords is not None else None,
                        json.dumps(missed_keywords) if missed_keywords is not None else None,
                        penalty_reason,
                        role_score,
                        domain_score,
                        domain_value_score,
                        domain_exp_score,
                        keyword_score,
                        location_score,
                        location_reason,
                        salary,
                        location,
                        pipeline_run_id,
                        scoring_source,
                        shadow_score,
                        json.dumps(shadow_payload) if shadow_payload is not None else None,
                    ),
                )
                job_id = cur.fetchone()["id"]
    finally:
        conn.close()
    return job_id


def find_manual_duplicate(url: str | None, company: str | None, title: str) -> dict | None:
    """Pre-insert duplicate probe for the dashboard's manual Add-job form. Matches by
    exact URL first, then by case-insensitive (company, title). Deliberately simpler
    than the scraper's normalize_job_key() dedup — a human pasting a LinkedIn posting
    needs a warning on the obvious match, not fuzzy-key coverage."""
    conn = _conn()
    try:
        with conn.cursor() as cur:
            if url:
                cur.execute(
                    "SELECT id, title, company, source, current_status, logged_at, ats_score FROM jobs "
                    "WHERE url = %s OR apply_url = %s ORDER BY id DESC LIMIT 1",
                    (url, url),
                )
                row = cur.fetchone()
                if row:
                    return dict(row)
            if company:
                cur.execute(
                    "SELECT id, title, company, source, current_status, logged_at, ats_score FROM jobs "
                    "WHERE lower(trim(company)) = lower(trim(%s)) "
                    "AND lower(trim(title)) = lower(trim(%s)) ORDER BY id DESC LIMIT 1",
                    (company, title),
                )
                row = cur.fetchone()
                if row:
                    return dict(row)
    finally:
        conn.close()
    return None


def create_manual_job(
    job: dict, ats_score: int | None = None, pipeline_run_id: str | None = None
) -> int:
    """A card added by hand through the dashboard, not found by a scraper run. Still
    needs a run_id (jobs.run_id is NOT NULL), so it gets its own single-job run; goes
    through log_job() with outcome='qualified' so it lands in the review queue exactly
    like a pipeline-found card (current_status='found').

    ats_score/pipeline_run_id come from the ResumeBuilder /api/jobs path, where the
    vacancy was already scored before Dimitry applied; the dashboard's Add-job form
    passes neither."""
    run_id = start_run(0, {"manual": 1})
    finish_run(run_id, {"qualified": 1}, gpt_calls=0)
    return log_job(
        run_id, job, outcome="qualified", ats_score=ats_score, pipeline_run_id=pipeline_run_id
    )


def mark_job_applied(job_id: int, source: str = "resumebuilder") -> dict:
    """Applied-transition for ResumeBuilder's /api/jobs: unlike update_job_status it
    also accepts rows that never entered the funnel (current_status NULL — a scraped
    vacancy the pipeline filtered out but Dimitry applied to anyway) and rows he had
    marked rejected. No-op when the job already sits at applied or further, so a
    duplicate application never drags a card backward in the funnel."""
    job = get_job(job_id)
    if not job:
        raise ValueError(f"Unknown job id {job_id}")
    old_status = job["current_status"]
    if old_status in FUNNEL_ORDER and FUNNEL_ORDER.index(old_status) >= FUNNEL_ORDER.index("applied"):
        return {"job_id": job_id, "old_status": old_status, "new_status": old_status}
    conn = _conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE jobs SET current_status = 'applied', rejection_reason = NULL, "
                    "applied_at = NOW() WHERE id = %s",
                    (job_id,),
                )
                cur.execute(
                    "INSERT INTO status_log (job_id, old_status, new_status, source) VALUES (%s,%s,%s,%s)",
                    (job_id, old_status, "applied", source),
                )
    finally:
        conn.close()
    logger.info(f"Status changed: job={job_id} {old_status} -> applied (via {source})")
    return {"job_id": job_id, "old_status": old_status, "new_status": "applied"}


def set_resume_run_id(job_id: int, resume_run_id: int) -> None:
    """Record which resume.generation_run belongs to this vacancy. Deliberately not
    conditional on the column being NULL — an explicit regeneration (not built yet,
    but cheap to allow) should simply point the card at the newest artifact."""
    conn = _conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE jobs SET resume_run_id = %s WHERE id = %s",
                    (resume_run_id, job_id),
                )
    finally:
        conn.close()


def get_resume_pdf(resume_run_id: int) -> tuple[bytes, str | None] | None:
    """(pdf_bytes, company) straight from the resume service's tables — same Postgres
    since the DB merge, so the dashboard can stream the PDF itself instead of minting
    a TTL'd share link against the resume service."""
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT a.pdf_bytes, g.company
                   FROM resume.artifact a
                   JOIN resume.generation_run g ON g.id = a.generation_run_id
                   WHERE a.generation_run_id = %s""",
                (resume_run_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            return bytes(row["pdf_bytes"]), row["company"]
    finally:
        conn.close()


def get_comments(job_id: int) -> list[dict]:
    """Newest first — the modal shows the latest note on top."""
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, body, created_at FROM job_comments WHERE job_id = %s ORDER BY id DESC",
                (job_id,),
            )
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def add_comment(job_id: int, body: str) -> int:
    conn = _conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO job_comments (job_id, body) VALUES (%s, %s) RETURNING id",
                    (job_id, body),
                )
                return cur.fetchone()["id"]
    finally:
        conn.close()


def delete_comment(job_id: int, comment_id: int) -> bool:
    """job_id in the WHERE guards against a stale/forged id deleting another card's
    note. Returns False when nothing matched (already deleted — treated as fine)."""
    conn = _conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM job_comments WHERE id = %s AND job_id = %s",
                    (comment_id, job_id),
                )
                return cur.rowcount > 0
    finally:
        conn.close()


def get_applied_today() -> list[dict]:
    """Vacancies applied to today (server-local date of applied_at) — the drill-down
    behind the Applied stat on the merged dashboard."""
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT id, title, company, source, ats_score, url, apply_url, applied_at
                   FROM jobs
                   WHERE applied_at IS NOT NULL AND applied_at::date = CURRENT_DATE
                   ORDER BY applied_at DESC""",
            )
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


ACTIVE_FUNNEL = ("applied", "recruiter_reply", "screen", "interview", "offer")


def find_cards_for_mail(company_hint: str | None, sender_domain: str | None,
                        title_hint: str | None, limit: int = 10,
                        include_aged: bool = True) -> list[dict]:
    """Candidate cards an incoming email might refer to (mail agent, 2026-08-22).

    Deliberately searches ONLY the active funnel: the agent physically cannot
    reach a 'found' card (never applied to) or a 'rejected' one (already closed).
    That single WHERE clause is most of the feature's safety.

    Ranking: apply_url/url host match (strong — but only ~40% of real emails come
    from a domain that matches, per the labelling pass) > exact company match >
    company substring. Title is a tie-breaker only; ATS subject lines rarely quote
    the exact stored title. Returns a compact projection, never SELECT j.*."""
    if not any((company_hint, sender_domain)):
        return []
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT id, title, company, url, apply_url, current_status, applied_at,
                          CASE
                            WHEN %(domain)s <> '' AND (url ILIKE %(dom_like)s OR apply_url ILIKE %(dom_like)s) THEN 3
                            WHEN %(company)s <> '' AND lower(trim(company)) = lower(trim(%(company)s)) THEN 2
                            WHEN %(company)s <> '' AND lower(company) LIKE %(comp_like)s THEN 1
                            ELSE 0
                          END AS match_score
                   FROM jobs
                   WHERE (current_status = ANY(%(statuses)s)
                          -- Aged-out cards are searchable too (found live 2026-08-22):
                          -- the Notion migration closed everything older than 30 days
                          -- as no_response, which mis-aged a LIVE Workday process that
                          -- had an interview scheduled. Those closures are the system's
                          -- guess, not Dimitry's decision, so mail evidence should be
                          -- able to reopen them. Cards HE rejected stay unreachable.
                          OR (%(include_aged)s AND current_status = 'rejected'
                              AND rejection_reason = 'no_response'))
                     AND coalesce(trim(company), '') <> ''
                     AND length(company) <= 50
                     AND company NOT ILIKE '%%linkedin%%'
                     AND (
                       (%(domain)s <> '' AND (url ILIKE %(dom_like)s OR apply_url ILIKE %(dom_like)s))
                       OR (%(company)s <> '' AND lower(company) LIKE %(comp_like)s)
                     )
                   ORDER BY (current_status <> 'rejected') DESC, match_score DESC,
                            CASE WHEN %(title)s <> '' AND lower(title) LIKE %(title_like)s THEN 0 ELSE 1 END,
                            applied_at DESC NULLS LAST
                   LIMIT %(limit)s""",
                {
                    "domain": sender_domain or "",
                    "dom_like": f"%{sender_domain}%" if sender_domain else "",
                    "company": company_hint or "",
                    "comp_like": f"%{(company_hint or '').lower().strip()}%",
                    "title": title_hint or "",
                    "title_like": f"%{(title_hint or '').lower().strip()}%",
                    "statuses": list(ACTIVE_FUNNEL),
                    "include_aged": include_aged,
                    "limit": limit,
                },
            )
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def record_mail_event(event: dict) -> dict:
    """Insert one processed email. ON CONFLICT (message_id) DO NOTHING makes a
    replay a no-op — returns the STORED row either way, with duplicate=True, so
    the caller can tell "already handled" from "just handled" without a 409."""
    conn = _conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO mail_event
                           (message_id, thread_id, received_at, from_addr, subject, excerpt,
                            job_id, company_hint, title_hint, classification, confidence,
                            proposed_status, proposed_reason, action)
                       VALUES (%(message_id)s, %(thread_id)s, %(received_at)s, %(from_addr)s,
                               %(subject)s, %(excerpt)s, %(job_id)s, %(company_hint)s,
                               %(title_hint)s, %(classification)s, %(confidence)s,
                               %(proposed_status)s, %(proposed_reason)s, %(action)s)
                       ON CONFLICT (message_id) DO NOTHING
                       RETURNING id""",
                    event,
                )
                row = cur.fetchone()
                if row:
                    return {"id": row["id"], "duplicate": False}
                cur.execute("SELECT id, action FROM mail_event WHERE message_id = %s",
                            (event["message_id"],))
                stored = cur.fetchone()
                return {"id": stored["id"], "duplicate": True, "action": stored["action"]}
    finally:
        conn.close()


def seen_mail_message_ids(message_ids: list[str]) -> set[str]:
    """Which of these emails are already in mail_event.

    Exists so process() can skip them BEFORE classifying. record_mail_event's
    ON CONFLICT already makes a replay a no-op in the database, but by then the
    LLM call has been made and paid for - and with a 7-day lookback on a daily
    cron, every message would otherwise be classified about seven times.
    """
    if not message_ids:
        return set()
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT message_id FROM mail_event WHERE message_id = ANY(%s)",
                        (list(message_ids),))
            return {r["message_id"] for r in cur.fetchall()}
    finally:
        conn.close()


def get_pending_mail_events(limit: int = 50) -> list[dict]:
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT e.*, j.title AS job_title, j.company AS job_company,
                          j.current_status AS job_status
                   FROM mail_event e LEFT JOIN jobs j ON j.id = e.job_id
                   WHERE e.action = 'pending' ORDER BY e.received_at DESC LIMIT %s""",
                (limit,))
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def resolve_mail_event(event_id: int, action: str, job_id: int | None = None,
                       status: str | None = None, reason: str | None = None) -> dict:
    """Confirm or dismiss a pending event. Confirming applies the status change
    through the normal update_job_status() — same validation, same status_log —
    with source='mail-agent-confirmed' so the whole feature stays revertible with
    one SELECT."""
    if action not in ("confirmed", "dismissed"):
        raise ValueError(f"Unknown action: {action}")
    applied = None
    if action == "confirmed" and status:
        applied = update_job_status(job_id, status, rejection_reason=reason,
                                    source="mail-agent-confirmed")
    conn = _conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE mail_event SET action = %s, resolved_at = NOW(), "
                    "job_id = coalesce(%s, job_id) WHERE id = %s",
                    (action, job_id, event_id))
    finally:
        conn.close()
    return {"event_id": event_id, "action": action, "transition": applied}


def get_active_target_companies() -> list[dict]:
    """Companies company_direct polls THIS run. Layers became real schedules on
    2026-08-20 (Dimitry's redesign): layer 1 = every run (4x/day), layer 2 =
    daily, layer 3 = every 2 days — intervals set slightly under 24/48h so the
    cron slot drift never skips a whole day. last_checked_at ASC NULLS FIRST so
    a tail skipped by the source's time budget is polled first next run."""
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT id, name, ats, slug FROM target_companies
                   WHERE status = 'active'
                     AND (last_checked_at IS NULL
                          OR last_checked_at < now() - make_interval(hours =>
                              CASE layer WHEN 1 THEN 0 WHEN 2 THEN 22 ELSE 46 END))
                   ORDER BY last_checked_at ASC NULLS FIRST, id""")
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def record_target_company_results(results: list[dict]) -> None:
    """Batch health update after a poll sweep — one connection for all rows
    (per-call _conn() would open 40+ connections per run).
    results: [{"id", "ok": bool, "posting_count": int|None, "error": str|None}]"""
    if not results:
        return
    conn = _conn()
    try:
        with conn:
            with conn.cursor() as cur:
                for r in results:
                    if r["ok"]:
                        cur.execute(
                            """UPDATE target_companies SET
                                   last_checked_at = NOW(), last_ok_at = NOW(),
                                   last_posting_count = %s, consecutive_failures = 0,
                                   zero_streak = CASE WHEN %s > 0 THEN 0 ELSE zero_streak + 1 END,
                                   last_error = NULL
                               WHERE id = %s""",
                            (r.get("posting_count") or 0, r.get("posting_count") or 0, r["id"]),
                        )
                    else:
                        cur.execute(
                            """UPDATE target_companies SET
                                   last_checked_at = NOW(),
                                   consecutive_failures = consecutive_failures + 1,
                                   last_error = %s
                               WHERE id = %s""",
                            ((r.get("error") or "")[:200], r["id"]),
                        )
    finally:
        conn.close()


def degrade_target_companies(threshold: int = 6) -> list[dict]:
    """Auto-degrade companies that failed `threshold` consecutive polls to 'dormant'
    (~1.5 days at 4 runs/day). Returns the degraded rows for the caller's single
    aggregated Telegram alert. Rows are never deleted; recovery is manual.
    Note: zero_streak (successful polls returning 0 postings) is recorded but does
    NOT drive degradation — an empty board can be the truth for a small company;
    it's telemetry for the 2-month signal review, not a trigger."""
    conn = _conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE target_companies
                       SET status = 'dormant',
                           notes = coalesce(notes || ' | ', '') || 'auto-dormant ' || CURRENT_DATE || ': ' || coalesce(last_error, '?')
                       WHERE status = 'active' AND consecutive_failures >= %s
                       RETURNING id, name, ats, slug, last_error""",
                    (threshold,),
                )
                return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def merge_duplicate(kept_id: int, dup_id: int) -> dict:
    """Fold a duplicate card into the one being kept (2026-08-19): notes move over,
    the resume link is inherited if the kept card lacks one, the kept card adopts
    the duplicate's funnel status when the duplicate got further, and the duplicate
    is rejected with reason 'duplicate'. Raises ValueError with a user-facing
    message on anything invalid — the route turns that into a 400."""
    if kept_id == dup_id:
        raise ValueError("A card cannot be merged into itself")
    kept, dup = get_job(kept_id), get_job(dup_id)
    if not kept:
        raise ValueError(f"Unknown card #{kept_id}")
    if not dup:
        raise ValueError(f"Unknown card #{dup_id}")
    if kept.get("current_status") in (None, "rejected"):
        raise ValueError(f"Card #{kept_id} is not a live funnel card — merge into the card you keep")

    from config import FUNNEL_ORDER  # local import, same cycle-avoidance as update_job_status
    conn = _conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE job_comments SET job_id = %s WHERE job_id = %s", (kept_id, dup_id))
                if dup.get("resume_run_id") and not kept.get("resume_run_id"):
                    cur.execute("UPDATE jobs SET resume_run_id = %s WHERE id = %s",
                                (dup["resume_run_id"], kept_id))
                # Adopt the duplicate's further-along status (it may carry the real
                # applied_at — keep the earliest known application time).
                ks, ds = kept.get("current_status"), dup.get("current_status")
                if ds in FUNNEL_ORDER and ks in FUNNEL_ORDER and \
                        FUNNEL_ORDER.index(ds) > FUNNEL_ORDER.index(ks):
                    cur.execute(
                        "UPDATE jobs SET current_status = %s, "
                        "applied_at = LEAST(coalesce(applied_at, %s), coalesce(%s, applied_at)) "
                        "WHERE id = %s",
                        (ds, dup.get("applied_at"), dup.get("applied_at"), kept_id),
                    )
                    cur.execute(
                        "INSERT INTO status_log (job_id, old_status, new_status, source) VALUES (%s,%s,%s,%s)",
                        (kept_id, ks, ds, "merge"),
                    )
                cur.execute(
                    "UPDATE jobs SET current_status = 'rejected', rejection_reason = 'duplicate' WHERE id = %s",
                    (dup_id,),
                )
                cur.execute(
                    "INSERT INTO status_log (job_id, old_status, new_status, source) VALUES (%s,%s,%s,%s)",
                    (dup_id, ds, "rejected", "merge"),
                )
                cur.execute(
                    "INSERT INTO job_comments (job_id, body) VALUES (%s, %s)",
                    (kept_id, f"Merged duplicate card #{dup_id} ({dup.get('source') or 'unknown source'}) into this one"),
                )
    finally:
        conn.close()
    logger.info(f"Merged duplicate: #{dup_id} -> #{kept_id}")
    return {"kept_id": kept_id, "dup_id": dup_id}


EDITABLE_JOB_FIELDS = (
    "title", "company", "url", "apply_url", "source", "salary", "location", "description",
)


def update_job_fields(job_id: int, fields: dict) -> None:
    """Card editing (2026-08-19): whitelist-only content fields. Computed columns
    (scores, status, resume_run_id) are deliberately not reachable here — they have
    their own flows. NUL bytes stripped like log_job() does."""
    updates = {k: v for k, v in fields.items() if k in EDITABLE_JOB_FIELDS}
    if not updates:
        return
    for k, v in updates.items():
        if isinstance(v, str):
            updates[k] = v.replace("\x00", "")
    set_clause = ", ".join(f"{k} = %s" for k in updates)
    conn = _conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE jobs SET {set_clause} WHERE id = %s",
                    (*updates.values(), job_id),
                )
    finally:
        conn.close()
    logger.info(f"Card edited: job={job_id} fields={sorted(updates)}")


def clear_resume_run_id(job_id: int) -> None:
    """Unlink a generated resume from the card (the artifact row stays) — used when
    the JD is edited: a resume tailored to the old text should not present itself
    as current."""
    conn = _conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE jobs SET resume_run_id = NULL WHERE id = %s", (job_id,))
    finally:
        conn.close()


def get_autogen_candidates(min_score: int, min_jd_chars: int, limit: int, cooldown_days: int = 180) -> list[dict]:
    """Release 2 (2026-08-19): cards eligible for end-of-run resume auto-generation.
    Only review-queue cards ('found' — applied cards already got their resume through
    the apply flow), scored by the CLOUD scorer (local scores are on a different,
    inflated scale), with a positive location signal and a real JD; resume_run_id IS
    NULL doubles as the this-one-was-done marker, so a failed generation is simply
    retried next run. Best scores first, capped."""
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT id, title, company, description, location, pipeline_run_id, ats_score
                   FROM jobs
                   WHERE current_status = 'found'
                     AND scoring_source = 'cloud'
                     AND ats_score >= %s
                     AND coalesce(location_score, 0) > 0
                     AND length(coalesce(description, '')) >= %s
                     AND coalesce(trim(company), '') != ''
                     AND resume_run_id IS NULL
                     -- Company-rejection cooldown (2026-08-19, the Payoneer case):
                     -- an engaged rejection within the window mutes automatic
                     -- resume spend for that company's new postings. Manual
                     -- Generate stays available.
                     AND NOT EXISTS (
                         SELECT 1 FROM jobs r
                         WHERE lower(trim(r.company)) = lower(trim(jobs.company))
                           AND r.rejection_reason IN ('company_rejected', 'prior_bad_interview')
                           AND coalesce(r.applied_at, r.logged_at) > now() - make_interval(days => %s)
                     )
                   ORDER BY ats_score DESC, id DESC
                   LIMIT %s""",
                (min_score, min_jd_chars, cooldown_days, limit),
            )
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def get_company_history(company: str | None, exclude_job_id: int) -> list[dict]:
    """Prior cards of the same company (case-insensitive), newest first — the
    modal's 'history with this company' block (2026-08-19, the Payoneer case).
    Only cards that went anywhere (applied+ or rejected); plain found/filtered
    rows are noise here."""
    if not (company or "").strip():
        return []
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT id, title, current_status, rejection_reason,
                          coalesce(applied_at, logged_at) AS at
                   FROM jobs
                   WHERE lower(trim(company)) = lower(trim(%s)) AND id != %s
                     AND (current_status IN ('applied','recruiter_reply','screen','interview','offer')
                          OR (current_status = 'rejected' AND applied_at IS NOT NULL)
                          OR rejection_reason IN ('company_rejected','prior_bad_interview','no_response'))
                   ORDER BY coalesce(applied_at, logged_at) DESC LIMIT 5""",
                (company, exclude_job_id),
            )
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def get_resume_flags(resume_run_id: int) -> dict | None:
    """Post-repair Skeptic findings + the pre-repair count for the flags dialog
    ('было 11 → стало 2'). before_count equals the findings length for runs
    generated before the repair stage existed (no *_before stored)."""
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT coalesce(skeptic_findings, '[]'::jsonb) AS findings,
                          jsonb_array_length(coalesce(
                              resolved_content->'skeptic_findings_before',
                              coalesce(skeptic_findings, '[]'::jsonb))) AS before_count,
                          jsonb_array_length(coalesce(
                              resolved_content->'repair_actions', '[]'::jsonb)) AS repairs
                   FROM resume.generation_run WHERE id = %s""",
                (resume_run_id,),
            )
            row = cur.fetchone()
            return dict(row) if row else None
    finally:
        conn.close()


def get_resume_skeptic_count(resume_run_id: int) -> int:
    """How many Skeptic findings the generation recorded — shown as a small counter
    next to the Open-resume link. 0 for a missing run or NULL findings (failed runs
    have skeptic_findings NULL per resumebuilder-cloud migration 0002)."""
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COALESCE(jsonb_array_length(skeptic_findings), 0) AS n "
                "FROM resume.generation_run WHERE id = %s",
                (resume_run_id,),
            )
            row = cur.fetchone()
            return row["n"] if row else 0
    finally:
        conn.close()


def update_job_scoring(job_id: int, result, pipeline_run_id: str) -> None:
    """Write a fresh cloud-scoring verdict onto an existing row — the dashboard's
    auto-score-after-Add-job and Re-score paths. `result` is an ats.ATSResult (typed
    loosely to keep db.py import-light). Unlike log_job() this never touches
    outcome/current_status: a hand-added card is Dimitry's decision to track,
    so a low score must not knock it off the kanban."""
    conn = _conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE jobs SET
                           ats_score = %s, domain = %s, why_apply = %s, why_not = %s,
                           matched_keywords = %s, missed_keywords = %s, penalty_reason = %s,
                           role_score = %s, domain_score = %s, domain_value_score = %s,
                           domain_exp_score = %s, keyword_score = %s, location_score = %s,
                           location_reason = %s, pipeline_run_id = %s, scoring_source = 'cloud'
                       WHERE id = %s""",
                    (
                        result.score,
                        result.domain,
                        result.why_apply,
                        result.why_not,
                        json.dumps(result.matched),
                        json.dumps(result.missed),
                        result.penalty_reason or None,
                        result.role_score,
                        result.domain_score,
                        result.domain_value_score,
                        result.domain_exp_score,
                        result.keyword_score,
                        result.location_score,
                        result.location_reason or None,
                        pipeline_run_id,
                        job_id,
                    ),
                )
    finally:
        conn.close()
    logger.info(f"Scoring updated: job={job_id} score={result.score} [{pipeline_run_id}]")


def load_seen_jobs() -> tuple[set[str], set[tuple[str, str]]]:
    """Return (seen_urls, seen_keys) — source of truth for dedup."""
    seen_urls: set[str] = set()
    seen_keys: set[tuple[str, str]] = set()
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT url, title, company FROM jobs WHERE outcome IN ('qualified', 'low_score')"
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    for row in rows:
        if row["url"]:
            seen_urls.add(row["url"])
        if row["title"] and row["company"]:
            seen_keys.add(normalize_job_key(row["company"], row["title"]))
    logger.info(f"DB: {len(seen_urls)} seen URLs, {len(seen_keys)} seen job keys")
    return seen_urls, seen_keys


# ══════════════════════════════════════════════════════════════════════════
# Review UI (SPEC_FRONTEND.md v1.2) — Card Review, Kanban, rejection reasons,
# stats, sources panel. All reads/writes stay in Postgres — never touches
# the Notion API (see spec Boundaries).
# ══════════════════════════════════════════════════════════════════════════

_REVIEW_SORT_COLUMNS = {"newest": "logged_at DESC", "score": "ats_score DESC NULLS LAST", "source": "source ASC"}


def get_review_jobs(sort: str = "newest") -> list[dict]:
    """Cards awaiting review — current_status='found' only. Auto-rejected cards
    (rejection_reason='geo_restricted_auto') never reach 'found', so no extra
    filter is needed here (see SPEC_FRONTEND.md v1.2 route note)."""
    order = _REVIEW_SORT_COLUMNS.get(sort, _REVIEW_SORT_COLUMNS["newest"])
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT * FROM jobs WHERE current_status = 'found' ORDER BY {order}")
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def get_kanban_jobs(
    q: str | None = None,
    score_min: int | None = None,
    domains: list[str] | None = None,
    sort: str = "newest",
) -> dict[str, dict[str, list[dict]]]:
    """Two bands (design_handoff_review_ui Turn 3): 'active' — the live funnel
    columns — and 'rejected', grouped by the stage a job was rejected FROM (read
    off status_log's last old_status -> 'rejected' transition, not current_status,
    which is just 'rejected' for all of them). Every current_status='rejected' row
    went through update_job_status(), which always inserts a status_log row first —
    so the LATERAL join here is never expected to miss; a row with no match (e.g.
    an old row rejected some other way) falls back to the 'found' sub-column rather
    than being silently dropped.

    q filters by title/company substring (case-insensitive) across every status —
    matches design_handoff_review_ui Turn 5's global search; non-matching rows are
    simply excluded here, the template dims/counts what's left.

    score_min/domains are Turn 9b's board filters (URL-param driven, no server-side
    state). domains matches substring-wise so a compound "AI/ML | EdTech" row shows
    up under either filter. sort='score' orders by ats_score desc instead of newest.
    """
    conn = _conn()
    try:
        with conn.cursor() as cur:
            sql = """
                SELECT j.*, sl.old_status AS rejected_from
                FROM jobs j
                LEFT JOIN LATERAL (
                    SELECT old_status FROM status_log
                    WHERE job_id = j.id AND new_status = 'rejected'
                    ORDER BY changed_at DESC LIMIT 1
                ) sl ON true
                WHERE j.current_status IS NOT NULL
            """
            params: list = []
            if q:
                sql += " AND (j.title ILIKE %s OR j.company ILIKE %s)"
                like = f"%{q}%"
                params += [like, like]
            if score_min is not None:
                sql += " AND j.ats_score >= %s"
                params.append(score_min)
            if domains:
                sql += " AND (" + " OR ".join(["j.domain ILIKE %s"] * len(domains)) + ")"
                params += [f"%{d}%" for d in domains]
            sql += " ORDER BY j.ats_score DESC NULLS LAST" if sort == "score" else " ORDER BY j.logged_at DESC"
            cur.execute(sql, params)
            rows = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()

    active: dict[str, list[dict]] = {s: [] for s in FUNNEL_ORDER}
    rejected: dict[str, list[dict]] = {s: [] for s in FUNNEL_ORDER}
    for row in rows:
        if row["current_status"] == "rejected":
            stage = row["rejected_from"] if row["rejected_from"] in FUNNEL_ORDER else "found"
            rejected[stage].append(row)
        else:
            active.setdefault(row["current_status"], []).append(row)
    return {"active": active, "rejected": rejected}


def get_job(job_id: int) -> dict | None:
    conn = _conn()
    try:
        with conn.cursor() as cur:
            # rejected_from (see get_kanban_jobs) so the detail peek modal can offer
            # "restore to <stage>" for a job opened from the Rejected band; NULL for
            # anything not currently rejected.
            cur.execute("""
                SELECT j.*, sl.old_status AS rejected_from
                FROM jobs j
                LEFT JOIN LATERAL (
                    SELECT old_status FROM status_log
                    WHERE job_id = j.id AND new_status = 'rejected'
                    ORDER BY changed_at DESC LIMIT 1
                ) sl ON true
                WHERE j.id = %s
            """, (job_id,))
            row = cur.fetchone()
            return dict(row) if row else None
    finally:
        conn.close()


def update_job_status(
    job_id: int, new_status: str, rejection_reason: str | None = None, source: str = "manual"
) -> dict:
    """Validates the transition and the rejection reason, updates jobs.current_status,
    and appends one status_log row. Raises ValueError with a user-facing message on
    any validation failure — callers (dashboard routes) turn that into a 400."""
    job = get_job(job_id)
    if not job:
        raise ValueError(f"Unknown job id {job_id}")
    old_status = job["current_status"]

    from config import validate_status_change  # local import avoids a cycle with config's own imports
    error = validate_status_change(old_status, new_status, rejection_reason)
    if error:
        raise ValueError(error)
    if new_status != "rejected":
        rejection_reason = None  # never persist a stale reason from a prior rejection

    conn = _conn()
    try:
        with conn:
            with conn.cursor() as cur:
                if new_status == "applied":
                    cur.execute(
                        "UPDATE jobs SET current_status = %s, rejection_reason = %s, applied_at = NOW() WHERE id = %s",
                        (new_status, rejection_reason, job_id),
                    )
                else:
                    cur.execute(
                        "UPDATE jobs SET current_status = %s, rejection_reason = %s WHERE id = %s",
                        (new_status, rejection_reason, job_id),
                    )
                cur.execute(
                    "INSERT INTO status_log (job_id, old_status, new_status, source) VALUES (%s,%s,%s,%s)",
                    (job_id, old_status, new_status, source),
                )
    finally:
        conn.close()
    logger.info(f"Status changed: job={job_id} {old_status} -> {new_status}")
    return {"job_id": job_id, "old_status": old_status, "new_status": new_status}


def get_rejection_reason_counts(date_from: str | None = None, date_to: str | None = None) -> list[dict]:
    conditions = ["current_status = 'rejected'"]
    params: list = []
    if date_from:
        conditions.append("logged_at >= %s")
        params.append(date_from)
    if date_to:
        conditions.append("logged_at <= %s")
        params.append(date_to)
    where = " AND ".join(conditions)
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT rejection_reason, COUNT(*) as cnt FROM jobs WHERE {where} "
                "GROUP BY rejection_reason ORDER BY cnt DESC",
                params,
            )
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def get_conversion_stats(date_from: str | None = None, date_to: str | None = None) -> dict:
    """Qualified -> applied conversion, overall + per calendar month, from status_log's
    first transition to 'applied' over the qualified count in the same window."""
    conditions = ["outcome = 'qualified'"]
    params: list = []
    if date_from:
        conditions.append("logged_at >= %s")
        params.append(date_from)
    if date_to:
        conditions.append("logged_at <= %s")
        params.append(date_to)
    where = " AND ".join(conditions)
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) as cnt FROM jobs WHERE {where}", params)
            qualified_total = cur.fetchone()["cnt"]

            cur.execute(f"""
                SELECT DATE_TRUNC('month', MIN(status_log.changed_at)) as month, status_log.job_id
                FROM status_log
                JOIN jobs ON jobs.id = status_log.job_id
                WHERE status_log.new_status = 'applied' AND {where}
                GROUP BY status_log.job_id
            """, params)
            first_applied = [dict(r) for r in cur.fetchall()]

            cur.execute(f"""
                SELECT DATE_TRUNC('month', logged_at) as month, COUNT(*) as cnt
                FROM jobs WHERE {where} GROUP BY month ORDER BY month
            """, params)
            qualified_by_month = {str(r["month"])[:7]: r["cnt"] for r in cur.fetchall()}
    finally:
        conn.close()

    applied_by_month: dict[str, int] = {}
    for row in first_applied:
        if row["month"]:
            applied_by_month[str(row["month"])[:7]] = applied_by_month.get(str(row["month"])[:7], 0) + 1
    applied_total = sum(applied_by_month.values())

    months = sorted(set(qualified_by_month) | set(applied_by_month))
    per_month = [
        {
            "month": m,
            "qualified": qualified_by_month.get(m, 0),
            "applied": applied_by_month.get(m, 0),
            "rate": round(100 * applied_by_month.get(m, 0) / qualified_by_month[m], 1) if qualified_by_month.get(m) else 0.0,
        }
        for m in months
    ]
    return {
        "qualified_total": qualified_total,
        "applied_total": applied_total,
        "rate": round(100 * applied_total / qualified_total, 1) if qualified_total else 0.0,
        "per_month": per_month,
    }


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    values = sorted(values)
    n = len(values)
    mid = n // 2
    return values[mid] if n % 2 else (values[mid - 1] + values[mid]) / 2


def get_tracker_series(date_from: str | None = None, date_to: str | None = None) -> dict:
    """Weekly-bucketed Found/Applied/Replies event counts for the Tracker chart
    (design_handoff_review_ui Turn 6 — replaces the manual Notion tracker). Found
    counts jobs discovered that week (logged_at); Applied/Replies count status_log
    events that week (changed_at) — an event-based read, not "jobs currently at X",
    so a job that applied then got rejected still shows up in the week it applied.
    date_from/date_to scope the chart; the summary totals (reply rate, median time
    to reply) are always all-time, matching the legend chips in the mock."""

    def _date_cond(col: str) -> tuple[str, list]:
        conds, params = [], []
        if date_from:
            conds.append(f"{col} >= %s")
            params.append(date_from)
        if date_to:
            conds.append(f"{col} <= %s")
            params.append(date_to)
        return (" AND " + " AND ".join(conds)) if conds else "", params

    conn = _conn()
    try:
        with conn.cursor() as cur:
            cond, params = _date_cond("logged_at")
            cur.execute(
                f"SELECT DATE_TRUNC('week', logged_at) as week, COUNT(*) as cnt "
                f"FROM jobs WHERE outcome='qualified'{cond} GROUP BY week ORDER BY week", params,
            )
            found = {str(r["week"])[:10]: r["cnt"] for r in cur.fetchall()}

            cond, params = _date_cond("changed_at")
            cur.execute(
                f"SELECT DATE_TRUNC('week', changed_at) as week, COUNT(*) as cnt "
                f"FROM status_log WHERE new_status='applied'{cond} GROUP BY week ORDER BY week", params,
            )
            applied = {str(r["week"])[:10]: r["cnt"] for r in cur.fetchall()}

            cur.execute(
                f"SELECT DATE_TRUNC('week', changed_at) as week, COUNT(*) as cnt "
                f"FROM status_log WHERE new_status='recruiter_reply'{cond} GROUP BY week ORDER BY week", params,
            )
            replies = {str(r["week"])[:10]: r["cnt"] for r in cur.fetchall()}

            cur.execute("SELECT COUNT(*) as cnt FROM status_log WHERE new_status='applied'")
            applied_total = cur.fetchone()["cnt"]
            cur.execute("SELECT COUNT(*) as cnt FROM status_log WHERE new_status='recruiter_reply'")
            reply_total = cur.fetchone()["cnt"]

            cur.execute("""
                SELECT EXTRACT(EPOCH FROM (r.first_reply - a.first_applied)) / 86400 as days
                FROM (SELECT job_id, MIN(changed_at) as first_applied FROM status_log
                      WHERE new_status='applied' GROUP BY job_id) a
                JOIN (SELECT job_id, MIN(changed_at) as first_reply FROM status_log
                      WHERE new_status='recruiter_reply' GROUP BY job_id) r
                  ON r.job_id = a.job_id AND r.first_reply > a.first_applied
            """)
            reply_days = [row["days"] for row in cur.fetchall() if row["days"] is not None]
    finally:
        conn.close()

    weeks = sorted(set(found) | set(applied) | set(replies))
    peak_week, peak_applied = max(applied.items(), key=lambda kv: kv[1], default=(None, 0))
    median_days = _median(reply_days)

    return {
        "weeks": weeks,
        "found": [found.get(w, 0) for w in weeks],
        "applied": [applied.get(w, 0) for w in weeks],
        "replies": [replies.get(w, 0) for w in weeks],
        "applied_total": applied_total,
        "reply_total": reply_total,
        "reply_rate": round(100 * reply_total / applied_total, 1) if applied_total else 0.0,
        "median_reply_days": round(median_days, 1) if median_days is not None else None,
        "peak_week": peak_week,
        "peak_week_applied": peak_applied,
    }


def get_funnel_stats() -> dict:
    """Cohort funnel (design_handoff_review_ui Turn 9a): for each funnel stage AFTER
    'found' (found isn't logged to status_log — log_job() sets it directly on INSERT,
    never through update_job_status() — so a reach-count for it would only reflect
    restore/un-reject events, wildly undercounting; the mock's funnel starts at
    Applied too), how many jobs ever reached it (status_log distinct job_id per
    new_status — reading status_log rather than current_status so a job later
    rejected still counts toward every stage it actually passed through), conversion
    % vs. the previous stage, median days spent at that stage before its next
    transition (forward or rejected), and rejected-jobs-by-origin-stage (same LATERAL
    pattern as get_kanban_jobs — origin CAN be 'found', so that one stays in scope)."""
    stages_after_found = FUNNEL_ORDER[1:]
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT new_status, COUNT(DISTINCT job_id) as cnt FROM status_log "
                "WHERE new_status = ANY(%s) GROUP BY new_status", (stages_after_found,),
            )
            reached = {r["new_status"]: r["cnt"] for r in cur.fetchall()}

            cur.execute("""
                SELECT t.new_status as stage,
                       EXTRACT(EPOCH FROM (t.next_changed - t.changed_at)) / 86400 as days
                FROM (
                    SELECT job_id, new_status, changed_at,
                           LEAD(changed_at) OVER (PARTITION BY job_id ORDER BY changed_at) as next_changed
                    FROM status_log
                ) t
                WHERE t.new_status = ANY(%s) AND t.next_changed IS NOT NULL
            """, (stages_after_found,))
            days_by_stage: dict[str, list[float]] = {s: [] for s in stages_after_found}
            for r in cur.fetchall():
                if r["stage"] in days_by_stage and r["days"] is not None:
                    days_by_stage[r["stage"]].append(r["days"])

            cur.execute("""
                SELECT sl.old_status AS stage, COUNT(*) as cnt
                FROM jobs j
                JOIN LATERAL (
                    SELECT old_status FROM status_log
                    WHERE job_id = j.id AND new_status = 'rejected'
                    ORDER BY changed_at DESC LIMIT 1
                ) sl ON true
                WHERE j.current_status = 'rejected'
                GROUP BY sl.old_status
            """)
            rejected_by_stage = {r["stage"]: r["cnt"] for r in cur.fetchall()}
    finally:
        conn.close()

    stages = []
    prev_cnt = None
    for s in stages_after_found:
        cnt = reached.get(s, 0)
        stages.append({
            "status": s,
            "count": cnt,
            "conversion_pct": round(100 * cnt / prev_cnt, 1) if prev_cnt else None,
            "median_days": round(_median(days_by_stage.get(s, [])), 1) if days_by_stage.get(s) else None,
        })
        prev_cnt = cnt
    return {"stages": stages, "rejected_by_stage": rejected_by_stage}


def get_sources_config() -> dict[str, bool]:
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT source, enabled FROM sources_config")
            return {r["source"]: r["enabled"] for r in cur.fetchall()}
    finally:
        conn.close()


def get_sources_summary() -> list[dict]:
    """Per-source fetched/qualified/yield% + enabled flag, for the Sources panel (2b).
    Fetched/qualified come from the existing jobs table — no new columns needed for
    the read side."""
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT source,
                       COUNT(*) as fetched,
                       SUM(CASE WHEN outcome = 'qualified' THEN 1 ELSE 0 END) as qualified,
                       SUM(CASE WHEN current_status NOT IN ('found', 'rejected') THEN 1 ELSE 0 END) as applied_from
                FROM jobs WHERE source IS NOT NULL
                GROUP BY source ORDER BY fetched DESC
            """)
            rows = [dict(r) for r in cur.fetchall()]
            enabled = get_sources_config()
    finally:
        conn.close()
    for r in rows:
        r["yield_pct"] = round(100 * r["qualified"] / r["fetched"], 1) if r["fetched"] else 0.0
        r["enabled"] = enabled.get(r["source"], True)  # unconfigured sources default enabled
    return rows


def toggle_source(name: str) -> bool:
    """Flips sources_config.enabled for `name`. Returns False if the source has never
    appeared in jobs.source (caller turns that into a 404 — no silent-create of an
    unknown source name)."""
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM jobs WHERE source = %s LIMIT 1", (name,))
            if not cur.fetchone():
                return False
            with conn:
                # No existing row means "enabled" by implicit default (get_sources_summary()'s
                # fallback) — so the first toggle must insert FALSE, not TRUE, to actually flip it.
                cur.execute(
                    """INSERT INTO sources_config (source, enabled, updated_at)
                       VALUES (%s, FALSE, NOW())
                       ON CONFLICT (source) DO UPDATE
                       SET enabled = NOT sources_config.enabled, updated_at = NOW()""",
                    (name,),
                )
    finally:
        conn.close()
    logger.info(f"Source toggled: {name}")
    return True


def get_last_run() -> dict | None:
    """Most recent completed scrape run — Turn 7g's auto-refresh banner and Turn 7c's
    stale-scraper warning both derive from this (id/finished_at to detect "run just
    finished", finished_at's age to detect "scraper's gone quiet"). None if no run has
    ever finished (fresh DB)."""
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, started_at, finished_at, qualified FROM runs "
                "WHERE finished_at IS NOT NULL ORDER BY id DESC LIMIT 1"
            )
            row = cur.fetchone()
            return dict(row) if row else None
    finally:
        conn.close()
