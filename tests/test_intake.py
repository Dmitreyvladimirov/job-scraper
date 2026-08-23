"""Tests for the intake pipeline (core/intake.py) and its Telegram transport.

Nothing here touches Postgres, the scoring service or Anthropic: every boundary is
monkeypatched, so the assertions are about intake's own decisions — what it refuses,
what it dedupes, what it pays for — rather than about the services behind it.
"""
import os
import sys
import types
from pathlib import Path

import pytest

os.environ.setdefault("DASHBOARD_TOKEN", "test-token-for-pytest")
sys.path.insert(0, str(Path(__file__).parent.parent / "core"))

import intake  # noqa: E402
import tg_bot  # noqa: E402


def _result(score=84):
    """Stand-in for ats.ATSResult — only the fields intake and tg_bot read."""
    return types.SimpleNamespace(
        score=score, role_score=26, domain_score=24, domain_value_score=12,
        domain_exp_score=12, keyword_score=20, location_score=14, location_reason="",
        penalty=0, penalty_reason="", domain="ai", why_apply="AI platform work",
        why_not="No formal security background", matched=["APIs", "SaaS"], missed=["Kubernetes"],
    )


@pytest.fixture
def wired(monkeypatch):
    """A working world: metadata extraction, an empty jobs table, a scorer that
    answers 84. Individual tests override one piece to test one decision."""
    state = {"created": [], "scored": [], "job_id": 4242}

    monkeypatch.setattr(intake, "_call_claude", lambda system, user: (
        '{"title": "Senior Product Manager", "company": "Acme", '
        '"location": "Remote, EU", "salary": null}', {}))
    monkeypatch.setattr(intake.db, "find_manual_duplicate", lambda url, company, title: None)

    def _create(job, **kwargs):
        state["created"].append(job)
        return state["job_id"]

    monkeypatch.setattr(intake.db, "create_manual_job", _create)
    monkeypatch.setattr(intake.db, "get_job", lambda job_id: dict(
        state["created"][-1], id=job_id) if state["created"] else None)
    monkeypatch.setattr(intake.db, "update_job_scoring",
                        lambda job_id, result, prid: state["scored"].append((job_id, result.score)))
    monkeypatch.setattr(intake.scoring_client, "analyze_via_cloud",
                        lambda job, prid: (_result(), "none"))
    return state


JD = "We are hiring a Senior Product Manager for our AI platform. " * 20


# --- what counts as a link ------------------------------------------------------

def test_bare_url_is_a_link():
    assert intake.looks_like_url("https://jobs.lever.co/acme/123")


def test_pasted_jd_mentioning_a_url_is_not_a_link():
    # The regression this guards: treating a JD that ends with "apply at https://..."
    # as a link would throw away the description the user already provided.
    assert not intake.looks_like_url(f"{JD} Apply at https://acme.com/jobs/1")


# --- refusals -------------------------------------------------------------------

def test_short_pasted_text_is_refused_not_scored(wired):
    result = intake.ingest("Senior PM, remote, apply now")
    assert result.status == "error"
    assert not wired["created"], "a stub must not become a card"


def test_linkedin_link_asks_for_text_without_fetching(monkeypatch):
    # No fetch stub is installed: if intake tried to fetch, the real network call
    # would make this test slow and flaky. Reaching need_text proves it short-circuits.
    monkeypatch.setattr(intake.utils, "_validate_url", lambda url: True)
    result = intake.ingest("https://www.linkedin.com/jobs/view/12345")
    assert result.status == "need_text"
    assert "текст" in result.message


def test_unfetchable_link_asks_for_text(monkeypatch, wired):
    monkeypatch.setattr(intake.utils, "_validate_url", lambda url: True)
    monkeypatch.setattr(intake.utils, "fetch_posting", lambda url: {})
    monkeypatch.setattr(intake.utils, "fetch_url_generic", lambda url, max_chars=0: "")
    monkeypatch.setattr(intake.utils, "_fetch_via_scrapingbee", lambda url: "")
    result = intake.ingest("https://acme.com/careers/1")
    assert result.status == "need_text"
    assert result.url == "https://acme.com/careers/1"
    assert not wired["created"]


# --- the happy paths ------------------------------------------------------------

def test_pasted_jd_becomes_a_scored_card(wired):
    result = intake.ingest(JD, source="Manual (Telegram)")
    assert result.status == "ok"
    assert (result.title, result.company) == ("Senior Product Manager", "Acme")
    assert result.score == 84
    assert wired["scored"] == [(4242, 84)]
    assert wired["created"][0]["source"] == "Manual (Telegram)"


def test_ats_link_is_fetched_and_scored(monkeypatch, wired):
    monkeypatch.setattr(intake.utils, "_validate_url", lambda url: True)
    monkeypatch.setattr(intake.utils, "fetch_posting",
                        lambda url: {"description": JD, "title": "PM, Platform"})
    result = intake.ingest("https://jobs.lever.co/acme/123")
    assert result.status == "ok"
    assert wired["created"][0]["url"] == "https://jobs.lever.co/acme/123"


