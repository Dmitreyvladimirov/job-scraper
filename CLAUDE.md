# CLAUDE.md

Project notes for Claude Code sessions.

## Purpose

- Keep the working state obvious for the next agent.
- Capture project-specific notes that are useful during implementation.
- Stay lighter than `AGENTS.md`, which is the project constitution.
- Point future work at the right docs instead of relying on memory.

## Current repo state

- Active project: JobScraper + resumebuilder-cloud (merged 2026-08-14: shared
  Postgres; cloud scoring CUT OVER 2026-08-17 — USE_CLOUD_SCORING=1,
  ATS_THRESHOLD=70, core/ats.py kept as rollback until Phase 5)
- Source of truth for the plan and roadmap: `PROJECT.md` (consolidated
  2026-08-17 from ROADMAP/BACKLOG/tasks/requirements — those live in `archive/`)
- Source of truth for session state: `CONTEXT.md` (Resume block = active track)
- Scraper tech spec: `SPEC.md` (reference); dashboard spec:
  `Design/frontend-spec/SPEC_FRONTEND.md` (reference, mostly implemented)
- Source audit: `SOURCES_DECISION.md` (reference; template for new sources)
- Светочка (personal assistant, separate repo `svetochka` when it exists):
  `SPEC_SVETA.md` is the accepted spec, `SVETOCHKA.md` the research behind it.
  No code yet — code is written only against the spec, stage Э0 first.
- Cloud services: `/Users/DimaKu/Documents/Coding/resumebuilder-cloud`
  (scoring/cards/resume; specs in its `docs/`)

## Current workstreams

See `PROJECT.md` → "Активный трек". Short version:
1. Scoring cutover, Release 1, Release 2, Phase 4, company_direct — all DONE and
   live. Rollback for scoring: `USE_CLOUD_SCORING=shadow` + revert threshold commit.
2. **Two finished features are waiting on deployment, not on code:**
   - External intake (2026-08-23): `core/intake.py` + `core/tg_bot.py`,
     `POST /api/intake`, `POST /tg/{secret}`. Needs 5 env vars on the Dashboard
     service and one run of `scripts/set_telegram_webhook.py`.
   - Mail agent (2026-08-22): `core/mail_agent.py`. Needs its own Railway cron
     service (`SERVICE_TYPE=mail`), Gmail OAuth via `scripts/mint_gmail_token.py`,
     and an `ANTHROPIC_API_KEY` — which no JobScraper service currently has.
3. Phase 5 cleanup (~2026-08-28): drop `core/ats.py`, `OPENAI_API_KEY`,
   `RESUME_MD` in `run.sh`, Postgres-hQr0; decide on the Notion write.

## Working rules

- Read `CONTEXT.md` first for the live task; `PROJECT.md` for the roadmap.
- Do not delete or rewrite user-authored project docs without explicit approval
  (standing rule in `CONTEXT.md` — docs were wrongly deleted once on 2026-07-07).
- Keep changes scoped to the task at hand.
- All documentation committed to git is in English (Dimitry's standing rule,
  2026-09-03). Bot-facing strings and quoted user messages stay in their
  original language — they are data, not documentation.
- If a change touches data model or workflow contracts, verify against
  `Design/design.md` / `SPEC_FRONTEND.md` before editing code.
- Scraper is live (cron 4×/день пн-пт) — never break the run path; feature
  flags + shadow patterns are the established rollout style.

## Notes

- This file is intentionally a working note, not a second constitution.
- If it ever grows into repeated policy text, move that policy to `AGENTS.md`
  instead of duplicating it here.
