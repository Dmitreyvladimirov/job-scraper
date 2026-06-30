import logging
import requests
from utils import retry

logger = logging.getLogger(__name__)

URL = "https://remoteok.com/api"

# RemoteOK requires a realistic User-Agent, otherwise returns 403
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; job-scraper/1.0)"}


def fetch() -> list[dict]:
    try:
        resp = retry(lambda: requests.get(
            URL,
            params={"tag": "product"},
            headers=HEADERS,
            timeout=15,
        ))
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.error(f"RemoteOK fetch failed: {e}")
        return []

    jobs = []
    for item in data:
        # First item is metadata, skip non-job entries
        if not isinstance(item, dict) or "position" not in item:
            continue

        tags = item.get("tags") or []
        location = ", ".join(item.get("location", []) or []) if isinstance(item.get("location"), list) else ""

        jobs.append({
            "title": item.get("position", ""),
            "company": item.get("company", ""),
            "url": item.get("url", ""),
            "description": item.get("description", ""),
            "location": location,
            "salary": item.get("salary", ""),
            "source": "RemoteOK",
            "published": item.get("date", "")[:10],
        })

    logger.info(f"RemoteOK: fetched {len(jobs)} jobs")
    return jobs
