"""FastAPI dashboard — reads from Postgres and serves analytics charts."""
import json
import os

import psycopg2
import psycopg2.extras
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse

DATABASE_URL = os.environ.get("DATABASE_URL", "")
TOKEN = os.environ.get("DASHBOARD_TOKEN", "")

app = FastAPI()


@app.get("/health")
def health():
    return {"status": "ok"}


def _check_token(token: str):
    if TOKEN and token != TOKEN:
        raise HTTPException(status_code=403, detail="Invalid token")


def _query(sql: str, params=()) -> list[dict]:
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


@app.get("/", response_class=HTMLResponse)
def root(token: str = Query(default="")):
    return HTMLResponse(f'<meta http-equiv="refresh" content="0;url=/dashboard?token={token}">')


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

    # --- serialize for JS ---
    daily_labels = json.dumps([str(r["day"]) for r in daily])
    daily_qualified = json.dumps([r["qualified"] for r in daily])
    daily_fetched = json.dumps([r["fetched"] for r in daily])

    funnel_labels = json.dumps(["Qualified", "Low score", "Wrong role", "Location", "Stale", "Dedup", "GPT limit"])
    funnel_values = json.dumps([
        funnel["qualified"] or 0, funnel["low_score"] or 0, funnel["role"] or 0,
        funnel["location"] or 0, funnel["stale"] or 0, funnel["dedup"] or 0, funnel["gpt_limit"] or 0,
    ])

    src_labels = json.dumps([r["source"] for r in by_source])
    src_qualified = json.dumps([r["qualified"] for r in by_source])
    src_total = json.dumps([r["total"] for r in by_source])

    score_labels = json.dumps([r["bucket"] for r in score_dist])
    score_values = json.dumps([r["cnt"] for r in score_dist])

    # --- recent runs table rows ---
    rows_html = ""
    for r in recent_runs:
        src = json.loads(r["sources_json"]) if r["sources_json"] else {}
        src_str = " · ".join(f"{k}: {v}" for k, v in src.items() if v)
        rows_html += f"""<tr>
            <td>{str(r['started_at'])[:16]}</td>
            <td>{r['total_fetched']}</td>
            <td class="green">{r['qualified']}</td>
            <td>{r['low_score']}</td>
            <td>{r['role']}</td>
            <td>{r['stale']}</td>
            <td>{r['dedup']}</td>
            <td>{r['gpt_calls']}</td>
            <td class="small">{src_str}</td>
        </tr>"""

    top_html = "".join(
        f"<tr><td>{r['company']}</td><td class='green'>{r['cnt']}</td><td>{r['top_score']}</td></tr>"
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
