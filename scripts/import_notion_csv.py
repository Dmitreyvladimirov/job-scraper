"""Import historical scraper jobs from Notion CSV export into Postgres.

Usage:
    DATABASE_URL=<DATABASE_PUBLIC_URL> python3 import_notion_csv.py

The script imports jobs with Status2 in:
  Scraped / found_by_scraper   → outcome='qualified'
  rejected_by_scraper          → outcome='low_score'
"""
import csv
import os
import re
import sys

import psycopg2
import psycopg2.extras

CSV_PATH = os.environ.get(
    "CSV_PATH",
    "/Users/DimaKu/Downloads/ExportBlock-da0137af-b757-4475-8cb4-564620440458-Part-1/"
    "Job Hunt tracker Template f71f92e0c9764cf2bb568063b5cea681.csv",
)
DATABASE_URL = os.environ.get("DATABASE_URL", "")
if not DATABASE_URL:
    raise SystemExit("ERROR: DATABASE_URL is not set")

OUTCOME_MAP = {
    "scraped":             "qualified",
    "found_by_scraper":    "qualified",
    "rejected_by_scraper": "low_score",
}

_NOTION_LINK = re.compile(r"\s*\(https?://[^)]+\)")


def clean_company(raw: str) -> str:
    """Strip Notion page link from company name: 'Google (https://...)' → 'Google'."""
    return _NOTION_LINK.sub("", raw).strip()


def extract_from_title(raw: str) -> tuple[str, str]:
    """Return (clean_title, company). Company extracted from trailing '(Name)' if Компания is empty."""
    raw = raw.strip()
    if raw.endswith(")") and "(" in raw:
        last_open = raw.rfind("(")
        inner = raw[last_open + 1:-1].strip()
        # Looks like a company name: short, no URL, not a German gender marker
        if inner and len(inner) < 60 and not inner.startswith("http") and "m/w/d" not in inner.lower():
            return raw[:last_open].strip(), inner
    return raw, ""


def main():
    with open(CSV_PATH, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    print(f"CSV: {len(rows)} rows total")

    scraper_rows = [
        r for r in rows
        if r.get("Status2", "").strip().lower() in OUTCOME_MAP
    ]
    print(f"Scraper rows to import: {len(scraper_rows)}")

    pg = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)

    # Create a single historical run for all imported records
    with pg.cursor() as cur:
        cur.execute("""
            INSERT INTO runs (started_at, finished_at, total_fetched, qualified,
                rejected_low_score, sources_json)
            VALUES ('2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00',
                %s, %s, %s, '{"notion_import": true}')
            RETURNING id
        """, (
            len(scraper_rows),
            sum(1 for r in scraper_rows if OUTCOME_MAP[r["Status2"].strip().lower()] == "qualified"),
            sum(1 for r in scraper_rows if OUTCOME_MAP[r["Status2"].strip().lower()] == "low_score"),
        ))
        run_id = cur.fetchone()["id"]
    pg.commit()
    print(f"Created historical run #{run_id}")

    imported = skipped = 0
    for row in scraper_rows:
        status = row.get("Status2", "").strip().lower()
        outcome = OUTCOME_MAP[status]
        url = row.get("Ссылка на вакансию", "").strip()
        company = clean_company(row.get("Компания", "").strip())
        title, company_from_title = extract_from_title(row.get("Позиция", "").strip())
        if not company:
            company = company_from_title
        ats_raw = row.get("ATS Score", "").strip()
        ats_score = int(float(ats_raw)) if ats_raw else None

        if not url and not title:
            skipped += 1
            continue

        with pg.cursor() as cur:
            cur.execute("""
                INSERT INTO jobs (run_id, url, title, company, outcome, ats_score)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (run_id, url or None, title or None, company or None, outcome, ats_score))
        pg.commit()
        imported += 1

    print(f"Imported: {imported}, skipped: {skipped}")

    # Summary
    with pg.cursor() as cur:
        cur.execute("SELECT COUNT(*) as c FROM runs"); print(f"Postgres runs: {cur.fetchone()['c']}")
        cur.execute("SELECT COUNT(*) as c FROM jobs"); print(f"Postgres jobs: {cur.fetchone()['c']}")
        cur.execute("SELECT COUNT(*) as c FROM jobs WHERE ats_score IS NOT NULL")
        print(f"Jobs with ATS score: {cur.fetchone()['c']}")

    pg.close()
    print("Done!")


if __name__ == "__main__":
    main()
