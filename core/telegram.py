import json
import logging
import os
import urllib.request
from config import TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
from notion_client import NOTION_DB_URL

_DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "")

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


def send_autogen_summary(generated: list[dict]) -> None:
    """Own message for the end-of-run resume batch (Release 2) — deliberately not
    part of send_run_summary: that one early-returns on quiet runs, while a paid
    generation from backlog cards must always be visible."""
    lines = "\n".join(
        f"• {g['title']} @ {g['company']} — {g['score']}/100"
        + (f" ({g['flags']} ⚑)" if g.get("flags") else "")
        for g in generated
    )
    dashboard_link = f"\n[Дашборд]({_DASHBOARD_URL})" if _DASHBOARD_URL else ""
    _send(f"📄 Автогенерация резюме: *{len(generated)}*\n{lines}{dashboard_link}")


def send_run_summary(counts: dict, top_jobs: list[dict], source_counts: dict | None = None) -> None:
    """One message per scraper run — summary only, no per-vacancy spam."""
    qualified = counts["qualified"]
    ats_errors = counts.get("ats_error", 0)
    # A run where scoring broke for every vacancy also has 0 qualified — staying silent
    # there hides exactly the failure worth reporting, so ats_errors overrides the
    # "nothing to say" shortcut.
    if qualified == 0 and ats_errors == 0:
        logger.info("Telegram: summary skipped (0 qualified)")
        return

    total = sum(counts.values())
    deduped = counts["dedup"]
    low_score = counts["score"]

    sources_line = ""
    if source_counts:
        sources_line = "📡 " + " | ".join(f"{n}: {c}" for n, c in source_counts.items()) + "\n"

    top_lines = ""
    for j in top_jobs[:3]:
        top_lines += f"• {j['title']} @ {j['company']} — {j['score']}/100\n"

    ats_error_line = f"⚠️ Ошибок скоринга: *{ats_errors}* (будут перепроверены в следующем прогоне)\n" if ats_errors else ""

    dashboard_link = f" | [Дашборд]({_DASHBOARD_URL})" if _DASHBOARD_URL else ""
    text = (
        f"🤖 *Прогон завершён*\n\n"
        f"{sources_line}"
        f"✅ Новых вакансий: *{qualified}*\n"
        f"📊 Всего проверено: {total} | Дубликаты: {deduped} | Низкий скор: {low_score}\n"
        f"{ats_error_line}\n"
        f"{top_lines}"
        f"\n[Открыть Notion]({NOTION_DB_URL}){dashboard_link}"
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
