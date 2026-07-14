import re
import sys
import logging
from datetime import datetime, timezone
from pathlib import Path

import filters
import ats
import db
import notion_client
import telegram
import sheets
# Disabled 2026-07-14 after source audit (see SOURCES_DECISION.md): himalayas,
# weworkremotely, remotive, remoteok, arbeitnow — API-side issues (ignored search
# params / stale backlog), 21/174 qualified from ~19k rows. Files kept in sources/.
from sources import jobicy, telegram_channels, jobgether
from config import ATS_THRESHOLD, COMPANY_COOLDOWN_DAYS, MAX_GPT_CALLS_PER_RUN, validate_secrets
from utils import strip_html, enrich_url, normalize_job_key, fetch_jd_from_url, fetch_url_generic

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

RESUME_PATH = Path(__file__).parent / "base_resume.md"


def load_resume() -> str:
    if not RESUME_PATH.exists():
        logger.error(f"Resume not found at {RESUME_PATH.resolve()}")
        sys.exit(1)
    text = RESUME_PATH.read_text(encoding="utf-8")
    logger.info(f"Resume loaded: {len(text)} chars")
    return text


def run() -> None:
    validate_secrets()
    db.init_db()
    logger.info("=== Job Scraper started ===")
    resume = load_resume()

    sources_data = [
        ("Jobicy",              jobicy.fetch()),
        ("TelegramChannels",    telegram_channels.fetch()),
        ("Jobgether",           jobgether.fetch()),
    ]
    source_counts = {name: len(batch) for name, batch in sources_data}
    jobs = [j for _, batch in sources_data for j in batch]

    for job in jobs:
        if job.get("description"):
            job["description"] = strip_html(job["description"])

    # Layer 1: deduplicate within this run by (company, title).
    # When duplicates exist, keep the one with the best description:
    # any source beats RemoteOK (AI summary); among equal-tier sources, longer description wins.
    from collections import defaultdict
    groups: dict[tuple, list[dict]] = defaultdict(list)
    no_key: list[dict] = []
    for job in jobs:
        key = normalize_job_key(job.get("company", ""), job.get("title", ""))
        if key[0] and key[1]:
            groups[key].append(job)
        else:
            no_key.append(job)

    cross_source_dedup = 0
    deduped: list[dict] = list(no_key)
    for key, group in groups.items():
        if len(group) == 1:
            deduped.append(group[0])
        else:
            best = max(group, key=lambda j: (
                0 if j.get("source", "").lower() == "remoteok" else 1,
                len(j.get("description") or ""),
            ))
            cross_source_dedup += len(group) - 1
            sources = [j["source"] for j in group]
            logger.info(f"Cross-source dedup: kept {best['source']} for {best['title']} @ {best['company']} (from {sources})")
            deduped.append(best)
    jobs = deduped

    total_fetched = len(jobs)
    logger.info(
        f"Total fetched: {total_fetched} ({cross_source_dedup} cross-source dupes removed) — "
        + ", ".join(f"{n}:{c}" for n, c in source_counts.items())
    )

    if total_fetched == 0:
        telegram.send_error("⚠️ Все job boards вернули 0 вакансий — возможны проблемы со скрапингом")

    seen_urls, seen_keys = db.load_seen_jobs()
    # Also load from Notion to catch pre-Postgres history (transition period)
    notion_seen_urls, notion_seen_keys = notion_client.load_seen_urls()
    seen_urls |= notion_seen_urls
    seen_keys |= notion_seen_keys
    company_history = notion_client.load_company_applications(COMPANY_COOLDOWN_DAYS)

    counts = {"qualified": 0, "role": 0, "location": 0, "language": 0, "stale": 0, "dedup": 0, "score": 0, "gpt_limit": 0, "ats_error": 0}
    top_jobs: list[dict] = []
    gpt_calls = 0
    started_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    run_id = db.start_run(total_fetched, source_counts)

    for job in jobs:
        if not job.get("url"):
            continue

        if not filters.passes_role_filter(job):
            counts["role"] += 1
            db.log_job(run_id, job, "role")
            continue

        if not filters.passes_language_filter(job):
            counts["language"] += 1
            db.log_job(run_id, job, "language")
            continue

        if not filters.passes_location_filter(job):
            counts["location"] += 1
            db.log_job(run_id, job, "location")
            continue

        if not filters.passes_date_filter(job):
            counts["stale"] += 1
            db.log_job(run_id, job, "stale")
            continue

        if job["url"] in seen_urls:
            counts["dedup"] += 1
            db.log_job(run_id, job, "dedup")
            continue

        # Layer 2: cross-run dedup by (company, title) — catches same job with different URL
        job_key = normalize_job_key(job.get("company", ""), job.get("title", ""))
        if job_key[0] and job_key[1] and job_key in seen_keys:
            counts["dedup"] += 1
            db.log_job(run_id, job, "dedup")
            continue

        if gpt_calls >= MAX_GPT_CALLS_PER_RUN:
            counts["gpt_limit"] += 1
            db.log_job(run_id, job, "gpt_limit")
            continue

        enrich_url(job)

        # For Telegram jobs: fetch full JD from the linked page; fallback to message text
        if job.get("source", "").startswith("Telegram:"):
            has_url = job.pop("_has_job_url", False)
            message_text = job.pop("_message_text", "")
            if has_url:
                job_url = job.get("url", "")
                description = fetch_jd_from_url(job_url)
                if not description:
                    description = fetch_url_generic(job_url)
                if description:
                    job["description"] = description
                    logger.info(f"  JD fetched: {len(description)} chars for {job['title']} @ {job['company']}")
                else:
                    job["description"] = message_text
                    logger.info(f"  ⚠️ No JD fetched for {job['title']} @ {job['company']}, using message text")
            else:
                job["description"] = message_text

        # For RemoteOK jobs: fetch full JD from the direct URL (RemoteOK only stores AI summary)
        if job.get("source", "").lower() == "remoteok":
            apply_url = job.get("apply_url") or ""
            jd_enriched = False
            if apply_url and apply_url != job.get("url", ""):
                full_jd = fetch_jd_from_url(apply_url) or fetch_url_generic(apply_url)
                if full_jd and len(full_jd) > len(job.get("description") or ""):
                    job["description"] = full_jd
                    jd_enriched = True
                    logger.info(f"  Full JD fetched: {len(full_jd)} chars for {job['title']} @ {job['company']}")
            if not jd_enriched:
                job["incomplete_description"] = True
                logger.info(f"  ⚠️ No direct URL — scoring {job['title']} @ {job['company']} from RemoteOK summary")

        # Try to extract company name from description if missing
        if not job.get("company") and job.get("description"):
            m = re.search(
                r"\b(?:компания|company|работодатель)[:\s]+([A-ZА-ЯЁ][^\n,.(]{2,40})",
                job["description"][:1500], re.IGNORECASE,
            )
            if m:
                job["company"] = m.group(1).strip()

        if filters.is_russia_based(job):
            job["russia_warning"] = True
            logger.info(f"  🇷🇺 Russia warning: {job['title']} @ {job['company']}")

        result = ats.analyze(job, resume)
        gpt_calls += 1

        # Analysis failure (network/parse) — do NOT mark as seen or rejected,
        # so the job gets re-scored on the next run instead of being lost forever
        if result is None:
            counts["ats_error"] += 1
            db.log_job(run_id, job, "ats_error")
            logger.warning(f"  ⚠️ ATS error — will retry next run: {job['title']} @ {job['company']}")
            continue

        logger.info(f"  {result.score:>3}/100  {job['title']} @ {job['company']}  [{job['source']}]")

        if result.score < ATS_THRESHOLD:
            notion_client.create_rejected_entry(job, result.score)
            seen_urls.add(job["url"])
            seen_keys.add(job_key)
            counts["score"] += 1
            db.log_job(run_id, job, "low_score", ats_score=result.score,
                       domain=result.domain, why_not=result.why_not)
            continue

        company_key = job.get("company", "").lower().strip()
        cooldown_match = company_history.get(company_key)
        if cooldown_match:
            logger.info(f"  ⚠️  Cooldown: {cooldown_match['company']} {cooldown_match['days_ago']}d ago")

        ok = notion_client.create_entry(job, result, cooldown_match=cooldown_match)
        seen_urls.add(job["url"])
        seen_keys.add(job_key)
        counts["qualified"] += 1
        db.log_job(run_id, job, "qualified", ats_score=result.score,
                   domain=result.domain, why_not=result.why_not)
        top_jobs.append({
            "title": job["title"],
            "company": job["company"],
            "score": result.score,
            "russia_warning": job.get("russia_warning", False),
        })

    top_jobs.sort(key=lambda x: x["score"], reverse=True)
    db.finish_run(run_id, counts, gpt_calls)
    sheets.log_run(counts, gpt_calls, source_counts, started_at=started_at)
    telegram.send_run_summary(counts, top_jobs, source_counts)

    logger.info(
        f"=== Done: {counts['qualified']} qualified | GPT calls: {gpt_calls}/{MAX_GPT_CALLS_PER_RUN} | "
        f"role:{counts['role']} language:{counts['language']} location:{counts['location']} "
        f"stale:{counts['stale']} dedup:{counts['dedup']} low_score:{counts['score']} gpt_limit:{counts['gpt_limit']} ats_error:{counts['ats_error']} ==="
    )


if __name__ == "__main__":
    run()
