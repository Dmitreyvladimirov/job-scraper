"""Telegram transport for intake — turns a forwarded link or a pasted job posting
into a scored card, and offers a resume on an explicit tap.

Built 2026-08-23. This module owns conversation shape only; every decision that
touches the pipeline lives in intake.py, so the HTTP endpoint and the bot cannot
drift apart.

Why the bot at all, when POST /api/intake exists: the share sheet. A vacancy is
almost always seen on a phone, inside LinkedIn or a Telegram channel, and "share →
the bot" is two taps from wherever it was spotted. The HTTP endpoint is the same
pipeline for anything that is not a human with a phone.

Access control is the chat id, not a token: the webhook path already carries a
secret, and a message from any other chat is dropped without a reply — an unknown
sender must not even learn that the bot exists.
"""
import logging
import threading
import time

import db
import intake
import telegram
from config import ATS_THRESHOLD, TELEGRAM_CHAT_ID

logger = logging.getLogger(__name__)

INTAKE_SOURCE = "Manual (Telegram)"

# A link that could not be fetched, kept until the next message from that chat so
# the pasted text lands on a card that still has its apply URL. In-process on a
# single-instance service: losing it to a redeploy costs a URL on one card, which
# is why nothing more durable was built for it.
_PENDING_URL_TTL = 30 * 60
_pending_url: dict[int, tuple[str, float]] = {}

_HELP = (
    "Кидай сюда вакансию — ссылкой или текстом.\n\n"
    "• Ссылка на Greenhouse / Lever / Ashby — вытяну описание сам\n"
    "• Ссылка на агрегатор — попробую, может не отдать текст\n"
    "• LinkedIn — описание за авторизацией, пришли текст сообщением\n"
    "• Просто текст вакансии — самый надёжный путь\n\n"
    "В ответ: скор по 4 осям, вердикт и карточка в дашборде. "
    "Резюме — кнопкой под ответом, автоматически не генерю."
)


def handle_update(update: dict) -> None:
    """One Telegram update, start to finish. Runs in a background thread off the
    webhook: Telegram re-delivers an update that is not acknowledged within seconds,
    and a scoring call takes 10-30 s, so the route answers 200 immediately and the
    work happens here. Never raises — an exception in this thread would be invisible
    to the user, so every path ends in a message."""
    try:
        if "callback_query" in update:
            _handle_callback(update["callback_query"])
        elif "message" in update:
            _handle_message(update["message"])
    except Exception:
        logger.exception("tg_bot: update handling failed")


def _allowed(chat_id) -> bool:
    """Only Dimitry's own chat. Compared as strings because TELEGRAM_CHAT_ID is an
    env var and Telegram sends an int."""
    return TELEGRAM_CHAT_ID and str(chat_id) == str(TELEGRAM_CHAT_ID)


def _handle_message(message: dict) -> None:
    chat_id = message.get("chat", {}).get("id")
    if not _allowed(chat_id):
        logger.warning(f"tg_bot: message from unauthorised chat {chat_id} — ignored")
        return

    # A forwarded channel post carries its body in `text` too; `caption` covers a
    # forwarded image/file whose posting text sits under the attachment.
    raw = (message.get("text") or message.get("caption") or "").strip()
    if not raw:
        telegram.send_message("Не вижу текста — пришли ссылку или текст вакансии.", chat_id)
        return

    if raw.startswith("/"):
        telegram.send_message(_HELP, chat_id)
        return

    progress_id = telegram.send_message("Принял, разбираю…", chat_id)
    url_hint = _take_pending_url(chat_id)
    try:
        result = intake.ingest(raw, source=INTAKE_SOURCE, url_hint=url_hint)
    except Exception as e:
        # intake.ingest promises not to raise, but the DB underneath it can. Letting
        # the exception reach handle_update()'s catch-all would leave "принял,
        # разбираю…" standing forever, which reads as a hung bot rather than a
        # failed call — so the placeholder always gets rewritten with the reason.
        logger.exception("tg_bot: intake failed")
        _reply(chat_id, progress_id, f"Сломалось на разборе: {str(e)[:300]}")
        return

    if result.status == "need_text":
        _pending_url[chat_id] = (result.url, time.time())

    text, markup = _render(result)
    _reply(chat_id, progress_id, text, markup)


def _handle_callback(callback: dict) -> None:
    chat_id = callback.get("message", {}).get("chat", {}).get("id")
    if not _allowed(chat_id):
        return
    telegram.answer_callback(callback.get("id", ""), "Работаю…")

    action, _, raw_id = (callback.get("data") or "").partition(":")
    if not raw_id.isdigit():
        return
    job_id = int(raw_id)

    if action == "resume":
        _do_resume(chat_id, job_id)
    elif action == "rescore":
        _do_rescore(chat_id, job_id)


