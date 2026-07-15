# IDEAS.md

Loose backlog of product ideas, technical improvements, and future directions.
This file is intentionally broader and less formal than `ROADMAP.md`.

## Product ideas

- Company research after a high ATS score:
  - when `score >= 75`, collect 2 to 3 concrete company facts;
  - surface them in Notion or the future frontend;
  - keep the facts short and useful, not a generic summary.
- Salary tracking:
  - parse salary ranges from job descriptions;
  - store numeric values for filtering and trend checks;
  - keep source text alongside parsed values for auditability.
- Expanded rejection taxonomy:
  - distinguish "bad score", "not remotely global", "inactive", and
    "bad company";
  - align dashboard analytics with actual rejection causes.
- Per-channel quality counters:
  - track fetched → role pass → qualified → accepted/rejected;
  - use the counters to identify dead channels and strong channels.
- Better source-level diagnostics:
  - keep source-specific notes when a parser is brittle;
  - write down whether a fix is a parser improvement or an upstream source
    quality issue.

## Workflow ideas

- Instant alert path for layer-1 companies:
  - if a targeted company produces a qualified role, alert immediately;
  - do not wait for the regular batch cadence.
- Retry-friendly source fetching:
  - treat transient 403s and flaky ATS pages as retryable;
  - distinguish "retry later" from "source is dead".
- Canary source rollout:
  - add a new source behind a small, measurable slice first;
  - promote only after a couple of weeks of signal collection.

## Larger direction

- The unified custom frontend instead of Notion as the write-surface for
  JobScraper + ResumeBuilder was already decided on 2026-07-02.
- The current stance is incremental rollout, not a rewrite:
  - keep the existing pipelines working;
  - move review and status-management into the custom UI gradually;
  - keep Notion as the old surface until the replacement proves itself.
- The current working theory is that `dashboard.py` is the natural place to
  grow that UI, unless the WorkSearch umbrella decision says otherwise.

## Open questions

- Which company-research idea is worth shipping first?
- Should salary parsing be strict extraction or best-effort enrichment?
- Which rejection reasons are actually worth instrumenting in the first pass?
- How much of the Notion flow should survive once the custom frontend is live?
- Is `jobgether` valuable enough to keep as a permanent source?

## Non-goals

- Do not turn this file into another roadmap.
- Do not use it to duplicate concrete task checklists that already belong in
  `tasks.md`.
