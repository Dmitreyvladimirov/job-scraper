"""FastAPI dashboard — reads from Postgres and serves analytics charts."""
import hmac
import json
import os
from html import escape
from pathlib import Path
from urllib.parse import quote, urlparse

import psycopg2
import psycopg2.extras
from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
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


def _check_token(token: str):
    if not TOKEN:
        # Belt-and-suspenders: startup should already have refused to boot without a
        # token, but never fail open on a protected endpoint if it somehow got here.
        raise HTTPException(status_code=503, detail="Dashboard misconfigured: DASHBOARD_TOKEN not set")
    if not hmac.compare_digest(token, TOKEN):
        raise HTTPException(status_code=403, detail="Invalid token")


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
def root(token: str = Query(default="")):
    return HTMLResponse(f'<meta http-equiv="refresh" content="0;url=/dashboard?token={quote(token, safe="")}">')


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(token: str = Query(default="")):
    _check_token(token)

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
        SELECT started_at, total_fetched, qualified,
               rejected_low_score as low_score,
               filtered_role as role, filtered_stale as stale,
               filtered_dedup as dedup, gpt_calls,
               sources_json
        FROM runs ORDER BY id DESC LIMIT 15
    """)

    top_companies = _query("""
        SELECT company, COUNT(*) as cnt, MAX(ats_score) as top_score
        FROM jobs WHERE outcome='qualified' AND company IS NOT NULL
        GROUP BY company ORDER BY cnt DESC, top_score DESC LIMIT 10
    """)

    # --- serialize for JS (values come from scraped sources — treat as untrusted) ---
    daily_labels = _json_js([str(r["day"]) for r in daily])
    daily_qualified = _json_js([r["qualified"] for r in daily])
    daily_fetched = _json_js([r["fetched"] for r in daily])

    funnel_labels = _json_js(["Qualified", "Low score", "Wrong role", "Location", "Stale", "Dedup", "GPT limit"])
    funnel_values = _json_js([
        funnel["qualified"] or 0, funnel["low_score"] or 0, funnel["role"] or 0,
        funnel["location"] or 0, funnel["stale"] or 0, funnel["dedup"] or 0, funnel["gpt_limit"] or 0,
    ])

    src_labels = _json_js([r["source"] for r in by_source])
    src_qualified = _json_js([r["qualified"] for r in by_source])
    src_total = _json_js([r["total"] for r in by_source])

    score_labels = _json_js([r["bucket"] for r in score_dist])
    score_values = _json_js([r["cnt"] for r in score_dist])

    # --- recent runs table rows ---
    rows_html = ""
    for r in recent_runs:
        src = json.loads(r["sources_json"]) if r["sources_json"] else {}
        src_str = " · ".join(f"{k}: {v}" for k, v in src.items() if v)
        rows_html += f"""<tr>
            <td>{escape(str(r['started_at'])[:16])}</td>
            <td>{r['total_fetched']}</td>
            <td class="green">{r['qualified']}</td>
            <td>{r['low_score']}</td>
            <td>{r['role']}</td>
            <td>{r['stale']}</td>
            <td>{r['dedup']}</td>
            <td>{r['gpt_calls']}</td>
            <td class="small">{escape(src_str)}</td>
        </tr>"""

    top_html = "".join(
        f"<tr><td>{escape(r['company'])}</td><td class='green'>{r['cnt']}</td><td>{r['top_score']}</td></tr>"
        for r in top_companies
    )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>JobScraper Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
          background: #0f1117; color: #e0e0e0; padding: 24px; }}
  h1 {{ font-size: 20px; font-weight: 600; margin-bottom: 24px; color: #fff; }}
  h2 {{ font-size: 13px; font-weight: 500; color: #888; text-transform: uppercase;
        letter-spacing: .05em; margin-bottom: 12px; }}
  .kpis {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; margin-bottom: 32px; }}
  .kpi {{ background: #1c1f2b; border-radius: 10px; padding: 20px; }}
  .kpi .val {{ font-size: 36px; font-weight: 700; color: #fff; line-height: 1; }}
  .kpi .label {{ font-size: 12px; color: #666; margin-top: 6px; }}
  .charts {{ display: grid; grid-template-columns: 1fr 1fr; gap: 20px; margin-bottom: 32px; }}
  .chart-box {{ background: #1c1f2b; border-radius: 10px; padding: 20px; }}
  .chart-box canvas {{ max-height: 220px; }}
  .wide {{ grid-column: 1 / -1; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  th {{ text-align: left; color: #555; font-weight: 500; padding: 8px 10px;
        border-bottom: 1px solid #2a2d3a; }}
  td {{ padding: 8px 10px; border-bottom: 1px solid #1a1d28; color: #ccc; }}
  tr:last-child td {{ border-bottom: none; }}
  .green {{ color: #4ade80; font-weight: 600; }}
  .small {{ font-size: 11px; color: #555; }}
  .section {{ background: #1c1f2b; border-radius: 10px; padding: 20px; margin-bottom: 20px; }}
  @media(max-width:700px) {{ .kpis,.charts {{ grid-template-columns: 1fr 1fr; }} }}
</style>
</head>
<body>
<h1>JobScraper Dashboard</h1>

<div class="kpis">
  <div class="kpi"><div class="val">{totals['runs']}</div><div class="label">Total runs</div></div>
  <div class="kpi"><div class="val green">{totals['qualified']}</div><div class="label">Qualified jobs</div></div>
  <div class="kpi"><div class="val">{totals['fetched']}</div><div class="label">Total fetched</div></div>
  <div class="kpi"><div class="val">{totals['gpt_calls']}</div><div class="label">GPT calls used</div></div>
</div>

<div class="charts">
  <div class="chart-box wide">
    <h2>Qualified jobs per day</h2>
    <canvas id="dailyChart"></canvas>
  </div>
  <div class="chart-box">
    <h2>Filter funnel (total)</h2>
    <canvas id="funnelChart"></canvas>
  </div>
  <div class="chart-box">
    <h2>ATS score distribution</h2>
    <canvas id="scoreChart"></canvas>
  </div>
  <div class="chart-box wide">
    <h2>Source performance</h2>
    <canvas id="sourceChart"></canvas>
  </div>
</div>

<div class="section">
  <h2>Top companies found</h2>
  <table>
    <tr><th>Company</th><th>Times found</th><th>Top ATS</th></tr>
    {top_html}
  </table>
</div>

<div class="section">
  <h2>Recent runs</h2>
  <table>
    <tr><th>Time (UTC)</th><th>Fetched</th><th>Qualified</th><th>Low score</th>
        <th>Role</th><th>Stale</th><th>Dedup</th><th>GPT</th><th>Sources</th></tr>
    {rows_html}
  </table>
</div>

<div class="section" hx-get="/stats/rejection-reasons?token={quote(token, safe="")}" hx-trigger="load" hx-swap="innerHTML">
  <h2>Rejection reasons</h2>
</div>

<div class="section" hx-get="/stats/conversion?token={quote(token, safe="")}" hx-trigger="load" hx-swap="innerHTML">
  <h2>Qualified → applied</h2>
</div>

<div class="section" hx-get="/sources?token={quote(token, safe="")}" hx-trigger="load" hx-swap="innerHTML">
  <h2>Sources</h2>
</div>

<script src="https://unpkg.com/htmx.org@1.9.12/dist/htmx.min.js"
        integrity="sha384-ujb1lZYygJmzgSwoxRggbCHcjc0rB2XoQrxeTUQyRjrOnlCoYta87iKBWq3EsdM2"
        crossorigin="anonymous"></script>
<link rel="stylesheet" href="/static/classical.css">
<script>
const C = (id, cfg) => new Chart(document.getElementById(id), cfg);
const grid = {{ color: '#2a2d3a' }};
const font = {{ color: '#888' }};

C('dailyChart', {{
  type: 'bar',
  data: {{
    labels: {daily_labels},
    datasets: [
      {{ label: 'Fetched', data: {daily_fetched}, backgroundColor: '#2a2d3a', yAxisID: 'y2' }},
      {{ label: 'Qualified', data: {daily_qualified}, backgroundColor: '#4ade80', yAxisID: 'y' }},
    ]
  }},
  options: {{ responsive: true, scales: {{
    y:  {{ grid, ticks: font, position: 'left',  title: {{ display:true, text:'Qualified', color:'#888' }} }},
    y2: {{ grid: {{drawOnChartArea:false}}, ticks: font, position: 'right', title: {{ display:true, text:'Fetched', color:'#888' }} }},
    x:  {{ grid, ticks: font }}
  }}, plugins: {{ legend: {{ labels: {{ color:'#888' }} }} }} }}
}});

C('funnelChart', {{
  type: 'bar',
  data: {{
    labels: {funnel_labels},
    datasets: [{{ data: {funnel_values},
      backgroundColor: ['#4ade80','#f87171','#fb923c','#60a5fa','#a78bfa','#94a3b8','#475569'] }}]
  }},
  options: {{ indexAxis:'y', responsive:true, plugins:{{ legend:{{display:false}} }},
    scales: {{ x: {{ grid, ticks: font }}, y: {{ grid, ticks: font }} }} }}
}});

C('scoreChart', {{
  type: 'bar',
  data: {{
    labels: {score_labels},
    datasets: [{{ data: {score_values}, backgroundColor: '#818cf8' }}]
  }},
  options: {{ responsive:true, plugins:{{ legend:{{display:false}} }},
    scales: {{ x: {{ grid, ticks: font }}, y: {{ grid, ticks: font }} }} }}
}});

C('sourceChart', {{
  type: 'bar',
  data: {{
    labels: {src_labels},
    datasets: [
      {{ label: 'Total fetched', data: {src_total}, backgroundColor: '#2a2d3a' }},
      {{ label: 'Qualified', data: {src_qualified}, backgroundColor: '#4ade80' }},
    ]
  }},
  options: {{ responsive:true, scales: {{
    x: {{ grid, ticks: font }}, y: {{ grid, ticks: font }}
  }}, plugins: {{ legend: {{ labels: {{ color:'#888' }} }} }} }}
}});
</script>
</body>
</html>"""
    return HTMLResponse(html)


