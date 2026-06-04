import logging
import requests
from datetime import datetime

logger = logging.getLogger(__name__)

URL = "https://himalayas.app/jobs/api"


def fetch(query: str = "product manager", limit: int = 20) -> list[dict]:
    try:
        resp = requests.get(URL, params={"q": query, "limit": limit}, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.error(f"Himalayas fetch failed: {e}")
        return []

    jobs = []
    for item in data.get("jobs", []):
        location_parts = item.get("locationRestrictions") or []
        location = ", ".join(location_parts)

        salary = ""
        min_s = item.get("minSalary")
        max_s = item.get("maxSalary")
        currency = item.get("currency", "USD")
        if min_s and max_s:
            salary = f"${min_s:,}–${max_s:,} {currency}"
        elif min_s:
            salary = f"from ${min_s:,} {currency}"

        pub_ts = item.get("pubDate") or 0
        published = datetime.fromtimestamp(pub_ts).date().isoformat() if pub_ts else ""

        jobs.append({
            "title": item.get("title", ""),
            "company": item.get("companyName", ""),
            "url": item.get("applicationLink", ""),
            "description": item.get("description", ""),
            "location": location,
            "salary": salary,
            "source": "Himalayas",
            "published": published,
        })

    logger.info(f"Himalayas: fetched {len(jobs)} jobs")
    return jobs
