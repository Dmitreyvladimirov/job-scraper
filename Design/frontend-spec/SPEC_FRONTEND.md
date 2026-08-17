# Spec: JobScraper Review UI (Card Review + Kanban + Stats)

> Phase 1 (Specify) — `agent-skills:spec-driven-development`. Scope confirmed
> 2026-07-15 (see `tasks.md` TASK-022, `CONTEXT.md` Divergences, three
> clarifying questions answered same session). Extends `design.md`/
> `requirements.md`/`tasks.md` (v1.0 → v1.1) rather than replacing them.
>
> **v1.2 (2026-08-03)** — visual design pass via Claude Design
> (`design_handoff_review_ui/`), deltas applied per `SPEC_UPDATES.md`. All
> "Open Questions" from v1.1 are resolved below (see each section); this is
> now Phase 2 (Plan) output as well as Phase 1.

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
│   ├── base.html            ← shared layout (Classical design system, dark-mode
│   │                            toggle via one body class + localStorage)
│   ├── card_review.html     ← 1a Register: full-width rows, inline expand
│   ├── kanban.html          ← 1f desktop 7-column board + 1j mobile bottom sheet
│   └── partials/
│       ├── card.html        ← single card fragment (HTMX swap target)
│       ├── rejection_form.html   ← 1h dialog
│       ├── stats_rejection_reasons.html  ← 1l
│       ├── stats_conversion.html         ← 1l qualified→applied
│       └── sources_panel.html            ← 2b
├── static/                  ← NEW
│   ├── htmx.min.js
│   └── classical.css        ← ported from design_handoff_review_ui/_ds/classical-*/styles.css
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
| `penalty_reason` | TEXT | NULL | **v1.2 addition.** Why the ATS hard-requirement penalty fired (e.g. "seniority mismatch — posting asks for Director level"), shown under the Penalty line in the breakdown. **Correction during implementation:** `ats.py` did not actually produce this (only a binary 0/15 `penalty`) — added a `penalty_reason` field to its JSON schema/`ATSResult`, same pattern as the existing `location_reason`. NULL for pre-v1.2 rows → UI hides the line. |
| `role_score`, `domain_score`, `domain_value_score`, `domain_exp_score`, `keyword_score`, `location_score` | INTEGER | NULL | **v1.2 addition, found during implementation.** The v1.1 spec never persisted the per-axis breakdown — only the total `ats_score` — but Card Review (1a/1c) shows it. All six already exist on `ATSResult`; wired through `scraper.py`'s `db.log_job()` calls alongside the other v1.2 fields. NULL for pre-v1.2 rows → breakdown block hidden (degraded state). |
| `location_reason` | TEXT | NULL | **v1.2 addition.** Already computed by `ats.py` (added earlier, TASK-026) but never persisted — now stored alongside `location_score` for the same breakdown display. |
| `salary` | TEXT | NULL | **v1.2 addition, found during implementation.** Every source already produces `job["salary"]`; never persisted. Card Review (1a/1b) shows it next to company. Read automatically off `job` inside `log_job()`, no new kwarg needed at call sites. |

`why_not` already exists in `jobs` — not duplicated.

**Decision (v1.2, was "Open Question" in v1.1):** `matched_keywords`/`missed_keywords` stay `JSONB` — the stats screen needs to query/filter by keyword, which plain `TEXT` can't support. Accepted as a deliberate, isolated deviation from `runs.sources_json`'s `TEXT` convention; not applied retroactively to `runs`.

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

### New table — `sources_config` (v1.2 addition, Sources panel / 2b)

```sql
CREATE TABLE IF NOT EXISTS sources_config (
    source     TEXT PRIMARY KEY,
    enabled    BOOLEAN DEFAULT TRUE,
    updated_at TIMESTAMP DEFAULT NOW()
);
```

Read side (fetched/qualified/yield%/applied-from + 30d trend) is a query over
existing `runs.sources_json` + `jobs.source` — no new columns needed for that.
The write side (enable/disable a source for **future** scrape runs) is new
behavior for `scraper.py`: at run start, read `sources_config`, skip any
source with `enabled=false` from `sources_data`. Already-found vacancies from
a disabled source are untouched — this only gates future fetches.

**Ask first** (added to Boundaries below): wiring the toggle into `scraper.py`'s
actual run-start behavior touches the production cron path, same caution as
the `db.log_job()` change.

## API / Routes (additions to `dashboard.py`)