# ══════════════════════════════════════════════════════════════════════════
# Review UI (SPEC_FRONTEND.md v1.2) — Card Review, Kanban, rejection reasons,
# stats, sources panel. Postgres-only — never calls the Notion API.
# ══════════════════════════════════════════════════════════════════════════

_VALID_SORTS = {"newest", "score", "source"}


@app.get("/review", response_class=HTMLResponse)
def review(request: Request, token: str = Query(default=""), sort: str = Query(default="newest")):
    _check_token(token)
    if sort not in _VALID_SORTS:
        sort = "newest"
    jobs = db.get_review_jobs(sort=sort)
    return templates.TemplateResponse(request, "card_review.html", {
        "jobs": jobs, "sort": sort, "token": token, "active": "review",
    })


@app.get("/kanban", response_class=HTMLResponse)
def kanban(request: Request, token: str = Query(default="")):
    _check_token(token)
    jobs_by_status = db.get_kanban_jobs()
    return templates.TemplateResponse(request, "kanban.html", {
        "jobs_by_status": jobs_by_status,
        "statuses": CURRENT_STATUSES,
        "status_labels": STATUS_LABELS,
        "status_labels_json": json.dumps(STATUS_LABELS),
        "statuses_json": json.dumps(CURRENT_STATUSES),
        "reason_labels": REJECTION_REASON_LABELS,
        "token": token, "active": "kanban",
    })


