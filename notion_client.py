import json
import logging
import urllib.request
from datetime import date
from config import NOTION_TOKEN, NOTION_DATABASE_ID

logger = logging.getLogger(__name__)

_HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}


def _post(url: str, payload: dict, method: str = "POST") -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    for k, v in _HEADERS.items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def load_seen_urls() -> set[str]:
    """One bulk request (paginated) at run start — returns all job URLs already in Notion."""
    seen: set[str] = set()
    cursor = None
    pages_fetched = 0

    while True:
        payload: dict = {"page_size": 100}
        if cursor:
            payload["start_cursor"] = cursor

        try:
            result = _post(
                f"https://api.notion.com/v1/databases/{NOTION_DATABASE_ID}/query",
                payload,
            )
        except Exception as e:
            logger.error(f"Notion bulk fetch failed (page {pages_fetched}): {e}")
            break

        for page_obj in result.get("results", []):
            url = (
                page_obj.get("properties", {})
                .get("Ссылка на вакансию", {})
                .get("url") or ""
            )
            if url:
                seen.add(url)

        pages_fetched += 1
        if not result.get("has_more"):
            break
        cursor = result.get("next_cursor")

    logger.info(f"Notion: loaded {len(seen)} known URLs ({pages_fetched} page(s))")
    return seen


def create_entry(job: dict, score: int) -> str | None:
    today = date.today().isoformat()
    company = job.get("company", "")
    title = f"{job['title']} ({company})" if company else job["title"]

    properties = {
        "Позиция": {"title": [{"text": {"content": title[:255]}}]},
        "Ссылка на вакансию": {"url": job["url"]},
        "Status2": {"select": {"name": "found_by_scraper"}},
        "Статус": {"select": {"name": "🤖 Найдено"}},
        "Date Applied": {"date": {"start": today}},
        "Подался сам": {"checkbox": False},
    }

    # Build page body: ATS score callout + full job description
    children = [
        {
            "object": "block",
            "type": "callout",
            "callout": {
                "rich_text": [{"type": "text", "text": {
                    "content": f"ATS Score: {score}/100 | Source: {job['source']} | {job.get('salary') or 'Salary not listed'}"
                }}],
                "icon": {"emoji": "⭐"},
            },
        }
    ]
    description = job.get("description", "")
    for i in range(0, min(len(description), 10000), 2000):
        children.append({
            "object": "block",
            "type": "paragraph",
            "paragraph": {
                "rich_text": [{"type": "text", "text": {"content": description[i:i + 2000]}}]
            },
        })

    try:
        resp = _post(
            "https://api.notion.com/v1/pages",
            {"parent": {"database_id": NOTION_DATABASE_ID}, "properties": properties, "children": children},
        )
        page_url = resp.get("url", "")
        logger.info(f"Notion: created '{title}' (score {score})")
        return page_url
    except Exception as e:
        logger.error(f"Notion: failed to create '{title}': {e}")
        return None
