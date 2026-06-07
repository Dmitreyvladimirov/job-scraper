import json
import logging
import urllib.request
from config import TELEGRAM_TOKEN, TELEGRAM_CHAT_ID

logger = logging.getLogger(__name__)

_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"


def send_vacancy(job: dict, score: int, cooldown_match: dict | None = None) -> None:
    salary = job.get("salary") or "не указана"
    location = job.get("location") or "Remote"
    company = job.get("company") or "—"

    warning = ""
    if cooldown_match:
        warning = (
            f"⚠️ *Уже подавался в эту компанию*\n"
            f"{cooldown_match['company']} — {cooldown_match['position']}\n"
            f"📅 {cooldown_match['date']} ({cooldown_match['days_ago']} дней назад)\n\n"
        )

    text = (
        f"{warning}"
        f"🤖 *Новая вакансия*\n\n"
        f"*{job['title']}* — {company}\n"
        f"📍 {location} | 💰 {salary}\n"
        f"⭐ ATS Score: {score}/100\n"
        f"🔗 [Открыть вакансию]({job['url']})\n"
        f"📡 {job['source']}"
    )

    payload = json.dumps({
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }).encode("utf-8")

    req = urllib.request.Request(f"{_API}/sendMessage", data=payload)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
        logger.info(f"Telegram: sent '{job['title']}'" + (" [COOLDOWN WARNING]" if cooldown_match else ""))
    except Exception as e:
        logger.error(f"Telegram: failed for '{job['title']}': {e}")
