import sys
import logging
from pathlib import Path

import filters
import ats
import db
import notion_client
import telegram
from sources import himalayas, weworkremotely, remotive, jobicy, remoteok
from config import ATS_THRESHOLD, COMPANY_COOLDOWN_DAYS, MAX_GPT_CALLS_PER_RUN, validate_secrets
from utils import strip_html

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
        ("Himalayas",       himalayas.fetch()),
        ("WeWorkRemotely",  weworkremotely.fetch()),
        ("Remotive",        remotive.fetch()),
        ("Jobicy",          jobicy.fetch()),
        ("RemoteOK",        remoteok.fetch()),
    ]
    source_counts = {name: len(batch) for name, batch in sources_data}
    jobs = [j for _, batch in sources_data for j in batch]

    for job in jobs:
        if job.get("description"):
            job["description"] = strip_html(job["description"])

    total_fetched = len(jobs)
    logger.info(f"Total fetched: {total_fetched} — " + ", ".join(f"{n}:{c}" for n, c in source_counts.items()))

    if total_fetched == 0:
        telegram.send_error("⚠️ Все job boards вернули 0 вакансий — возможны проблемы со скрапингом")

    seen_urls = notion_client.load_seen_urls()
    company_history = notion_client.load_company_applications(COMPANY_COOLDOWN_DAYS)

    counts = {"qualified": 0, "role": 0, "location": 0, "stale": 0, "dedup": 0, "score": 0, "gpt_limit": 0}
    top_jobs: list[dict] = []
    gpt_calls = 0
    run_id = db.start_run(total_fetched, source_counts)

    for job in jobs:
        if not job.get("url"):
            continue

        if not filters.passes_role_filter(job):
            counts["role"] += 1
            db.log_job(run_id, job, "role")
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

        if gpt_calls >= MAX_GPT_CALLS_PER_RUN:
            counts["gpt_limit"] += 1
            db.log_job(run_id, job, "gpt_limit")
            continue

        result = ats.analyze(job, resume)
        gpt_calls += 1
        logger.info(f"  {result.score:>3}/100  {job['title']} @ {job['company']}  [{job['source']}]")

        if result.score < ATS_THRESHOLD:
            ok = notion_client.create_rejected_entry(job, result.score)
            if ok:
                seen_urls.add(job["url"])
            counts["score"] += 1
            db.log_job(run_id, job, "low_score", ats_score=result.score)
            continue

        company_key = job.get("company", "").lower().strip()
        cooldown_match = company_history.get(company_key)
        if cooldown_match:
            logger.info(f"  ⚠️  Cooldown: {cooldown_match['company']} {cooldown_match['days_ago']}d ago")

        ok = notion_client.create_entry(job, result, cooldown_match=cooldown_match)
        if ok:
            seen_urls.add(job["url"])
        counts["qualified"] += 1
        db.log_job(run_id, job, "qualified", ats_score=result.score)
        top_jobs.append({
            "title": job["title"],
            "company": job["company"],
            "score": result.score,
        })

    top_jobs.sort(key=lambda x: x["score"], reverse=True)
    db.finish_run(run_id, counts, gpt_calls)
    telegram.send_run_summary(counts, top_jobs, source_counts)

    logger.info(
        f"=== Done: {counts['qualified']} qualified | GPT calls: {gpt_calls}/{MAX_GPT_CALLS_PER_RUN} | "
        f"role:{counts['role']} location:{counts['location']} stale:{counts['stale']} "
        f"dedup:{counts['dedup']} low_score:{counts['score']} gpt_limit:{counts['gpt_limit']} ==="
    )


if __name__ == "__main__":
    run()