| Method | Path | Returns | Purpose |
|---|---|---|---|
| GET | `/review` | full page | Card Review list, `current_status='found'` **only** — cards the pipeline auto-rejected (`rejection_reason='geo_restricted_auto'`) must NOT appear here; they show up in Kanban's "Rejected" column with a read-only tag instead (an early design mock showed one in Review — fixed, v1.2). |
| GET | `/kanban` | full page | Board view, all statuses as columns (desktop: 1f drag board; mobile ≤390px: 1j bottom-sheet status picker, same route/data) |
| POST | `/jobs/{id}/status` | HTML fragment (updated card) | Change `current_status`; body includes `rejection_reason` when target is `rejected` — **400 if missing/invalid for that transition** |
| GET | `/jobs/{id}/reject-form` | HTML fragment | Rejection reason selector (HTMX-loaded into a modal/inline slot) |
| GET | `/stats/rejection-reasons` | HTML fragment | Bar chart data, extends existing `/dashboard` KPI page |
| GET | `/stats/conversion` | HTML fragment | **v1.2 addition.** Qualified→applied %, overall + per-month, computed from `status_log` (first transition to `applied`) over qualified count. Listed in `FRONTEND_DESIGN_BRIEF.md` but missing from v1.1's route table — now explicit. |
| POST | `/sources/{name}/toggle` | HTML fragment | **v1.2 addition.** Flips `sources_config.enabled`. Unknown source name → 404. |

**v1.2 addition — shared stats period param:** `/stats/rejection-reasons`,
`/stats/conversion`, and any future stats fragment accept
`?from=YYYY-MM-DD&to=YYYY-MM-DD`. The 2c period bar's presets (7d/30d/quarter/
all time) are client-side sugar that compute these two params — one HTMX
request re-renders the stats container per selection, not one request per
chart. Invalid/malformed dates → 400.

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
- **v1.2 additions:**
  - `penalty_reason` NULL-tolerant rendering (pre-v1.1 rows don't break the template)
  - `POST /sources/{name}/toggle` — unknown source → 404; a disabled source is excluded from `sources_data` at the next scraper run (unit-level, not a live run)
  - Stats routes respect `from`/`to` params; invalid dates → 400

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
  timing, don't run mid-scrape. Note: this repo's local `.env` `DATABASE_URL`
  points at the same production instance (no separate dev DB), so even a
  local `db.init_db()` test run touches production.
- Any change to `scraper.py`'s existing `db.log_job()` call signature (adding
  `why_apply`/keywords/`penalty_reason` args) — touches the production write
  path the cron job depends on 4×/day
- **v1.2 addition:** wiring `sources_config` into `scraper.py`'s run-start
  source selection — same production-cron caution as above

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
- [x] ~~Visual style matches `FRONTEND_DESIGN_BRIEF.md` (dark theme, same
      palette as current `dashboard.py`)~~ **Superseded (v1.2, decision
      2026-08-02):** full redesign on the Classical design system (light,
      editorial serif — Cormorant Garamond/Lora, warm near-white ground,
      color as stroke not fill), with an optional dark-mode toggle (2a) that
      flips the same tokens rather than a separate template. New criterion:
      visual style matches `design_handoff_review_ui/`, tokens from
      `static/classical.css`; dark mode = one `body` class + `localStorage`.
- [ ] **v1.2 addition:** dark-mode toggle persists across reload (`localStorage`)
      and flips tokens without a page reload/separate template
- [ ] **v1.2 addition:** Sources panel (2b) shows per-source fetched/qualified/
      yield%/applied-from + 30d sparkline; toggling a source off excludes it
      from the next scraper run (not from already-found vacancies)

## Open Questions — resolved in v1.2 (design review 2026-08-03)

All four v1.1 open questions are now resolved by the `design_handoff_review_ui/`
package; kept here for history, not re-litigated:

- ~~Drag-and-drop kanban mechanics on mobile~~ → **1j**, tap status chip →
  bottom-sheet picker (no drag). 1k (swipe actions) kept in the design file
  as an optional alternative but needs an undo affordance not yet designed —
  not building 1k in this pass.
- ~~Detail view vs. list-only with inline expand~~ → **list-only with inline
  expand** (1a — Notion-style row expand; no separate single-vacancy page)
- ~~Exact ATS breakdown visualization~~ → **1c**, plain color-coded numbers,
  plus the new `penalty_reason` line under Penalty. (1d hairline bars / 1e
  dot-leader ledger kept in the design file for reference, not built.)
- ~~`JSONB` for keywords~~ → confirmed, see Data Model Changes above.

---

**Gate:** v1.1 was Phase 1 output; this v1.2 revision folds in the completed
Phase 2 design pass (dependency ordering below). Next step is implementation
per `tasks.md` (new TASK entries), respecting the three "Ask first" items above.
