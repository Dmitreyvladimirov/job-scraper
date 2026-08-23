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


# --- Two-way bot: everything below is used by the intake webhook, not by the -------
# --- scraper's one-way notifications above. -------------------------------------
#
# Deliberately plain text (no parse_mode) unlike the notifications above: a reply
# echoes a scraped job title back at the user, and a title containing '*' or '_'
# makes Telegram reject the whole message with a 400 on unparsable entities. Links
# are auto-linked by the client anyway, so Markdown buys nothing here.

def send_message(text: str, chat_id: str | int | None = None,
                 reply_markup: dict | None = None) -> int | None:
    """Plain-text message. Returns the sent message_id so a later call can edit it
    in place (the bot answers "принял" first and rewrites that same message with the
    verdict ~30 s later, instead of stacking three messages per vacancy)."""
    body = {"chat_id": chat_id or TELEGRAM_CHAT_ID, "text": text,
            "disable_web_page_preview": True}
    if reply_markup:
        body["reply_markup"] = reply_markup
    result = _call("sendMessage", body)
    return (result or {}).get("message_id")


def edit_message(message_id: int, text: str, chat_id: str | int | None = None,
                 reply_markup: dict | None = None) -> None:
    """Rewrite a message already sent. A failure here is never fatal — worst case
    the user keeps reading 'принял, работаю', so the caller falls back to a fresh
    send_message() rather than losing the verdict."""
    body = {"chat_id": chat_id or TELEGRAM_CHAT_ID, "message_id": message_id,
            "text": text, "disable_web_page_preview": True}
    if reply_markup:
        body["reply_markup"] = reply_markup
    _call("editMessageText", body)


def answer_callback(callback_id: str, text: str = "") -> None:
    """Stop the spinner on a tapped inline button. Telegram shows the button as
    pending for ~15 s if this never arrives, which reads as a broken bot even when
    the work behind it is running fine."""
    _call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})


def send_document(content: bytes, filename: str, caption: str = "",
                  chat_id: str | int | None = None) -> None:
    """Upload a file (the generated resume PDF) into the chat.

    The PDF is sent as bytes rather than as a dashboard link on purpose: every
    dashboard route is cookie-authenticated, so a link would make Dimitry log in on
    the phone before he can look at what he just asked for.
    """
    import requests

    requests.post(
        f"{_API}/sendDocument",
        data={"chat_id": chat_id or TELEGRAM_CHAT_ID, "caption": caption[:1024]},
        files={"document": (filename, content, "application/pdf")},
        timeout=60,
    ).raise_for_status()


def _call(method: str, body: dict) -> dict | None:
    """One Bot API call. Returns the `result` object, or None on any failure — the
    bot's job is to answer a human, and no reply is a better outcome than a 500
    propagating out of a webhook handler Telegram will then retry."""
    try:
        payload = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(f"{_API}/{method}", data=payload)
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8")).get("result")
    except Exception as e:
        logger.error(f"Telegram: {method} failed: {e}")
        return None
