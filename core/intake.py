"""Intake — the external entry point: a vacancy Dimitry sends by hand gets fetched,
identified, carded and scored, exactly as if a scraper run had found it.

Built 2026-08-23. Two transports call in here and neither owns any of the logic:
the Telegram webhook (core/dashboard.py, POST /tg/{secret}) and POST /api/intake.
Adding a third (an iOS Shortcut hitting the HTTP one, a future email path) is a
transport, not a feature.

Three deliberate decisions, each with a reason that outlived an alternative:

1. **A short fetch is refused, not scored.** LinkedIn serves a login wall to every
   logged-out fetch, and an aggregator SPA serves an empty shell — both look like
   a job description with ~200 characters in it. Scoring those reproduces exactly
   the short-JD inflation the local scorer was retired for (a 33-char JD once
   scored 94/100). Under config.INTAKE_MIN_JD_CHARS intake returns
   status="need_text" and asks for the pasted description instead of guessing.

2. **Company and title come from the JD text, via Haiku.** An ATS posting API
   returns a real title but only a board slug for the company; an aggregator page
   returns neither; a pasted text returns nothing structured. One cheap call
   (~$0.0005) covers all three uniformly, and the resume service files its output
   BY company name, so a slug there is a visible defect in the PDF.

3. **A resume is never generated here.** Dimitry's call, 2026-08-23: scoring is
   automatic on every send, generation waits for an explicit command (the bot's
   button, or POST /api/jobs/{id}/resume). Manual sends skew toward vacancies he
   already likes, so an always-on generation would spend on nearly every one of
   them before he has read the verdict.
"""
import json
import logging
import os
import time
from dataclasses import dataclass, field

import db
import resume_client
import scoring_client
import utils
from config import (
    DESCRIPTION_MAX_CHARS, INTAKE_META_MODEL, INTAKE_MIN_JD_CHARS, RESUME_MIN_JD_CHARS,
)

logger = logging.getLogger(__name__)

# Hosts that never serve a job description to an unauthenticated fetch. Listed to
# produce an honest "paste the text" answer immediately rather than after three
# escalating fetch attempts that are certain to fail (LinkedIn is also excluded
# from the scraper's own fetch path for the same reason).
_LOGIN_WALLED = ("linkedin.com", "glassdoor.com", "indeed.com", "dice.com")


@dataclass
class IntakeResult:
    """What both transports render. `status` drives the reply; everything else is
    filled in as far as the run got.

    status:
      "ok"         — carded and scored; job_id, score and result are set
      "scored_off" — carded, but the scoring service failed; the card is real and
                     re-scorable from the dashboard, so this is not an error
      "duplicate"  — this vacancy is already a card; job_id points at the existing
                     one and nothing was created or paid for
      "need_text"  — the URL yielded no usable JD; ask for the pasted description
      "error"      — nothing usable came in at all
    """

    status: str
    message: str = ""
    job_id: int | None = None
    created: bool = False
    title: str | None = None
    company: str | None = None
    location: str | None = None
    url: str | None = None
    score: int | None = None
    result: object | None = None          # ats.ATSResult when status == "ok"
    existing: dict | None = None          # the duplicate's row, for status="duplicate"
    resume_run_id: int | None = None
    resume_blocked: str | None = field(default=None)


def looks_like_url(raw: str) -> bool:
    """A message that IS a link, not one that merely mentions one. A pasted job
    description frequently contains an apply URL somewhere in its body; treating
    that as "the user sent a link" would throw away the text he already gave us."""
    raw = raw.strip()
    return raw.startswith(("http://", "https://")) and len(raw.split()) == 1


