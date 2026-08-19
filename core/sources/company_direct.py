"""company_direct — polls target companies' ATS boards (Greenhouse/Lever/Ashby)
straight from the target_companies table, feeding the normal batch pipeline.

The point (2026-07 plan, built 2026-08-19): vacancies of hand-picked companies
reach the funnel before — or regardless of whether — they ever hit aggregators.
MVP scope: batch only (no instant alert), status='active' rows only, layers are
labels not schedules.

Design notes (from the architecture review):
- PM pre-filter runs HERE via the pipeline's own filters.passes_role_filter —
  a company board is 95%+ non-PM roles, and logging those as outcome='role'
  rows would add ~4000 junk rows/day and wreck the Sources panel yield stats.
- `published` stays empty ON PURPOSE: a posting living on the company's own
  board is open today; ATS dates (updated_at / createdAt) would get long-lived
  postings wrongly culled by the 14-day staleness filter that exists to fight
  aggregator backlog. Do not "fix" this.
- One attempt per company, no retries, hard time budget: sources_data blocks
  the whole run, and a global ATS outage must cost minutes, not tens of them.
- Health goes to target_companies columns in one batch write; companies that
  fail 6 consecutive polls (~1.5 days) auto-degrade to 'dormant' with a single
  aggregated Telegram alert. Rows are never deleted.
"""
import logging
import time

import ats_boards
import db
import filters
import telegram

logger = logging.getLogger(__name__)

SOURCE_NAME = "CompanyDirect"
CRAWL_DELAY_SEC = 0.4
REQUEST_TIMEOUT = 10.0
TIME_BUDGET_SEC = 240
DEGRADE_AFTER_FAILURES = 6


def _to_source_jobs(company: dict, postings: list[dict], deadline: float) -> list[dict]:
    """deadline is time.monotonic()-based: per-posting Greenhouse description
    fetches (10s timeout each) must also respect the run's time budget (QA
    review 2026-08-19) — a board with many PM matches could otherwise stretch
    one loop iteration by tens of seconds between budget checks. Dropping the
    tail is safe: the board is re-listed from scratch every run."""
    jobs = []
    for p in postings:
        if not filters.passes_role_filter({"title": p["title"]}):
            continue
        if time.monotonic() > deadline:
            logger.warning(f"CompanyDirect: {company['name']} — budget hit mid-board, remaining postings deferred")
            break
        description = p["description"] or ats_boards.fetch_description(
            company["ats"], company["slug"], p["id"], timeout=REQUEST_TIMEOUT)
        jobs.append({
            "title": p["title"],
            # Canonical name from the seed row, not whatever the ATS reports:
            # keeps normalize_job_key stable and dedups cleanly against the same
            # vacancy arriving via aggregators.
            "company": company["name"],
            "url": p["url"],
            "apply_url": p["url"],  # already a direct link; enrich_url won't touch it
            "description": description,
            "location": p["location"],
            "salary": p["salary"],
            "source": SOURCE_NAME,
            "published": "",  # deliberate — see module docstring
        })
    return jobs


def fetch() -> list[dict]:
    try:
        companies = db.get_active_target_companies()
    except Exception as e:
        logger.error(f"CompanyDirect: cannot read target_companies: {e}")
        return []
    if not companies:
        return []

    jobs: list[dict] = []
    results: list[dict] = []
    started = time.monotonic()
    for i, company in enumerate(companies):
        if time.monotonic() - started > TIME_BUDGET_SEC:
            logger.warning(
                f"CompanyDirect: time budget hit, {len(companies) - i} companies "
                f"deferred to next run")
            break
        if i:
            time.sleep(CRAWL_DELAY_SEC)
        # The WHOLE per-company body is guarded (QA review 2026-08-19): fetch()
        # is evaluated while building sources_data, BEFORE start_run() — an
        # uncaught exception here would kill the entire run for all sources.
        # _to_source_jobs/fetch_description are internally safe today, but this
        # boundary must not depend on callees staying that way.
        try:
            postings = ats_boards.LISTERS[company["ats"]](
                company["slug"], timeout=REQUEST_TIMEOUT)
            matched = _to_source_jobs(company, postings, started + TIME_BUDGET_SEC)
            # ok is recorded only after the full body succeeded — otherwise a
            # failure mid-body would append a second, contradicting entry.
            results.append({"id": company["id"], "ok": True, "posting_count": len(postings)})
            if postings:
                logger.info(
                    f"CompanyDirect: {company['name']} — {len(postings)} postings, "
                    f"{len(matched)} PM-matched")
            jobs.extend(matched)
        except ats_boards.AtsBoardNotFound:
            results.append({"id": company["id"], "ok": False, "error": "not_found"})
        except Exception as e:
            results.append({"id": company["id"], "ok": False, "error": str(e)[:200]})

    _record_health(results, polled=len(results), total=len(companies))
    logger.info(f"CompanyDirect: fetched {len(jobs)} jobs from {len(results)} boards")
    return jobs


def _record_health(results: list[dict], polled: int, total: int) -> None:
    """Telemetry writes must never cost us the fetched vacancies."""
    try:
        db.record_target_company_results(results)
        degraded = db.degrade_target_companies(DEGRADE_AFTER_FAILURES)
        failed = [r for r in results if not r["ok"]]
        alerts = []
        if degraded:
            names = ", ".join(f"{d['name']} ({d['ats']}/{d['slug']}: {d['last_error']})" for d in degraded)
            alerts.append(f"CompanyDirect: {len(degraded)} companies auto-dormant — {names}")
        if polled and len(failed) > polled // 2:
            alerts.append(
                f"CompanyDirect: {len(failed)}/{polled} boards failed this run — likely outage")
        if alerts:
            telegram.send_error("\n".join(alerts))
    except Exception:
        logger.exception("CompanyDirect: health recording failed (non-fatal)")
