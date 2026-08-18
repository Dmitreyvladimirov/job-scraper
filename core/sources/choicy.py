"""choicy.work — Bubble.io job board with an open (unauthenticated) Data API.

Discovered 2026-08-17: /api/1.1/obj/jobs serves the full job objects (title,
description, salary range, remote flag, status) and /api/1.1/obj/companies
resolves the company references. ~30 live postings (Visible + Approved), fresh
through Aug 2026. No HTML scraping, no ScrapingBee — plain JSON.
"""
import json
import logging

import requests
from utils import retry

logger = logging.getLogger(__name__)

BASE = "https://choicy.work/api/1.1/obj"

# Server-side filter: only live, moderated postings. Bubble's constraint syntax
# is a JSON list passed as a query param.
_CONSTRAINTS = json.dumps([
    {"key": "Visible", "constraint_type": "equals", "value": "true"},
    {"key": "jobStatus", "constraint_type": "equals", "value": "Approved"},
])


def _fetch_companies() -> dict[str, str]:
    """id -> name for every company. One page is plenty (fewer companies than
    jobs); a failure degrades to empty names rather than dropping the jobs."""
    try:
        resp = retry(lambda: requests.get(f"{BASE}/companies", params={"limit": 100}, timeout=15))
        resp.raise_for_status()
        rows = resp.json()["response"]["results"]
    except Exception as e:
        logger.warning(f"Choicy companies fetch failed (jobs will lack company names): {e}")
        return {}
    return {r["_id"]: (r.get("Name") or r.get("name") or "") for r in rows}


def fetch() -> list[dict]:
    try:
        resp = retry(lambda: requests.get(
            f"{BASE}/jobs",
            params={
                "constraints": _CONSTRAINTS,
                "sort_field": "Created Date",
                "descending": "true",
                "limit": 100,
            },
            timeout=15,
        ))
        resp.raise_for_status()
        rows = resp.json()["response"]["results"]
    except Exception as e:
        logger.error(f"Choicy fetch failed: {e}")
        return []

    companies = _fetch_companies()

    jobs = []
    for item in rows:
        # Bubble text fields can carry NUL bytes (seen live 2026-08-18 on the
        # Афиша posting) — Postgres TEXT rejects them, so strip at the source too
        # (db.log_job also guards, but clean data beats relying on the guard).
        description = (item.get("Description") or "").replace("\x00", "").strip()
        if not description:
            continue  # nothing for the scorer to work with

        salary = ""
        rng = item.get("Salary range") or []
        if len(rng) == 2 and rng[1]:
            currency = item.get("Currency") or ""
            period = (item.get("Period") or "").lower()
            low = f"{rng[0]}–" if rng[0] else "up to "
            salary = f"{low}{rng[1]} {currency}" + (f"/{period}" if period else "")

        jobs.append({
            "title": (item.get("Name") or "").strip(),
            "company": companies.get(item.get("Company") or "", ""),
            "url": f"https://choicy.work/job/{item['_id']}",
            "description": description,
            "location": "Remote" if item.get("Remote") else "",
            "salary": salary,
            "source": "Choicy",
            "published": (item.get("Created Date") or "")[:10],
        })

    logger.info(f"Choicy: fetched {len(jobs)} jobs")
    return jobs
