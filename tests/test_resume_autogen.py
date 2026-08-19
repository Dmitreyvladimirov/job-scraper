"""Release 2 batch (scraper.autogen_resumes) — the loop's failure semantics are
the contract under test: transient failures skip-and-continue (the card retries
next run because resume_run_id stays NULL), a config failure alerts once and
aborts the whole batch, successes persist the run id. The candidate gating
itself lives in SQL (db.get_autogen_candidates) and is exercised against the
real DB read-only."""
import os
import sys
from pathlib import Path

os.environ.setdefault("DASHBOARD_TOKEN", "test-token-for-pytest")
sys.path.insert(0, str(Path(__file__).parent.parent / "core"))

import db  # noqa: E402
import scraper  # noqa: E402
from resume_client import ResumeOutcome  # noqa: E402


def _candidates(n):
    return [
        {"id": 100 + i, "title": f"PM {i}", "company": "Acme", "description": "x" * 600,
         "location": "Remote", "pipeline_run_id": None, "ats_score": 90 - i}
        for i in range(n)
    ]


def test_autogen_success_persists_run_ids(monkeypatch):
    stored = {}
    monkeypatch.setattr(scraper.db, "get_autogen_candidates", lambda *a, **k: _candidates(3))
    monkeypatch.setattr(scraper.db, "set_resume_run_id", lambda job_id, rid: stored.update({job_id: rid}))
    monkeypatch.setattr(scraper.telegram, "send_autogen_summary", lambda g: None)
    monkeypatch.setattr(scraper.resume_client, "generate_via_cloud",
                        lambda job, prid: (ResumeOutcome(1000 + job["id"], []), "none"))
    assert scraper.autogen_resumes() == 3
    assert stored == {100: 1100, 101: 1101, 102: 1102}


def test_autogen_transient_failure_skips_card(monkeypatch):
    stored = {}
    monkeypatch.setattr(scraper.db, "get_autogen_candidates", lambda *a, **k: _candidates(3))
    monkeypatch.setattr(scraper.db, "set_resume_run_id", lambda job_id, rid: stored.update({job_id: rid}))

    def flaky(job, prid):
        if job["id"] == 101:
            return None, "transient"
        return ResumeOutcome(1000 + job["id"], []), "none"

    monkeypatch.setattr(scraper.telegram, "send_autogen_summary", lambda g: None)
    monkeypatch.setattr(scraper.resume_client, "generate_via_cloud", flaky)
    assert scraper.autogen_resumes() == 2
    assert 101 not in stored  # left NULL -> retried next run


def test_autogen_db_write_failure_does_not_kill_batch(monkeypatch):
    # The QA-found blocker: a DB blip in set_resume_run_id (after a PAID call)
    # must neither raise out of autogen_resumes() nor stop the remaining cards.
    stored = {}
    monkeypatch.setattr(scraper.db, "get_autogen_candidates", lambda *a, **k: _candidates(3))

    def flaky_write(job_id, rid):
        if job_id == 101:
            raise RuntimeError("connection dropped")
        stored[job_id] = rid

    monkeypatch.setattr(scraper.db, "set_resume_run_id", flaky_write)
    monkeypatch.setattr(scraper.telegram, "send_autogen_summary", lambda g: None)
    monkeypatch.setattr(scraper.resume_client, "generate_via_cloud",
                        lambda job, prid: (ResumeOutcome(1000 + job["id"], []), "none"))
    assert scraper.autogen_resumes() == 2  # 101 lost its write, batch survived
    assert sorted(stored) == [100, 102]


def test_autogen_sends_own_telegram_message(monkeypatch):
    sent = []
    monkeypatch.setattr(scraper.db, "get_autogen_candidates", lambda *a, **k: _candidates(2))
    monkeypatch.setattr(scraper.db, "set_resume_run_id", lambda job_id, rid: None)
    monkeypatch.setattr(scraper.telegram, "send_autogen_summary", lambda g: sent.append(g))
    monkeypatch.setattr(scraper.resume_client, "generate_via_cloud",
                        lambda job, prid: (ResumeOutcome(1, [{}]), "none"))
    assert scraper.autogen_resumes() == 2
    assert len(sent) == 1 and len(sent[0]) == 2
    assert sent[0][0]["flags"] == 1


def test_autogen_config_failure_alerts_once_and_aborts(monkeypatch):
    alerts = []
    calls = []
    monkeypatch.setattr(scraper.db, "get_autogen_candidates", lambda *a, **k: _candidates(4))
    monkeypatch.setattr(scraper.db, "set_resume_run_id", lambda job_id, rid: None)
    monkeypatch.setattr(scraper.telegram, "send_error", lambda msg: alerts.append(msg))

    def broken(job, prid):
        calls.append(job["id"])
        return None, "config"

    monkeypatch.setattr(scraper.resume_client, "generate_via_cloud", broken)
    assert scraper.autogen_resumes() == 0
    assert len(alerts) == 1      # one alert, not four
    assert len(calls) == 1       # aborted after the first config failure


def test_autogen_candidate_query_failure_is_swallowed(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(scraper.db, "get_autogen_candidates", boom)
    assert scraper.autogen_resumes() == 0  # never raises into the run


def test_candidate_query_shape_against_real_db():
    rows = db.get_autogen_candidates(80, 500, 5)
    assert len(rows) <= 5
    for r in rows:
        assert r["ats_score"] >= 80
        assert len(r["description"]) >= 500
        assert (r["company"] or "").strip()
