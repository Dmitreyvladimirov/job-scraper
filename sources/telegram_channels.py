"""
Telegram public channel source — scrapes t.me/s/{channel} web previews.

No authentication required. Works for any public channel.
Paginates backwards until MAX_JOB_AGE_DAYS cutoff is reached.
"""
import re
import logging
import requests
from html import unescape
from datetime import datetime, timezone, timedelta
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

_APPLY_INDICATORS = {"тут", "here", "apply", "откликнуться", "подробнее", "link", "details"}

_JOB_URL_PATTERNS = [
    "greenhouse.io", "lever.co", "ashby.com", "workable.com",
    "smartrecruiters.com", "remocate.app/jobs",
    "hh.ru/vacancy/", "/jobs/", "/careers/", "/vacancy/", "/job/",
]

# linkedin.com is never fetchable without login — every page (job listings included)
# returns the same "Sign in to LinkedIn... Continue to join or sign in" auth-wall
# boilerplate as body text, which was leaking into company/JD fields. Treat all of
# linkedin.com as unfetchable rather than whitelisting specific sub-paths.
_SKIP_URL_PATTERNS = [
    "t.me/", "youtube.com", "youtu.be", "twitter.com", "x.com",
    "/people/", "annualreport", "tilda.ws", "linkedin.com",
    "instagram.com", "facebook.com",
]

_SECONDARY_MARKERS = [
    "другие вакансии", "other vacancies", "другие позиции",
    "ещё вакансии", "больше вакансий",
]


def _is_listing_page(url: str) -> bool:
    """Return True if URL points to a general careers/jobs listing page (not a specific role).
    E.g. ursastar.us/careers or company.com/jobs — no specific job ID after the keyword."""
    try:
        from urllib.parse import urlparse
        path = urlparse(url).path.rstrip("/")
        return bool(re.search(
            r"/(careers|jobs|vacancies|vacancy|positions|openings|work)$",
            path, re.IGNORECASE,
        ))
    except Exception:
        return False


def _is_job_url(url: str, display: str = "") -> bool:
    url_lower = url.lower()
    for pat in _SKIP_URL_PATTERNS:
        if pat in url_lower:
            return False
    # Skip general listing pages — they contain many jobs, not a specific role
    if _is_listing_page(url):
        return False
    for pat in _JOB_URL_PATTERNS:
        if pat in url_lower:
            return True
    if any(ind in display.lower() for ind in _APPLY_INDICATORS):
        return True
    return False


def _secondary_offset(text: str) -> int:
    text_lower = text.lower()
    for marker in _SECONDARY_MARKERS:
        idx = text_lower.find(marker)
        if idx != -1:
            return idx
    return len(text)


# Generic post headers that are NOT the job title — some channels (e.g.
# @remotejobss) put a banner line first and the actual title on line 2
_HEADER_MARKERS = re.compile(
    r"^(?:job opportunity|new job|new vacancy|vacancy|вакансия|новая вакансия|hiring)\W*$",
    re.IGNORECASE,
)

# Explicit "Company: X" line (e.g. @remotejobss: "🏢 Company: Lively")
_COMPANY_LINE = re.compile(r"^(?:company|компания)\s*[:—–-]\s*(.+)$", re.IGNORECASE)

_LEADING_DECOR = re.compile(r"^[\U00010000-\U0010ffff☀-⟿⬀-⯿\s]+")


def _extract_title_company(text: str) -> tuple[str, str]:
    lines = [l.strip() for l in text.split("\n") if l.strip()]

    # Scan the first few lines: skip banner headers, capture an explicit
    # "Company:" line, and take the first remaining line as the title candidate
    company_line = ""
    candidates: list[str] = []
    for line in lines[:6]:
        line = _LEADING_DECOR.sub("", line)
        if not line or _HEADER_MARKERS.match(line):
            continue
        m = _COMPANY_LINE.match(line)
        if m:
            if not company_line:
                company_line = m.group(1).strip()
            continue
        candidates.append(line)

    first_line = candidates[0] if candidates else (
        _LEADING_DECOR.sub("", lines[0]) if lines else ""
    )
    first_line_clean = re.sub(r"https?://\S+", "", first_line).strip().rstrip(":")
    for line in (first_line_clean, first_line):
        m = re.match(r"^(.+?)\s+в\s+(.+?)(?:\s*[:(—\-]|$)", line)
        if m:
            return m.group(1).strip(), m.group(2).strip()
        m = re.match(r"^(.+?)\s+(?:at|@)\s+(.+?)(?:\s*[:(—\-]|$)", line, re.IGNORECASE)
        if m:
            return m.group(1).strip(), m.group(2).strip()
    return first_line_clean or first_line, company_line


