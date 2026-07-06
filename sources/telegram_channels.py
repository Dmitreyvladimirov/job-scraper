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
from urllib.parse import urlparse

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

_PM_ROLE_RE = re.compile(
    r"\b(?:"
    r"(?:senior|lead|principal|technical|growth|group|ai|head|chief|vp|director)?\s*"
    r"product\s+(?:manager|owner|lead|director)|"
    r"head\s+of\s+product|chief\s+product\s+officer|cpo|"
    r"продакт(?:-менеджер|-программист|-лид)?|"
    r"руководител[ья]\s+продукт"
    r")\b",
    re.IGNORECASE,
)

_TITLE_PREFIX_RE = re.compile(
    r"^(?:вакансия|vacancy|позиция|position|роль|role)\s*[:—-]\s*",
    re.IGNORECASE,
)

_INVALID_COMPANY_RE = re.compile(
    r"^(?:remote|worldwide|удал[её]нно|senior|middle|junior|"
    r"russia|россия|serbia|bulgaria|germany|hungary|singapore|"
    r"полная занятость|гибрид|офис)$",
    re.IGNORECASE,
)


def _is_absolute_http_url(url: str) -> bool:
    try:
        parsed = urlparse((url or "").strip())
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)
    except (TypeError, ValueError):
        return False


def _is_navigation_url(url: str) -> bool:
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")
    if parsed.fragment and not path:
        return True
    if path.lower() in {"", "/search", "/jobs/search"}:
        query = parsed.query.lower()
        return not query or bool(re.search(r"(?:^|&)(?:q|query|search)=", query))
    return False


def _has_pm_signal(text: str) -> bool:
    return bool(_PM_ROLE_RE.search(text or ""))


def _clean_title(value: str) -> str:
    value = _TITLE_PREFIX_RE.sub("", value.strip())
    value = re.sub(r"https?://\S+", "", value).strip(" \t:—-•")
    return value


def _clean_company(value: str) -> str:
    value = re.sub(r"^[\s:—-]+|[\s:—-]+$", "", value)
    value = re.sub(
        r"\s+(?:\(|,)\s*(?:senior|middle|junior).*$",
        "",
        value,
        flags=re.IGNORECASE,
    )
    if not value or len(value) > 80 or _INVALID_COMPANY_RE.match(value):
        return ""
    return value


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
    if not _is_absolute_http_url(url) or _is_navigation_url(url):
        return False
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


def _extract_title_company(text: str) -> tuple[str, str]:
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    first_line = lines[0] if lines else ""
    first_line = re.sub(r"^[\U00010000-\U0010ffff☀-⟿⬀-⯿\s]+", "", first_line)
    first_line_clean = _clean_title(first_line)

    title_index = 0
    title_line = first_line_clean
    if not _has_pm_signal(title_line):
        for idx, line in enumerate(lines):
            candidate = _clean_title(line)
            if _has_pm_signal(candidate):
                title_index = idx
                title_line = candidate
                break

    # Explicit company labels near the selected role are the strongest signal.
    for line in lines[title_index + 1:title_index + 5]:
        m = re.match(
            r"^(?:компания|company|работодатель)\s*[:—-]\s*(.+)$",
            line,
            re.IGNORECASE,
        )
        if m:
            company = _clean_company(m.group(1))
            if company:
                return title_line, company

    for pattern in (
        r"^(.+?)\s+в\s+(.+?)(?:\s*[:(—\-]|$)",
        r"^(.+?)\s+(?:at|@)\s+(.+?)(?:\s*[:(—\-]|$)",
    ):
        m = re.match(pattern, title_line, re.IGNORECASE)
        if m:
            return _clean_title(m.group(1)), _clean_company(m.group(2))

    # A digest may name the company in its first role and mention the PM role
    # later. Reuse only that company; never replace the selected PM title.
    for line in (first_line_clean, first_line):
        for pattern in (
            r"^(.+?)\s+в\s+(.+?)(?:\s*[:(—\-]|$)",
            r"^(.+?)\s+(?:at|@)\s+(.+?)(?:\s*[:(—\-]|$)",
        ):
            m = re.match(pattern, line, re.IGNORECASE)
            if m:
                return title_line, _clean_company(m.group(2))
    return title_line or first_line_clean or first_line, ""


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
        if not _is_absolute_http_url(url) or _is_navigation_url(url):
            continue
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
        if not _is_absolute_http_url(url) or _is_navigation_url(url):
            continue
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
        if not _is_absolute_http_url(full_url):
            continue
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


def _fetch_channel(
    channel: str,
    cutoff_date: datetime,
    max_pages: int = 5,
    metrics: dict | None = None,
) -> list[dict]:
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

            if metrics is not None:
                metrics["fetched"] += 1
                if _has_pm_signal(msg["text"]):
                    metrics["pm_signal"] += 1

            title, company = _extract_title_company(msg["text"])
            if not title or len(title) < 4:
                continue

            main_url = _pick_job_url(msg["links"], msg["text"])
            tg_url = f"https://t.me/{slug}/{msg['msg_id']}" if msg.get("msg_id") else f"https://t.me/{slug}"
            source = f"Telegram:{slug}"

            # If the only URL found is a listing page, expand it into individual roles
            if not main_url:
                listing_url = next(
                    (
                        url for url, _ in msg["links"]
                        if _is_absolute_http_url(url)
                        and _is_listing_page(url)
                        and "t.me/" not in url
                    ),
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


def fetch_with_metrics() -> tuple[list[dict], dict[str, dict[str, int]]]:
    from config import TELEGRAM_JOB_CHANNELS, MAX_JOB_AGE_DAYS

    if not TELEGRAM_JOB_CHANNELS:
        return [], {}

    days = MAX_JOB_AGE_DAYS or 14
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    all_jobs = []
    channel_metrics: dict[str, dict[str, int]] = {}
    for channel in TELEGRAM_JOB_CHANNELS:
        slug = channel.lstrip("@")
        metrics = {
            "fetched": 0,
            "pm_signal": 0,
            "role_pass": 0,
            "enriched": 0,
            "quality_gate": 0,
            "ats": 0,
            "qualified": 0,
        }
        channel_metrics[slug] = metrics
        try:
            all_jobs.extend(_fetch_channel(channel, cutoff, metrics=metrics))
        except Exception as e:
            logger.error(f"Telegram: failed to fetch {channel}: {e}")

    logger.info(f"Telegram total: {len(all_jobs)} raw messages")
    return all_jobs, channel_metrics


def fetch() -> list[dict]:
    jobs, _ = fetch_with_metrics()
    return jobs