def test_url_hint_keeps_the_link_on_a_text_card(wired):
    """The second half of the LinkedIn flow: the link was refused, the text arrives
    next, and the card must still carry the URL to apply through."""
    result = intake.ingest(JD, url_hint="https://www.linkedin.com/jobs/view/12345")
    assert result.status == "ok"
    assert wired["created"][0]["url"] == "https://www.linkedin.com/jobs/view/12345"


def test_scoring_failure_still_leaves_a_card(monkeypatch, wired):
    monkeypatch.setattr(intake.scoring_client, "analyze_via_cloud",
                        lambda job, prid: (None, "transient"))
    result = intake.ingest(JD)
    assert result.status == "scored_off"
    assert result.job_id == 4242 and wired["created"], "the card is worth keeping without a score"


# --- dedup ----------------------------------------------------------------------

def test_known_vacancy_returns_the_existing_card(monkeypatch, wired):
    monkeypatch.setattr(intake.db, "find_manual_duplicate", lambda url, company, title: {
        "id": 77, "title": "Senior Product Manager", "company": "Acme",
        "current_status": "applied", "source": "Jobgether", "ats_score": 81})
    result = intake.ingest(JD)
    assert result.status == "duplicate"
    assert result.job_id == 77
    assert not wired["created"], "a duplicate must not create a twin"
    assert not wired["scored"], "a duplicate must not be paid for again"


def test_force_overrides_dedup(monkeypatch, wired):
    monkeypatch.setattr(intake.db, "find_manual_duplicate", lambda url, company, title: {"id": 77})
    result = intake.ingest(JD, force=True)
    assert result.status == "ok"
    assert wired["created"]


# --- metadata extraction --------------------------------------------------------

def test_metadata_survives_a_fenced_reply(monkeypatch):
    monkeypatch.setattr(intake, "_call_claude", lambda s, u: (
        'Here you go:\n```json\n{"title": "PM", "company": "Acme", '
        '"location": null, "salary": null}\n```', {}))
    meta = intake._extract_meta(JD)
    assert meta["title"] == "PM" and meta["company"] == "Acme"
    assert meta["location"] is None


def test_metadata_failure_falls_back_to_the_board_title(monkeypatch):
    def _boom(system, user):
        raise RuntimeError("no API key")

    monkeypatch.setattr(intake, "_call_claude", _boom)
    assert intake._extract_meta(JD, title_hint="PM, Platform") == {"title": "PM, Platform"}


# --- resume gates ---------------------------------------------------------------

def test_resume_blocked_without_company():
    assert "Company" in intake.resume_block_reason({"company": "", "description": JD})


def test_resume_blocked_on_a_stub_jd():
    assert "JD is under" in intake.resume_block_reason({"company": "Acme", "description": "short"})


def test_resume_allowed_on_a_full_card():
    assert intake.resume_block_reason({"company": "Acme", "description": JD}) is None


def test_existing_resume_is_not_regenerated(monkeypatch):
    monkeypatch.setattr(intake.db, "get_job", lambda job_id: {
        "id": job_id, "company": "Acme", "description": JD, "resume_run_id": 9})

    def _never(job, prid):
        raise AssertionError("a second generation must never be paid for")

    monkeypatch.setattr(intake.resume_client, "generate_via_cloud", _never)
    assert intake.generate_resume(1) == (9, None)


def test_resume_generation_records_the_run(monkeypatch):
    recorded = {}
    monkeypatch.setattr(intake.db, "get_job", lambda job_id: {
        "id": job_id, "company": "Acme", "description": JD, "resume_run_id": None})
    monkeypatch.setattr(intake.resume_client, "generate_via_cloud", lambda job, prid: (
        types.SimpleNamespace(generation_run_id=55, skeptic_findings=[]), "none"))
    monkeypatch.setattr(intake.db, "set_resume_run_id",
                        lambda job_id, run_id: recorded.update(job_id=job_id, run_id=run_id))
    assert intake.generate_resume(1) == (55, None)
    assert recorded == {"job_id": 1, "run_id": 55}


# --- Telegram transport ---------------------------------------------------------

def test_only_the_owner_chat_is_served(monkeypatch):
    monkeypatch.setattr(tg_bot, "TELEGRAM_CHAT_ID", "75785258")
    assert tg_bot._allowed(75785258)
    assert not tg_bot._allowed(11111111)


def test_no_chat_id_configured_means_nobody_is_allowed(monkeypatch):
    monkeypatch.setattr(tg_bot, "TELEGRAM_CHAT_ID", "")
    assert not tg_bot._allowed(75785258), "an unset allowlist must not fall open"


def test_pending_url_expires(monkeypatch):
    tg_bot._pending_url[1] = ("https://acme.com/1", 0.0)  # epoch = long expired
    assert tg_bot._take_pending_url(1) is None
    assert 1 not in tg_bot._pending_url, "an expired entry is consumed, not left behind"


