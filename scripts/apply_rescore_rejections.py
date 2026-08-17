"""One-off script: apply the geo-rejection verdicts from rescore_pending.py's output
(rescore_final.json -> still_rejected) back to Notion — moves them out of Статус=Активно
so the pending-review table only shows genuinely eligible cards.

For each rejected card:
  - Статус -> "🚫 Отклонено"
  - Status2 -> "rejected_by_scraper" (matches the existing scraper-rejection convention)
  - Appends a "🌍" callout documenting the location_reason that triggered the reject.

Usage: python3 scripts/apply_rescore_rejections.py
"""
import json
import logging
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "core"))
from config import NOTION_TOKEN  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}

REPORT_PATH = Path(__file__).parent.parent / "rescore_final.json"
PAGE_ID_RE = re.compile(r"([0-9a-f]{32})$")


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


def page_id_from_url(url: str) -> str | None:
    m = PAGE_ID_RE.search(url.replace("-", ""))
    if not m:
        return None
    h = m.group(1)
    return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


def update_status(page_id: str) -> None:
    _request(
        f"https://api.notion.com/v1/pages/{page_id}",
        {"properties": {
            "Статус": {"select": {"name": "🚫 Отклонено"}},
            "Status2": {"select": {"name": "rejected_by_scraper"}},
        }},
        method="PATCH",
    )


def append_reason_callout(page_id: str, reason: str) -> None:
    _request(
        f"https://api.notion.com/v1/blocks/{page_id}/children",
        {"children": [{
            "object": "block", "type": "callout",
            "callout": {
                "rich_text": [{"type": "text", "text": {
                    "content": f"🌍 Отклонено при пересчёте (TASK-026 re-score): {reason}"
                }}],
                "icon": {"emoji": "🌍"},
            },
        }]},
        method="PATCH",
    )


def main() -> None:
    report = json.loads(REPORT_PATH.read_text(encoding="utf-8"))
    rejected = report["still_rejected"]
    logger.info(f"Applying rejection to {len(rejected)} cards")

    updated = failed = skipped = 0
    for i, r in enumerate(rejected, 1):
        label = f"[{i}/{len(rejected)}] {r.get('company') or '?'} — {r.get('title','')[:50]}"
        page_id = page_id_from_url(r["url"])
        if not page_id:
            logger.error(f"{label}: could not parse page_id from {r['url']}")
            skipped += 1
            continue
        try:
            update_status(page_id)
            append_reason_callout(page_id, r.get("location_reason", ""))
            logger.info(f"{label}: OK")
            updated += 1
        except Exception as e:
            logger.error(f"{label}: FAILED — {e}")
            failed += 1
        time.sleep(0.3)

    logger.info(f"\nDone — updated: {updated} | failed: {failed} | skipped: {skipped}")


if __name__ == "__main__":
    main()
