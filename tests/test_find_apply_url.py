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


def test_enrich_url_never_overwrites_source_supplied_apply_url(monkeypatch):
    # Jobgether ships its own authoritative applyUrl; the weak title-matcher must
    # not replace it (live bug: cards pointed at the wrong position of the right
    # company — Deliveroo/Smartsheet/Vanta, 2026-08-19).
    called = []
    monkeypatch.setattr(utils, "find_apply_url", lambda c, t: called.append(1) or "https://wrong")
    job = {"url": "https://jobgether.com/offer/abc", "company": "Vanta",
           "title": "Senior PM", "apply_url": "https://jobs.ashbyhq.com/vanta/right-id/application"}
    utils.enrich_url(job)
    assert job["apply_url"] == "https://jobs.ashbyhq.com/vanta/right-id/application"
    assert called == []  # matcher not even consulted


# --- Wrong-posting bug, 2026-08-25 (job 80194 @ Hopper) ------------------------
# The Jobicy card "Principal Product Manager - Pricing and Personalization"
# (Ireland) linked to Hopper's French Montréal posting for a different role,
# because the matcher accepted a 2-word overlap and never looked at location.

_HOPPER_PRICING = "Principal Product Manager - Pricing and Personalization"
_HOPPER_CONVERSATIONAL = ("Directeur ou directrice principal(e) de produit, IA conversationnelle"
                          "// Principal Product Manager- Conversational AI ")


def test_title_match_rejects_a_different_role_with_the_same_prefix():
    assert not utils._title_match(_HOPPER_PRICING, _HOPPER_CONVERSATIONAL)
    assert utils._title_match(_HOPPER_PRICING, _HOPPER_PRICING)


def test_title_match_tolerates_board_bookkeeping_in_the_title():
    # One miss per five words, and "(100% Remote - USA)" is not evidence either way.
    assert utils._title_match("Senior Product Manager, Growth (Remote)",
                              "Senior Product Manager - Growth & Retention (100% Remote - USA)")


def _ashby_board(postings):
    return [("jobs.ashbyhq.com/api/non-user-graphql",
             _Resp(200, {"data": {"jobBoard": {"jobPostings": postings}}}))]


_HOPPER_POSTINGS = [
    {"id": "conversational-fr", "title": _HOPPER_CONVERSATIONAL, "locationName": "Montréal - Remote"},
    {"id": "pricing-ie", "title": _HOPPER_PRICING, "locationName": "Ireland - Remote"},
    {"id": "pricing-ca", "title": _HOPPER_PRICING, "locationName": "Toronto - Remote"},
]


def test_ashby_same_title_in_two_countries_is_resolved_by_location(monkeypatch):
    _install(monkeypatch, _ashby_board(_HOPPER_POSTINGS),
             page_titles={"https://jobs.ashbyhq.com/hopper": "Hopper Jobs"})
    url = utils.find_apply_url("Hopper", _HOPPER_PRICING, "Remote — Ireland")
    assert url == "https://jobs.ashbyhq.com/hopper/pricing-ie"


def test_ashby_accent_folded_location_still_matches(monkeypatch):
    _install(monkeypatch, _ashby_board(_HOPPER_POSTINGS),
             page_titles={"https://jobs.ashbyhq.com/hopper": "Hopper Jobs"})
    assert utils.find_apply_url("Hopper", _HOPPER_CONVERSATIONAL,
                                "Remote — Montreal") == "https://jobs.ashbyhq.com/hopper/conversational-fr"


def test_ashby_unresolvable_location_returns_none_rather_than_a_guess(monkeypatch):
    # Two equally plausible postings and a location that singles out neither:
    # no link is recoverable downstream, a link to the wrong country is not.
    _install(monkeypatch, _ashby_board(_HOPPER_POSTINGS),
             page_titles={"https://jobs.ashbyhq.com/hopper": "Hopper Jobs"})
    assert utils.find_apply_url("Hopper", _HOPPER_PRICING, "Remote — Anywhere") is None


