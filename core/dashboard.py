"""FastAPI dashboard — reads from Postgres and serves analytics charts."""
import csv
import hashlib
import hmac
from html import escape as html_escape
import io
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import threading

import psycopg2
import psycopg2.extras
from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from pydantic import BaseModel, Field
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException

import db
import intake
import resume_client
import scoring_client
import tg_bot
from config import (
    CURRENT_STATUSES, DOMAIN_COLORS, FUNNEL_ORDER, REJECTION_REASON_LABELS,
    TELEGRAM_WEBHOOK_SECRET, USER_REJECTION_REASONS,
)

logger = logging.getLogger(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL", "")
TOKEN = os.environ.get("DASHBOARD_TOKEN", "")

app = FastAPI()

_BASE_DIR = Path(__file__).parent
app.mount("/static", StaticFiles(directory=_BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=_BASE_DIR / "templates")
# Starlette's Jinja2Templates does not register a `tojson` filter by default (that's a
# Flask-ism) — register our own, reusing the same `<` escape as _json_js() below so a
# scraped value (job title, company) can't break out of a JS string context when
# embedded in an inline event handler (see kanban.html's status-chip onclick).
templates.env.filters["tojson"] = lambda v: json.dumps(v).replace("<", "\\u003c")


def _asset_version() -> str:
    """Content hash of the stylesheet, appended to its URL as ?v=.

    Without it a CSS change is invisible until the browser happens to revalidate:
    StaticFiles sends an ETag and Last-Modified but no Cache-Control, so browsers
    fall back to heuristic caching and may serve a stale copy for hours. On
    2026-08-24 that turned a pure refactor — inline styles moved into classes —
    into a broken-looking board: the new HTML referenced classes that the cached
    stylesheet did not have yet. A hash in the URL makes every deploy a new URL,
    so the answer stops being "hard-refresh".
    """
    try:
        return hashlib.md5((_BASE_DIR / "static" / "classical.css").read_bytes()).hexdigest()[:8]
    except OSError:
        # A missing stylesheet is the template's problem to show, not a reason to
        # refuse to start; fall back to a constant so the page still renders.
        return "dev"


templates.env.globals["asset_v"] = _asset_version()


def _safe_job_url(url: str | None) -> str | None:
    """job.apply_url/job.url come from scraped sources (incl. Telegram channel posts,
    which we don't control) with no scheme validation upstream — reject anything that
    isn't http(s) so a `javascript:`-URI job link can't execute in the dashboard's
    origin when clicked (Open posting)."""
    if not url:
        return None
    try:
        return url if urlparse(url).scheme in ("http", "https") else None
    except ValueError:
        return None


templates.env.filters["safe_url"] = _safe_job_url


def _domain_tags(domain: str | None) -> list[dict]:
    """Split a compound "X | Y" domain string into individual tags.

    Compound is the intended shape, not a legacy artefact — this docstring used to
    claim the opposite while 86 of 284 cloud-scored cards carried two or more
    domains, because the rubric offered them as a pipe-separated menu and never
    said whether to pick one. Playlab (#76427) is what made it visible: an AI
    product built for 60,000 educators, tagged AI/ML with no EdTech. The rubric now
    asks for every domain that applies, up to three, most defining first.
    AI/ML — the candidate's primary target domain — keeps the existing accent-gold
    tag-accent styling; anything in DOMAIN_COLORS (config.py) gets its own oklch hue;
    anything else (incl. "Other") falls back to a plain neutral tag."""
    if not domain:
        return []
    # The rubric said "Healthcare" for a while and the palette has always said
    # "HealthTech", so six cards rendered plain grey. The rubric now emits
    # HealthTech; this keeps the older rows coloured instead of rescoring them.
    aliases = {"Healthcare": "HealthTech"}
    # Split on '+' and ',' as well as the '|' the rubric asks for: the model
    # improvises a separator now and then (one live card read
    # "Growth/Consumer + B2B SaaS + AI/ML" and rendered as a single unreadable
    # blob). '/' is deliberately not a separator — "AI/ML" and "Growth/Consumer"
    # contain one.
    tags = []
    for name in (d.strip() for d in re.split(r"[|+,]", domain)):
        if not name:
            continue
        name = aliases.get(name, name)
        if name == "AI/ML":
            tags.append({"name": name, "class": "tag tag-accent", "style": ""})
        elif name in DOMAIN_COLORS:
            hue = DOMAIN_COLORS[name]
            tags.append({"name": name, "class": "tag", "style":
                f"color:oklch(.45 .09 {hue});border:1px solid oklch(.62 .09 {hue});background:oklch(.62 .09 {hue} / .1)"})
        else:
            tags.append({"name": name, "class": "tag tag-neutral", "style": ""})
    return tags


templates.env.globals["domain_tags"] = _domain_tags


def _short_date(value) -> str:
    """DD.MM for the compact card meta row. Accepts a datetime (logged_at/applied_at)
    or job.published, which is a scraped TEXT field with a "YYYY-MM-DD..." prefix when
    the source provides one at all."""
    if not value:
        return ""
    if hasattr(value, "strftime"):
        return value.strftime("%d.%m")
    try:
        from datetime import datetime as _dt
        return _dt.strptime(str(value)[:10], "%Y-%m-%d").strftime("%d.%m")
    except ValueError:
        return ""


templates.env.filters["short_date"] = _short_date


# Country-name -> ISO code, inverted from the jobgether expansion map, plus the
# names that appear in locations from other sources / the Notion import.
from sources.jobgether import _ISO_COUNTRIES as _CODE_TO_NAME  # noqa: E402
_NAME_TO_CODE = {v.lower(): k for k, v in _CODE_TO_NAME.items()}
_NAME_TO_CODE.update({"united states": "US", "uk": "GB", "czech republic": "CZ",
                      "the netherlands": "NL", "south korea": "KR", "hong kong": "HK"})


def _flag(code: str) -> str:
    return "".join(chr(0x1F1E6 + ord(c) - 65) for c in code.upper()) if len(code) == 2 else ""


def _location_badge(location: str | None) -> str:
    """Compact location for cards: flags + short label (2026-08-19, Dimitry's ask).
    'Remote worldwide' -> '🌍 Remote'; 'Remote — China, India' -> '🇨🇳🇮🇳 Remote';
    'Tel Aviv, Israel' -> '🇮🇱 Tel Aviv, Israel'; unknown text passes through."""
    if not location:
        return ""
    loc = location.strip()
    if loc.lower() in ("remote worldwide", "remote", "worldwide"):
        return "🌍 Remote"
    if loc.startswith("Remote — "):
        parts = [p.strip() for p in loc[len("Remote — "):].split(",")]
        flags = [_flag(_NAME_TO_CODE.get(p.lower(), p if len(p) == 2 else "")) for p in parts]
        flags = [f for f in flags if f]
        shown = "".join(flags[:3]) + (f" +{len(flags) - 3}" if len(flags) > 3 else "")
        return (shown + " Remote").strip() if shown else loc
    code = _NAME_TO_CODE.get(loc.lower())
    if code:
        return f"{_flag(code)} {loc}"
    for name, code in _NAME_TO_CODE.items():
        if name in loc.lower():
            return f"{_flag(code)} {loc}"
    return loc


templates.env.filters["location_badge"] = _location_badge

STATUS_LABELS = {
    "found": "Found", "applied": "Applied", "recruiter_reply": "Recruiter reply",
    "screen": "Screen", "interview": "Interview", "offer": "Offer", "rejected": "Rejected",
}


@app.on_event("startup")
def _validate_config() -> None:
    # Fail fast rather than silently serving an unauthenticated dashboard — matches
    # config.validate_secrets()'s fail-fast philosophy for the scraper service.
    if not TOKEN:
        raise RuntimeError("DASHBOARD_TOKEN is not set — refusing to start with an unauthenticated dashboard")


@app.get("/health")
def health():
    return {"status": "ok"}


COOKIE_NAME = "dashboard_token"
COOKIE_MAX_AGE = 60 * 60 * 24 * 180  # ~6 months


def _authenticate(request: Request) -> None:
    """Cookie-only — the token never travels in a URL again after /login sets the
    cookie, closing the Referer-leak / browser-history-leak surface a query-param
    token had. Log in once at /login; every other route just reads the cookie."""
    if not TOKEN:
        # Belt-and-suspenders: startup should already have refused to boot without a
        # token, but never fail open on a protected endpoint if it somehow got here.
        raise HTTPException(status_code=503, detail="Dashboard misconfigured: DASHBOARD_TOKEN not set")
    cookie_token = request.cookies.get(COOKIE_NAME, "")
    if not cookie_token or not hmac.compare_digest(cookie_token, TOKEN):
        raise HTTPException(status_code=403, detail="Not authenticated — log in at /login")


_LOGIN_MAX_ATTEMPTS = 10
_LOGIN_WINDOW_SECONDS = 300  # 5 minutes
_login_failures: list[float] = []


def _check_login_rate_limit() -> None:
    """/login is reachable by anyone who finds the Railway URL, unlike the old
    query-param token which at least required already knowing a secret to even try —
    hmac.compare_digest defeats timing attacks but does nothing to slow raw guess
    throughput, so add a simple in-process lockout. Single personal-tool deployment,
    single process — no need for a shared store across replicas."""
    now = time.time()
    while _login_failures and _login_failures[0] < now - _LOGIN_WINDOW_SECONDS:
        _login_failures.pop(0)
    if len(_login_failures) >= _LOGIN_MAX_ATTEMPTS:
        raise HTTPException(status_code=429, detail="Too many failed login attempts — try again later")


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    response = templates.TemplateResponse(request, "login.html", {})
    response.headers["Cache-Control"] = "no-store"
    return response


@app.post("/login")
def login_submit(request: Request, token: str = Form(...)):
    if not TOKEN:
        raise HTTPException(status_code=503, detail="Dashboard misconfigured: DASHBOARD_TOKEN not set")
    _check_login_rate_limit()
    if not hmac.compare_digest(token, TOKEN):
        _login_failures.append(time.time())
        # Turn 7b's "Access token invalid" state — re-render the same form with an
        # inline message instead of a bare 403 JSON blob (nowhere to go from that).
        response = templates.TemplateResponse(request, "login.html", {"error": True}, status_code=403)
        response.headers["Cache-Control"] = "no-store"
        return response
    redirect = RedirectResponse(url="/", status_code=302)
    redirect.set_cookie(
        COOKIE_NAME, TOKEN, max_age=COOKIE_MAX_AGE,
        httponly=True, secure=True, samesite="strict",
    )
    redirect.headers["Cache-Control"] = "no-store"
    return redirect


@app.get("/logout")
def logout():
    redirect = RedirectResponse(url="/login", status_code=302)
    redirect.delete_cookie(COOKIE_NAME)
    return redirect


def _query(sql: str, params=()) -> list[dict]:
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor, connect_timeout=10)
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def _json_js(value) -> str:
    """JSON for embedding inside a <script> block: escape < so scraped values
    (source/company names) can't terminate the script tag or open new markup."""
    return json.dumps(value).replace("<", "\\u003c")


def _is_company_artifact(name: str | None) -> bool:
    """Some sources (LinkedIn-derived postings) leak consent-banner/UI text into the
    company field instead of a real name -- flag anything implausibly long or that
    names the platform itself, so the template can de-emphasize it instead of hiding
    it (the row is still a real qualified job, just with an unreliable company label)."""
    if not name:
        return True
    return len(name) > 50 or "linkedin" in name.lower()


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(
    request: Request,
    date_from: str | None = Query(default=None, alias="from"),
    date_to: str | None = Query(default=None, alias="to"),
):
    """Merged page (2026-08-17): application stats (the former /tracker — weekly
    series, funnel, applied-today drill-down) on top, scraper analytics below."""
    _authenticate(request)
    date_from, date_to = _parse_date_range(date_from, date_to)

    # --- application stats (former /tracker) ---
    series = db.get_tracker_series(date_from, date_to)
    app_funnel = db.get_funnel_stats()
    applied_today_count = len(db.get_applied_today())
    from datetime import date as _date, timedelta as _timedelta
    today = _date.today()
    presets = {
        "7 weeks": str(today - _timedelta(weeks=7)),
        "Quarter": str(today - _timedelta(days=90)),
    }

    # --- scraper analytics queries ---
    totals = _query("""
        SELECT COUNT(*) as runs,
               COALESCE(SUM(qualified),0) as qualified,
               COALESCE(SUM(gpt_calls),0) as gpt_calls,
               COALESCE(SUM(total_fetched),0) as fetched
        FROM runs
    """)[0]

    daily = _query("""
        SELECT DATE(started_at) as day,
               SUM(qualified) as qualified,
               SUM(total_fetched) as fetched,
               COUNT(*) as runs
        FROM runs
        GROUP BY day ORDER BY day
    """)

    funnel = _query("""
        SELECT
            SUM(qualified) as qualified,
            SUM(rejected_low_score) as low_score,
            SUM(filtered_role) as role,
            SUM(filtered_location) as location,
            SUM(filtered_stale) as stale,
            SUM(filtered_dedup) as dedup,
            SUM(filtered_gpt_limit) as gpt_limit
        FROM runs
    """)[0]

    by_source = _query("""
        SELECT source, COUNT(*) as total,
               SUM(CASE WHEN outcome='qualified' THEN 1 ELSE 0 END) as qualified
        FROM jobs WHERE source IS NOT NULL
        GROUP BY source ORDER BY qualified DESC
    """)

    score_dist = _query("""
        SELECT
            CASE
                WHEN ats_score >= 90 THEN '90-100'
                WHEN ats_score >= 80 THEN '80-89'
                WHEN ats_score >= 70 THEN '70-79'
                WHEN ats_score >= 60 THEN '60-69'
                ELSE '<60'
            END as bucket,
            COUNT(*) as cnt
        FROM jobs WHERE ats_score IS NOT NULL
        GROUP BY bucket ORDER BY bucket DESC
    """)

    recent_runs = _query("""
        SELECT started_at, total_fetched, qualified, gpt_calls
        FROM runs ORDER BY id DESC LIMIT 15
    """)

    top_companies = _query("""
        SELECT company, COUNT(*) as cnt, MAX(ats_score) as top_score
        FROM jobs WHERE outcome='qualified' AND company IS NOT NULL
        GROUP BY company ORDER BY cnt DESC, top_score DESC LIMIT 10
    """)
    for r in top_companies:
        r["is_artifact"] = _is_company_artifact(r["company"])

    return templates.TemplateResponse(request, "dashboard.html", {
        "active": "dashboard",
        "date_from": date_from,
        "date_to": date_to,
        "presets": presets,
        "series": series,
        "funnel": app_funnel,
        "funnel_order": FUNNEL_ORDER,
        "status_labels": STATUS_LABELS,
        "applied_today_count": applied_today_count,
        "weeks_json": _json_js(series["weeks"]),
        "found_json": _json_js(series["found"]),
        "applied_json": _json_js(series["applied"]),
        "replies_json": _json_js(series["replies"]),
        "totals": totals,
        "recent_runs": recent_runs,
        "top_companies": top_companies,
        "has_artifacts": any(r["is_artifact"] for r in top_companies),
        "daily_labels": _json_js([str(r["day"]) for r in daily]),
        "daily_qualified": _json_js([r["qualified"] for r in daily]),
        "daily_fetched": _json_js([r["fetched"] for r in daily]),
        "funnel_labels": _json_js(["Qualified", "Low score", "Wrong role", "Location", "Stale", "Dedup", "GPT limit"]),
        "funnel_values": _json_js([
            funnel["qualified"] or 0, funnel["low_score"] or 0, funnel["role"] or 0,
            funnel["location"] or 0, funnel["stale"] or 0, funnel["dedup"] or 0, funnel["gpt_limit"] or 0,
        ]),
        "score_labels": _json_js([r["bucket"] for r in score_dist]),
        "score_values": _json_js([r["cnt"] for r in score_dist]),
        "src_labels": _json_js([r["source"] for r in by_source]),
        "src_qualified": _json_js([r["qualified"] for r in by_source]),
        "src_total": _json_js([r["total"] for r in by_source]),
    })


# ══════════════════════════════════════════════════════════════════════════
# Review UI (SPEC_FRONTEND.md v1.2) — Card Review, Kanban, rejection reasons,
# stats, sources panel. Postgres-only — never calls the Notion API.
# ══════════════════════════════════════════════════════════════════════════

_VALID_SORTS = {"newest", "score", "source"}


@app.get("/review", response_class=HTMLResponse)
def review(request: Request, sort: str = Query(default="newest")):
    _authenticate(request)
    if sort not in _VALID_SORTS:
        sort = "newest"
    jobs = db.get_review_jobs(sort=sort)
    return templates.TemplateResponse(request, "card_review.html", {
        "jobs": jobs, "sort": sort, "active": "review",
    })


ALL_DOMAINS = ["AI/ML"] + list(DOMAIN_COLORS.keys()) + ["Other"]


@app.get("/", response_class=HTMLResponse)
@app.get("/kanban", response_class=HTMLResponse)
def kanban(
    request: Request,
    q: str | None = Query(default=None),
    score_min: str | None = Query(default=None),
    domain: str | None = Query(default=None),
    sort: str = Query(default="newest"),
):
    # Kanban is the landing page (design_handoff_review_ui Turn 5) — "/" and "/kanban"
    # are the same view, not a redirect, so the URL bar reads "/kanban" either way
    # (nav links point at /kanban) and a bookmark to either still works.
    _authenticate(request)
    q = (q or "").strip() or None
    sort = sort if sort in ("newest", "score") else "newest"
    domains = [d for d in (domain or "").split(",") if d in ALL_DOMAINS]
    # score_min is typed as str, not int, so an empty "Min score" field (browsers submit
    # score_min= for a blank number input, not an omitted param) doesn't 422 before this
    # line ever runs — FastAPI's own int coercion has no concept of "blank means unset".
    score_min_val = None
    if score_min:
        try:
            score_min_val = int(score_min)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid score_min: {score_min!r}")
    board = db.get_kanban_jobs(q=q, score_min=score_min_val, domains=domains or None, sort=sort)
    active_total = sum(len(v) for v in board["active"].values())
    # Counts come from rejected_counts, not from the rendered lists: the board draws
    # only the newest slice per stage (db.KANBAN_REJECTED_LIMIT), and the header must
    # still say how many there really are.
    rejected_counts = board["rejected_counts"]
    rejected_total = sum(rejected_counts.values())
    rejected_breakdown = [
        f"from {STATUS_LABELS[s]} {rejected_counts[s]}" for s in FUNNEL_ORDER if rejected_counts[s]
    ]
    active_filters = (1 if score_min_val is not None else 0) + len(domains)
    funnel = db.get_funnel_stats()
    return templates.TemplateResponse(request, "kanban.html", {
        "board": board,
        "funnel_order": FUNNEL_ORDER,
        "funnel": funnel,
        "active_total": active_total,
        "rejected_total": rejected_total,
        "rejected_breakdown": rejected_breakdown,
        "rejected_limit": db.KANBAN_REJECTED_LIMIT,
        "q": q,
        "score_min": score_min_val,
        "domains": domains,
        "all_domains": ALL_DOMAINS,
        "active_filters": active_filters,
        "sort": sort,
        "status_labels": STATUS_LABELS,
        "status_labels_json": json.dumps(STATUS_LABELS),
        "statuses_json": json.dumps(CURRENT_STATUSES),
        "funnel_order_json": json.dumps(FUNNEL_ORDER),
        "reason_labels": REJECTION_REASON_LABELS,
        "active": "kanban",
    })


# One definition of the generation gates for every surface (dashboard button, bot
# button, POST /api/jobs/{id}/resume) — see intake.resume_block_reason. The template
# disables the button with this exact reason; the 422 in the route below is the
# backstop for direct callers.
_resume_block_reason = intake.resume_block_reason


def _resume_slot_context(job: dict, error: str | None = None) -> dict:
    """Everything partials/resume_button.html needs, derived fresh from the row."""
    skeptic_count = 0
    if job.get("resume_run_id"):
        skeptic_count = db.get_resume_skeptic_count(job["resume_run_id"])
    return {
        "job": job,
        "skeptic_count": skeptic_count,
        "resume_block_reason": None if job.get("resume_run_id") else _resume_block_reason(job),
        "resume_error": error,
    }


@app.get("/jobs/{job_id}/detail", response_class=HTMLResponse)
def job_detail(request: Request, job_id: int):
    """Card peek modal (design_handoff_review_ui Turn 4a) — full ATS breakdown as
    an overlay, opened by clicking a kanban card instead of dragging it."""
    _authenticate(request)
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Unknown job id")
    next_status = None
    if job["current_status"] in FUNNEL_ORDER:
        idx = FUNNEL_ORDER.index(job["current_status"])
        if idx + 1 < len(FUNNEL_ORDER):
            next_status = FUNNEL_ORDER[idx + 1]
    return templates.TemplateResponse(request, "partials/job_detail_modal.html", {
        "job": job, "next_status": next_status, "status_labels": STATUS_LABELS,
        "job_id": job_id, "comments": db.get_comments(job_id),
        "company_history": db.get_company_history(job.get("company"), job_id),
        **_resume_slot_context(job),
    })


@app.post("/jobs/{job_id}/merge-into", response_class=HTMLResponse)
def merge_into(request: Request, job_id: int, target_id: str = Form(...)):
    """Fold this card (the duplicate) into target_id (the one kept). Returns a
    toast + reload script — both cards' board positions change."""
    _authenticate(request)
    if not target_id.strip().lstrip("#").isdigit():
        raise HTTPException(status_code=400, detail=f"Not a card number: {target_id!r}")
    try:
        result = db.merge_duplicate(int(target_id.strip().lstrip("#")), job_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    toast = json.dumps(f"Card #{job_id} merged into #{result['kept_id']}").replace("</", "<\\/")
    return HTMLResponse(
        f"<script>window.showToast && window.showToast({toast});"
        f"setTimeout(function(){{location.reload()}}, 600)</script>")


@app.get("/jobs/{job_id}/edit-form", response_class=HTMLResponse)
def edit_job_form(request: Request, job_id: int):
    _authenticate(request)
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Unknown job id")
    return templates.TemplateResponse(request, "partials/edit_job_form.html", {"job": job})


@app.post("/jobs/{job_id}/edit", response_class=HTMLResponse)
def edit_job(
    request: Request,
    job_id: int,
    title: str = Form(...),
    company: str = Form(default=""),
    url: str = Form(default=""),
    apply_url: str = Form(default=""),
    source: str = Form(default=""),
    salary: str = Form(default=""),
    location: str = Form(default=""),
    description: str = Form(default=""),
):
    """Save card edits (2026-08-19). Content fields only; on a scoring-relevant
    change (title/company/JD) the card is re-scored in the background, and a
    changed JD unlinks the previously generated resume (tailored to the old
    text — the artifact itself stays in resume.artifact). Returns the refreshed
    detail modal."""
    _authenticate(request)
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Unknown job id")
    title = title.strip()
    if not title:
        raise HTTPException(status_code=400, detail="Title is required")

    fields = {
        "title": title,
        "company": company.strip() or None,
        "url": url.strip() or None,
        "apply_url": apply_url.strip() or url.strip() or None,
        "source": source.strip() or None,
        "salary": salary.strip() or None,
        "location": location.strip() or None,
        "description": description.strip() or None,
    }
    jd_changed = (fields["description"] or "") != (job.get("description") or "")
    scoring_changed = jd_changed or fields["title"] != (job.get("title") or "") \
        or (fields["company"] or "") != (job.get("company") or "")

    db.update_job_fields(job_id, fields)
    if jd_changed and job.get("resume_run_id"):
        db.clear_resume_run_id(job_id)
    if scoring_changed and (fields["description"] or "").strip():
        threading.Thread(target=_rescore_job_async, args=(job_id,), daemon=True).start()

    return job_detail(request, job_id)


def _rescore_job_async(job_id: int) -> None:
    """Background re-score after an edit — unlike _auto_score_job this runs even
    when a score already exists (the edit made it stale)."""
    try:
        job = db.get_job(job_id)
        if not job or not (job.get("description") or "").strip():
            return
        pipeline_run_id = f"dash-edit-{job_id}-{int(time.time())}"
        result, _error_kind = scoring_client.analyze_via_cloud(job, pipeline_run_id)
        if result is not None:
            db.update_job_scoring(job_id, result, pipeline_run_id)
    except Exception:
        logger.exception(f"Re-score after edit failed for job {job_id}")


@app.post("/jobs/{job_id}/comments", response_class=HTMLResponse)
def add_comment(request: Request, job_id: int, body: str = Form(...)):
    _authenticate(request)
    if not db.get_job(job_id):
        raise HTTPException(status_code=404, detail="Unknown job id")
    body = body.strip()
    if not body:
        raise HTTPException(status_code=400, detail="Empty note")
    db.add_comment(job_id, body[:2000])
    return templates.TemplateResponse(request, "partials/comments_block.html", {
        "job_id": job_id, "comments": db.get_comments(job_id),
    })


@app.delete("/jobs/{job_id}/comments/{comment_id}", response_class=HTMLResponse)
def delete_comment(request: Request, job_id: int, comment_id: int):
    _authenticate(request)
    db.delete_comment(job_id, comment_id)
    return templates.TemplateResponse(request, "partials/comments_block.html", {
        "job_id": job_id, "comments": db.get_comments(job_id),
    })


@app.post("/jobs/{job_id}/resume", response_class=HTMLResponse)
def generate_resume(request: Request, job_id: int):
    """Generate-resume button in the peek modal. Synchronous on purpose — the browser
    waits behind hx-disabled-elt/hx-indicator (~1–2 min), and the swapped-in fragment
    is the Open-resume link, so 'click, wait, open' needs no polling machinery.
    A repeat click (or a second window) finds resume_run_id already set and just gets
    the ready link back without paying for a second generation."""
    _authenticate(request)
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Unknown job id")

    if job.get("resume_run_id"):
        return templates.TemplateResponse(
            request, "partials/resume_button.html", _resume_slot_context(job))

    reason = _resume_block_reason(job)
    if reason:
        raise HTTPException(status_code=422, detail=reason)

    # Reuse the scoring correlation id when the row has one, so the vacancy's score
    # and resume runs line up under a single pipeline_run_id in the cloud tables.
    pipeline_run_id = job.get("pipeline_run_id") or f"dash-{job_id}-{int(time.time())}"
    outcome, error_kind = resume_client.generate_via_cloud(job, pipeline_run_id)
    if outcome is None:
        error = ("Resume service misconfigured (auth/deploy) — check RESUME_TOKEN"
                 if error_kind == "config"
                 else "Generation failed — try again in a minute")
        return templates.TemplateResponse(
            request, "partials/resume_button.html", _resume_slot_context(job, error=error))

    db.set_resume_run_id(job_id, outcome.generation_run_id)
    job["resume_run_id"] = outcome.generation_run_id
    return templates.TemplateResponse(
        request, "partials/resume_button.html", _resume_slot_context(job))


@app.get("/jobs/{job_id}/resume-flags", response_class=HTMLResponse)
def resume_flags(request: Request, job_id: int):
    """Skeptic-flags dialog behind the badge on the Open-resume link: what
    survived the repair round, with the was→now counts."""
    _authenticate(request)
    job = db.get_job(job_id)
    if not job or not job.get("resume_run_id"):
        raise HTTPException(status_code=404, detail="No resume generated for this vacancy")
    flags = db.get_resume_flags(job["resume_run_id"])
    if flags is None:
        raise HTTPException(status_code=404, detail="Resume run not found")
    return templates.TemplateResponse(request, "partials/resume_flags_modal.html", {
        "flags": flags,
    })


@app.get("/jobs/{job_id}/resume-pdf")
def resume_pdf(request: Request, job_id: int):
    """Stream the generated PDF straight from resume.artifact — same Postgres since
    the 2026-08-14 DB merge, so no share-link round-trip through the resume service.
    Cookie-authenticated like every other dashboard route; 'inline' so the browser
    previews with its own download button (same choice as the resume service)."""
    _authenticate(request)
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Unknown job id")
    if not job.get("resume_run_id"):
        raise HTTPException(status_code=404, detail="No resume generated for this vacancy")
    stored = db.get_resume_pdf(job["resume_run_id"])
    if stored is None:
        raise HTTPException(status_code=404, detail="Resume artifact not found")
    pdf_bytes, company = stored
    filename = f"Dimitry Kucher_PM_{company or job['resume_run_id']}.pdf".replace('"', "")
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


@app.post("/jobs/{job_id}/rescore", response_class=HTMLResponse)
def rescore_job(request: Request, job_id: int):
    """Re-score button in the Add-job duplicate dialog — the likeliest reason the
    old card's score looks wrong is that it predates the cloud scorer, so offer a
    fresh verdict right where the stale one is shown. Synchronous like the scraper's
    own scoring call (~10–30 s behind hx-disabled-elt)."""
    _authenticate(request)
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Unknown job id")
    if not (job.get("description") or "").strip():
        return templates.TemplateResponse(request, "partials/rescore_slot.html", {
            "job_id": job_id, "ats_score": job.get("ats_score"),
            "error": "No JD text on this card — nothing to score",
        })
    pipeline_run_id = f"dash-rescore-{job_id}-{int(time.time())}"
    result, _error_kind = scoring_client.analyze_via_cloud(job, pipeline_run_id)
    if result is None:
        return templates.TemplateResponse(request, "partials/rescore_slot.html", {
            "job_id": job_id, "ats_score": job.get("ats_score"),
            "error": "Scoring failed — try again in a minute",
        })
    db.update_job_scoring(job_id, result, pipeline_run_id)
    return templates.TemplateResponse(request, "partials/rescore_slot.html", {
        "job_id": job_id, "ats_score": result.score, "updated": True,
    })


def _auto_score_job(job_id: int) -> None:
    """Background auto-score for hand-added cards (they never met the scraper's
    scoring path, so without this they stay unscored forever). Daemon thread off the
    Add-job request: the toast returns immediately, the score lands on the card by
    the next page load. Any failure is logged and swallowed — an unscored card is
    exactly as usable as before this feature existed."""
    try:
        job = db.get_job(job_id)
        if not job or job.get("ats_score") is not None:
            return
        if not (job.get("description") or "").strip():
            return
        pipeline_run_id = f"dash-add-{job_id}-{int(time.time())}"
        result, _error_kind = scoring_client.analyze_via_cloud(job, pipeline_run_id)
        if result is not None:
            db.update_job_scoring(job_id, result, pipeline_run_id)
    except Exception:
        logger.exception(f"Auto-score failed for job {job_id}")


@app.post("/jobs/{job_id}/status", response_class=HTMLResponse)
def change_job_status(
    request: Request,
    job_id: int,
    new_status: str = Form(...),
    rejection_reason: str | None = Form(default=None),
):
    _authenticate(request)
    try:
        db.update_job_status(job_id, new_status, rejection_reason=rejection_reason)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    job = db.get_job(job_id)
    if new_status == "found" or (job and job["current_status"] == "found"):
        # Still/again in the review list — return the row fragment (HTMX target
        # for /review is a <details> row; /kanban's JS ignores the body on success).
        return templates.TemplateResponse(request, "partials/card.html", {"job": job})
    # Moved out of review — swap to an empty node so the row disappears from /review's list.
    return HTMLResponse(f'<div id="job-{job_id}"></div>')


@app.post("/jobs/bulk-status")
def bulk_change_status(
    request: Request,
    ids: str = Form(...),
    new_status: str = Form(...),
    rejection_reason: str | None = Form(default=None),
):
    """Turn 8 bulk actions (multi-select "Move to" / "Reject all"). update_job_status()
    is per-job with its own connection — there's no single-transaction path here, same
    tolerant-partial-success shape already used by the one-off Notion-sync/stale-cleanup
    maintenance scripts, rather than an all-or-nothing bulk write. The caller reloads the
    board after; this just reports what happened."""
    _authenticate(request)
    job_ids = [int(x) for x in ids.split(",") if x.strip().isdigit()]
    if not job_ids:
        raise HTTPException(status_code=400, detail="No job ids provided")
    updated, errors = 0, []
    for job_id in job_ids:
        try:
            db.update_job_status(job_id, new_status, rejection_reason=rejection_reason, source="bulk")
            updated += 1
        except ValueError as e:
            errors.append({"job_id": job_id, "error": str(e)})
    return {"updated": updated, "errors": errors}


@app.get("/jobs/{job_id}/reject-form", response_class=HTMLResponse)
def reject_form(request: Request, job_id: int):
    _authenticate(request)
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Unknown job id")
    reasons = [(r, REJECTION_REASON_LABELS[r]) for r in USER_REJECTION_REASONS]
    return templates.TemplateResponse(request, "partials/rejection_form.html", {
        "job": job, "reasons": reasons,
    })


@app.get("/jobs/new-form", response_class=HTMLResponse)
def new_job_form(request: Request):
    _authenticate(request)
    return templates.TemplateResponse(request, "partials/add_job_form.html", {})


@app.post("/jobs", response_class=HTMLResponse)
def create_job(
    request: Request,
    title: str = Form(...),
    company: str = Form(default=""),
    url: str = Form(default=""),
    source: str = Form(default=""),
    salary: str = Form(default=""),
    location: str = Form(default=""),
    description: str = Form(default=""),
    force: str = Form(default="0"),
):
    _authenticate(request)
    title = title.strip()
    if not title:
        raise HTTPException(status_code=400, detail="Title is required")

    # A hand-added vacancy is the likeliest of all to already exist (the scraper may
    # have found it first) — warn instead of silently creating a twin. force=1 comes
    # from the warning dialog's "Add anyway".
    if force != "1":
        existing = db.find_manual_duplicate(url.strip() or None, company.strip() or None, title)
        if existing:
            fields = [
                ("title", title), ("company", company), ("url", url), ("source", source),
                ("salary", salary), ("location", location), ("description", description),
            ]
            return templates.TemplateResponse(request, "partials/add_job_duplicate.html", {
                "existing": existing, "fields": fields,
            })

    job_id = db.create_manual_job({
        "title": title,
        "company": company.strip() or None,
        "url": url.strip() or None,
        "apply_url": url.strip() or None,
        "source": source.strip() or "Manual",
        "salary": salary.strip() or None,
        "location": location.strip() or None,
        "description": description.strip() or None,
        "published": None,
    })
    # Auto-score in the background (Release 1): the toast must not wait the ~10–30 s
    # a cloud scoring call takes, and a scoring failure must not fail the Add.
    has_jd = bool(description.strip())
    if has_jd:
        threading.Thread(target=_auto_score_job, args=(job_id,), daemon=True).start()
    # Replaces the dialog (hx-swap=outerHTML on #add-job-dialog): the form vanishes and
    # the swapped-in script fires a toast — no location.reload(), so kanban filters,
    # selection and scroll position survive. The new card appears on the next page load.
    # .replace guards against </script> breakout from a pasted title — json.dumps
    # escapes quotes but not the '</' sequence that would close the script tag.
    toast_text = f"Added: {title}" + (" — scoring in background" if has_jd else "")
    toast_title = json.dumps(toast_text).replace("</", "<\\/")
    return HTMLResponse(f"<script>window.showToast && window.showToast({toast_title})</script>")


def _authenticate_bearer(request: Request) -> None:
    """API-key auth for machine callers (ResumeBuilder Cards service) — same
    DASHBOARD_TOKEN secret as the browser login, sent as a Bearer header instead
    of the cookie, so no extra secret to provision or rotate."""
    if not TOKEN:
        raise HTTPException(status_code=503, detail="Dashboard misconfigured: DASHBOARD_TOKEN not set")
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer ") or not hmac.compare_digest(auth[len("Bearer "):], TOKEN):
        raise HTTPException(status_code=401, detail="Invalid or missing bearer token")


class ApiJobPayload(BaseModel):
    """Body of POST /api/jobs — a vacancy Dimitry applied to via ResumeBuilder."""

    title: str = Field(..., min_length=1)
    company: str | None = None
    url: str | None = None
    description: str | None = None
    source: str = "ResumeBuilder"
    salary: str | None = None
    location: str | None = None
    ats_score: int | None = None
    pipeline_run_id: str | None = None


@app.post("/api/jobs")
def api_create_job(payload: ApiJobPayload, request: Request):
    """Machine twin of the Add-job form for ResumeBuilder's Step 2.5: upserts the
    vacancy and moves it to 'applied'. A URL/company+title match updates the existing
    row (the scraper may well have found the vacancy first) instead of creating a
    twin card on the kanban."""
    _authenticate_bearer(request)
    title = payload.title.strip()
    company = (payload.company or "").strip() or None
    url = (payload.url or "").strip() or None

    existing = db.find_manual_duplicate(url, company, title)
    if existing:
        job_id, created = existing["id"], False
    else:
        job_id = db.create_manual_job(
            {
                "title": title,
                "company": company,
                "url": url,
                "apply_url": url,
                "source": payload.source.strip() or "ResumeBuilder",
                "salary": (payload.salary or "").strip() or None,
                "location": (payload.location or "").strip() or None,
                "description": (payload.description or "").strip() or None,
                "published": None,
            },
            ats_score=payload.ats_score,
            pipeline_run_id=payload.pipeline_run_id,
        )
        created = True
    transition = db.mark_job_applied(job_id, source="resumebuilder")
    return JSONResponse({
        "job_id": job_id,
        "created": created,
        "status": transition["new_status"],
        "previous_status": transition["old_status"],
    })


class ApiIntakePayload(BaseModel):
    """Body of POST /api/intake — one vacancy sent in from outside the pipeline.

    Exactly one of url/text is expected; both are accepted (a caller that has the
    link AND the text, e.g. an iOS Shortcut grabbing the page it is sharing, gets
    the best of both: the text is used, the link is attached to the card).
    """

    url: str | None = None
    text: str | None = None
    force: bool = False
    source: str = "Manual (API)"


@app.post("/api/intake")
def api_intake(payload: ApiIntakePayload, request: Request):
    """The external entry point: URL or pasted JD in, scored card out.

    Synchronous, unlike the Add-job form's background scoring — the caller here is
    a script or a Shortcut that wants the verdict as its answer, not a card that
    quietly acquires a score a minute later. Budget ~10-40 s.

    Resume generation is deliberately NOT part of this call (Dimitry, 2026-08-23):
    POST /api/jobs/{id}/resume is the explicit second step.
    """
    _authenticate_bearer(request)
    raw = (payload.text or "").strip() or (payload.url or "").strip()
    if not raw:
        raise HTTPException(status_code=422, detail="Send either url or text")

    url_hint = (payload.url or "").strip() or None
    result = intake.ingest(
        raw, source=payload.source.strip() or "Manual (API)", force=payload.force,
        # A caller that sent both fields is pasting text for a known link; the link
        # then rides along as the card's URL instead of being thrown away.
        url_hint=url_hint if (payload.text or "").strip() else None,
    )

    body = {
        "status": result.status,
        "job_id": result.job_id,
        "created": result.created,
        "title": result.title,
        "company": result.company,
        "location": result.location,
        "url": result.url,
        "score": result.score,
        "message": result.message,
        "resume_blocked": result.resume_blocked,
    }
    if result.result is not None:
        r = result.result
        body["breakdown"] = {
            "role": r.role_score, "domain": r.domain_score, "keywords": r.keyword_score,
            "location": r.location_score, "penalty": r.penalty,
            "why_apply": r.why_apply, "why_not": r.why_not,
            "matched": r.matched, "missed": r.missed, "domain_label": r.domain,
        }
    if result.status == "duplicate" and result.existing:
        body["existing"] = {k: str(v) for k, v in result.existing.items()}
    # 200 for every outcome the pipeline understands, including "need_text" and
    # "duplicate": those are answers, not failures, and a Shortcut showing an HTTP
    # error instead of "paste the description" would be a worse tool.
    return JSONResponse(body)


@app.post("/api/jobs/{job_id}/resume")
def api_generate_resume(job_id: int, request: Request):
    """Explicit resume generation for an existing card — the machine twin of the
    peek modal's button and of the bot's. Idempotent: a card that already has a
    resume returns the same run id without paying twice."""
    _authenticate_bearer(request)
    resume_run_id, error = intake.generate_resume(job_id)
    if error:
        raise HTTPException(status_code=422, detail=error)
    return JSONResponse({
        "job_id": job_id,
        "resume_run_id": resume_run_id,
        "skeptic_findings": db.get_resume_skeptic_count(resume_run_id),
        "pdf_url": f"/jobs/{job_id}/resume-pdf",
    })


@app.post("/tg/{secret}")
async def telegram_webhook(secret: str, request: Request):
    """Telegram bot webhook — the phone-shaped door into the same intake pipeline.

    Answers 200 before doing any work on purpose: Telegram re-delivers an update it
    has not seen acknowledged within seconds, and one intake takes 10-40 s, so
    replying late would process every vacancy two or three times over.

    Two independent checks, because this route cannot be cookie- or bearer-
    authenticated (Telegram decides what it sends): the secret in the path, and the
    secret Telegram echoes in X-Telegram-Bot-Api-Secret-Token. The chat allowlist in
    tg_bot._allowed() is the third. An unset secret answers 503 rather than falling
    open — this is the one route on the service that is reachable without a login.
    """
    if not TELEGRAM_WEBHOOK_SECRET:
        raise HTTPException(status_code=503, detail="TELEGRAM_WEBHOOK_SECRET not set")
    header = request.headers.get("x-telegram-bot-api-secret-token", "")
    if not (hmac.compare_digest(secret, TELEGRAM_WEBHOOK_SECRET)
            and hmac.compare_digest(header, TELEGRAM_WEBHOOK_SECRET)):
        raise HTTPException(status_code=403, detail="Bad webhook secret")

    try:
        update = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Malformed update")
    tg_bot.start(update)
    return JSONResponse({"ok": True})


@app.get("/jobs/bulk-reject-form", response_class=HTMLResponse)
def bulk_reject_form(request: Request, ids: str = Query(...)):
    """Turn 8b — one reason applied to every selected job; any row can be dropped
    from the list client-side before submitting (see bulk_reject_form.html)."""
    _authenticate(request)
    job_ids = [int(x) for x in ids.split(",") if x.strip().isdigit()]
    jobs = [j for j in (db.get_job(jid) for jid in job_ids) if j]
    if not jobs:
        raise HTTPException(status_code=404, detail="No matching jobs")
    reasons = [(r, REJECTION_REASON_LABELS[r]) for r in USER_REJECTION_REASONS]
    return templates.TemplateResponse(request, "partials/bulk_reject_form.html", {
        "jobs": jobs, "reasons": reasons, "status_labels": STATUS_LABELS,
    })


def _parse_date_range(date_from: str | None, date_to: str | None) -> tuple[str | None, str | None]:
    from datetime import date
    for value in (date_from, date_to):
        if value is None:
            continue
        try:
            date.fromisoformat(value)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid date: {value!r} (expected YYYY-MM-DD)")
    return date_from, date_to


@app.get("/tracker")
def tracker(
    request: Request,
    date_from: str | None = Query(default=None, alias="from"),
    date_to: str | None = Query(default=None, alias="to"),
):
    """Merged into /dashboard (2026-08-17) — permanent redirect keeps old bookmarks
    working, preserving the period filter."""
    params = []
    if date_from:
        params.append(f"from={date_from}")
    if date_to:
        params.append(f"to={date_to}")
    return RedirectResponse(url="/dashboard" + ("?" + "&".join(params) if params else ""), status_code=301)


def _mail_event_hints(event: dict) -> dict:
    """Cards an unmatched mail event might belong to, recomputed at render time.

    The matcher already ran this search when the mail arrived and then threw the
    result away: decide() refuses to pick between two open applications, and the
    queue was left asking for a card number typed from memory (2026-08-27 —
    Guidde/Adapty sat unmatched with both candidates one query away). Nothing new
    is stored: company_hint, from_addr and title_hint are already on the row, so
    this works for events recorded before this screen learned to show them.

    `closed` answers the other half: TryHackMe and Synthesia matched nothing only
    because the card is already rejected, and "no card found" is a misleading way
    to say "you already closed this one"."""
    from mail_agent import sender_domain
    domain = sender_domain(event.get("from_addr") or "")
    args = (event.get("company_hint"), domain, event.get("title_hint"))
    candidates = db.find_cards_for_mail(*args, limit=5)
    closed = []
    if not candidates:
        closed = [c for c in db.find_cards_for_mail(*args, limit=5, include_closed=True)
                  if c.get("current_status") == "rejected"]
    return {"candidates": candidates, "closed": closed}


@app.get("/mail", response_class=HTMLResponse)
def mail_queue(request: Request):
    """Mail agent review queue (2026-08-22). Everything here is a PROPOSAL — no
    card has been touched yet; v1 applies nothing without this screen or the
    Telegram buttons."""
    _authenticate(request)
    events = db.get_pending_mail_events()
    for event in events:
        if not event.get("job_id"):
            event.update(_mail_event_hints(event))
    return templates.TemplateResponse(request, "mail_queue.html", {
        "active": "mail",
        "events": events,
        "statuses": CURRENT_STATUSES,
        "status_labels": STATUS_LABELS,
        "reasons": [(r, REJECTION_REASON_LABELS[r]) for r in USER_REJECTION_REASONS],
    })


def _mail_event_error(event_id: int, message: str) -> HTMLResponse:
    """Inline, swappable error for the mail queue. Kept as a 200 on purpose — see
    the note in resolve_mail_event."""
    safe = html_escape(message)
    return HTMLResponse(
        f'<div id="mail-event-{event_id}" class="mail-event-error">{safe}</div>')


@app.post("/mail/events/{event_id}/resolve", response_class=HTMLResponse)
def resolve_mail_event(
    request: Request,
    event_id: int,
    action: str = Form(...),
    job_id: str = Form(default=""),
    status: str = Form(default=""),
    reason: str = Form(default=""),
):
    """Confirm or dismiss one proposal. Confirming goes through the normal
    db.update_job_status(), so the transition is validated exactly like a manual
    one and lands in status_log with source='mail-agent-confirmed' — which keeps
    the whole feature revertible with a single query."""
    _authenticate(request)
    if action not in ("confirmed", "dismissed"):
        raise HTTPException(status_code=400, detail=f"Unknown action: {action!r}")
    jid = int(job_id) if job_id.strip().lstrip("#").isdigit() else None
    # A user mistake answers with 200 and a visible reason, not 400: HTMX does not
    # swap a 4xx response, so the old behaviour left the button looking dead. Real
    # protocol errors (an unknown action above) still raise.
    if action == "confirmed" and not jid:
        return _mail_event_error(event_id, "Нужен номер карточки — впиши #id и нажми снова")
    try:
        db.resolve_mail_event(event_id, action, job_id=jid,
                              status=status or None, reason=reason or None)
    except ValueError as e:
        return _mail_event_error(event_id, str(e))
    verb = "применено" if action == "confirmed" else "пропущено"
    return HTMLResponse(
        f'<div id="mail-event-{event_id}" style="font-size:12.5px;color:var(--color-neutral-600);'
        f'padding:8px 0">Событие #{event_id} — {verb}.</div>')


@app.get("/applied-today", response_class=HTMLResponse)
def applied_today(request: Request):
    """Table behind the dashboard's Applied-today stat — today's applications with
    posting links and kanban-card deep links."""
    _authenticate(request)
    return templates.TemplateResponse(request, "partials/applied_today_modal.html", {
        "jobs": db.get_applied_today(),
    })


@app.get("/stats/rejection-reasons", response_class=HTMLResponse)
def stats_rejection_reasons(
    request: Request,
    date_from: str | None = Query(default=None, alias="from"),
    date_to: str | None = Query(default=None, alias="to"),
):
    _authenticate(request)
    date_from, date_to = _parse_date_range(date_from, date_to)
    counts = db.get_rejection_reason_counts(date_from, date_to)
    return templates.TemplateResponse(request, "partials/stats_rejection_reasons.html", {
        "counts": counts, "reason_labels": REJECTION_REASON_LABELS,
    })


@app.get("/stats/conversion", response_class=HTMLResponse)
def stats_conversion(
    request: Request,
    date_from: str | None = Query(default=None, alias="from"),
    date_to: str | None = Query(default=None, alias="to"),
):
    _authenticate(request)
    date_from, date_to = _parse_date_range(date_from, date_to)
    stats = db.get_conversion_stats(date_from, date_to)
    return templates.TemplateResponse(request, "partials/stats_conversion.html", {"stats": stats})


@app.get("/sources", response_class=HTMLResponse)
def sources_panel(request: Request):
    _authenticate(request)
    sources = db.get_sources_summary()
    return templates.TemplateResponse(request, "partials/sources_panel.html", {
        "sources": sources,
    })


@app.post("/sources/{name}/toggle", response_class=HTMLResponse)
def toggle_source_route(request: Request, name: str):
    _authenticate(request)
    if not db.toggle_source(name):
        raise HTTPException(status_code=404, detail=f"Unknown source: {name}")
    sources = db.get_sources_summary()
    return templates.TemplateResponse(request, "partials/sources_panel.html", {
        "sources": sources,
    })


# The scraper's Railway cron, mirrored here so the staleness check knows when a run
# was actually due: "7 6,9,12,15 * * 1-5" — four times a day, Monday to Friday, UTC.
# Keep in sync with the service's cronSchedule; a copy is unavoidable (Railway holds
# the schedule, the dashboard has to reason about it) but a wrong copy only ever
# makes the banner early or late, never touches data.
SCRAPE_CRON_HOURS_UTC = (6, 9, 12, 15)
SCRAPE_CRON_WEEKDAYS = (0, 1, 2, 3, 4)  # datetime.weekday(): Mon=0 .. Fri=4
SCRAPE_GRACE_MINUTES = 90  # a slot counts as missed only this long after it fires


def _last_due_run(now: datetime) -> datetime | None:
    """The most recent cron slot that should already have produced a run.

    Replaces a flat 20-hour threshold, which called every weekend a failure: the
    gap between Friday 15:07 and Monday 06:07 UTC is 63 hours by design, so the
    banner cried wolf every Saturday and Sunday (seen live 2026-08-30) and trained
    the eye to ignore the one signal that should mean the cron is actually stuck."""
    candidate = now - timedelta(minutes=SCRAPE_GRACE_MINUTES)
    for _ in range(14):  # two weeks back is far more than any legitimate gap
        if candidate.weekday() in SCRAPE_CRON_WEEKDAYS:
            due = [h for h in SCRAPE_CRON_HOURS_UTC if h <= candidate.hour]
            if due:
                return candidate.replace(hour=max(due), minute=0, second=0, microsecond=0)
        candidate = (candidate - timedelta(days=1)).replace(hour=23, minute=59)
    return None


@app.get("/api/last-run")
def api_last_run(request: Request):
    """Turn 7g auto-refresh + Turn 7c stale-scraper banner — polled every 5 min from
    base.html. The run id answers "did a new run just land"; `stale` answers "has the
    scraper gone quiet", and is computed here rather than in the browser because only
    the server knows the schedule the answer depends on."""
    _authenticate(request)
    run = db.get_last_run()
    if not run:
        return {"run_id": None, "qualified": 0, "hours_ago": None, "stale": False}
    finished = datetime.fromisoformat(run["finished_at"])
    if finished.tzinfo is None:
        finished = finished.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    hours_ago = (now - finished).total_seconds() / 3600
    due = _last_due_run(now)
    return {"run_id": run["id"], "qualified": run["qualified"],
            "hours_ago": round(hours_ago, 1), "stale": bool(due and finished < due)}


_CSV_VIEWS = {"tracker", "vacancies", "rejections"}


@app.get("/export.csv")
def export_csv(
    request: Request,
    view: str = Query(...),
    date_from: str | None = Query(default=None, alias="from"),
    date_to: str | None = Query(default=None, alias="to"),
):
    """Turn 7f / SPEC_UPDATES.md §12 — streamed CSV, no library beyond stdlib csv.
    Three views: tracker (weekly series, respects the same from/to as /tracker),
    vacancies (every qualified job + its current status), rejections (rejected jobs
    + reason). date_from/date_to only apply to the tracker view — the other two are
    always all-time (matches "All vacancies" / "Rejection log" in the 7f mock, which
    carry no period selector)."""
    _authenticate(request)
    if view not in _CSV_VIEWS:
        raise HTTPException(status_code=400, detail=f"Unknown export view: {view!r} (expected one of {sorted(_CSV_VIEWS)})")
    date_from, date_to = _parse_date_range(date_from, date_to)

    buf = io.StringIO()
    writer = csv.writer(buf)
    if view == "tracker":
        series = db.get_tracker_series(date_from, date_to)
        writer.writerow(["week", "found", "applied", "replies"])
        for i, week in enumerate(series["weeks"]):
            writer.writerow([week, series["found"][i], series["applied"][i], series["replies"][i]])
    elif view == "vacancies":
        rows = _query("""
            SELECT title, company, source, domain, ats_score, current_status, rejection_reason,
                   logged_at, applied_at, published, url
            FROM jobs WHERE outcome = 'qualified' ORDER BY logged_at DESC
        """)
        writer.writerow(["title", "company", "source", "domain", "ats_score", "status",
                          "rejection_reason", "found_at", "applied_at", "published", "url"])
        for r in rows:
            writer.writerow([r["title"], r["company"], r["source"], r["domain"], r["ats_score"],
                              r["current_status"], r["rejection_reason"], r["logged_at"],
                              r["applied_at"], r["published"], r["url"]])
    else:  # rejections
        rows = _query("""
            SELECT title, company, source, domain, ats_score, rejection_reason, logged_at
            FROM jobs WHERE current_status = 'rejected' ORDER BY logged_at DESC
        """)
        writer.writerow(["title", "company", "source", "domain", "ats_score", "rejection_reason", "found_at"])
        for r in rows:
            writer.writerow([r["title"], r["company"], r["source"], r["domain"], r["ats_score"],
                              r["rejection_reason"], r["logged_at"]])

    buf.seek(0)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv", headers={
        "Content-Disposition": f'attachment; filename="{view}.csv"',
    })


@app.exception_handler(StarletteHTTPException)
async def _http_exception_handler(request: Request, exc: StarletteHTTPException):
    # Only intercept Starlette's own "no route matched" 404 — every deliberate
    # `raise HTTPException(404, "...")` inside a route (unknown job id, unknown
    # source, htmx fragment targets) keeps its plain JSON body; those are read by
    # nothing but `r.ok`/`event.detail.successful` checks in the page JS, and several
    # are hit by htmx fragment calls that were never meant to render a full page.
    if exc.status_code == 404 and exc.detail == "Not Found":
        return templates.TemplateResponse(request, "error.html", {
            "code": 404,
            "heading": "Page not found",
            "message": "This link doesn't match anything here — it may be old, or the vacancy was removed in a stale-cleanup pass.",
        }, status_code=404)
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)


@app.exception_handler(Exception)
async def _unhandled_exception_handler(request: Request, exc: Exception):
    logger.exception(f"Unhandled error on {request.method} {request.url.path}")
    return templates.TemplateResponse(request, "error.html", {
        "code": 500,
        "heading": "Something broke",
        "message": "The database is unreachable or something failed unexpectedly. Data is safe — try again in a minute.",
    }, status_code=500)
