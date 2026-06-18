import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from utils import normalize_job_key

logger = logging.getLogger(__name__)

# /data/jobs.db on Railway (persistent volume), ./jobs.db locally
DB_PATH = os.environ.get("DB_PATH", str(Path(__file__).parent / "jobs.db"))


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    with _connect() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS runs (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at   TEXT NOT NULL,
                finished_at  TEXT,
                total_fetched      INTEGER DEFAULT 0,
                qualified          INTEGER DEFAULT 0,
                rejected_low_score INTEGER DEFAULT 0,
                filtered_role      INTEGER DEFAULT 0,
                filtered_location  INTEGER DEFAULT 0,
                filtered_stale     INTEGER DEFAULT 0,
                filtered_dedup     INTEGER DEFAULT 0,
                filtered_gpt_limit INTEGER DEFAULT 0,
                gpt_calls          INTEGER DEFAULT 0,
                sources_json       TEXT
            );

            CREATE TABLE IF NOT EXISTS jobs (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
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
                outcome     TEXT,  -- qualified | low_score | role | location | stale | dedup | gpt_limit
                logged_at   TEXT DEFAULT (datetime('now'))
            );

            CREATE INDEX IF NOT EXISTS idx_jobs_run_id ON jobs(run_id);
            CREATE INDEX IF NOT EXISTS idx_jobs_outcome ON jobs(outcome);
            CREATE INDEX IF NOT EXISTS idx_runs_started ON runs(started_at);
            CREATE INDEX IF NOT EXISTS idx_jobs_url ON jobs(url);
        """)
        for migration in [
            "ALTER TABLE jobs ADD COLUMN published TEXT",
            "ALTER TABLE runs ADD COLUMN filtered_language INTEGER DEFAULT 0",
            "ALTER TABLE jobs ADD COLUMN apply_url TEXT",
            "ALTER TABLE jobs ADD COLUMN description TEXT",
            "ALTER TABLE jobs ADD COLUMN domain TEXT",
            "ALTER TABLE jobs ADD COLUMN why_not TEXT",
        ]:
            try:
                conn.execute(migration)
            except sqlite3.OperationalError:
                pass
    logger.info(f"DB: initialised at {DB_PATH}")


def start_run(total_fetched: int, source_counts: dict) -> int:
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO runs (started_at, total_fetched, sources_json) VALUES (?, ?, ?)",
            (now, total_fetched, json.dumps(source_counts)),
        )
        run_id = cur.lastrowid
    logger.info(f"DB: run #{run_id} started")
    return run_id


def finish_run(run_id: int, counts: dict, gpt_calls: int) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        conn.execute("""
            UPDATE runs SET
                finished_at        = ?,
                qualified          = ?,
                rejected_low_score = ?,
                filtered_role      = ?,
                filtered_language  = ?,
                filtered_location  = ?,
                filtered_stale     = ?,
                filtered_dedup     = ?,
                filtered_gpt_limit = ?,
                gpt_calls          = ?
            WHERE id = ?
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
    logger.info(f"DB: run #{run_id} finished")


def log_job(
    run_id: int,
    job: dict,
    outcome: str,
    ats_score: int | None = None,
    domain: str | None = None,
    why_not: str | None = None,
) -> None:
    desc = (job.get("description") or "")[:8000]  # cap at 8k chars
    with _connect() as conn:
        conn.execute(
            """INSERT INTO jobs
               (run_id, url, apply_url, title, company, source, published,
                description, ats_score, domain, why_not, outcome)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
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
            ),
        )


def load_seen_jobs() -> tuple[set[str], set[tuple[str, str]]]:
    """Return (seen_urls, seen_keys) from SQLite — the source of truth for dedup.
    Only includes jobs that were actually sent to GPT (qualified + low_score).
    Filtered-out jobs (role/location/language/stale) are excluded — they may
    legitimately reappear from another source with a better description.
    """
    seen_urls: set[str] = set()
    seen_keys: set[tuple[str, str]] = set()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT url, title, company FROM jobs WHERE outcome IN ('qualified', 'low_score')"
        ).fetchall()
    for row in rows:
        if row["url"]:
            seen_urls.add(row["url"])
        if row["title"] and row["company"]:
            seen_keys.add(normalize_job_key(row["company"], row["title"]))
    logger.info(f"DB: {len(seen_urls)} seen URLs, {len(seen_keys)} seen job keys (source of truth)")
    return seen_urls, seen_keys