def test_greenhouse_location_picks_the_right_country(monkeypatch):
    _install(monkeypatch, [
        ("boards-api.greenhouse.io/v1/boards/acme/jobs", _Resp(200, {"jobs": [
            {"title": "Senior Product Manager, Platform", "location": {"name": "Berlin, Germany"},
             "absolute_url": "https://boards.greenhouse.io/acme/jobs/1"},
            {"title": "Senior Product Manager, Platform", "location": {"name": "Tel Aviv, Israel"},
             "absolute_url": "https://boards.greenhouse.io/acme/jobs/2"},
        ]})),
        ("boards-api.greenhouse.io/v1/boards/acme", _Resp(200, {"name": "Acme"})),
    ])
    url = utils.find_apply_url("Acme", "Senior Product Manager, Platform", "Tel Aviv, Israel")
    assert url == "https://boards.greenhouse.io/acme/jobs/2"


def test_lever_location_picks_the_right_country(monkeypatch):
    _install(monkeypatch, [
        ("api.lever.co/v0/postings/acme", _Resp(200, [
            {"text": "Senior Product Manager, Growth", "categories": {"location": "New York"},
             "hostedUrl": "https://jobs.lever.co/acme/us"},
            {"text": "Senior Product Manager, Growth", "categories": {"location": "Dublin, Ireland"},
             "hostedUrl": "https://jobs.lever.co/acme/ie"},
        ])),
    ], page_titles={"https://jobs.lever.co/acme": "Acme"})
    assert utils.find_apply_url("Acme", "Senior Product Manager, Growth",
                                "Remote — Ireland") == "https://jobs.lever.co/acme/ie"


# --- Jobicy's own apply link wins over the name-guess --------------------------

class _Page:
    """A fetched HTML page, as _safe_get() returns it."""

    def __init__(self, status_code, text):
        self.status_code = status_code
        self.text = text


_JOBICY_PAGE = ("<html><script>var asteroid={'action':'act1','nonce':'n1',"
                "'post_id':151539,'increment_clicks':true};</script></html>")


def test_jobicy_source_link_is_preferred_over_the_title_matcher(monkeypatch):
    guessed = []
    _install(monkeypatch, [
        ("jobicy.com/signals.php",
         _Resp(200, {"url": "https://jobs.ashbyhq.com/hopper/pricing-ie/application"})),
    ])
    monkeypatch.setattr(utils, "_safe_get", lambda url, **kw: _Page(200, _JOBICY_PAGE))
    monkeypatch.setattr(utils, "find_apply_url",
                        lambda c, t, loc="": guessed.append(1) or "https://jobs.ashbyhq.com/hopper/wrong-role")
    job = {"url": "https://jobicy.com/jobs/151539-principal-product-manager",
           "company": "Hopper", "title": _HOPPER_PRICING, "location": "Remote — Ireland"}
    utils.enrich_url(job)
    # /application trimmed so fetch_posting() can still read the job id off the path
    assert job["apply_url"] == "https://jobs.ashbyhq.com/hopper/pricing-ie"
    assert guessed == []


def test_jobicy_falls_back_to_the_matcher_when_the_page_gives_nothing(monkeypatch):
    _install(monkeypatch, [])
    monkeypatch.setattr(utils, "_safe_get", lambda url, **kw: _Page(404, ""))
    monkeypatch.setattr(utils, "find_apply_url",
                        lambda c, t, loc="": "https://jobs.ashbyhq.com/hopper/pricing-ie")
    job = {"url": "https://jobicy.com/jobs/1-x", "company": "Hopper",
           "title": _HOPPER_PRICING, "location": "Remote — Ireland"}
    utils.enrich_url(job)
    assert job["apply_url"] == "https://jobs.ashbyhq.com/hopper/pricing-ie"


def test_normalize_apply_url_trims_application_suffix_and_tracking():
    assert utils._normalize_apply_url(
        "https://jobs.ashbyhq.com/acme/abc/application?utm_source=jobicy&ref=x"
    ) == "https://jobs.ashbyhq.com/acme/abc?ref=x"
    assert utils._normalize_apply_url(
        "https://boards.greenhouse.io/acme/jobs/1?utm_medium=feed"
    ) == "https://boards.greenhouse.io/acme/jobs/1"
