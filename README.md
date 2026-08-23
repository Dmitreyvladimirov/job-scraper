# job-scraper

An automated job-hunting pipeline: scrape postings, score them against a fixed rubric with an LLM, track them through a kanban, and generate a tailored resume for the ones worth applying to.

I built this because searching for a product role is mostly repetitive work — reading the same postings across five aggregators, judging fit, copying the good ones somewhere, and rewriting a resume for each. This runs that loop four times a day and hands me a shortlist.

It is a personal tool, running in production for me since early 2026. Not a product, not accepting users. Public because it is a reasonable sample of how I build.

## How it works

```
sources ──► filters ──► LLM scoring ──► Postgres ──► dashboard ──► resume generation
   │           │             │                          │
Jobgether   role, lang,   4-axis rubric            review, kanban,
Jobicy      location,     (role / domain /         tracker, mail queue
Choicy      freshness     keywords / location)
Telegram
```

A cron run polls the active sources, drops anything that fails the cheap filters before it costs a token, scores what survives, writes it to Postgres, and sends a Telegram digest. Everything after that happens in the dashboard: reviewing the shortlist, moving cards through the funnel, and triggering a resume for a specific posting.

## Components

| Component | Path | Runs as | What it does |
|---|---|---|---|
| Scraper | `core/scraper.py`, `core/sources/` | Railway cron, 4×/day on weekdays | Polls sources, filters, scores, persists, notifies |
| Dashboard | `core/dashboard.py`, `core/templates/` | Railway web service | Review, kanban, tracker, add-job, mail queue, source config. FastAPI + Jinja2 + HTMX |
| Intake | `core/intake.py`, `core/tg_bot.py` | Same service | Manual submission of a posting by URL or pasted text, over a bearer-auth endpoint or a Telegram bot |
| Mail agent | `core/mail_agent.py`, `core/gmail_client.py` | Railway cron, 2×/day | Reads mail from known ATS senders and *proposes* a card status change. It never applies one itself |
| Scoring client | `core/scoring_client.py` | Library | Calls the scoring service; `core/ats.py` remains as a local fallback path |
| Resume client | `core/resume_client.py` | Library | Calls the resume service. One retry only, because generation costs money |

The scoring and resume services live in a companion repo and share the same Postgres instance.

## Choosing the scoring model

The interesting part of this project is not the scraping, it is deciding whether to trust a model with the filtering.

Scoring started as a local call and later moved to a dedicated service on a different model. Rather than swapping it and hoping, I ran both in shadow for three days — every posting scored twice, neither result acting on anything — and compared 70 pairs against postings I had labelled by hand.

The new path agreed with my own judgement on 80% of them against 44% for the old one, with no errors and no case where it scored a posting *higher* than the old path. That last check mattered more than the accuracy number: a false negative costs me one missed posting, a false positive costs me an application I should not have sent. Only then did the cutover happen, and the threshold moved from 60 to 70 at the same time.

The same instinct shows up in the mail agent: it can read my inbox and work out that a rejection arrived, but it is not allowed to move the card. It queues a suggestion and I confirm it.

## Stack

Python 3.12, FastAPI, Jinja2, HTMX, PostgreSQL, Railway. LLM scoring through a separate service. Telegram Bot API for digests, Telethon for channel sources, Gmail API for the mail agent. Greenhouse / Lever / Ashby public APIs to resolve real apply URLs.

## Tests

229 test functions under `tests/`, covering filters, status transitions, the scoring and resume clients, dashboard routes, intake, the mail agent, and apply-URL resolution. CI runs on GitHub Actions (`.github/workflows/scraper.yml`).

```bash
pip install -r requirements.txt
pytest
```

## Notes

Configuration is entirely environment-driven (`core/config.py`); no credentials are committed. Some project documentation in this repo is written in Russian — it is working notes, not something I expected anyone else to read.
