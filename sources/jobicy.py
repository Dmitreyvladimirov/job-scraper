import logging
import requests
from utils import retry

logger = logging.getLogger(__name__)

URL = "https://jobicy.com/api/v2/remote-jobs"


def fetch() -> list[dict]:
    try:
        resp = retry(lambda: requests.get(
            URL,
            params={"count": 50, "industry": "management", "tag": "product manager"},
            timeout=15,
        ))
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.error(f"Jobicy fetch failed: {e}")
        return []

    jobs = []
    for item in data.get("jobs", []):
        salary_min = item.get("jobSalaryMin", "")
        salary_max = item.get("jobSalaryMax", "")
        salary = f"${salary_min}–${salary_max}" if salary_min and salary_max else ""

        # Every Jobicy listing is remote by construction, but jobGeo holds only the
        # geo restriction ("USA", "Europe", "Anywhere") — without the "Remote" prefix
        # passes_location_filter() rejected nearly all of them
        geo = item.get("jobGeo", "")
        location = f"Remote — {geo}" if geo else "Remote"

        jobs.append({
            "title": item.get("jobTitle", ""),
            "company": item.get("companyName", ""),
            "url": item.get("url", ""),
            "description": item.get("jobDescription", ""),
            "location": location,
            "salary": salary,
            "source": "Jobicy",
            "published": item.get("pubDate", "")[:10],
        })

    logger.info(f"Jobicy: fetched {len(jobs)} jobs")
    return jobs