def ingest(raw: str, *, source: str = "Manual", force: bool = False,
           url_hint: str | None = None) -> IntakeResult:
    """Take a URL or a pasted job description; return a scored card.

    url_hint carries the link from an earlier "need_text" round, so the card the
    pasted text produces still has an apply URL attached (the Telegram transport
    remembers the last refused link per chat and passes it back here).

    Never raises: a transport must be able to answer the user whatever happened.
    """
    raw = (raw or "").strip()
    if not raw:
        return IntakeResult(status="error", message="Пустое сообщение")

    url = url_hint
    hints: dict = {}

    if looks_like_url(raw):
        url = raw
        jd, hints, why = _fetch_jd(url)
        if not jd:
            return IntakeResult(status="need_text", url=url, message=why)
    else:
        jd = raw
        if len(jd) < INTAKE_MIN_JD_CHARS:
            return IntakeResult(
                status="error", url=url,
                message=f"Текст короче {INTAKE_MIN_JD_CHARS} символов — этого мало для честной оценки",
            )

    jd = jd[:DESCRIPTION_MAX_CHARS]
    title_hint = hints.get("title") or ""
    meta = _extract_meta(jd, title_hint=title_hint, url=url)
    title = meta.get("title") or title_hint or "Untitled vacancy"
    company = meta.get("company") or None
    # A location the job board states outright beats one the model read out of
    # prose -- it is 15 of the 100 scoring points, and the earlier pipeline already
    # lost a whole rubric axis by computing location and then dropping it.
    location = hints.get("location") or meta.get("location") or None

    existing = db.find_manual_duplicate(url, company, title)
    if existing and not force:
        return IntakeResult(
            status="duplicate", job_id=existing["id"], existing=existing,
            title=existing.get("title"), company=existing.get("company"), url=url,
            score=existing.get("ats_score"),
        )

    job_id = db.create_manual_job({
        "title": title,
        "company": company,
        "url": url,
        "apply_url": url,
        "source": source,
        "salary": meta.get("salary") or None,
        "location": location,
        "description": jd,
        "published": None,
    })

    job = db.get_job(job_id)
    pipeline_run_id = f"intake-{job_id}-{int(time.time())}"
    result, error_kind = scoring_client.analyze_via_cloud(job, pipeline_run_id)
    if result is None:
        logger.error(f"intake: scoring failed for job {job_id} ({error_kind})")
        return IntakeResult(
            status="scored_off", job_id=job_id, created=True, title=title,
            company=company, location=location, url=url,
            message=("Скоринг не сработал (конфиг сервиса)" if error_kind == "config"
                     else "Скоринг не сработал — карточка создана, можно перескорить в дашборде"),
        )

    db.update_job_scoring(job_id, result, pipeline_run_id)
    job["ats_score"] = result.score
    return IntakeResult(
        status="ok", job_id=job_id, created=True, title=title, company=company,
        location=location, url=url, score=result.score, result=result,
        resume_blocked=resume_block_reason(job),
    )


def resume_block_reason(job: dict) -> str | None:
    """Release-1 generation gates, shared by every surface that offers generation
    (the dashboard button, the bot's button, POST /api/jobs/{id}/resume). The resume
    service would happily generate from a bare title, but the output files by company
    and tailors by JD text — so both are quality gates here, not service requirements."""
    if not (job.get("company") or "").strip():
        return "Company is required — the resume is titled and filed by company"
    if len(job.get("description") or "") < RESUME_MIN_JD_CHARS:
        return f"JD is under {RESUME_MIN_JD_CHARS} chars — paste the full description first"
    return None


def generate_resume(job_id: int) -> tuple[int | None, str | None]:
    """Explicit-command resume generation. Returns (resume_run_id, error_message).

    Idempotent by design: a card that already has a resume_run_id returns it without
    paying for a second generation, so a double-tapped button (or a retried HTTP
    call) costs nothing — same contract as the dashboard button.
    """
    job = db.get_job(job_id)
    if not job:
        return None, "Карточка не найдена"
    if job.get("resume_run_id"):
        return job["resume_run_id"], None

    reason = resume_block_reason(job)
    if reason:
        return None, reason

    pipeline_run_id = job.get("pipeline_run_id") or f"intake-resume-{job_id}-{int(time.time())}"
    outcome, error_kind = resume_client.generate_via_cloud(job, pipeline_run_id)
    if outcome is None:
        return None, ("Сервис резюме не настроен (auth/deploy)" if error_kind == "config"
                      else "Генерация не удалась — попробуй ещё раз через минуту")

    db.set_resume_run_id(job_id, outcome.generation_run_id)
    return outcome.generation_run_id, None


def rescore(job_id: int) -> tuple[object | None, str | None]:
    """Re-score an existing card. Returns (ATSResult, error_message).

    The useful answer to "you already have this vacancy" is usually a fresh verdict
    rather than a second card: the likeliest reason an old score looks wrong is that
    it predates the cloud scorer (the dashboard's duplicate dialog offers the same
    action for the same reason).
    """
    job = db.get_job(job_id)
    if not job:
        return None, "Карточка не найдена"
    if not (job.get("description") or "").strip():
        return None, "У карточки нет текста вакансии — нечего скорить"

    pipeline_run_id = f"intake-rescore-{job_id}-{int(time.time())}"
    result, error_kind = scoring_client.analyze_via_cloud(job, pipeline_run_id)
    if result is None:
        return None, ("Скоринг не настроен (auth/deploy)" if error_kind == "config"
                      else "Скоринг не сработал — попробуй ещё раз через минуту")
    db.update_job_scoring(job_id, result, pipeline_run_id)
    return result, None


