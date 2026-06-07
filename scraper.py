import sys
import logging
from pathlib import Path

import filters
import ats
import notion_client
import job_cache
import telegram
from sources import himalayas, weworkremotely
from config import ATS_THRESHOLD, validate_secrets

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
    logger.info(f"Resume loaded: {len(text)} chars from {RESUME_PATH.name}")
    return text


def run() -> None:
    validate_secrets()
    logger.info("=== Job Scraper started ===")
    resume = load_resume()

    jobs = himalayas.fetch() + weworkremotely.fetch()
    logger.info(f"Total fetched: {len(jobs)}")

    # Build seen set: Notion (qualified) + local cache (all processed)
    processed = job_cache.load()
    seen_urls = notion_client.load_seen_urls() | set(processed.keys())

    counts = {"qualified": 0, "role": 0, "location": 0, "dedup": 0, "score": 0}

    for job in jobs:
        if not job.get("url"):
            continue

        if not filters.passes_role_filter(job):
            counts["role"] += 1
            continue

        if not filters.passes_location_filter(job):
            counts["location"] += 1
            continue

        if job["url"] in seen_urls:
            counts["dedup"] += 1
            continue

        job_score = ats.score(job, resume)
        logger.info(f"  {job_score:>3}/100  {job['title']} @ {job['company']}  [{job['source']}]")

        if job_score < ATS_THRESHOLD:
            job_cache.record(processed, job, "low_score", job_score)
            seen_urls.add(job["url"])
            counts["score"] += 1
            continue

        notion_client.create_entry(job, job_score)
        telegram.send_vacancy(job, job_score)
        job_cache.record(processed, job, "qualified", job_score)
        seen_urls.add(job["url"])
        counts["qualified"] += 1

    job_cache.save(processed)

    logger.info(
        f"=== Done: {counts['qualified']} qualified | "
        f"filtered out — role:{counts['role']} location:{counts['location']} "
        f"dedup:{counts['dedup']} low_score:{counts['score']} ==="
    )


if __name__ == "__main__":
    run()
