"""
jobgether.com — server-rendered job board, Cloudflare-fronted.

Listing page (/remote-jobs/product-manager-tech) has offer links in raw HTML.
Each offer page embeds a clean schema.org JobPosting in a JSON-LD <script> block
(title, company, location requirements, remote flag, salary) — parsed directly,
no HTML text-scraping needed for job details.

robots.txt allows crawling with Crawl-delay: 2 — respected via a fixed delay
between offer-page fetches. Cloudflare intermittently 403s even compliant
requests, hence the retry() wrapper on every fetch.
"""
import json
import logging
import re
import time
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from utils import retry, strip_html

logger = logging.getLogger(__name__)

BASE_URL = "https://jobgether.com"
LISTING_URL = f"{BASE_URL}/remote-jobs/product-manager-tech"

# Cloudflare responds inconsistently to generic UAs; a realistic browser UA
# matches what worked during manual verification.
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}

CRAWL_DELAY_SEC = 2


def _fetch_page(url: str) -> str | None:
    try:
        resp = retry(lambda: requests.get(url, headers=_HEADERS, timeout=15))
        resp.raise_for_status()
        # Jobgether omits charset in Content-Type, so requests falls back to
        # ISO-8859-1 while pages are UTF-8 — dashes/accents got corrupted
        resp.encoding = resp.apparent_encoding
        return resp.text
    except Exception as e:
        logger.error(f"Jobgether: failed to fetch {url}: {e}")
        return None


def _extract_offer_links(html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    links = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.startswith("/offer/"):
            links.add(BASE_URL + href)
        elif href.startswith(f"{BASE_URL}/offer/"):
            links.add(href)
    return sorted(links)


def _extract_job_posting(html: str) -> dict | None:
    """Pull the schema.org JobPosting block out of a jobgether offer page."""
    for match in re.finditer(
        r'<script type="application/ld\+json">(.*?)</script>', html, re.DOTALL
    ):
        try:
            data = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        if data.get("@type") == "JobPosting":
            return data
    return None


def _location_from_posting(posting: dict) -> str:
    if posting.get("jobLocationType") != "TELECOMMUTE":
        return ""
    countries = [
        c.get("name", "")
        for c in (posting.get("applicantLocationRequirements") or [])
        if isinstance(c, dict)
    ]
    countries = [c for c in countries if c]
    if not countries:
        return "Remote worldwide"
    return "Remote — " + ", ".join(countries)


def _salary_from_posting(posting: dict) -> str:
    salary = posting.get("baseSalary") or {}
    value = salary.get("value") or {}
    min_v, max_v, currency = value.get("minValue"), value.get("maxValue"), salary.get("currency", "")
    if min_v and max_v:
        return f"{min_v:,}–{max_v:,} {currency}".strip()
    if min_v:
        return f"from {min_v:,} {currency}".strip()
    return ""


def fetch() -> list[dict]:
    html = _fetch_page(LISTING_URL)
    if not html:
        return []

    offer_urls = _extract_offer_links(html)
    logger.info(f"Jobgether: {len(offer_urls)} offer links found on listing page")

    jobs = []
    for i, url in enumerate(offer_urls):
        if i > 0:
            time.sleep(CRAWL_DELAY_SEC)

        detail_html = _fetch_page(url)
        if not detail_html:
            continue

        posting = _extract_job_posting(detail_html)
        if not posting:
            logger.warning(f"Jobgether: no JobPosting JSON-LD found at {url}")
            continue

        title = posting.get("title", "")
        company = (posting.get("hiringOrganization") or {}).get("name", "")
        description = strip_html(posting.get("description", ""))
        # datePosted is a full JS Date string ("Fri Jul 03 2026 ..."), not ISO —
        # re-parse into YYYY-MM-DD when possible, else leave blank.
        raw_date = posting.get("datePosted", "")
        try:
            published = datetime.strptime(raw_date[:24], "%a %b %d %Y %H:%M:%S").date().isoformat()
        except (ValueError, TypeError):
            published = ""

        jobs.append({
            "title": title,
            "company": company,
            "url": url,
            "description": description,
            "location": _location_from_posting(posting),
            "salary": _salary_from_posting(posting),
            "source": "Jobgether",
            "published": published,
        })

    logger.info(f"Jobgether: parsed {len(jobs)} jobs")
    return jobs