def _fetch_jd(url: str) -> tuple[str, dict, str]:
    """(jd_text, hints, why_empty) for a job URL, cheapest source first.

    `hints` carries whatever the board stated itself -- title always, location when
    the board's API gives one -- and is empty for the HTML fallbacks, which know
    nothing beyond the text.

    Three escalating attempts: the ATS posting API (free, exact, has a title), a
    plain HTML fetch (free, works on server-rendered pages), then ScrapingBee
    (costs credits, renders JS — the only thing that gets text out of an
    aggregator SPA). Each is skipped once an earlier one produced enough text.
    """
    if not utils._validate_url(url):
        return "", {}, "Ссылка не открывается (или ведёт во внутреннюю сеть)"

    if utils._url_host_matches(url, _LOGIN_WALLED):
        host = url.split("/")[2] if "/" in url[8:] else url
        return "", {}, (f"{host} отдаёт описание только авторизованным — "
                        "пришли текст вакансии сообщением, ссылку я привяжу к карточке")

    posting = utils.fetch_posting(url)
    hints = {k: v for k, v in posting.items() if k in ("title", "location") and v}
    jd = posting.get("description", "")
    if len(jd) >= INTAKE_MIN_JD_CHARS:
        return jd, hints, ""

    generic = utils.fetch_url_generic(url, max_chars=DESCRIPTION_MAX_CHARS)
    if len(generic) >= INTAKE_MIN_JD_CHARS:
        return generic, hints, ""

    html = utils._fetch_via_scrapingbee(url)
    if html:
        rendered = utils.strip_html(html)[:DESCRIPTION_MAX_CHARS]
        if len(rendered) >= INTAKE_MIN_JD_CHARS:
            return rendered, hints, ""

    return "", hints, ("По ссылке не нашлось описания вакансии — "
                       "пришли текст сообщением, ссылку я привяжу к карточке")


_META_SYSTEM = """You extract structured fields from a job posting. Answer with a single
JSON object and nothing else: {"title": ..., "company": ..., "location": ..., "salary": ...}

Rules:
- title: the role title only, no company name, no seniority padding that isn't in the text.
- company: the HIRING company. If the posting is published by a recruiting agency on behalf
  of an unnamed client, use null rather than the agency name.
- location: as written in the posting ("Remote, EU", "Tel Aviv, Israel", "Remote worldwide").
- salary: only if an actual figure or range is stated; otherwise null.
- Use null for anything the text does not state. Never invent, never infer from the URL."""


def _extract_meta(jd: str, *, title_hint: str = "", url: str | None = None) -> dict:
    """Title/company/location/salary out of the JD text.

    Failure here is not fatal: an unparsed response falls back to the ATS-provided
    title and no company, which still produces a scored card (the scoring service
    takes both as optional) — only resume generation stays blocked until Dimitry
    fills the company in from the dashboard.
    """
    hint = f"\nThe job board reports the title as: {title_hint}" if title_hint else ""
    hint += f"\nThe posting URL is: {url}" if url else ""
    try:
        raw, _usage = _call_claude(_META_SYSTEM, f"{jd[:12000]}{hint}")
        parsed = json.loads(_json_slice(raw))
    except Exception:
        logger.exception("intake: metadata extraction failed — falling back to the title hint")
        return {"title": title_hint}

    out = {}
    for key in ("title", "company", "location", "salary"):
        value = parsed.get(key)
        out[key] = value.strip() if isinstance(value, str) and value.strip() else None
    if not out.get("title"):
        out["title"] = title_hint or None
    return out


def _json_slice(text: str) -> str:
    """The JSON object out of a reply that may have wrapped it in prose or a fence."""
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError(f"no JSON object in model reply: {text[:200]!r}")
    return text[start:end + 1]


def _call_claude(system: str, user: str) -> tuple[str, dict]:
    """Anthropic call for field extraction. Deliberately a local copy of
    mail_agent._call_claude rather than a shared helper — the same choice
    scoring_client and resume_client made about their own _post_once: eight lines
    duplicated is cheaper than a common module every caller has to reason about."""
    import anthropic

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    resp = client.messages.create(model=INTAKE_META_MODEL, max_tokens=300,
                                  system=system, messages=[{"role": "user", "content": user}])
    text = "".join(b.text for b in resp.content if b.type == "text")
    return text, {"input_tokens": resp.usage.input_tokens, "output_tokens": resp.usage.output_tokens}
