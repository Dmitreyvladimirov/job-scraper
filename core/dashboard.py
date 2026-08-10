"""FastAPI dashboard — reads from Postgres and serves analytics charts."""
import csv
import hmac
import io
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import psycopg2
import psycopg2.extras
from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException

import db
from config import CURRENT_STATUSES, DOMAIN_COLORS, FUNNEL_ORDER, REJECTION_REASON_LABELS, USER_REJECTION_REASONS

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
    """Split a possibly-compound "X | Y" domain string (legacy rows scored before
    domain classification was tightened to a single value) into individual tags.
    AI/ML — the candidate's primary target domain — keeps the existing accent-gold
    tag-accent styling; anything in DOMAIN_COLORS (config.py) gets its own oklch hue;
    anything else (incl. "Other") falls back to a plain neutral tag."""
    if not domain:
        return []
    tags = []
    for name in (d.strip() for d in domain.split("|")):
        if not name:
            continue
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
    rejected_total = sum(len(v) for v in board["rejected"].values())
    rejected_breakdown = [
        f"from {STATUS_LABELS[s]} {len(board['rejected'][s])}" for s in FUNNEL_ORDER if board["rejected"][s]
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


@app.get("/tracker", response_class=HTMLResponse)
def tracker(
    request: Request,
    date_from: str | None = Query(default=None, alias="from"),
    date_to: str | None = Query(default=None, alias="to"),
):
    """Design Turn 6 — replaces the manual Notion tracker. Weekly Found/Applied/Replies
    series (period-scoped) plus the Turn 9a funnel efficiency section (all-time)."""
    _authenticate(request)
    date_from, date_to = _parse_date_range(date_from, date_to)
    series = db.get_tracker_series(date_from, date_to)
    funnel = db.get_funnel_stats()

    from datetime import date as _date, timedelta as _timedelta
    today = _date.today()
    presets = {
        "7 weeks": str(today - _timedelta(weeks=7)),
        "Quarter": str(today - _timedelta(days=90)),
    }

    return templates.TemplateResponse(request, "tracker.html", {
        "active": "tracker",
        "date_from": date_from,
        "date_to": date_to,
        "presets": presets,
        "series": series,
        "funnel": funnel,
        "status_labels": STATUS_LABELS,
        "weeks_json": _json_js(series["weeks"]),
        "found_json": _json_js(series["found"]),
        "applied_json": _json_js(series["applied"]),
        "replies_json": _json_js(series["replies"]),
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


@app.get("/api/last-run")
def api_last_run(request: Request):
    """Turn 7g auto-refresh + Turn 7c stale-scraper banner — polled every 5 min from
    base.html. hours_ago lets the client decide both "did a new run just land" (id
    changed since page load) and "has the scraper gone quiet" (hours_ago past a
    threshold), without a second endpoint."""
    _authenticate(request)
    run = db.get_last_run()
    if not run:
        return {"run_id": None, "qualified": 0, "hours_ago": None}
    finished = datetime.fromisoformat(run["finished_at"])
    if finished.tzinfo is None:
        finished = finished.replace(tzinfo=timezone.utc)
    hours_ago = (datetime.now(timezone.utc) - finished).total_seconds() / 3600
    return {"run_id": run["id"], "qualified": run["qualified"], "hours_ago": round(hours_ago, 1)}


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
