# CLAUDE.md

Project notes for Claude Code sessions.

## Purpose

- Keep the working state obvious for the next agent.
- Capture project-specific notes that are useful during implementation.
- Stay lighter than `AGENTS.md`, which is the project constitution.
- Point future work at the right docs instead of relying on memory.

## Current repo state

- Active project: JobScraper + resumebuilder-cloud (merged 2026-08-14: shared
  Postgres, cloud scoring in shadow mode, unified plan)
- Source of truth for the plan and roadmap: `PROJECT.md` (consolidated
  2026-08-17 from ROADMAP/BACKLOG/tasks/requirements — those live in `archive/`)
- Source of truth for session state: `CONTEXT.md` (Resume block = active track)
- Scraper tech spec: `SPEC.md` (reference); dashboard spec:
  `Design/frontend-spec/SPEC_FRONTEND.md` (reference, mostly implemented)
- Source audit: `SOURCES_DECISION.md` (reference; template for new sources)
- Cloud services: `/Users/DimaKu/Documents/Coding/resumebuilder-cloud`
  (scoring/cards/resume; specs in its `docs/`)

## Current workstreams

See `PROJECT.md` → "Активный трек". Short version:
1. Scoring cutover (`USE_CLOUD_SCORING=1`, `ATS_THRESHOLD` 60→70) — pending
   Dimitry's go after the Saturday shadow report.
2. Release 1 resume features (Generate-resume button, auto-score on Add-job,
   Re-score in duplicate dialog) — pending Dimitry's go.
3. Release 2 auto-generation (score ≥80 gates) — after cutover.
4. Phase 4 input-data fixes, Phase 5 cleanup (~2026-08-28).

## Working rules

- Read `CONTEXT.md` first for the live task; `PROJECT.md` for the roadmap.
- Do not delete or rewrite user-authored project docs without explicit approval
  (standing rule in `CONTEXT.md` — docs were wrongly deleted once on 2026-07-07).
- Keep changes scoped to the task at hand.
- If a change touches data model or workflow contracts, verify against
  `Design/design.md` / `SPEC_FRONTEND.md` before editing code.
- Scraper is live (cron 4×/день пн-пт) — never break the run path; feature
  flags + shadow patterns are the established rollout style.

## Notes

- This file is intentionally a working note, not a second constitution.
- If it ever grows into repeated policy text, move that policy to `AGENTS.md`
  instead of duplicating it here.
