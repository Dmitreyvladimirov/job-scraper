"""Characterization tests for utils.find_apply_url() — written BEFORE the
ats_boards refactor (company_direct build, step 1). utils.py had zero tests while
sitting in the live cron path; these pin the current behavior byte-for-byte so
the refactor can be verified against them.

HTTP is faked by monkeypatching utils.requests.get/post with a URL router; the
weak page-title signal is faked via utils._page_title.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "core"))

import utils  # noqa: E402


class _Resp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class _Router:
    """Routes fake HTTP by substring of the URL; records calls in order."""

    def __init__(self, rules):
        self.rules = rules  # list of (substring, response_or_exception)
        self.calls = []

    def _match(self, url):
        self.calls.append(url)
        for sub, resp in self.rules:
            if sub in url:
                if isinstance(resp, Exception):
                    raise resp
                return resp
        return _Resp(404, {})

    def get(self, url, **kw):
        return self._match(url)

    def post(self, url, **kw):
        return self._match(url)


def _install(monkeypatch, rules, page_titles=None):
    router = _Router(rules)
    monkeypatch.setattr(utils.requests, "get", router.get)
    monkeypatch.setattr(utils.requests, "post", router.post)
    monkeypatch.setattr(utils, "_page_title",
                        lambda url: (page_titles or {}).get(url, ""))
    return router


_GH_JOBS = {"jobs": [
    {"title": "Senior Product Manager, Platform", "absolute_url": "https://boards.greenhouse.io/acme/jobs/123"},
    {"title": "Account Executive", "absolute_url": "https://boards.greenhouse.io/acme/jobs/456"},
]}


def test_greenhouse_happy_path_returns_absolute_url(monkeypatch):
    _install(monkeypatch, [
        ("boards-api.greenhouse.io/v1/boards/acme/jobs", _Resp(200, _GH_JOBS)),
        ("boards-api.greenhouse.io/v1/boards/acme", _Resp(200, {"name": "Acme"})),
    ])
    url = utils.find_apply_url("Acme", "Senior Product Manager, Platform")
    assert url == "https://boards.greenhouse.io/acme/jobs/123"


def test_greenhouse_name_mismatch_is_not_trusted(monkeypatch):
    # Board resolves but belongs to a different company -> no result from GH,
    # and Lever/Ashby (404 here) yield nothing -> None.
    _install(monkeypatch, [
        ("boards-api.greenhouse.io/v1/boards/insider/jobs", _Resp(200, _GH_JOBS)),
        ("boards-api.greenhouse.io/v1/boards/insider", _Resp(200, {"name": "Business Insider"})),
    ])
    assert utils.find_apply_url("Insider", "Senior Product Manager, Platform") is None


def test_lever_match_verified_by_board_page_title(monkeypatch):
    _install(monkeypatch, [
        ("api.lever.co/v0/postings/acme", _Resp(200, [
            {"text": "Senior Product Manager, Growth", "hostedUrl": "https://jobs.lever.co/acme/uuid-1"},
        ])),
    ], page_titles={"https://jobs.lever.co/acme": "Acme"})
    url = utils.find_apply_url("Acme", "Senior Product Manager, Growth")
    assert url == "https://jobs.lever.co/acme/uuid-1"


def test_lever_unverifiable_board_title_is_skipped(monkeypatch):
    # Title matches a posting, but the board page gives no company name ->
    # current behavior: do not trust it, return None (documented false-negative cost).
    _install(monkeypatch, [
        ("api.lever.co/v0/postings/acme", _Resp(200, [
            {"text": "Senior Product Manager, Growth", "hostedUrl": "https://jobs.lever.co/acme/uuid-1"},
        ])),
    ], page_titles={})
    assert utils.find_apply_url("Acme", "Senior Product Manager, Growth") is None


def test_ashby_url_is_constructed_from_slug_and_id(monkeypatch):
    _install(monkeypatch, [
        ("jobs.ashbyhq.com/api/non-user-graphql", _Resp(200, {
            "data": {"jobBoard": {"jobPostings": [
                {"id": "p-42", "title": "Senior Product Manager, API"},
            ]}},
        })),
    ], page_titles={"https://jobs.ashbyhq.com/acme": "Acme Jobs"})
    url = utils.find_apply_url("Acme", "Senior Product Manager, API")
    assert url == "https://jobs.ashbyhq.com/acme/p-42"


def test_network_exceptions_everywhere_return_none(monkeypatch):
    _install(monkeypatch, [
        ("greenhouse", ConnectionError("boom")),
        ("lever", ConnectionError("boom")),
        ("ashby", ConnectionError("boom")),
    ])
    assert utils.find_apply_url("Acme", "Senior Product Manager") is None


def test_slug_variants_are_tried_in_order(monkeypatch):
    # "Acme Inc" -> variants include "acme-inc", "acmeinc", "acme"; the GH board
    # only exists for the suffix-stripped one.
    router = _install(monkeypatch, [
        ("boards-api.greenhouse.io/v1/boards/acme/jobs", _Resp(200, _GH_JOBS)),
        ("boards-api.greenhouse.io/v1/boards/acme", _Resp(200, {"name": "Acme Inc"})),
    ])
    url = utils.find_apply_url("Acme Inc", "Senior Product Manager, Platform")
    assert url == "https://boards.greenhouse.io/acme/jobs/123"
    assert any("acme-inc" in c for c in router.calls)  # earlier variant was tried first
