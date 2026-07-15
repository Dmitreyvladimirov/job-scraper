# Spec: JobScraper Review UI (Card Review + Kanban)

> Phase 1 (Specify) — `agent-skills:spec-driven-development`. Scope confirmed
> 2026-07-15 (see `tasks.md` TASK-022, `CONTEXT.md` Divergences, three
> clarifying questions answered same session). Extends `design.md`/
> `requirements.md`/`tasks.md` (v1.0 → v1.1) rather than replacing them.

## Objective

Extend the existing `dashboard.py` (FastAPI + Chart.js, read-only, deployed on
Railway) into a read-write tool for the two ROADMAP-approved scenarios:

1. **Card Review** — replace manual review of scraper-found vacancies in
   Notion with an in-app view showing the full ATS breakdown, matched/missed
   keywords, and why-apply/why-not reasoning.
2. **Kanban** — move a card through the application funnel
   (`found → applied → recruiter_reply → screen → interview → offer` /
   `rejected`), with a **mandatory reason** when marking `rejected`.

**User:** Dimitry, sole user, personal job search tool. No multi-tenant
concerns.

**Success looks like:** Dimitry stops opening Notion to review scraper
output and to move cards through the funnel; both happen in this UI instead,
backed by Postgres as source of truth for this new surface (Notion stays
untouched — this build never writes to Notion).

**Explicitly out of scope (confirmed 2026-07-15):**
- Manual URL entry (stays in `JobPostBot`, separate service — not modified
  here, not merged with this build)
- Notion sync in either direction (`resume_version`, `notion_id`, full
  `status_log.source='notion_sync'` — that's REQ-111/112, a separate,
  larger integration deliberately deferred)
- Email/recruiter-timing tracking

## Tech Stack

Extends the existing stack, two new pieces:

| Component | Choice | Why |
|---|---|---|
| Language | Python 3.12+ | existing |
| Web framework | FastAPI | existing (`dashboard.py`) |
| Templates | **Jinja2** (new dependency) | `dashboard.py` currently builds one big f-string HTML page. HTMX partial updates (kanban drag, inline status change, rejection form) need composable templates — f-strings don't scale to that. Jinja2 is the standard HTMX pairing, still zero build-tool. |
| Interactivity | **HTMX** (new, CDN script tag, no npm) | Per `FRONTEND_DESIGN_BRIEF.md` — server-rendered fragments, not a SPA/React |
| DB driver | psycopg2 | existing |
| Charts | Chart.js | existing |
| Testing | **pytest** (new) | project has no test framework today; adding minimal, not full coverage |

## Commands

```
Dev:   uvicorn dashboard:app --reload --host 0.0.0.0 --port 8000
Test:  pytest tests/ -v
Prod:  same as today — SERVICE_TYPE=dashboard → run.sh → uvicorn (no --reload)
```

No build step (Railway deploys the repo directly, same as today). No linter
currently configured in this project — not introducing one in this spec;
follow existing code style by convention (see below).

## Project Structure (additions only)

```
JobScraper/
├── dashboard.py            ← existing, gains new routes (see API section)
├── templates/               ← NEW
│   ├── base.html            ← shared layout (dark theme, matches current CSS)
│   ├── card_review.html
│   ├── kanban.html
│   └── partials/
│       ├── card.html        ← single card fragment (HTMX swap target)
│       ├── rejection_form.html
│       └── stats_rejection_reasons.html
├── static/                  ← NEW, if HTMX isn't loaded via CDN
│   └── htmx.min.js
├── tests/                   ← NEW
│   ├── test_status_transitions.py
│   ├── test_rejection_reason.py
│   └── test_dashboard_routes.py
└── db.py                    ← existing, gains new columns/tables (below)
```

## Data Model Changes

