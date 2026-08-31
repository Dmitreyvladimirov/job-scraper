"""Re-read job descriptions that were stored as listing stubs, and flag the rest.

Why: a card whose description is a stub was scored on the title and the company name
(Chili Piper #84808, 2026-08-31 — 232 characters ending in "Loading...", scored 84,
resume generated with no AI-consulting block on a JD that requires hands-on AI). The
scraper now tops such descriptions up from the resolved posting URL; this script does
the same for rows already in the database.

Two phases, both optional:
  --refresh   re-fetch the JD from apply_url for every thin row (needs DATABASE_URL only)
  --rescore   ask the production dashboard to re-score the rows it refreshed
              (needs DASHBOARD_URL + DASHBOARD_TOKEN: the scoring service's own token
              lives on Railway, so the score is recomputed through the dashboard rather
              than by calling the scoring service from here)

Without --apply nothing is written: the run prints what it would do.

Usage:
    DATABASE_URL=... python3 scripts/backfill_thin_jds.py --refresh
    DATABASE_URL=... python3 scripts/backfill_thin_jds.py --refresh --apply
    DASHBOARD_URL=... DASHBOARD_TOKEN=... python3 scripts/backfill_thin_jds.py --rescore --apply
"""
import argparse
import logging
import os
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent.parent / "core"))

import db  # noqa: E402
from config import SCORING_MIN_JD_CHARS  # noqa: E402
from utils import fetch_jd_from_url, fetch_url_generic  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def thin_rows(days: int) -> list[dict]:
    """Scored cards whose stored description is too short to have been the real ad."""
    conn = db._conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT id, title, company, source, url, apply_url, current_status,
                          ats_score, length(coalesce(description, '')) AS jd_chars
                   FROM jobs
                   WHERE ats_score IS NOT NULL
                     AND length(coalesce(description, '')) < %s
                     AND logged_at > now() - make_interval(days => %s)
                   ORDER BY (current_status = 'found') DESC, ats_score DESC""",
                (SCORING_MIN_JD_CHARS, days))
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def named_rows(ids: list[int]) -> list[dict]:
    """The same projection as thin_rows(), for ids given on the command line."""
    conn = db._conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT id, title, company, source, url, apply_url, current_status,
                          ats_score, length(coalesce(description, '')) AS jd_chars
                   FROM jobs WHERE id = ANY(%s) ORDER BY id""", (ids,))
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def refresh(rows: list[dict], apply: bool) -> list[int]:
    """Re-fetch each row's JD from its posting URL. Returns the ids actually updated."""
    updated = []
    for row in rows:
        apply_url = row.get("apply_url") or ""
        if not apply_url or apply_url == (row.get("url") or ""):
            logger.info(f"  #{row['id']} {row['company']}: no separate posting URL — flag only")
            _flag(row["id"], apply)
            continue
        jd = fetch_jd_from_url(apply_url) or fetch_url_generic(apply_url)
        if not jd or len(jd) <= row["jd_chars"]:
            logger.info(f"  #{row['id']} {row['company']}: posting gave nothing better — flag only")
            _flag(row["id"], apply)
            continue
        logger.info(f"  #{row['id']} {row['company']}: {row['jd_chars']} -> {len(jd)} chars"
                    + ("" if apply else "  (dry run)"))
        if apply:
            _write(row["id"], jd)
        updated.append(row["id"])
    return updated


def _write(job_id: int, jd: str) -> None:
    conn = db._conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE jobs SET description = %s, incomplete_description = FALSE WHERE id = %s",
                    (jd.replace("\x00", "")[:20000], job_id))
    finally:
        conn.close()


def _flag(job_id: int, apply: bool) -> None:
    """Mark the score as read off a stub — the card stays, its number stops pretending."""
    if not apply:
        return
    conn = db._conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE jobs SET incomplete_description = TRUE WHERE id = %s", (job_id,))
    finally:
        conn.close()


def rescore(job_ids: list[int], apply: bool) -> None:
    base = os.environ.get("DASHBOARD_URL", "").rstrip("/")
    token = os.environ.get("DASHBOARD_TOKEN", "")
    if not (base and token):
        sys.exit("--rescore needs DASHBOARD_URL and DASHBOARD_TOKEN")
    for job_id in job_ids:
        if not apply:
            logger.info(f"  #{job_id}: would POST /jobs/{job_id}/rescore  (dry run)")
            continue
        r = requests.post(f"{base}/jobs/{job_id}/rescore", timeout=120,
                          cookies={"dashboard_token": token})
        logger.info(f"  #{job_id}: rescore -> {r.status_code}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--rescore", action="store_true")
    ap.add_argument("--apply", action="store_true", help="actually write; otherwise dry run")
    ap.add_argument("--ids", help="comma-separated job ids, instead of the query")
    args = ap.parse_args()

    if args.ids:
        # Explicit ids bypass the length filter: after --refresh those rows are no
        # longer thin, and --rescore still has to reach them.
        rows = named_rows([int(i) for i in args.ids.split(",")])
    else:
        rows = thin_rows(args.days)
    logger.info(f"{len(rows)} cards selected"
                + ("" if args.ids else f" (scored, under {SCORING_MIN_JD_CHARS} chars of JD)"))

    updated = refresh(rows, args.apply) if args.refresh else [r["id"] for r in rows]
    logger.info(f"refreshed: {len(updated)}")
    if args.rescore:
        rescore(updated, args.apply)


if __name__ == "__main__":
    main()
