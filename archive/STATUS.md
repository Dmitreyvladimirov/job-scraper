# STATUS.md

## Current state

JobScraper is running in production on Railway with:

- a cron scraper running Monday through Friday, four times per day;
- a separate dashboard service;
- Postgres as the single source of truth, replacing the older Notion-only
  model;
- sources including Himalayas, Remotive, Jobicy, WeWorkRemotely, RemoteOK,
  Arbeitnow, Jobgether, and Telegram channels.

ATS scoring, deduplication, and URL enrichment are active in production.
The repo already has a clear split between:

- fetch/source code in `sources/`;
- normalization/filtering in `filters.py` and `utils.py`;
- scoring in `ats.py`;
- Notion write-paths in `notion_client.py`;
- dashboard/reporting in `dashboard.py`.

## Architectural decision

The decision to build a custom frontend instead of using Notion as the write
surface was made on 2026-07-02. The rollout is phased:

1. manual review of scraper-found cards;
2. kanban movement by status;
3. rejection reasons and review metadata;
4. stats and funnel visibility.

This is not a blank-slate frontend project. The current plan is to extend the
existing `dashboard.py` / Postgres-backed surface rather than inventing a
second stack if the current one can absorb the workflow.

## Current emphasis

- Keep scraper behaviour stable while refining source quality.
- Improve Telegram parsing where it loses company/title metadata.
- Treat Postgres as the read-side source of truth for analytics.
- Keep Notion as the existing write-surface until the new UI demonstrates
  value.
- Make sure any data-model change is reflected in `design.md` first.

## Active roadmap areas

- `TASK-001`: Lever / Ashby `_company_match()` fix.
- `TASK-003`: workflow dispatch env fix.
- `TASK-004`..`TASK-006`: Telegram parsing improvements for known channel
  formats.
- `TASK-007`..`TASK-011`: company-direct source and discovery work.
- `TASK-012`..`TASK-015`: ResumeBuilder / Postgres sync and reapplication
  guard.
- `TASK-019`: per-channel Telegram counters in the dashboard.
- `TASK-022`: resolve the WorkSearch / JobPostBot / frontend boundary.

## Known gaps

- Some Telegram channels still need per-channel counters.
- Company-direct sourcing is still a roadmap item.
- The broader ResumeBuilder/Postgres sync is not yet fully implemented.
- The exact boundary between JobScraper and WorkSearch frontends is still an
  open architectural question.

## Operational reminders

- The current task is tracked in `CONTEXT.md`.
- If a future change needs an architectural decision, update `design.md`
  first, not just `STATUS.md`.
