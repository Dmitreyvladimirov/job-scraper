import json
import logging
import urllib.request
from datetime import date, timedelta
from config import NOTION_TOKEN, NOTION_DATABASE_ID
from utils import normalize_job_key

logger = logging.getLogger(__name__)

_HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}

NOTION_DB_URL = f"https://www.notion.so/{NOTION_DATABASE_ID.replace('-', '')}"


def _post(url: str, payload: dict, method: str = "POST") -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    for k, v in _HEADERS.items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _query_all(filter_payload: dict | None = None) -> list[dict]:
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


def load_seen_urls() -> tuple[set[str], set[tuple[str, str]]]:
    pages = _query_all()
    seen_urls: set[str] = set()
    seen_keys: set[tuple[str, str]] = set()
    for p in pages:
        props = p.get("properties", {})

        url = props.get("Ссылка на вакансию", {}).get("url") or ""
        if url:
            seen_urls.add(url)

        comp_parts = props.get("Компания", {}).get("rich_text", [])
        company = comp_parts[0].get("plain_text", "") if comp_parts else ""

        title_parts = props.get("Позиция", {}).get("title", [])
        raw_title = title_parts[0].get("plain_text", "") if title_parts else ""

        # Fallback: extract company from "Position (Company)" title format
        if not company and "(" in raw_title and raw_title.endswith(")"):
            company = raw_title[raw_title.rfind("(") + 1:-1]
            raw_title = raw_title[:raw_title.rfind("(")].strip()

        if company and raw_title:
            seen_keys.add(normalize_job_key(company, raw_title))

    logger.info(f"Notion: loaded {len(seen_urls)} known URLs, {len(seen_keys)} known job keys")
    return seen_urls, seen_keys


def load_company_applications(cooldown_days: int) -> dict[str, dict]:
    cutoff = (date.today() - timedelta(days=cooldown_days)).isoformat()
    pages = _query_all({
        "and": [
            {"property": "Status2", "select": {"equals": "applied"}},
            {"property": "Date Applied", "date": {"on_or_after": cutoff}},
        ]
    })
    history: dict[str, dict] = {}
    for p in pages:
        props = p.get("properties", {})
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
        date_str = (props.get("Date Applied", {}).get("date") or {}).get("start", "")
        if not date_str:
            continue
        applied_date = date.fromisoformat(date_str)
        days_ago = (date.today() - applied_date).days
        title_parts = props.get("Позиция", {}).get("title") or []
        position = title_parts[0].get("text", {}).get("content", "") if title_parts else ""
        key = company.lower().strip()
        if key not in history or days_ago < history[key]["days_ago"]:
            history[key] = {"company": company, "position": position,
                            "date": date_str, "days_ago": days_ago}
    logger.info(f"Notion: {len(history)} companies applied to in last {cooldown_days}d")
    return history


def _make_properties(job: dict, status2: str, status: str, score: int | None = None) -> dict:
    today = date.today().isoformat()
    company = job.get("company", "")
    title = f"{job['title']} ({company})" if company else job["title"]
    props = {
        "Позиция": {"title": [{"text": {"content": title[:255]}}]},
        "Ссылка на вакансию": {"url": job.get("apply_url") or job["url"]},
        "Status2": {"select": {"name": status2}},
        "Статус": {"select": {"name": status}},
        "Date Applied": {"date": {"start": today}},
        "Подался сам": {"checkbox": False},
    }
    if company:
        props["Компания"] = {"rich_text": [{"text": {"content": company[:255]}}]}
    if score is not None:
        props["ATS Score"] = {"number": score}
    return props


def _text_block(content: str) -> dict:
    return {
        "object": "block", "type": "paragraph",
        "paragraph": {"rich_text": [{"type": "text", "text": {"content": content}}]},
    }


def _callout(content: str, emoji: str) -> dict:
    return {
        "object": "block", "type": "callout",
        "callout": {
            "rich_text": [{"type": "text", "text": {"content": content}}],
            "icon": {"emoji": emoji},
        },
    }


