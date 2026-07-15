"""One-off script: find Notion entries with RemoteOK/Arbeitnow URLs and replace with direct apply URLs."""
import logging
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent.parent / "core"))
from config import NOTION_TOKEN, NOTION_DATABASE_ID
from utils import find_apply_url

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}
PLATFORM_HOSTS = ("remoteok.com", "arbeitnow.com", "weworkremotely.com")


def _request(url: str, payload: dict, method: str = "POST") -> dict:
    r = requests.request(method, url, json=payload, headers=HEADERS, timeout=60)
    r.raise_for_status()
    return r.json()


def fetch_platform_entries() -> list[dict]:
    """Return all Notion pages whose job URL contains a platform host."""
    results, cursor = [], None
    while True:
        payload: dict = {"page_size": 100}
        if cursor:
            payload["start_cursor"] = cursor
        data = _request(
            f"https://api.notion.com/v1/databases/{NOTION_DATABASE_ID}/query", payload
        )
        for page in data.get("results", []):
            url = (
                page.get("properties", {})
                .get("Ссылка на вакансию", {})
                .get("url") or ""
            )
            if any(h in url.lower() for h in PLATFORM_HOSTS):
                props = page.get("properties", {})
                title_parts = props.get("Позиция", {}).get("title") or []
                title = title_parts[0]["text"]["content"] if title_parts else ""
                company_parts = props.get("Компания", {}).get("rich_text") or []
                company = company_parts[0]["text"]["content"] if company_parts else ""
                results.append(
                    {"page_id": page["id"], "title": title, "company": company, "url": url}
                )
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return results


def update_url(page_id: str, new_url: str) -> None:
    _request(
        f"https://api.notion.com/v1/pages/{page_id}",
        {"properties": {"Ссылка на вакансию": {"url": new_url}}},
        method="PATCH",
    )


def main() -> None:
    entries = fetch_platform_entries()
    logger.info(f"Found {len(entries)} entries with platform URLs")

    updated = skipped = failed = 0
    for e in entries:
        company = e["company"]
        raw_title = e["title"]
        # If company field is empty, extract it from "Position (Company)" title format
        if not company and "(" in raw_title and raw_title.endswith(")"):
            company = raw_title[raw_title.rfind("(") + 1:-1]
            title = raw_title[:raw_title.rfind("(")].strip()
        else:
            title = raw_title.replace(f" ({company})", "").strip() if company else raw_title

        direct = find_apply_url(company, title)
        if not direct:
            logger.info(f"  ❌ {company} — {title[:50]}")
            skipped += 1
            continue

        try:
            update_url(e["page_id"], direct)
            logger.info(f"  ✅ {company} → {direct}")
            updated += 1
        except Exception as ex:
            logger.error(f"  ⚠️  {company} update failed: {ex}")
            failed += 1

    logger.info(f"\nDone — updated: {updated} | not found: {skipped} | errors: {failed}")


if __name__ == "__main__":
    main()
