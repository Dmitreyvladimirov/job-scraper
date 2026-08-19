"""Import the 2026 job-search wave from the Notion Job Hunt tracker into Postgres.

Scope and rules agreed with Dimitry on 2026-08-19:
- Manual cards only (Status2 NOT in the scraper's own mirror statuses) whose
  Date Applied (or created time) is on/after 2026-01-01.
- Status mapping: applied / "No News" -> applied; "Recruiter interview" -> screen;
  "Hiring manager interview" / "Secont step interview" / "Home assignment" ->
  interview; "Asked referral" -> found; Offer -> offer; Rejected -> rejected.
- 30-day aging (both applied and interview stages): if the wave date is older
  than 30 days the card lands as rejected — reason no_response for silent
  applies, company_rejected for processes that reached interviews and stalled —
  with the FULL status_log chain (found -> applied -> ... ) stamped at the wave
  date, so the funnel/rejected-from analytics keep the real history.
- Dedup: URL then company+title against existing jobs. An existing card that is
  already applied-or-further is left alone (dashboard truth wins); found/NULL/
  rejected cards are upgraded in place with the imported chain.
- Extras carried over: ATS Score, JD text, source ("Где нашёл"), location
  ("Тип вакансии": Israel / Fully Remote / Spain / Russia), and a job_comments
  note for "Слили из-за иврита".

Dry-run by default; --apply writes. Never deletes anything.
"""
import argparse
import json
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "core"))

import db  # noqa: E402
from config import NOTION_TOKEN, NOTION_DATABASE_ID  # noqa: E402

WAVE_START = "2026-01-01"
AGE_DAYS = 30

MANUAL_STATUSES = {
    "applied", "No News", "Recruiter interview", "Hiring manager interview",
    "Secont step interview", "Home assignment", "Asked referral", "Offer",
    "Rejected", "Not anymore",
}

STAGE_MAP = {
    "applied": "applied", "No News": "applied",
    "Recruiter interview": "screen",
    "Hiring manager interview": "interview",
    "Secont step interview": "interview",
    "Home assignment": "interview",
    "Asked referral": "found",
    "Offer": "offer",
}
FINAL_REJECTED = {"Rejected": "company_rejected", "Not anymore": "inactive_closed"}

CHAIN = ["found", "applied", "recruiter_reply", "screen", "interview", "offer"]

LOCATION_MAP = {
    "🇮🇱 Israel": "Israel", "🌎 Fully Remote": "Remote worldwide",
    "🇪🇸 Spain": "Spain", "🇷🇺 Russia": "Russia",
}
SOURCE_MAP = {
    "Telegramm": "Telegram (manual)", "LinkedIn": "LinkedIn", "REFERAL": "Referral",
    "reached": "Outreach", "Mevihaver": "Mevihaver", "Secrettelavivjobs": "SecretTelAviv",
    "Glassdoor": "Glassdoor", "Gvahim": "Gvahim", "Startupnationcentral": "StartupNationCentral",
    "NefeshBNefesh": "NefeshBNefesh",
}


