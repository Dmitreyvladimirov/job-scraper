# CLAUDE.md

Project notes for Claude Code sessions.

## Purpose

- Keep the working state obvious for the next agent.
- Capture project-specific notes that are useful during implementation.
- Stay lighter than `AGENTS.md`, which is the project constitution.
- Point future work at the right docs instead of relying on memory.

## Current repo state

- Active project: JobScraper
- Main focus: job sourcing, Telegram parsing, Notion writes, dashboard/reporting
- Source of truth for task state: `CONTEXT.md`
- Source of truth for scope: `requirements.md`, `design.md`, `tasks.md`
- Source of truth for roadmap and tradeoffs: `ROADMAP.md`
- The repo currently contains restored working docs plus a few active code deltas.

## Current workstreams

1. Source quality and parsing
   - Keep Telegram parsing improvements in `sources/telegram_channels.py`.
   - Keep `jobgether` as a candidate source if it proves useful.
   - Continue with source-specific fixes before broad redesigns.

2. ResumeBuilder / frontend direction
   - The repo has a documented plan for `db_manual.py`, `sync_notion.py`, and
     a status history model in Postgres.
   - The larger frontend question is still split between JobScraper and the
     WorkSearch umbrella context.
   - `dashboard.py` is already the seed of the custom frontend discussion.

3. Dashboard / analytics
   - The dashboard is the read-side analytics layer.
   - The long-term plan is to extend it carefully rather than inventing a
     second UI stack if the existing one can absorb the workflow.

## Working rules

- Read `CONTEXT.md` first for the live task.
- Prefer `requirements.md`, `design.md`, and `tasks.md` over memory.
- Do not delete or rewrite user-authored project docs without explicit approval.
- Keep changes scoped to the task at hand.
- If a change would touch data model or workflow contracts, verify against
  `design.md` before editing code.

## Relevant open threads

- `TASK-001`: add `_company_match()` to Lever and Ashby branches in
  `utils.py::find_apply_url()`.
- `TASK-003`: pass `DATABASE_URL` in GitHub Actions workflow dispatch.
- `TASK-004`..`TASK-006`: Telegram parsing fixes for known channel formats.
- `TASK-012`..`TASK-015`: ResumeBuilder / Postgres status history and
  reapplication guard.
- `TASK-019`: per-channel Telegram counters in the dashboard.
- `TASK-022`: resolve the WorkSearch / JobPostBot / frontend boundary.

## Notes

- This file is intentionally a working note, not a second constitution.
- If it ever grows into repeated policy text, move that policy to `AGENTS.md`
  instead of duplicating it here.