def test_pending_url_is_returned_once():
    import time as _time
    tg_bot._pending_url[2] = ("https://acme.com/2", _time.time())
    assert tg_bot._take_pending_url(2) == "https://acme.com/2"
    assert tg_bot._take_pending_url(2) is None


def test_render_offers_a_resume_button_when_the_card_supports_one():
    result = intake.IntakeResult(status="ok", job_id=7, title="PM", company="Acme",
                                 score=84, result=_result())
    text, markup = tg_bot._render(result)
    assert "84/100" in text and "#7" in text
    buttons = [b["callback_data"] for b in markup["inline_keyboard"][0]]
    assert buttons == ["resume:7", "rescore:7"]


def test_render_hides_the_resume_button_when_blocked():
    result = intake.IntakeResult(status="ok", job_id=7, title="PM", score=61,
                                 result=_result(61), resume_blocked="Company is required")
    text, markup = tg_bot._render(result)
    assert "🚫" in text, "below the threshold the verdict must read as a no"
    buttons = [b["callback_data"] for b in markup["inline_keyboard"][0]]
    assert buttons == ["rescore:7"]


def test_render_need_text_has_no_buttons():
    text, markup = tg_bot._render(
        intake.IntakeResult(status="need_text", message="пришли текст вакансии"))
    assert markup is None and "текст" in text


def test_bot_reports_a_crash_instead_of_hanging(monkeypatch):
    """A DB failure under intake must rewrite the placeholder, not leave the user
    staring at "принял, разбираю…" — a hung bot is indistinguishable from a slow one."""
    sent = []
    monkeypatch.setattr(tg_bot, "TELEGRAM_CHAT_ID", "1")
    monkeypatch.setattr(tg_bot.telegram, "send_message",
                        lambda text, chat_id=None, reply_markup=None: sent.append(text) or 100)
    monkeypatch.setattr(tg_bot.telegram, "edit_message",
                        lambda mid, text, chat_id=None, reply_markup=None: sent.append(text))

    def _boom(raw, **kwargs):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(tg_bot.intake, "ingest", _boom)
    tg_bot._handle_message({"chat": {"id": 1}, "text": "a" * 500})
    assert "Сломалось на разборе" in sent[-1] and "connection refused" in sent[-1]


def test_board_location_beats_the_models_guess(monkeypatch, wired):
    """Ashby's board API states the location outright; location is 15 of the 100
    scoring points, and the pipeline has lost a whole rubric axis this way before."""
    monkeypatch.setattr(intake.utils, "_validate_url", lambda url: True)
    monkeypatch.setattr(intake.utils, "fetch_posting", lambda url: {
        "description": JD, "title": "TPM, Data Ingestion", "location": "Remote"})
    result = intake.ingest("https://jobs.ashbyhq.com/protege/abc")
    assert result.location == "Remote", "the model said 'Remote, EU'; the board said Remote"
    assert wired["created"][0]["location"] == "Remote"


def test_html_fallback_carries_no_location(monkeypatch, wired):
    # A scraped page states nothing authoritatively — the model's read is all there is.
    monkeypatch.setattr(intake.utils, "_validate_url", lambda url: True)
    monkeypatch.setattr(intake.utils, "fetch_posting", lambda url: {})
    monkeypatch.setattr(intake.utils, "fetch_url_generic", lambda url, max_chars=0: JD)
    result = intake.ingest("https://acme.com/careers/1")
    assert result.location == "Remote, EU"


# --- duplicate probe normalisation (2026-08-23) ---------------------------------
# db.find_manual_duplicate matched on exact strings, so one posting seen twice with
# a different title suffix or a different tracking tail became two cards. These test
# the normalisation only — the SQL around it needs a database.

def test_title_suffix_does_not_make_a_new_vacancy():
    """The live failure: FinAgra landed as two cards on 2026-08-23."""
    from db import _normalize_posting_title as norm
    assert norm("Senior Product Manager (FinAgra)") == norm("Senior Product Manager")
    assert norm("Product Manager - Marketplace") == norm("product manager   marketplace")


def test_genuinely_different_roles_stay_different():
    # Adapty is hiring both; collapsing them would hide a real vacancy, which is a
    # worse failure than a twin card.
    from db import _normalize_posting_title as norm
    assert norm("Head of Product") != norm("Product Lead, Analytics & ML")
    assert norm("Partner Product Manager") != norm("Product Manager - Marketplace")


def test_tracking_parameters_do_not_make_a_new_vacancy():
    from db import _normalize_posting_url as norm
    base = "https://finagra.com/careers"
    assert norm(base + "?ashby_jid=badba632&utm_source=yr9PYa8Yz0") == base
    assert norm(base + "/") == base
    assert norm(None) == ""


def test_different_postings_on_one_board_stay_different():
    from db import _normalize_posting_url as norm
    assert norm("https://jobs.ashbyhq.com/supabase/aaa") != norm("https://jobs.ashbyhq.com/supabase/bbb")
