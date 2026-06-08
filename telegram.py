import json
import logging
import urllib.request
from config import TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
from notion_client import NOTION_DB_URL

logger = logging.getLogger(__name__)

_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"


def _send(text: str) -> None:
    payload = json.dumps({
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }).encode("utf-8")
    req = urllib.request.Request(f"{_API}/sendMessage", data=payload)
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=10) as resp:
        resp.read()


def send_run_summary(counts: dict, top_jobs: list[dict], source_counts: dict | None = None) -> None:
    """One message per scraper run — summary only, no per-vacancy spam."""
    qualified = counts["qualified"]
    total = sum(counts.values())
    deduped = counts["dedup"]
    low_score = counts["score"]

    sources_line = ""
    if source_counts:
        sources_line = "📡 " + " | ".join(f"{n}: {c}" for n, c in source_counts.items()) + "\n"

    if qualified == 0:
        text = (
            f"🤖 Прогон завершён\n"
            f"{sources_line}"
            f"Новых вакансий не найдено "
            f"(всего: {total}, дубликаты: {deduped}, низкий скор: {low_score})\n"
            f"[Открыть Notion]({NOTION_DB_URL})"
        )
    else:
        top_lines = ""
        for j in top_jobs[:3]:
            top_lines += f"• {j['title']} @ {j['company']} — {j['score']}/100\n"

        text = (
            f"🤖 *Прогон завершён*\n\n"
            f"{sources_line}"
            f"✅ Новых вакансий: *{qualified}*\n"
            f"📊 Всего проверено: {total} | Дубликаты: {deduped} | Низкий скор: {low_score}\n\n"
            f"{top_lines}"
            f"\n[Открыть Notion]({NOTION_DB_URL})"
        )

    try:
        _send(text)
        logger.info(f"Telegram: summary sent ({qualified} qualified)")
    except Exception as e:
        logger.error(f"Telegram: summary failed: {e}")


def send_error(message: str) -> None:
    """Send a critical error alert (e.g. auth failure)."""
    try:
        _send(f"⚠️ *Job Scraper error*\n\n{message}")
    except Exception as e:
        logger.error(f"Telegram: error alert failed: {e}")
