# SPEC_FRONTEND.md — required updates (from design review, 2026-08-03)

Apply these deltas to `SPEC_FRONTEND.md` v1.1 before Phase 2 (Plan).

## 1. Visual style criterion is stale
Success criterion "Visual style matches FRONTEND_DESIGN_BRIEF.md (dark theme, same palette as current dashboard.py)" — **replace**. Decision 2026-08-02: full redesign on the Classical design system (light, editorial serif), with an optional **dark theme toggle** (see 2a) that reuses the same layout with flipped tokens. New criterion:
- Visual style matches the handoff package (`design_handoff_review_ui/`), Classical tokens from `styles.css`; dark mode = one body class + localStorage, no separate templates.

## 2. New field: `penalty_reason`
Card Review shows *why* the ATS penalty was applied (e.g. "seniority mismatch — posting asks for Director level").
- `jobs.penalty_reason TEXT NULL` (additive, same `ADD COLUMN IF NOT EXISTS` convention).
- Source: already produced by ATS scoring (`ats.py`) — persist it in `db.log_job()` alongside `why_apply` (same "Ask first" caution: touches the scraper write path).
- NULL for pre-v1.1 rows → UI hides the line (degraded state already designed).

## 3. New surface: Sources panel (2b) — scope addition
Read: per-source fetched/qualified/yield/applied + 30d trend (queryable from existing `runs.sources_json` + `jobs.source`).
Write: **enable/disable a source for future scrape runs** — this exceeds the current read-write scope (which only touches `current_status`/`rejection_reason`).
- Suggested: `sources_config` table (`source TEXT PRIMARY KEY, enabled BOOLEAN DEFAULT TRUE, updated_at TIMESTAMP`) + `POST /sources/{name}/toggle`; `scraper.py` reads it at run start.
- "Ask first" item: this touches scraper behavior — confirm before wiring.

## 4. Stats period filter (2c) — route change
All stats fragments accept a shared range: `?from=YYYY-MM-DD&to=YYYY-MM-DD` (presets 7d/30d/quarter/all are client-side sugar for the same params). Applies to `/stats/rejection-reasons` and any new stats fragment (qualified→applied, per-source). One period bar → one HTMX request re-rendering the stats container.

## 5. Qualified→applied stat — make explicit
Brief lists it; spec routes don't. Add `GET /stats/conversion` (fragment): overall % + per-month breakdown, computed from `status_log` (first transition to `applied`) over qualified count.

## 6. Route note for the review screen
`/review` lists only `current_status='found'` (confirmed). Cards auto-rejected by the pipeline (`geo_restricted_auto`) must NOT appear in `/review` — they appear in Kanban "Rejected" with the read-only tag. (An early mock showed one in review; fixed.)

## 7. Tests to add (matching the above)
- `penalty_reason` NULL-tolerant rendering.
- `POST /sources/{name}/toggle` — unknown source → 404; disabled source excluded by scraper run (unit-level).
- Stats routes respect `from`/`to` params; invalid dates → 400.