def _do_resume(chat_id, job_id: int) -> None:
    progress_id = telegram.send_message(f"Делаю резюме под #{job_id}…", chat_id)
    resume_run_id, error = intake.generate_resume(job_id)
    if error:
        _reply(chat_id, progress_id, f"Резюме не получилось: {error}")
        return

    stored = db.get_resume_pdf(resume_run_id)
    if stored is None:
        _reply(chat_id, progress_id, f"Резюме готово (run {resume_run_id}), но PDF не нашёлся в базе")
        return

    pdf_bytes, company = stored
    flags = db.get_resume_skeptic_count(resume_run_id)
    caption = f"Резюме под #{job_id}" + (f" — {flags} флажков Скептика" if flags else "")
    _reply(chat_id, progress_id, f"Готово: run {resume_run_id}")
    telegram.send_document(pdf_bytes, f"Dimitry Kucher_PM_{company or job_id}.pdf", caption, chat_id)


def _do_rescore(chat_id, job_id: int) -> None:
    progress_id = telegram.send_message(f"Перескориваю #{job_id}…", chat_id)
    result, error = intake.rescore(job_id)
    if error:
        _reply(chat_id, progress_id, f"Не вышло: {error}")
        return
    job = db.get_job(job_id) or {}
    _reply(
        chat_id, progress_id,
        f"{_verdict(result.score)} {result.score}/100 — {job.get('title')} @ {job.get('company')}\n\n"
        + _breakdown(result),
        _keyboard(job_id, intake.resume_block_reason(job)),
    )


def _reply(chat_id, progress_id: int | None, text: str, markup: dict | None = None) -> None:
    """Rewrite the "принял" placeholder with the real answer, or send a new message
    if there was no placeholder (the edit itself failing is handled inside
    telegram.edit_message and leaves the placeholder in place — annoying, not lossy)."""
    if progress_id:
        telegram.edit_message(progress_id, text[:4000], chat_id, markup)
    else:
        telegram.send_message(text[:4000], chat_id, markup)


def _take_pending_url(chat_id) -> str | None:
    """The URL from the last "couldn't fetch this" answer, if it is still fresh."""
    pending = _pending_url.pop(chat_id, None)
    if not pending:
        return None
    url, stamp = pending
    return url if time.time() - stamp < _PENDING_URL_TTL else None


def _verdict(score: int | None) -> str:
    if score is None:
        return "❔"
    return "✅" if score >= ATS_THRESHOLD else "🚫"


def _keyboard(job_id: int, resume_blocked: str | None) -> dict:
    """Resume is a button, never automatic (Dimitry, 2026-08-23). When the card
    cannot support a generation the button is replaced by the reason instead of
    being offered and then refused on tap."""
    row = [{"text": "🔁 Перескорить", "callback_data": f"rescore:{job_id}"}]
    if not resume_blocked:
        row.insert(0, {"text": "📄 Сделать резюме", "callback_data": f"resume:{job_id}"})
    return {"inline_keyboard": [row]}


def _breakdown(result) -> str:
    lines = [
        f"Оси: role {result.role_score}/30 · domain {result.domain_score}/30 · "
        f"keywords {result.keyword_score}/25 · location {result.location_score}/15"
        + (f" · штраф −{result.penalty}" if result.penalty else ""),
    ]
    if result.why_apply:
        lines.append(f"\nЗа: {result.why_apply}")
    if result.why_not:
        lines.append(f"Против: {result.why_not}")
    if result.matched:
        lines.append(f"\nСовпало: {', '.join(result.matched[:8])}")
    if result.missed:
        lines.append(f"Не хватает: {', '.join(result.missed[:8])}")
    return "\n".join(lines)


def _render(result: intake.IntakeResult) -> tuple[str, dict | None]:
    """IntakeResult -> (message text, inline keyboard)."""
    if result.status == "ok":
        header = f"{_verdict(result.score)} {result.score}/100 — {result.title}"
        if result.company:
            header += f" @ {result.company}"
        if result.location:
            header += f"\n📍 {result.location}"
        body = f"{header}\n\n{_breakdown(result.result)}\n\nКарточка #{result.job_id}"
        if result.resume_blocked:
            body += f"\nРезюме недоступно: {result.resume_blocked}"
        return body, _keyboard(result.job_id, result.resume_blocked)

    if result.status == "duplicate":
        row = result.existing or {}
        job = db.get_job(result.job_id) or {}
        return (
            f"Такая вакансия уже есть — карточка #{result.job_id}\n"
            f"{row.get('title')} @ {row.get('company')}\n"
            f"Скор: {row.get('ats_score') or '—'} · статус: {row.get('current_status') or '—'} · "
            f"источник: {row.get('source') or '—'}",
            _keyboard(result.job_id, intake.resume_block_reason(job)),
        )

    if result.status == "scored_off":
        return f"Карточка #{result.job_id} создана, но {result.message}", None

    return result.message or "Не получилось разобрать это сообщение", None


def start(update: dict) -> None:
    """Spawn handle_update in a daemon thread — the webhook route's only job."""
    threading.Thread(target=handle_update, args=(update,), daemon=True).start()
