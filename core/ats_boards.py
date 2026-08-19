"""Reusable ATS board listers for Greenhouse / Lever / Ashby (company_direct build,
2026-08-19). One function per provider, one normalized posting shape, no retries —
the callers decide severity (the enrich path swallows errors and moves on; the
company_direct source records health per company and gets another chance in 6h).

Response shapes were confirmed against live boards on 2026-08-19 (fixtures in
tests/fixtures/ats/): Lever answers an unknown slug with HTTP 200 and
{"ok": false, "error": "Document not found"} — not a 404; Ashby's documented
posting-api ships descriptionHtml/descriptionPlain in the listing itself.
"""
import logging
import re
from datetime import datetime, timezone
from html import unescape

import requests

from utils import strip_html, _GENERIC_HEADERS

logger = logging.getLogger(__name__)


class AtsBoardError(Exception):
    """Board could not be read: network, 5xx, unparseable response."""


class AtsBoardNotFound(AtsBoardError):
    """No board exists for this slug (404 / Lever ok:false / missing jobs key) —
    kept distinct from a transient failure because the reaction differs: this is
    the slug-rot signal that warrants re-verification, not a retry."""


# slug comes from the target_companies table (human/LLM-entered) and is inserted
# into a URL path — without this gate "acme/../x" or "acme?x=" would rewrite the
# request. find_apply_url()'s _slugify() can't produce such strings, but the DB can.
_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _check_slug(slug: str) -> None:
    if not _SLUG_RE.match(slug or ""):
        raise AtsBoardNotFound(f"invalid slug: {slug!r}")


def _get_json(url: str, *, timeout: float, params: dict | None = None):
    """The single HTTP seam — tests monkeypatch this, not requests globally."""
    resp = requests.get(url, params=params, headers=_GENERIC_HEADERS, timeout=timeout)
    return resp.status_code, (resp.json() if resp.content else None)


def _posting(id_, title, url, location="", description="", salary="", published=""):
    return {
        "id": str(id_), "title": title or "", "url": url or "",
        "location": location or "", "description": description or "",
        "salary": salary or "", "published": published or "",
    }


def list_greenhouse(slug: str, *, timeout: float = 10.0) -> list[dict]:
    _check_slug(slug)
    try:
        status, data = _get_json(
            f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", timeout=timeout)
    except AtsBoardError:
        raise
    except Exception as e:
        raise AtsBoardError(f"greenhouse/{slug}: {e}") from e
    if status == 404:
        raise AtsBoardNotFound(f"greenhouse/{slug}: 404")
    if status != 200 or not isinstance(data, dict) or "jobs" not in data:
        raise AtsBoardError(f"greenhouse/{slug}: http_{status}")
    postings = []
    for j in data["jobs"]:
        published = (j.get("first_published") or "")[:10]
        postings.append(_posting(
            j.get("id"), j.get("title"), j.get("absolute_url"),
            location=(j.get("location") or {}).get("name", ""),
            description="",  # not in the listing; fetch_description() gets it per PM posting
            published=published,
        ))
    return postings


def list_lever(slug: str, *, timeout: float = 10.0) -> list[dict]:
    _check_slug(slug)
    try:
        status, data = _get_json(
            f"https://api.lever.co/v0/postings/{slug}", params={"mode": "json"}, timeout=timeout)
    except AtsBoardError:
        raise
    except Exception as e:
        raise AtsBoardError(f"lever/{slug}: {e}") from e
    if status == 404 or (isinstance(data, dict) and data.get("ok") is False):
        raise AtsBoardNotFound(f"lever/{slug}: not found")
    if status == 403:
        raise AtsBoardError(f"lever/{slug}: http_403")
    if status != 200 or not isinstance(data, list):
        raise AtsBoardError(f"lever/{slug}: http_{status}")
    postings = []
    for j in data:
        created = j.get("createdAt")
        published = ""
        if isinstance(created, (int, float)) and created > 0:
            published = datetime.fromtimestamp(created / 1000, tz=timezone.utc).date().isoformat()
        postings.append(_posting(
            j.get("id"), j.get("text"), j.get("hostedUrl"),
            location=(j.get("categories") or {}).get("location", ""),
            description=strip_html(j.get("descriptionPlain") or j.get("description") or ""),
            published=published,
        ))
    return postings


def list_ashby(slug: str, *, timeout: float = 10.0) -> list[dict]:
    _check_slug(slug)
    try:
        status, data = _get_json(
            f"https://api.ashbyhq.com/posting-api/job-board/{slug}", timeout=timeout)
    except AtsBoardError:
        raise
    except Exception as e:
        raise AtsBoardError(f"ashby/{slug}: {e}") from e
    if status == 404:
        raise AtsBoardNotFound(f"ashby/{slug}: 404")
    if status != 200 or not isinstance(data, dict):
        raise AtsBoardError(f"ashby/{slug}: http_{status}")
    if "jobs" not in data:
        raise AtsBoardNotFound(f"ashby/{slug}: no job board in response")
    postings = []
    for j in data["jobs"]:
        location = j.get("location") or ""
        if j.get("isRemote") and "remote" not in location.lower():
            location = f"Remote — {location}" if location else "Remote"
        postings.append(_posting(
            j.get("id"), j.get("title"), j.get("jobUrl"),
            location=location,
            description=strip_html(unescape(j.get("descriptionHtml") or "")) or (j.get("descriptionPlain") or ""),
            published=(j.get("publishedAt") or "")[:10],
        ))
    return postings


LISTERS = {"greenhouse": list_greenhouse, "lever": list_lever, "ashby": list_ashby}


def fetch_description(ats: str, slug: str, posting_id: str, *, timeout: float = 10.0) -> str:
    """Description of a single posting by (ats, slug, id) — only Greenhouse needs it
    (its listing carries no content; ?content=true would ship the whole board's HTML).
    Keyed by slug+id rather than URL parsing on purpose: Greenhouse absolute_urls can
    live on a company's custom careers domain, which URL-parsers can't decode.
    Returns "" on any failure — an empty description is not a reason to lose a vacancy."""
    if ats != "greenhouse":
        return ""
    try:
        _check_slug(slug)
        status, data = _get_json(
            f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{posting_id}", timeout=timeout)
        if status == 200 and isinstance(data, dict):
            return strip_html(unescape(data.get("content") or ""))
    except Exception as e:
        logger.debug(f"fetch_description greenhouse/{slug}/{posting_id}: {e}")
    return ""
