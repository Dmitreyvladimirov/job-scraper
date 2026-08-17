import json
import logging
import os
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras
from utils import normalize_job_key

logger = logging.getLogger(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL", "")


def _conn():
    if not DATABASE_URL:
        raise EnvironmentError("DATABASE_URL is not set")
    return psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)


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
                        notion_id   TEXT UNIQUE,
                        current_status TEXT,
                        rejection_reason TEXT,
                        source_type TEXT DEFAULT 'scraper',
                        deleted_at  TIMESTAMP,
                        logged_at   TIMESTAMP DEFAULT NOW()
                    )
                """)
                cur.execute("ALTER TABLE jobs ALTER COLUMN run_id DROP NOT NULL")
                cur.execute("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS notion_id TEXT UNIQUE")
                cur.execute("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS current_status TEXT")
                cur.execute("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS rejection_reason TEXT")
                cur.execute("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS source_type TEXT DEFAULT 'scraper'")
                cur.execute("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMP")
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS status_log (
                        id          SERIAL PRIMARY KEY,
                        job_id      INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                        old_status  TEXT,
                        new_status  TEXT NOT NULL,
                        changed_at  TIMESTAMP DEFAULT NOW(),
                        source      TEXT
                    )
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS telegram_channel_metrics (
                        run_id       INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                        channel      TEXT NOT NULL,
                        fetched      INTEGER NOT NULL DEFAULT 0,
                        pm_signal    INTEGER NOT NULL DEFAULT 0,
                        role_pass    INTEGER NOT NULL DEFAULT 0,
                        enriched     INTEGER NOT NULL DEFAULT 0,
                        quality_gate INTEGER NOT NULL DEFAULT 0,
                        ats          INTEGER NOT NULL DEFAULT 0,
                        qualified    INTEGER NOT NULL DEFAULT 0,
                        PRIMARY KEY (run_id, channel)
                    )
                """)
                cur.execute("CREATE INDEX IF NOT EXISTS idx_jobs_run_id ON jobs(run_id)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_jobs_outcome ON jobs(outcome)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_jobs_current_status ON jobs(current_status)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_jobs_rejection_reason ON jobs(rejection_reason)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_status_log_job_id ON status_log(job_id)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_status_log_new_status ON status_log(new_status)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_runs_started ON runs(started_at)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_jobs_url ON jobs(url)")
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
) -> None:
    desc = (job.get("description") or "")[:8000]
    current_status = "qualified" if outcome == "qualified" else "rejected" if outcome == "low_score" else None
    rejection_reason = "low_score" if outcome == "low_score" else None
    conn = _conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO jobs
                       (run_id, url, apply_url, title, company, source, published,
                        description, ats_score, domain, why_not, outcome,
                        current_status, rejection_reason)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
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
                        rejection_reason,
                    ),
                )
    finally:
        conn.close()


def save_telegram_channel_metrics(run_id: int, metrics: dict[str, dict[str, int]]) -> None:
    if not metrics:
        return
    conn = _conn()
    try:
        with conn:
            with conn.cursor() as cur:
                for channel, counts in metrics.items():
                    cur.execute(
                        """INSERT INTO telegram_channel_metrics
                           (run_id, channel, fetched, pm_signal, role_pass, enriched,
                            quality_gate, ats, qualified)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                           ON CONFLICT (run_id, channel) DO UPDATE SET
                               fetched = EXCLUDED.fetched,
                               pm_signal = EXCLUDED.pm_signal,
                               role_pass = EXCLUDED.role_pass,
                               enriched = EXCLUDED.enriched,
                               quality_gate = EXCLUDED.quality_gate,
                               ats = EXCLUDED.ats,
                               qualified = EXCLUDED.qualified""",
                        (
                            run_id,
                            channel,
                            counts.get("fetched", 0),
                            counts.get("pm_signal", 0),
                            counts.get("role_pass", 0),
                            counts.get("enriched", 0),
                            counts.get("quality_gate", 0),
                            counts.get("ats", 0),
                            counts.get("qualified", 0),
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
