"""FastAPI dashboard — reads from Postgres and serves analytics charts."""
import hmac
import json
import os
import time
from pathlib import Path
from urllib.parse import urlparse

import psycopg2
import psycopg2.extras
from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import db
from config import CURRENT_STATUSES, REJECTION_REASON_LABELS, USER_REJECTION_REASONS

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
def login_submit(token: str = Form(...)):
    if not TOKEN:
        raise HTTPException(status_code=503, detail="Dashboard misconfigured: DASHBOARD_TOKEN not set")
    _check_login_rate_limit()
    if not hmac.compare_digest(token, TOKEN):
        _login_failures.append(time.time())
        raise HTTPException(status_code=403, detail="Invalid token")
    redirect = RedirectResponse(url="/dashboard", status_code=302)
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


@app.get("/", response_class=HTMLResponse)
def root():
    return RedirectResponse(url="/dashboard", status_code=302)


def _is_company_artifact(name: str | None) -> bool:
    """Some sources (LinkedIn-derived postings) leak consent-banner/UI text into the
    company field instead of a real name -- flag anything implausibly long or that
    names the platform itself, so the template can de-emphasize it instead of hiding
    it (the row is still a real qualified job, just with an unreliable company label)."""
    if not name:
        return True
    return len(name) > 50 or "linkedin" in name.lower()


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request):
    _authenticate(request)

    # --- data queries ---
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


@app.get("/kanban", response_class=HTMLResponse)
def kanban(request: Request):
    _authenticate(request)
    jobs_by_status = db.get_kanban_jobs()
    return templates.TemplateResponse(request, "kanban.html", {
        "jobs_by_status": jobs_by_status,
        "statuses": CURRENT_STATUSES,
        "status_labels": STATUS_LABELS,
        "status_labels_json": json.dumps(STATUS_LABELS),
        "statuses_json": json.dumps(CURRENT_STATUSES),
        "reason_labels": REJECTION_REASON_LABELS,
        "active": "kanban",
    })


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
