import json
import logging
import urllib.request
from datetime import date, timedelta
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


def _query_all(filter_payload: dict | None = None) -> list[dict]:
    """Paginate through entire DB query, return all result pages."""
    results = []
    cursor = None
    while True:
        payload: dict = {"page_size": 100}
        if filter_payload:
            payload["filter"] = filter_payload
        if cursor:
            payload["start_cursor"] = cursor
        try:
            data = _post(f"https://api.notion.com/v1/databases/{NOTION_DATABASE_ID}/query", payload)
        except Exception as e:
            logger.error(f"Notion query failed: {e}")
            break
        results.extend(data.get("results", []))
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return results


def load_seen_urls() -> set[str]:
    """One bulk paginated request — returns all job URLs in Notion (any status)."""
    pages = _query_all()
    seen = set()
    for p in pages:
        url = p.get("properties", {}).get("Ссылка на вакансию", {}).get("url") or ""
        if url:
            seen.add(url)
    logger.info(f"Notion: loaded {len(seen)} known URLs")
    return seen


def load_company_applications(cooldown_days: int) -> dict[str, dict]:
    """
    Returns {normalized_company: {company, position, date, days_ago}}
    for entries with Status2='Applied' within the cooldown window.
    Used to warn about repeat applications to the same company.
    """
    cutoff = (date.today() - timedelta(days=cooldown_days)).isoformat()
    pages = _query_all({
        "and": [
            {"property": "Status2", "select": {"equals": "Applied"}},
            {"property": "Date Applied", "date": {"on_or_after": cutoff}},
        ]
    })

    history: dict[str, dict] = {}
    for p in pages:
        props = p.get("properties", {})

        # Company: from Компания field, fall back to parsing title "(Company)"
        company = ""
        rt = props.get("Компания", {}).get("rich_text") or []
        if rt:
            company = rt[0].get("text", {}).get("content", "")
        if not company:
            title_parts = props.get("Позиция", {}).get("title") or []
            title = title_parts[0].get("text", {}).get("content", "") if title_parts else ""
            if "(" in title and title.endswith(")"):
                company = title[title.rfind("(") + 1:-1]
        if not company:
            continue

        # Date
        date_str = (props.get("Date Applied", {}).get("date") or {}).get("start", "")
        if not date_str:
            continue
        applied_date = date.fromisoformat(date_str)
        days_ago = (date.today() - applied_date).days

        # Position title
        title_parts = props.get("Позиция", {}).get("title") or []
        position = title_parts[0].get("text", {}).get("content", "") if title_parts else ""

        key = company.lower().strip()
        # Keep only the most recent application per company
        if key not in history or days_ago < history[key]["days_ago"]:
            history[key] = {
                "company": company,
                "position": position,
                "date": date_str,
                "days_ago": days_ago,
            }

    logger.info(f"Notion: found {len(history)} companies applied to in last {cooldown_days} days")
    return history


def _make_properties(job: dict, status2: str, status: str) -> dict:
    today = date.today().isoformat()
    company = job.get("company", "")
    title = f"{job['title']} ({company})" if company else job["title"]
    props = {
        "Позиция": {"title": [{"text": {"content": title[:255]}}]},
        "Ссылка на вакансию": {"url": job["url"]},
        "Status2": {"select": {"name": status2}},
        "Статус": {"select": {"name": status}},
        "Date Applied": {"date": {"start": today}},
        "Подался сам": {"checkbox": False},
    }
    if company:
        props["Компания"] = {"rich_text": [{"text": {"content": company[:255]}}]}
    return props


def create_entry(job: dict, score: int) -> None:
    """Write a qualified vacancy to Notion with full JD as page body."""
    props = _make_properties(job, "found_by_scraper", "🤖 Найдено")
    company = job.get("company", "")
    title = f"{job['title']} ({company})" if company else job["title"]

    children = [{
        "object": "block", "type": "callout",
        "callout": {
            "rich_text": [{"type": "text", "text": {"content":
                f"ATS Score: {score}/100 | Source: {job['source']} | {job.get('salary') or 'Salary not listed'}"
            }}],
            "icon": {"emoji": "⭐"},
        },
    }]
    description = job.get("description", "")
    for i in range(0, min(len(description), 10000), 2000):
        children.append({
            "object": "block", "type": "paragraph",
            "paragraph": {"rich_text": [{"type": "text", "text": {"content": description[i:i + 2000]}}]},
        })

    try:
        _post("https://api.notion.com/v1/pages",
              {"parent": {"database_id": NOTION_DATABASE_ID}, "properties": props, "children": children})
        logger.info(f"Notion: ✅ '{title}' (score {score})")
    except Exception as e:
        logger.error(f"Notion: failed to create '{title}': {e}")


def create_rejected_entry(job: dict, score: int) -> None:
    """Write a rejected vacancy — minimal entry (no JD), used only for dedup on future runs."""
    props = _make_properties(job, "rejected_by_scraper", "🚫 Отклонено")
    company = job.get("company", "")
    title = f"{job['title']} ({company})" if company else job["title"]
    try:
        _post("https://api.notion.com/v1/pages",
              {"parent": {"database_id": NOTION_DATABASE_ID}, "properties": props})
        logger.info(f"Notion: 🚫 logged rejected '{title}' (score {score})")
    except Exception as e:
        logger.error(f"Notion: failed to log rejected '{title}': {e}")
