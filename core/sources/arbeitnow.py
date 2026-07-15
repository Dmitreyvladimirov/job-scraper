import logging
import requests
from datetime import datetime
from utils import retry

logger = logging.getLogger(__name__)

URL = "https://arbeitnow.com/api/job-board-api"


def fetch() -> list[dict]:
    jobs = []
    for page in range(1, 4):  # 3 pages × ~100 results to maximise coverage
        try:
            resp = retry(lambda p=page: requests.get(
                URL,
                params={"search": "product manager", "remote": "1", "page": p},
                timeout=15,
            ))
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.error(f"Arbeitnow fetch page {page} failed: {e}")
            break

        batch = data.get("data", [])
        if not batch:
            break

        for item in batch:
            if not item.get("remote"):
                continue

            ts = item.get("created_at", 0)
            published = datetime.fromtimestamp(ts).date().isoformat() if ts else ""

            jobs.append({
                "title": item.get("title", ""),
                "company": item.get("company_name", ""),
                "url": item.get("url", ""),
                "description": item.get("description", ""),
                "location": "Remote",  # remote=True confirmed above
                "salary": "",
                "source": "Arbeitnow",
                "published": published,
            })

        if not data.get("links", {}).get("next"):
            break

    logger.info(f"Arbeitnow: fetched {len(jobs)} remote jobs")
    return jobs