def _notion_query_all() -> list[dict]:
    pages, cursor = [], None
    while True:
        body = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        req = urllib.request.Request(
            f"https://api.notion.com/v1/databases/{NOTION_DATABASE_ID}/query",
            data=json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {NOTION_TOKEN}",
                     "Notion-Version": "2022-06-28", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
        pages.extend(data["results"])
        if not data.get("has_more"):
            return pages
        cursor = data["next_cursor"]


def _prop_text(page, name):
    p = page["properties"].get(name) or {}
    parts = p.get("title") or p.get("rich_text") or []
    return "".join(x.get("plain_text", "") for x in parts).strip()


def _prop_select(page, name):
    p = page["properties"].get(name) or {}
    return (p.get("select") or {}).get("name")


def _extract(page) -> dict | None:
    status2 = _prop_select(page, "Status2")
    if status2 not in MANUAL_STATUSES:
        return None
    date_applied = ((page["properties"].get("Date Applied") or {}).get("date") or {}).get("start")
    wave_date = (date_applied or page.get("created_time", ""))[:10]
    if not wave_date or wave_date < WAVE_START:
        return None
    title = _prop_text(page, "Позиция")
    if not title:
        return None
    if "DUPLICATE" in title.upper():
        return None  # Dimitry marked these in Notion as duplicates of scraper cards
    return {
        "title": title,
        "company": _prop_text(page, "Компания") or None,
        "url": (page["properties"].get("Ссылка на вакансию") or {}).get("url"),
        "description": _prop_text(page, "Job description") or None,
        "ats_score": (page["properties"].get("ATS Score") or {}).get("number"),
        "status2": status2,
        "wave_date": wave_date,
        "location": LOCATION_MAP.get(_prop_select(page, "Тип вакансии")),
        "source": SOURCE_MAP.get(_prop_select(page, "Где нашёл") or "", "NotionImport"),
        "hebrew_flag": (page["properties"].get("Слили из-за иврита") or {}).get("checkbox", False),
    }


def _resolve(entry: dict, today) -> tuple[str, str | None, list[str]]:
    """-> (final_status, rejection_reason, status_log_chain_beyond_found)."""
    if entry["status2"] in FINAL_REJECTED:
        stage = STAGE_MAP.get(entry["status2"], "applied")
        chain = CHAIN[1:CHAIN.index("applied") + 1]
        return "rejected", FINAL_REJECTED[entry["status2"]], chain + ["rejected"]
    stage = STAGE_MAP[entry["status2"]]
    if stage == "found":
        return "found", None, []
    chain = CHAIN[1:CHAIN.index(stage) + 1]
    aged = datetime.strptime(entry["wave_date"], "%Y-%m-%d").date() < today - timedelta(days=AGE_DAYS)
    if aged and stage != "offer":
        reason = "no_response" if stage == "applied" else "company_rejected"
        return "rejected", reason, chain + ["rejected"]
    return stage, None, chain


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    today = datetime.now(timezone.utc).date()
    pages = _notion_query_all()
    entries = [e for e in (_extract(p) for p in pages) if e]
    print(f"Notion pages: {len(pages)}, manual 2026-wave entries: {len(entries)}")

    # One-shot dedup index instead of 349 per-entry connections.
    conn = db._conn()
    with conn.cursor() as cur:
        cur.execute("SELECT id, url, apply_url, company, title, current_status FROM jobs")
        rows = cur.fetchall()
    conn.close()
    by_url = {}
    by_key = {}
    for r in rows:
        for u in (r["url"], r["apply_url"]):
            if u:
                by_url.setdefault(u, r)
        if r["company"] and r["title"]:
            by_key.setdefault((r["company"].strip().lower(), r["title"].strip().lower()), r)

    def _find_existing(e):
        if e["url"] and e["url"] in by_url:
            return by_url[e["url"]]
        if e["company"]:
            return by_key.get((e["company"].strip().lower(), e["title"].strip().lower()))
        return None

    plan = []
    for e in entries:
        final, reason, chain = _resolve(e, today)
        existing = _find_existing(e)
        action = "create"
        if existing:
            cur = existing.get("current_status")
            if cur in ("applied", "recruiter_reply", "screen", "interview", "offer"):
                action = "skip (already live in funnel)"
            else:
                action = f"upgrade #{existing['id']} (was {cur})"
        plan.append({**e, "final": final, "reason": reason, "chain": chain,
                     "action": action, "existing_id": existing["id"] if existing else None})

    from collections import Counter
    print("\nFinal statuses:", dict(Counter(p["final"] for p in plan)))
    print("Rejection reasons:", dict(Counter(p["reason"] for p in plan if p["reason"])))
    print("Actions:", dict(Counter(p["action"].split(" ")[0] for p in plan)))
    print("Sources:", dict(Counter(p["source"] for p in plan)))
    print("Locations:", dict(Counter(p["location"] or "(none)" for p in plan)))
    print("\nLive-status cards after import (the ones you'll see in the funnel):")
    for p in plan:
        if p["final"] in ("applied", "screen", "interview", "offer") and not p["action"].startswith("skip"):
            print(f"  {p['final']:10} {p['wave_date']} {p['title'][:46]} @ {p['company'] or '?'}")

    if not args.apply:
        print("\nDry-run (no writes). Re-run with --apply to import.")
        return

    run_id = db.start_run(0, {"notion_wave_2026": True})
    created = upgraded = skipped = 0
    conn = db._conn()
    try:
        with conn:
            with conn.cursor() as cur:
                for p in plan:
                    if p["action"].startswith("skip"):
                        skipped += 1
                        continue
                    wave_ts = p["wave_date"]
                    if p["existing_id"]:
                        job_id = p["existing_id"]
                        upgraded += 1
                    else:
                        cur.execute(
                            """INSERT INTO jobs (run_id, url, apply_url, title, company, source,
                                                 description, ats_score, location, outcome,
                                                 current_status, logged_at)
                               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'qualified','found',%s)
                               RETURNING id""",
                            (run_id, p["url"], p["url"], p["title"], p["company"], p["source"],
                             (p["description"] or "")[:20000] or None, p["ats_score"],
                             p["location"], wave_ts),
                        )
                        job_id = cur.fetchone()["id"]
                        created += 1
                    prev = "found"
                    for stage in p["chain"]:
                        cur.execute(
                            "INSERT INTO status_log (job_id, old_status, new_status, source, changed_at) "
                            "VALUES (%s,%s,%s,'notion_import',%s)",
                            (job_id, prev, stage, wave_ts),
                        )
                        prev = stage
                    cur.execute(
                        """UPDATE jobs SET current_status = %s, rejection_reason = %s,
                               applied_at = CASE WHEN %s THEN %s::timestamp ELSE applied_at END
                           WHERE id = %s""",
                        (p["final"], p["reason"], "applied" in p["chain"], wave_ts, job_id),
                    )
                    if p["hebrew_flag"]:
                        cur.execute(
                            "INSERT INTO job_comments (job_id, body) VALUES (%s, %s)",
                            (job_id, "Notion: слили из-за иврита"),
                        )
        conn.commit()
    finally:
        conn.close()
    db.finish_run(run_id, {"qualified": created}, gpt_calls=0)
    print(f"\nDone: created {created}, upgraded {upgraded}, skipped {skipped} (run #{run_id})")


if __name__ == "__main__":
    main()
