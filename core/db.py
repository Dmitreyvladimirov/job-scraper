import json
import logging
import os
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras
from config import CURRENT_STATUSES
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
) -> None:
    """why_apply/keywords/penalty_reason/score-breakdown are new (SPEC_FRONTEND.md v1.2,
    Review UI) — optional/backward-compatible params, wired from scraper.py's low_score
    and qualified call sites (ats_error/role/language/etc. calls stay as before, no
    ATSResult available at those points). salary is read straight off `job` (every
    source already produces it) rather than needing a separate kwarg at each call site."""
    desc = (job.get("description") or "")[:8000]
    salary = job.get("salary") or None
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
                        keyword_score, location_score, location_reason, salary)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
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
                    ),
                )
    finally:
        conn.close()


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


def get_kanban_jobs() -> dict[str, list[dict]]:
    """All statuses, grouped — the funnel columns (1f shows 'Found' as the first
    column too, same cards /review lists, just as an overview here)."""
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM jobs WHERE current_status IS NOT NULL ORDER BY logged_at DESC")
            rows = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()
    grouped: dict[str, list[dict]] = {s: [] for s in CURRENT_STATUSES}
    for row in rows:
        grouped.setdefault(row["current_status"], []).append(row)
    return grouped


def get_job(job_id: int) -> dict | None:
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM jobs WHERE id = %s", (job_id,))
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