@app.post("/jobs/{job_id}/status", response_class=HTMLResponse)
def change_job_status(
    request: Request,
    job_id: int,
    token: str = Query(default=""),
    new_status: str = Form(...),
    rejection_reason: str | None = Form(default=None),
):
    _check_token(token)
    try:
        db.update_job_status(job_id, new_status, rejection_reason=rejection_reason)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    job = db.get_job(job_id)
    if new_status == "found" or (job and job["current_status"] == "found"):
        # Still/again in the review list — return the row fragment (HTMX target
        # for /review is a <details> row; /kanban's JS ignores the body on success).
        return templates.TemplateResponse(request, "partials/card.html", {"job": job, "token": token})
    # Moved out of review — swap to an empty node so the row disappears from /review's list.
    return HTMLResponse(f'<div id="job-{job_id}"></div>')


@app.get("/jobs/{job_id}/reject-form", response_class=HTMLResponse)
def reject_form(request: Request, job_id: int, token: str = Query(default="")):
    _check_token(token)
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Unknown job id")
    reasons = [(r, REJECTION_REASON_LABELS[r]) for r in USER_REJECTION_REASONS]
    return templates.TemplateResponse(request, "partials/rejection_form.html", {
        "job": job, "reasons": reasons, "token": token,
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
    request: Request, token: str = Query(default=""),
    date_from: str | None = Query(default=None, alias="from"),
    date_to: str | None = Query(default=None, alias="to"),
):
    _check_token(token)
    date_from, date_to = _parse_date_range(date_from, date_to)
    counts = db.get_rejection_reason_counts(date_from, date_to)
    return templates.TemplateResponse(request, "partials/stats_rejection_reasons.html", {
        "counts": counts, "reason_labels": REJECTION_REASON_LABELS,
    })


@app.get("/stats/conversion", response_class=HTMLResponse)
def stats_conversion(
    request: Request, token: str = Query(default=""),
    date_from: str | None = Query(default=None, alias="from"),
    date_to: str | None = Query(default=None, alias="to"),
):
    _check_token(token)
    date_from, date_to = _parse_date_range(date_from, date_to)
    stats = db.get_conversion_stats(date_from, date_to)
    return templates.TemplateResponse(request, "partials/stats_conversion.html", {"stats": stats})


@app.get("/sources", response_class=HTMLResponse)
def sources_panel(request: Request, token: str = Query(default="")):
    _check_token(token)
    sources = db.get_sources_summary()
    return templates.TemplateResponse(request, "partials/sources_panel.html", {
        "sources": sources, "token": token,
    })


@app.post("/sources/{name}/toggle", response_class=HTMLResponse)
def toggle_source_route(request: Request, name: str, token: str = Query(default="")):
    _check_token(token)
    if not db.toggle_source(name):
        raise HTTPException(status_code=404, detail=f"Unknown source: {name}")
    sources = db.get_sources_summary()
    return templates.TemplateResponse(request, "partials/sources_panel.html", {
        "sources": sources, "token": token,
    })