def _extract_location(text: str) -> str:
    if re.search(r"удалённо|удаленно|remote|worldwide|из любой точки", text, re.IGNORECASE):
        return "remote"
    m = re.search(r"📍\s*(.+?)(?:\n|$)", text)
    if m:
        return m.group(1).strip()
    m = re.search(r"офис\s+в\s+(.+?)(?:[,\n]|$)", text, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return ""


def _pick_job_url(links: list[tuple[str, str]], text: str) -> str | None:
    cutoff = _secondary_offset(text)

    # Build list of (url, display, position_in_text) — only links before secondary section
    primary = []
    for url, display in links:
        pos = text.find(display) if display and display in text else len(text)
        if pos < cutoff:
            primary.append((url, display))

    # Priority 1: labeled as apply link
    for url, display in primary:
        if any(ind in display.lower() for ind in _APPLY_INDICATORS) and _is_job_url(url, display):
            return url

    # Priority 2: known job board domain
    for url, display in primary:
        if _is_job_url(url):
            return url

    # Priority 3: first ~300 chars (Remocate style — URL right after title)
    short_text = text[:300]
    for url, display in links:
        if url in short_text or display in short_text[:300]:
            if _is_job_url(url):
                return url

    # Priority 4: any external link not in skip list and not a listing page
    # Catches company career pages that aren't on known ATS domains
    for url, display in primary:
        url_lower = url.lower()
        if not any(pat in url_lower for pat in _SKIP_URL_PATTERNS) and not _is_listing_page(url):
            return url

    return None


def _fetch_page(url: str) -> str | None:
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=15)
        resp.raise_for_status()
        return resp.text
    except Exception as e:
        logger.error(f"Failed to fetch {url}: {e}")
        return None


def _parse_messages(html: str) -> list[dict]:
    """Parse message blocks from a t.me/s channel page."""
    soup = BeautifulSoup(html, "html.parser")
    messages = []

    for wrap in soup.find_all("div", class_="tgme_widget_message_wrap"):
        text_el = wrap.find(
            "div",
            class_=lambda c: c and "tgme_widget_message_text" in c.split(),
        )
        if not text_el:
            continue

        # get_text(separator="\n") would insert a newline at every tag boundary,
        # not just real line breaks — splitting "Title в <b>Company</b>" onto two
        # "lines" and breaking title/company extraction. Replace only actual <br>
        # tags with newlines first, then join inline content with no separator.
        for br in text_el.find_all("br"):
            br.replace_with("\n")
        text = text_el.get_text().strip()
        if not text or len(text) < 20:
            continue

        links = [
            (unescape(a["href"]), a.get_text(strip=True))
            for a in text_el.find_all("a", href=True)
        ]

        time_el = wrap.find("time", attrs={"datetime": True})
        published = ""
        if time_el:
            try:
                published = time_el["datetime"][:10]
            except Exception:
                pass

        # Message permalink → extract numeric ID
        date_link = wrap.find("a", class_=lambda c: c and "tgme_widget_message_date" in c.split())
        msg_id = ""
        if date_link and date_link.get("href"):
            parts = date_link["href"].rstrip("/").split("/")
            msg_id = parts[-1] if parts else ""

        messages.append({
            "text": text,
            "links": links,
            "published": published,
            "msg_id": msg_id,
        })

    return messages


def _get_before_url(html: str, channel: str) -> str | None:
    """Return the paginated URL for older messages, or None if not found."""
    soup = BeautifulSoup(html, "html.parser")
    link = soup.find("a", class_=lambda c: c and "tme_messages_more" in c.split())
    if link and link.get("href"):
        return f"https://t.me/s/{channel.lstrip('@')}" + link["href"]
    # fallback: look for data-before attribute
    more = soup.find(attrs={"data-before": True})
    if more:
        return f"https://t.me/s/{channel.lstrip('@')}?before={more['data-before']}"
    return None


_PM_KEYWORDS = [
    "product manager", "head of product", "product lead", "product owner",
    "principal pm", "vp product", "director of product", "chief product",
    "продакт", "product director", "group pm",
]


