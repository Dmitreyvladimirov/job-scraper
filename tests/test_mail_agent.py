"""Mail agent — the decision logic, which is where the risk lives.

Cases are taken from real emails labelled on 2026-08-20/22 (Triple Whale's 4-email
interview sequence, the Payoneer multi-application case, Usersnap's frozen role,
HRplus's unfinished application)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "core"))

import mail_agent  # noqa: E402


def _card(id_=1, status="applied", company="Acme"):
    return {"id": id_, "title": "Senior PM", "company": company,
            "current_status": status, "url": "", "apply_url": ""}


# --- sender_domain: the ~40% signal ---

def test_neutral_ats_hosts_yield_no_company():
    for addr in ("no-reply@ashbyhq.com", "no-reply@us.greenhouse-mail.io",
                 "no-reply@hire.lever.co", "olivia318@paradox.ai",
                 "723be1f6-9744@pinpoint.email", "servicetitan@myworkday.com"):
        assert mail_agent.sender_domain(addr) == "", addr


def test_comeet_style_subdomain_carries_the_company():
    # Real senders: Comeet and Teamtailor put the company in a subdomain.
    assert mail_agent.sender_domain("no-reply@kornit.comeet-notifications.com") == "kornit"
    assert mail_agent.sender_domain("no-reply@guesty.comeet-notifications.com") == "guesty"
    assert mail_agent.sender_domain("katia@insense.na.teamtailor-mail.com") == "insense"


def test_company_own_domain_is_kept():
    assert mail_agent.sender_domain("noreply@armis.com") == "armis.com"
    assert mail_agent.sender_domain("naama.fine@safebreach.com") == "safebreach.com"


# --- decide(): idempotency, ambiguity, note-only classes ---

def test_acknowledgement_never_moves_a_card():
    # An ATS auto-reply is not a human reply — counting it as one inflates reply rate.
    out = mail_agent.decide("acknowledgement", [_card()])
    assert out["proposed_status"] is None and out["action"] == "ignored"


def test_interview_sequence_does_not_walk_the_card_forward():
    # Triple Whale sent invite → confirmed → calendar → reminder in 3 days.
    invite = mail_agent.decide("interview_invite", [_card(status="applied")])
    assert invite["proposed_status"] == "recruiter_reply"
    scheduled = mail_agent.decide("interview_scheduled", [_card(status="recruiter_reply")])
    assert scheduled["proposed_status"] == "screen"
    # reminder + calendar invite must be inert
    for label in ("interview_reminder", "calendar_invite"):
        out = mail_agent.decide(label, [_card(status="screen")])
        assert out["proposed_status"] is None, label


def test_stage_already_reached_is_a_no_op():
    # Dimitry moved the card manually; the email arrives afterwards.
    out = mail_agent.decide("interview_invite", [_card(status="interview")])
    assert out["action"] == "ignored" and out["proposed_status"] is None


def test_rejection_proposes_but_never_auto_applies():
    out = mail_agent.decide("rejection", [_card()])
    assert out["proposed_status"] == "rejected"
    assert out["proposed_reason"] == "company_rejected"
    assert out["action"] == "pending"  # a false auto-reject would trigger the 180-day company cooldown


def test_second_rejection_for_a_closed_card_is_a_no_op():
    out = mail_agent.decide("rejection", [_card(status="rejected")])
    assert out["action"] == "ignored"


def test_frozen_role_is_not_a_personal_rejection():
    # Usersnap: "hiring for this position is currently on hold"
    out = mail_agent.decide("position_on_hold", [_card()])
    assert out["proposed_reason"] == "inactive_closed"
    assert out["proposed_status"] == "rejected" and out["action"] == "pending"


def test_ambiguous_company_is_never_guessed():
    # The Payoneer case: several open applications at one company.
    out = mail_agent.decide("rejection", [_card(1), _card(2), _card(3)])
    assert out["job_id"] is None and out["action"] == "pending"
    assert "3 candidate cards" in out["note"]


def test_unmatched_email_is_recorded_not_dropped():
    out = mail_agent.decide("rejection", [])
    assert out["action"] == "pending" and out["job_id"] is None


def test_noise_touches_nothing():
    out = mail_agent.decide("noise", [_card()])
    assert out["action"] == "ignored" and out["proposed_status"] is None


# --- classify(): the closed-set guarantee ---

def _llm(payload):
    return lambda system, user: (payload, {"cost_usd": 0.0001})


def test_unknown_label_degrades_to_noise_not_to_a_status():
    parsed, err = mail_agent.classify(
        {"message_id": "x"}, _llm('{"label": "reject_everything", "company": "Acme"}'))
    assert parsed["label"] == "noise" and err == "none"


def test_prompt_injection_cannot_produce_a_transition():
    # Even if the model is talked into emitting a status, decide() only ever reads
    # the label — there is no path from email text to an arbitrary DB write.
    parsed, _ = mail_agent.classify(
        {"message_id": "x"},
        _llm('{"label": "noise", "company": "Acme", "new_status": "offer"}'))
    out = mail_agent.decide(parsed["label"], [_card()])
    assert out["proposed_status"] is None


def test_llm_failure_is_transient_not_fatal():
    def boom(system, user):
        raise RuntimeError("api down")
    parsed, err = mail_agent.classify({"message_id": "x"}, boom)
    assert parsed is None and err == "transient"


def test_non_json_reply_is_transient():
    parsed, err = mail_agent.classify({"message_id": "x"}, _llm("I think this is a rejection"))
    assert parsed is None and err == "transient"


# --- process(): batch resilience ---

def test_batch_survives_a_failing_message(monkeypatch):
    monkeypatch.setattr(mail_agent.db, "find_cards_for_mail", lambda *a, **k: [_card()])
    monkeypatch.setattr(mail_agent.db, "add_comment", lambda *a: None)
    stored = []
    monkeypatch.setattr(mail_agent.db, "record_mail_event",
                        lambda e: stored.append(e) or {"id": len(stored), "duplicate": False})

    def flaky(system, user):
        if "boom" in user:
            raise RuntimeError("classify blew up")
        return '{"label": "acknowledgement", "company": "Acme", "confidence": 0.9}', {}

    msgs = [{"message_id": "1", "subject": "ok"}, {"message_id": "2", "subject": "boom"},
            {"message_id": "3", "subject": "ok"}]
    stats = mail_agent.process(msgs, flaky)
    assert stats["seen"] == 3 and stats["errors"] == 1 and len(stored) == 2


def test_duplicate_message_is_a_no_op(monkeypatch):
    monkeypatch.setattr(mail_agent.db, "find_cards_for_mail", lambda *a, **k: [_card()])
    commented = []
    monkeypatch.setattr(mail_agent.db, "add_comment", lambda *a: commented.append(a))
    monkeypatch.setattr(mail_agent.db, "record_mail_event",
                        lambda e: {"id": 1, "duplicate": True, "action": "pending"})
    stats = mail_agent.process(
        [{"message_id": "1", "subject": "s"}],
        _llm('{"label": "rejection", "company": "Acme", "confidence": 0.95}'))
    assert stats["duplicates"] == 1
    assert commented == []  # a replay must not re-comment on the card
