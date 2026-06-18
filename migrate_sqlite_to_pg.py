"""One-shot migration: SQLite (old Railway Volume) → Postgres (new Railway DB).

Run from the old 'Job scraper !' Railway Console after setting DATABASE_URL:
    python3 migrate_sqlite_to_pg.py
"""
import os
import sqlite3
import psycopg2
import psycopg2.extras

DB_PATH = os.environ.get("DB_PATH", "/data/jobs.db")
DATABASE_URL = os.environ.get("DATABASE_URL", "")

if not DATABASE_URL:
    raise SystemExit("ERROR: DATABASE_URL is not set")
if not os.path.exists(DB_PATH):
    raise SystemExit(f"ERROR: SQLite not found at {DB_PATH}")

sqlite = sqlite3.connect(DB_PATH)
sqlite.row_factory = sqlite3.Row
pg = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)

# ── inventory ────────────────────────────────────────────────────────────────
src_runs  = sqlite.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
src_jobs  = sqlite.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
print(f"SQLite  → {src_runs} runs, {src_jobs} jobs")

with pg.cursor() as cur:
    cur.execute("SELECT COUNT(*) as c FROM runs"); pg_runs = cur.fetchone()["c"]
    cur.execute("SELECT COUNT(*) as c FROM jobs"); pg_jobs = cur.fetchone()["c"]
    cur.execute("SELECT started_at FROM runs")
    existing_started_at = {r["started_at"] for r in cur.fetchall()}
print(f"Postgres → {pg_runs} runs, {pg_jobs} jobs (already)")

# ── migrate runs ─────────────────────────────────────────────────────────────
id_map = {}   # old SQLite run id → new Postgres run id
migrated_runs = skipped_runs = 0

for run in sqlite.execute("SELECT * FROM runs ORDER BY id").fetchall():
    if run["started_at"] in existing_started_at:
        skipped_runs += 1
        continue

    def rv(key, default=None):
        try:
            v = run[key]
            return v if v is not None else default
        except (IndexError, KeyError):
            return default

    with pg.cursor() as cur:
        cur.execute("""
            INSERT INTO runs (
                started_at, finished_at, total_fetched, qualified,
                rejected_low_score, filtered_role, filtered_language,
                filtered_location, filtered_stale, filtered_dedup,
                filtered_gpt_limit, gpt_calls, sources_json
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            RETURNING id
        """, (
            rv("started_at"),   rv("finished_at"),
            rv("total_fetched", 0), rv("qualified", 0),
            rv("rejected_low_score", 0), rv("filtered_role", 0),
            rv("filtered_language", 0), rv("filtered_location", 0),
            rv("filtered_stale", 0), rv("filtered_dedup", 0),
            rv("filtered_gpt_limit", 0), rv("gpt_calls", 0),
            rv("sources_json"),
        ))
        id_map[run["id"]] = cur.fetchone()["id"]
    pg.commit()
    migrated_runs += 1

print(f"Runs: migrated {migrated_runs}, skipped {skipped_runs}")

# ── migrate jobs ─────────────────────────────────────────────────────────────
migrated_jobs = skipped_jobs = 0

for job in sqlite.execute("SELECT * FROM jobs ORDER BY id").fetchall():
    if job["run_id"] not in id_map:
        skipped_jobs += 1
        continue

    def jv(key, default=None):
        try:
            v = job[key]
            return v if v is not None else default
        except (IndexError, KeyError):
            return default

    with pg.cursor() as cur:
        cur.execute("""
            INSERT INTO jobs (
                run_id, url, apply_url, title, company, source,
                published, description, ats_score, domain, why_not, outcome
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (
            id_map[job["run_id"]],
            jv("url"),        jv("apply_url"),  jv("title"),
            jv("company"),    jv("source"),     jv("published"),
            jv("description"), jv("ats_score"), jv("domain"),
            jv("why_not"),    jv("outcome"),
        ))
    pg.commit()
    migrated_jobs += 1

print(f"Jobs:  migrated {migrated_jobs}, skipped {skipped_jobs}")

# ── final count ───────────────────────────────────────────────────────────────
with pg.cursor() as cur:
    cur.execute("SELECT COUNT(*) as c FROM runs"); final_runs = cur.fetchone()["c"]
    cur.execute("SELECT COUNT(*) as c FROM jobs"); final_jobs = cur.fetchone()["c"]
print(f"\nDone! Postgres now has {final_runs} runs, {final_jobs} jobs")

sqlite.close()
pg.close()