def _expand_listing_page(
    listing_url: str, company: str, source: str, published: str, tg_url: str
) -> list[dict]:
    """Fetch a careers listing page and return individual PM job dicts found on it."""
    html = _fetch_page(listing_url)
    if not html:
        return []

    soup = BeautifulSoup(html, "html.parser")
    from urllib.parse import urljoin, urlparse

    base = f"{urlparse(listing_url).scheme}://{urlparse(listing_url).netloc}"
    seen: set[str] = set()
    jobs: list[dict] = []

    for a in soup.find_all("a", href=True):
        href = unescape(a["href"].strip())
        if not href or href.startswith("#"):
            continue
        full_url = urljoin(base, href)
        # Must be same domain and deeper path than the listing page
        if urlparse(full_url).netloc != urlparse(listing_url).netloc:
            continue
        if full_url == listing_url or full_url in seen:
            continue
        if _is_listing_page(full_url):
            continue

        link_text = a.get_text(strip=True).lower()
        url_slug = urlparse(full_url).path.lower()
        combined = f"{link_text} {url_slug}"
        if not any(kw in combined for kw in _PM_KEYWORDS):
            continue

        seen.add(full_url)
        title = a.get_text(strip=True) or full_url.rstrip("/").split("/")[-1].replace("-", " ").title()
        jobs.append({
            "title": title,
            "company": company,
            "url": full_url,
            "apply_url": full_url,
            "description": "",
            "_message_text": "",
            "_has_job_url": True,
            "location": "remote",
            "salary": "",
            "source": source,
            "published": published,
        })

    logger.info(f"Listing page {listing_url}: found {len(jobs)} PM role links")
    return jobs


def _fetch_channel(channel: str, cutoff_date: datetime, max_pages: int = 5) -> list[dict]:
    slug = channel.lstrip("@")
    url: str | None = f"https://t.me/s/{slug}"
    jobs = []
    pages_fetched = 0

    while url and pages_fetched < max_pages:
        html = _fetch_page(url)
        if not html:
            break

        messages = _parse_messages(html)
        pages_fetched += 1
        hit_cutoff = False

        for msg in reversed(messages):  # oldest first on page
            pub = msg.get("published", "")
            if pub:
                try:
                    msg_date = datetime.fromisoformat(pub).replace(tzinfo=timezone.utc)
                    if msg_date < cutoff_date:
                        hit_cutoff = True
                        continue
                except ValueError:
                    pass

            title, company = _extract_title_company(msg["text"])
            if not title or len(title) < 4:
                continue

            main_url = _pick_job_url(msg["links"], msg["text"])
            tg_url = f"https://t.me/{slug}/{msg['msg_id']}" if msg.get("msg_id") else f"https://t.me/{slug}"
            source = f"Telegram:{slug}"

            # If the only URL found is a listing page, expand it into individual roles
            if not main_url:
                listing_url = next(
                    (url for url, _ in msg["links"] if _is_listing_page(url) and "t.me/" not in url),
                    None,
                )
                if listing_url:
                    expanded = _expand_listing_page(listing_url, company, source, pub, tg_url)
                    if expanded:
                        jobs.extend(expanded)
                        continue
                    # Listing page inaccessible — fall through to create entry with t.me URL

            jobs.append({
                "title": title,
                "company": company,
                "url": main_url or tg_url,
                "apply_url": main_url,
                "description": "",
                "_message_text": msg["text"],
                "_has_job_url": bool(main_url),
                "location": _extract_location(msg["text"]),
                "salary": "",
                "source": f"Telegram:{slug}",
                "published": pub,
            })

        if hit_cutoff:
            break

        url = _get_before_url(html, slug)

    logger.info(f"Telegram @{slug}: {len(jobs)} messages parsed ({pages_fetched} pages)")
    return jobs


def fetch() -> list[dict]:
    from config import TELEGRAM_JOB_CHANNELS, MAX_JOB_AGE_DAYS

    if not TELEGRAM_JOB_CHANNELS:
        return []

    days = MAX_JOB_AGE_DAYS or 14
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    all_jobs = []
    for channel in TELEGRAM_JOB_CHANNELS:
        try:
            all_jobs.extend(_fetch_channel(channel, cutoff))
        except Exception as e:
            logger.error(f"Telegram: failed to fetch {channel}: {e}")

    logger.info(f"Telegram total: {len(all_jobs)} raw messages")
    return all_jobs
