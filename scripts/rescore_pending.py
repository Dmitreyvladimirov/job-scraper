"""One-off script: re-score every pending Notion card (Status2=Scraped, Статус=Активно)
with the fixed ats.py LOCATION rubric (TASK-026), and produce a ranked/filtered shortlist.

The 'Job description' Notion property is empty on cards created before that property was
wired up, so the full JD is read back from the page body's 'Полное описание вакансии'
toggle block instead.

Usage: python3 scripts/rescore_pending.py
Output: rescore_report.json (all results) + a printed summary/shortlist.
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
import ats  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}

RESUME_PATH = Path(__file__).parent.parent / "core" / "base_resume.md"
OUT_PATH = Path(__file__).parent.parent / "rescore_report.json"

# Worldwide/Israel = 15, EMEA = 8 both count as geographically eligible per REQ-003;
# US-only / LATAM / APAC / Russia = 0 is a hard reject.
LOCATION_PASS_THRESHOLD = 8
SCORE_PASS_THRESHOLD = 60  # matches config.ATS_THRESHOLD


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


def fetch_full_jd(page_id: str) -> str:
    """Full JD lives in the 'Полное описание вакансии' toggle block's paragraph children."""
    blocks = _request(f"https://api.notion.com/v1/blocks/{page_id}/children?page_size=100")
    for b in blocks.get("results", []):
        if b.get("type") == "toggle" and "Полное описание" in _rich_text_plain(b["toggle"].get("rich_text")):
            children = _request(f"https://api.notion.com/v1/blocks/{b['id']}/children?page_size=100")
            return "".join(
                _rich_text_plain(c["paragraph"].get("rich_text"))
                for c in children.get("results", [])
                if c.get("type") == "paragraph"
            )
    return ""


def main() -> None:
    resume_text = RESUME_PATH.read_text(encoding="utf-8")
    pages = fetch_pending_cards()
    logger.info(f"Found {len(pages)} pending cards (Status2=Scraped, Статус=Активно)")

    results = []
    for i, page in enumerate(pages, 1):
        page_id = page["id"]
        url = page.get("url", "")
        old_score = page.get("properties", {}).get("ATS Score", {}).get("number")
        title, company = extract_title_company(page)
        label = f"[{i}/{len(pages)}] {company or '?'} — {title[:50]}"

        try:
            description = fetch_full_jd(page_id)
        except Exception as e:
            logger.error(f"{label}: JD fetch failed: {e}")
            results.append({"url": url, "title": title, "company": company, "old_score": old_score, "error": f"jd_fetch_failed: {e}"})
            continue

        if not description:
            logger.warning(f"{label}: no JD text found, skipping")
            results.append({"url": url, "title": title, "company": company, "old_score": old_score, "error": "no_jd_text"})
            continue

        result = ats.analyze({"title": title, "company": company, "description": description}, resume_text)
        if result is None:
            logger.error(f"{label}: ATS analysis failed")
            results.append({"url": url, "title": title, "company": company, "old_score": old_score, "error": "ats_failed"})
            continue

        eligible = result.location_score >= LOCATION_PASS_THRESHOLD and result.score >= SCORE_PASS_THRESHOLD
        logger.info(
            f"{label}: score={result.score} (was {old_score}) location={result.location_score} "
            f"({result.location_reason}) → {'PASS' if eligible else 'reject'}"
        )
        results.append({
            "url": url, "title": title, "company": company,
            "old_score": old_score, "new_score": result.score,
            "location_score": result.location_score, "location_reason": result.location_reason,
            "eligible": eligible,
        })
        time.sleep(0.3)

    OUT_PATH.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"Wrote {OUT_PATH}")

    shortlist = sorted([r for r in results if r.get("eligible")], key=lambda r: -r["new_score"])
    rejected_location = [r for r in results if r.get("eligible") is False and r.get("location_score", 15) < LOCATION_PASS_THRESHOLD]
    rejected_score = [r for r in results if r.get("eligible") is False and r.get("location_score", 15) >= LOCATION_PASS_THRESHOLD]
    errors = [r for r in results if "error" in r]

    logger.info("\n=== SUMMARY ===")
    logger.info(f"Total processed: {len(results)}")
    logger.info(f"Shortlist (score>={SCORE_PASS_THRESHOLD} AND location>={LOCATION_PASS_THRESHOLD}): {len(shortlist)}")
    logger.info(f"Rejected — location: {len(rejected_location)}")
    logger.info(f"Rejected — score (location OK): {len(rejected_score)}")
    logger.info(f"Errors/skipped: {len(errors)}")


if __name__ == "__main__":
    main()
