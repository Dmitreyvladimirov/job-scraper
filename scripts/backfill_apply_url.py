"""One-off script: backfill direct apply URLs on already-created Notion cards.

TASK-001/TASK-027 (core/utils.py::enrich_url) only runs at scrape time for new jobs —
cards created before that fix shipped still have "Ссылка на вакансию" pointing at the
aggregator page (Jobgether/Jobicy/remoteworldwide.net) instead of the real ATS link.

This re-runs the same enrich_url() logic against every card currently in
Status2=Scraped / Статус=Активно (the pending-review set) and updates the property
in place wherever a direct link is found. Cards already triaged (applied/rejected)
are left untouched — no reason to touch history.

Usage: python3 scripts/backfill_apply_url.py
"""
import json
import logging
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "core"))
from config import NOTION_TOKEN, NOTION_DATABASE_ID  # noqa: E402
from utils import enrich_url  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}

OUT_PATH = Path(__file__).parent.parent / "backfill_report.json"


def _request(url: str, payload: dict | None = None, method: str = "GET") -> dict:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    for k, v in HEADERS.items():
        req.add_header(k, v)
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 2:
                time.sleep(2 * (attempt + 1))
                continue
            raise


def fetch_pending_cards() -> list[dict]:
    results, cursor = [], None
    filter_payload = {
        "and": [
            {"property": "Status2", "select": {"equals": "Scraped"}},
            {"property": "Статус", "select": {"equals": "Активно"}},
        ]
    }
    while True:
        payload = {"page_size": 100, "filter": filter_payload}
        if cursor:
            payload["start_cursor"] = cursor
        data = _request(f"https://api.notion.com/v1/databases/{NOTION_DATABASE_ID}/query", payload, "POST")
        results.extend(data.get("results", []))
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return results


def _rich_text_plain(rt_list) -> str:
    return "".join(t.get("plain_text", "") for t in rt_list or [])


def extract_title_company(page: dict) -> tuple[str, str]:
    props = page.get("properties", {})
    raw_title = _rich_text_plain(props.get("Позиция", {}).get("title"))
    company = _rich_text_plain(props.get("Компания", {}).get("rich_text"))
    if not company and "(" in raw_title and raw_title.endswith(")"):
        company = raw_title[raw_title.rfind("(") + 1:-1]
        raw_title = raw_title[:raw_title.rfind("(")].strip()
    return raw_title, company


def update_job_url(page_id: str, new_url: str) -> None:
    _request(
        f"https://api.notion.com/v1/pages/{page_id}",
        {"properties": {"Ссылка на вакансию": {"url": new_url}}},
        method="PATCH",
    )


def main() -> None:
    pages = fetch_pending_cards()
    logger.info(f"Found {len(pages)} pending cards (Status2=Scraped, Статус=Активно)")

    results = []
    updated = unchanged = failed = 0
    for i, page in enumerate(pages, 1):
        page_id = page["id"]
        current_url = page.get("properties", {}).get("Ссылка на вакансию", {}).get("url") or ""
        title, company = extract_title_company(page)
        label = f"[{i}/{len(pages)}] {company or '?'} — {title[:50]}"

        try:
            job = {"url": current_url, "title": title, "company": company}
            enrich_url(job)
            direct = job.get("apply_url")
        except Exception as e:
            logger.error(f"{label}: enrich_url failed: {e}")
            results.append({"page_id": page_id, "company": company, "title": title, "old_url": current_url, "error": str(e)})
            failed += 1
            continue

        if direct and direct != current_url:
            try:
                update_job_url(page_id, direct)
                logger.info(f"{label}: {current_url} -> {direct}")
                results.append({"page_id": page_id, "company": company, "title": title, "old_url": current_url, "new_url": direct})
                updated += 1
            except Exception as e:
                logger.error(f"{label}: Notion update failed: {e}")
                results.append({"page_id": page_id, "company": company, "title": title, "old_url": current_url, "error": str(e)})
                failed += 1
        else:
            unchanged += 1
        time.sleep(0.2)

    OUT_PATH.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"\nDone — updated: {updated} | unchanged: {unchanged} | failed: {failed}")
    logger.info(f"Wrote {OUT_PATH}")


if __name__ == "__main__":
    main()