def create_entry(job: dict, result, cooldown_match: dict | None = None, doc_url: str | None = None) -> bool:
    """Write a qualified vacancy with full analysis in page body. Returns True on success."""
    from ats import ATSResult
    props = _make_properties(job, "Scraped", "Активно", score=result.score)
    company = job.get("company", "")
    title = f"{job['title']} ({company})" if company else job["title"]

    matched_str = ", ".join(result.matched) if result.matched else "—"
    missed_str = ", ".join(result.missed) if result.missed else "—"
    salary = job.get("salary") or "не указана"

    penalty_str = f"  |  Penalty: −{result.penalty}" if result.penalty else ""
    children = [
        _callout(
            f"ATS Score: {result.score}/100  |  {result.domain or '—'}  |  Зарплата: {salary}  |  {job['source']}{penalty_str}\n"
            f"Role: {result.role_score}/30  |  Domain: {result.domain_score}/30 (Value: {result.domain_value_score}/15 · Exp: {result.domain_exp_score}/15)  |  Keywords: {result.keyword_score}/25  |  Location: {result.location_score}/15",
            "⭐"
        ),
        _text_block(f"✅ Стоит рассмотреть: {result.why_apply}") if result.why_apply else _text_block(""),
        _text_block(f"❌ Риски: {result.why_not}") if result.why_not else _text_block(""),
        _text_block(f"🔑 Совпало: {matched_str}"),
        _text_block(f"🎯 Не хватает: {missed_str}"),
    ]

    if job.get("incomplete_description"):
        children.append(_callout(
            "Описание взято из RemoteOK (краткое AI-саммари) — прямая ссылка не найдена. "
            "Оценка ATS может быть неточной. Проверь вакансию вручную перед откликом.",
            "⚠️"
        ))

    if cooldown_match:
        children.append(_callout(
            f"⚠️ Cooldown: уже подавался в {cooldown_match['company']} "
            f"{cooldown_match['days_ago']} дней назад — {cooldown_match['position']}",
            "⚠️"
        ))

    if doc_url:
        children.append({
            "object": "block", "type": "callout",
            "callout": {
                "rich_text": [{"type": "text", "text": {
                    "content": "📄 Черновик резюме в Google Docs → ",
                }, "annotations": {}}, {"type": "text", "text": {
                    "content": doc_url, "link": {"url": doc_url},
                }}],
                "icon": {"emoji": "📄"},
            },
        })

    # Full JD in a toggle
    description = job.get("description", "")
    if description:
        jd_children = []
        for i in range(0, min(len(description), 10000), 2000):
            jd_children.append(_text_block(description[i:i + 2000]))
        children.append({
            "object": "block", "type": "toggle",
            "toggle": {
                "rich_text": [{"type": "text", "text": {"content": "Полное описание вакансии"}}],
                "children": jd_children,
            },
        })

    try:
        _post("https://api.notion.com/v1/pages",
              {"parent": {"database_id": NOTION_DATABASE_ID},
               "properties": props, "children": children})
        logger.info(f"Notion: ✅ '{title}' score={result.score}")
        return True
    except Exception as e:
        logger.error(f"Notion: failed '{title}': {e}")
        return False


def create_rejected_entry(job: dict, score: int) -> bool:
    """Minimal entry for dedup — no JD, just enough to prevent reprocessing. Returns True on success."""
    props = _make_properties(job, "rejected_by_scraper", "🚫 Отклонено", score=score)
    company = job.get("company", "")
    title = f"{job['title']} ({company})" if company else job["title"]
    try:
        _post("https://api.notion.com/v1/pages",
              {"parent": {"database_id": NOTION_DATABASE_ID}, "properties": props})
        logger.info(f"Notion: 🚫 rejected '{title}' score={score}")
        return True
    except Exception as e:
        logger.error(f"Notion: failed rejected '{title}': {e}")
        return False
