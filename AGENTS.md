# AGENTS.md

═══════════════════════════════════════════════════════════
PROJECT CONSTITUTION — JOBSCRAPER
Rewritten 2026-07-15 — `great_cto` removed 2026-07-06, replaced by
standalone `agent-skills` + named subagents (see below).
═══════════════════════════════════════════════════════════

## Session context

  0. Read `CONTEXT.md` first — it has the active task and session state
  1. Read `ROADMAP.md` for scope/priority context, `requirements.md` +
     `design.md` + `tasks.md` for the current spec (generated via
     `agent-skills:spec-driven-development`)

HARD CONSTRAINTS (no exceptions):
  ✗ Never implement requirements not explicitly defined in requirements.md
  ✗ Never alter the data model without updating design.md first
  ✗ Never mark a task [x] without verifying its acceptance criterion
  ✗ Never create files not listed or implied in design.md
  ✗ Never guess when a requirement is ambiguous — ask instead
  ✗ Never delete or archive project docs without explicit user approval

AFTER COMPLETING A TASK:
  1. Run the verification step listed in tasks.md (or acceptance criterion)
  2. Mark the task [x] in tasks.md
  3. Update CONTEXT.md resume block with the next active task
  4. Record any divergences from design.md in CONTEXT.md "Divergences from spec"

IF IMPLEMENTATION MUST DEVIATE FROM DESIGN:
  1. Stop immediately
  2. Describe the conflict clearly
  3. Wait for explicit approval
  4. Update design.md FIRST, then implement
═══════════════════════════════════════════════════════════

## Project context

- **Type:** solo tool, single user (Dimitry), personal job search automation
- **Stack:** Python 3.12+, FastAPI (dashboard), Postgres (Railway), OpenAI
  GPT-4o-mini (ATS scoring), Telegram Bot API, Notion API
- **Owners:** Dimitry (sole)

## Default dev pipeline (from global CLAUDE.md — applies to all coding projects)

`great_cto`'s gated multi-agent pipeline (`architect → pm → senior-dev →
qa-engineer → security-officer`) is replaced by standalone `agent-skills` +
named subagents:

| Stage | Current tool |
|---|---|
| Spec / requirements | `agent-skills:spec-driven-development` (`/spec`) |
| Task breakdown | `agent-skills:planning-and-task-breakdown` (`/plan`) |
| Architecture review | **architect-reviewer** agent |
| Product/priority review | **product-manager** agent |
| Implementation | `agent-skills:incremental-implementation` (`/build`) |
| QA / real test of whether it works | **qa-expert** agent |
| Security review | **security-auditor** agent — anything touching user input, auth, secrets, external integrations (Notion/OpenAI/Telegram/Railway tokens all apply here) |
| Final review | `agent-skills:code-review-and-quality` (`/review`) — 5-axis: correctness/readability/architecture/security/performance |
| Pre-launch checklist | `agent-skills:shipping-and-launch` (`/ship`) |

**For nontrivial changes** (new feature, refactor touching more than one
file, anything security- or data-sensitive) — default to `/spec → /plan →
/build → /review`, `/ship` before anything ships, or spawn the specific
agent(s) from the table above. Confirm scope with Dimitry before invoking
multiple agents if there's ambiguity.

**For trivial changes** (typo fix, one-line config tweak, a quick question)
— just do it directly, no need for the full chain.

## Subagent routing

When dispatching the **Agent** tool for this project, match to the actual
work — this project has no `security-officer`/`pci-reviewer`/`mobile-store-
reviewer`/etc. roster (those were `great_cto` archetypes that don't apply to
a solo Python scraper). Use:

| Trigger | Use `subagent_type:` |
|---|---|
| New feature spec, architecture decisions | `architect-reviewer` |
| Priority/scope tradeoffs, roadmap questions | `product-manager` |
| "Does this actually work" / test coverage | `qa-expert` |
| Touches Notion/OpenAI/Telegram/Railway credentials, external URLs, user-supplied JD text | `security-auditor` |
| Cross-file exploratory research ("where is X", "which files reference Y") | `Explore` |
| Everything else | `general-purpose` |

## Style + conventions

- Match existing patterns in the file being edited — this codebase favors
  small, single-purpose modules (`sources/*.py` = one file per job board),
  f-string SQL with `%s` placeholders (never string-interpolated), type
  hints on function signatures, `logging` module (not `print`).
- No comments explaining *what* code does — only *why*, when the reason
  isn't obvious from the code itself (see root `CLAUDE.md`).
- Conventional commits not enforced — match the existing `git log` style
  (imperative, descriptive first line).
- No secrets in code. Real secrets live in `.env` (gitignored) locally and
  Railway environment variables in production.

## Out of scope

Do not invent business decisions. If the spec is ambiguous, ask Dimitry
directly — there is no team to escalate to. Do not delete/archive/rename
project docs without his explicit go-ahead (see 2026-07-15 file audit —
several docs look redundant but are still actively cross-referenced).

---

_Rewritten 2026-07-15 after `great_cto` removal — see global CLAUDE.md
"Default Dev Pipeline" section for the authoritative mapping if this drifts
out of sync again._
