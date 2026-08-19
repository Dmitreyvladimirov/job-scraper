"""Seed / verify / re-check the target_companies table (company_direct source).

Data lives in scripts/target_companies_seed.json (git-reviewed); this script is
an idempotent upsert with a two-step network verification of every slug guess:
 step 1 — does the board resolve on that ATS;
 step 2 — does the board's company name match ours (utils._company_match; Lever
          and Ashby expose no name in their APIs, so the board page <title> is
          used, which is empty for client-rendered Ashby boards — those come
          back as 'resolved_name_unknown' and need an eyeball, not automation).

Usage:
  python scripts/seed_target_companies.py             # dry-run report, no writes
  python scripts/seed_target_companies.py --verify    # + network verification
  python scripts/seed_target_companies.py --verify --apply
                                                      # write: verdict ok + layer 1 -> active,
                                                      # everything else -> pending
  python scripts/seed_target_companies.py --recheck   # re-verify existing DB rows (manual sweep)

ON CONFLICT (ats, slug) DO NOTHING — re-running the seed never overwrites a
human decision (a manual 'dead', an edited name).
"""
import argparse
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "core"))

import ats_boards  # noqa: E402
import db  # noqa: E402
from utils import _company_match, _page_title  # noqa: E402

SEED_PATH = Path(__file__).parent / "target_companies_seed.json"
DELAY_SEC = 0.3


def board_company_name(ats: str, slug: str) -> str:
    """Company name as the board itself reports it; '' when unavailable."""
    try:
        if ats == "greenhouse":
            status, data = ats_boards._get_json(
                f"https://boards-api.greenhouse.io/v1/boards/{slug}", timeout=8)
            if status == 200 and isinstance(data, dict):
                return data.get("name") or ""
            return ""
        if ats == "lever":
            return _page_title(f"https://jobs.lever.co/{slug}")
        if ats == "ashby":
            title = _page_title(f"https://jobs.ashbyhq.com/{slug}")
            return re.sub(r"\s+Jobs$", "", title, flags=re.I).strip()
    except Exception:
        pass
    return ""


def verify(entry: dict) -> tuple[str, str]:
    """Returns (verdict, detail). Verdicts: ok | resolved_name_unknown |
    name_mismatch | not_found | error."""
    ats, slug, name = entry["ats"], entry["slug"], entry["name"]
    try:
        postings = ats_boards.LISTERS[ats](slug, timeout=8)
    except ats_boards.AtsBoardNotFound:
        return "not_found", ""
    except Exception as e:
        return "error", str(e)[:120]
    board_name = board_company_name(ats, slug)
    if not board_name:
        return "resolved_name_unknown", f"{len(postings)} postings, board name unavailable"
    if _company_match(name, board_name):
        return "ok", f"{len(postings)} postings, board name '{board_name}'"
    return "name_mismatch", f"board name '{board_name}' != '{name}'"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--recheck", action="store_true")
    args = ap.parse_args()

    if args.recheck:
        conn = db._conn()
        with conn.cursor() as cur:
            cur.execute("SELECT name, ats, slug, status FROM target_companies ORDER BY id")
            rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        for r in rows:
            verdict, detail = verify(r)
            print(f"{verdict:24} {r['ats']:10} {r['slug']:24} {r['name']} ({r['status']}) {detail}")
            time.sleep(DELAY_SEC)
        return

    entries = json.loads(SEED_PATH.read_text())
    verdicts = {}
    for e in entries:
        if args.verify:
            verdict, detail = verify(e)
            time.sleep(DELAY_SEC)
        else:
            verdict, detail = "unverified", ""
        verdicts[(e["ats"], e["slug"])] = verdict
        print(f"{verdict:24} L{e['layer']} {e['region']:6} {e['ats']:10} {e['slug']:24} {e['name']} {detail}")

    summary = {}
    for v in verdicts.values():
        summary[v] = summary.get(v, 0) + 1
    print(f"\nSummary: {summary}")

    if not args.apply:
        print("\nDry-run (no writes). Use --verify --apply to seed.")
        return

    conn = db._conn()
    inserted = 0
    with conn:
        with conn.cursor() as cur:
            for e in entries:
                verdict = verdicts[(e["ats"], e["slug"])]
                status = "active" if (verdict == "ok" and e["layer"] == 1) else "pending"
                notes = e.get("notes", "")
                if verdict != "unverified":
                    notes = f"{notes} | verify {time.strftime('%Y-%m-%d')}: {verdict}".strip(" |")
                cur.execute(
                    """INSERT INTO target_companies
                           (name, ats, slug, status, layer, region, provenance, notes, verified_at)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s, CASE WHEN %s = 'ok' THEN NOW() END)
                       ON CONFLICT (ats, slug) DO NOTHING""",
                    (e["name"], e["ats"], e["slug"], status, e["layer"],
                     e["region"], e["provenance"], notes, verdict),
                )
                inserted += cur.rowcount
    conn.close()
    print(f"Inserted {inserted} new rows (existing rows untouched).")


if __name__ == "__main__":
    main()
