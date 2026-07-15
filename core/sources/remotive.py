import logging
import requests
from utils import retry

logger = logging.getLogger(__name__)

URL = "https://remotive.com/api/remote-jobs"


def fetch() -> list[dict]:
    try:
        resp = retry(lambda: requests.get(URL, params={"category": "product", "limit": 50}, timeout=15))
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.error(f"Remotive fetch failed: {e}")
        return []

    jobs = []
    for item in data.get("jobs", []):
        jobs.append({
            "title": item.get("title", ""),
            "company": item.get("company_name", ""),
            "url": item.get("url", ""),
            "description": item.get("description", ""),
            "location": item.get("candidate_required_location", ""),
            "salary": item.get("salary", ""),
            "source": "Remotive",
            "published": item.get("publication_date", "")[:10],
        })

    logger.info(f"Remotive: fetched {len(jobs)} jobs")
    return jobs
