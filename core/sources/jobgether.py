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
from html import unescape

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


_APPLY_URL_RE = re.compile(r'applyUrl":"(https?://[^"]+)"')

# JSON-LD applicantLocationRequirements ships ISO alpha-2 codes ("CN", "GB") —
# the cloud scorer's location axis misreads rare codes ("Remote — CN" scored
# 15/15 live on 2026-08-19 while "Remote — US" correctly got 0). Expand to
# names the model can't misparse. Codes not in the map pass through unchanged.
_ISO_COUNTRIES = {
    "US": "USA", "GB": "United Kingdom", "CA": "Canada", "AU": "Australia",
    "DE": "Germany", "FR": "France", "ES": "Spain", "IT": "Italy", "NL": "Netherlands",
    "BE": "Belgium", "AT": "Austria", "CH": "Switzerland", "SE": "Sweden", "NO": "Norway",
    "DK": "Denmark", "FI": "Finland", "IE": "Ireland", "PT": "Portugal", "PL": "Poland",
    "CZ": "Czechia", "SK": "Slovakia", "HU": "Hungary", "RO": "Romania", "BG": "Bulgaria",
    "GR": "Greece", "CY": "Cyprus", "EE": "Estonia", "LV": "Latvia", "LT": "Lithuania",
    "UA": "Ukraine", "MD": "Moldova", "RS": "Serbia", "HR": "Croatia", "SI": "Slovenia",
    "BA": "Bosnia", "MK": "North Macedonia", "AL": "Albania", "ME": "Montenegro",
    "TR": "Turkey", "IL": "Israel", "AE": "UAE", "SA": "Saudi Arabia", "QA": "Qatar",
    "IN": "India", "CN": "China", "JP": "Japan", "KR": "South Korea", "SG": "Singapore",
    "HK": "Hong Kong", "TW": "Taiwan", "TH": "Thailand", "VN": "Vietnam", "PH": "Philippines",
    "ID": "Indonesia", "MY": "Malaysia", "PK": "Pakistan", "BD": "Bangladesh",
    "UZ": "Uzbekistan", "KZ": "Kazakhstan", "GE": "Georgia", "AM": "Armenia", "AZ": "Azerbaijan",
    "BY": "Belarus", "RU": "Russia",
    "BR": "Brazil", "AR": "Argentina", "MX": "Mexico", "CL": "Chile", "CO": "Colombia",
    "PE": "Peru", "UY": "Uruguay", "VE": "Venezuela", "EC": "Ecuador", "BO": "Bolivia",
    "PY": "Paraguay", "CR": "Costa Rica", "PA": "Panama", "GT": "Guatemala", "DO": "Dominican Republic",
    "ZA": "South Africa", "NG": "Nigeria", "KE": "Kenya", "EG": "Egypt", "MA": "Morocco",
    "NZ": "New Zealand", "IS": "Iceland", "LU": "Luxembourg", "MT": "Malta",
}


def _expand_country(code: str) -> str:
    return _ISO_COUNTRIES.get(code.strip(), code.strip())


def _extract_apply_url(html: str) -> str:
    """Jobgether's own outbound apply link, embedded in the offer page's JSON blob
    (html-escaped). This is the AUTHORITATIVE direct link — even to ATS hosts we
    can't enrich ourselves (Workday etc.). utm_* tracking params are dropped."""
    m = _APPLY_URL_RE.search(unescape(html))
    if not m:
        return ""
    url = m.group(1)
    if "?" in url:
        base, query = url.split("?", 1)
        kept = [p for p in query.split("&") if not p.lower().startswith("utm_")]
        url = base + ("?" + "&".join(kept) if kept else "")
    return url


def clean_description(raw_html: str) -> str:
    """Drop the boilerplate opening paragraph ("This a Full Remote job, the offer
    is available from: Texas (USA)") that every offer carries — it poisons the
    scorer's location axis in both directions (Phase 4; 311/312 recent JDs had it,
    per the 2026-08-14 label comparison). The real geo signal is parsed cleanly
    from applicantLocationRequirements into `location`, so this paragraph is pure
    noise. Cut at the tag boundary, first occurrence only, and only at the start."""
    return re.sub(r"^\s*<p>\s*This a Full Remote job[^<]*</p>\s*", "", raw_html, count=1)


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
    countries = [_expand_country(c) for c in countries if c]
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
        description = strip_html(clean_description(posting.get("description", "")))
        # datePosted is a full JS Date string ("Fri Jul 03 2026 ..."), not ISO —
        # re-parse into YYYY-MM-DD when possible, else leave blank.
        raw_date = posting.get("datePosted", "")
        try:
            published = datetime.strptime(raw_date[:24], "%a %b %d %Y %H:%M:%S").date().isoformat()
        except (ValueError, TypeError):
            published = ""

        apply_url = _extract_apply_url(detail_html)
        jobs.append({
            "title": title,
            "company": company,
            "url": url,
            "apply_url": apply_url or None,
            "description": description,
            "location": _location_from_posting(posting),
            "salary": _salary_from_posting(posting),
            "source": "Jobgether",
            "published": published,
        })

    logger.info(f"Jobgether: parsed {len(jobs)} jobs")
    return jobs