Extends `db.py::init_db()` — additive, idempotent (`ALTER TABLE ... ADD
COLUMN IF NOT EXISTS`, matching the project's existing `CREATE TABLE IF NOT
EXISTS` convention; no new migrations framework).

### `jobs` — new columns

| Column | Type | Default | Notes |
|---|---|---|---|
| `why_apply` | TEXT | NULL | currently only in `ATSResult`, never persisted — `scraper.py` must be updated to pass it to `db.log_job()` |
| `matched_keywords` | JSONB | NULL | array of strings. **Deviation from existing convention**: `runs.sources_json` uses plain `TEXT` storing `json.dumps()`; using `JSONB` here instead because Card Review needs to query/filter by keyword later (stats screen) — `JSONB` supports that, `TEXT` doesn't. Flag for review. |
| `missed_keywords` | JSONB | NULL | same as above |
| `current_status` | TEXT | `'found'` | kanban state. Legal values: `found, applied, recruiter_reply, screen, interview, offer, rejected` — validated at the application layer (`filters.py`-style constant list in `config.py`), not a DB `CHECK` constraint (project has none today, staying consistent) |
| `rejection_reason` | TEXT | NULL | one of the 6 categories below. Required (app-level validation, 400 if missing) whenever `current_status` is set to `rejected` |

`why_not` already exists in `jobs` — not duplicated.

### Rejection reason — 6 categories (confirmed, `FRONTEND_DESIGN_BRIEF.md`)

```
low_score_after_review   Плохо подошла по скорингу после проверки
remote_one_country       Ремоут, но только в одной стране
not_remote_at_all        Вакансия не удалённая вообще
inactive_closed          Вакансия неактивна / закрыта
bad_in_general           Вакансия плохая в принципе
geo_restricted_auto      GEO_RESTRICTED (авто-LLM) — set programmatically,
                          not user-selectable in the rejection form; shown
                          as a read-only tag when present
```

### New table — `status_log`

```sql
CREATE TABLE IF NOT EXISTS status_log (
    id SERIAL PRIMARY KEY,
    job_id INTEGER NOT NULL REFERENCES jobs(id),
    old_status TEXT,
    new_status TEXT NOT NULL,
    changed_at TIMESTAMP DEFAULT NOW(),
    source TEXT DEFAULT 'manual'  -- always 'manual' in this scope; 'notion_sync' is REQ-112, out of scope here
);
CREATE INDEX IF NOT EXISTS idx_status_log_job_id ON status_log(job_id);
```

**Deliberately NOT included** (belongs to REQ-111/112, out of scope): `notion_id`,
`source_type`, `resume_version`, `deleted_at`, `sync_meta` table, any Notion
API write calls.

## API / Routes (additions to `dashboard.py`)

| Method | Path | Returns | Purpose |
|---|---|---|---|
| GET | `/review` | full page | Card Review list, `current_status='found'` |
| GET | `/kanban` | full page | Board view, all statuses as columns |
| POST | `/jobs/{id}/status` | HTML fragment (updated card) | Change `current_status`; body includes `rejection_reason` when target is `rejected` — **400 if missing/invalid for that transition** |
| GET | `/jobs/{id}/reject-form` | HTML fragment | Rejection reason selector (HTMX-loaded into a modal/inline slot) |
| GET | `/stats/rejection-reasons` | HTML fragment | Bar chart data, extends existing `/dashboard` KPI page |

All routes keep the existing `?token=` query-param auth (`_check_token()`,
unchanged).

## Code Style

Follow existing conventions verbatim — no new patterns introduced:

```python
# Parametrized SQL, never string-interpolated (matches db.py throughout)
cur.execute(
    "UPDATE jobs SET current_status = %s, rejection_reason = %s WHERE id = %s",
    (new_status, rejection_reason, job_id),
)

# Type hints on every function signature (matches utils.py/filters.py/db.py)
def validate_rejection_reason(reason: str | None) -> bool:
    return reason in REJECTION_REASONS

# Logging via the standard `logging` module, f-string messages (matches scraper.py)
logger.info(f"Status changed: job={job_id} {old_status} -> {new_status}")

# Comments only where non-obvious — no restating what the code already shows
```

## Testing Strategy

`pytest`, `tests/` directory, **minimal, not exhaustive** — per project scale
(solo tool, not a team codebase needing regression armor everywhere):

- `test_status_transitions.py` — pure-function tests: which `current_status`
  transitions are legal (e.g. `offer → found` should be rejected), reuses
  the same style as existing `filters.py` logic tests would look like if they
  existed
- `test_rejection_reason.py` — `rejected` without a valid reason → rejected;
  all 6 valid reasons accepted; `geo_restricted_auto` not settable via the
  user-facing form endpoint
- `test_dashboard_routes.py` — FastAPI `TestClient`, smoke-test each new
  route returns 200 with a valid token, 401/403 without

Not testing: HTML rendering pixel-fidelity, Chart.js output, HTMX swap
mechanics (manual browser check per `run` skill before shipping).

## Boundaries

**Always:**
- Parametrized SQL for every query touching new tables/columns
- Validate `rejection_reason` server-side against the 6-value list — never
  trust the client dropdown alone
- Run `pytest tests/ -v` before considering a task in this spec done
- Keep every write scoped to Postgres — this build **never** calls the
  Notion API

**Ask first:**
- Running the `ALTER TABLE`/`CREATE TABLE` migration against the production
  Railway Postgres (same DB the live scraper cron writes to) — confirm
  timing, don't run mid-scrape
- Any change to `scraper.py`'s existing `db.log_job()` call signature (adding
  `why_apply`/keywords args) — touches the production write path the cron
  job depends on 4×/day

**Never:**
- Write to the Notion API from any code introduced by this spec
- Auto-transition `current_status` without an explicit user action (no
  inferred/automatic kanban moves in this scope — `geo_restricted_auto` is
  the one exception, and it's set by the existing ResumeBuilder pipeline
  script, not by this dashboard code)
- Touch `jobs.description`, `ats_score`, `outcome`, or any column the
  scraper pipeline already writes — this build only adds new columns, never
  modifies the meaning of existing ones

## Success Criteria

- [ ] `/review` renders real scraper-found vacancies (`current_status='found'`)
      with ATS breakdown + matched/missed keywords sourced from Postgres —
      zero Notion API calls at render time
- [ ] Moving a card to any status via `/jobs/{id}/status` persists
      `current_status` and appends one row to `status_log`
- [ ] Marking `rejected` without `rejection_reason` returns 400; all 6 reasons
      are selectable except `geo_restricted_auto`
- [ ] `/stats/rejection-reasons` shows a rejection-reason breakdown chart on
      the existing `/dashboard` page
- [ ] All current `/dashboard` functionality (KPIs, existing 4 charts, top
      companies, recent runs table) continues to work unchanged
- [ ] `pytest tests/ -v` passes
- [ ] Visual style matches `FRONTEND_DESIGN_BRIEF.md` (dark theme, same
      palette as current `dashboard.py`)

## Open Questions (carried from `FRONTEND_DESIGN_BRIEF.md`, unresolved — Phase 2/3 territory, not blocking Phase 1 approval)

- Drag-and-drop kanban mechanics on mobile (vs. dropdown/swipe fallback)
- Detail view (single vacancy page) vs. list-only with inline expand
- Exact visualization of the 4-axis ATS breakdown (bars vs. radar vs. plain numbers)
- `JSONB` for keywords is a deviation from the existing `TEXT`-stores-JSON
  convention (`runs.sources_json`) — confirm this is acceptable before Phase 4

---

**Gate:** this is Phase 1 output. Per the skill's workflow, next step is your
review/approval, then Phase 2 (Plan) — dependency ordering, what's built
first, risk notes — before breaking into `tasks.md` TASK entries and writing
any code.
