"""Smoke tests for the Review UI routes (SPEC_FRONTEND.md v1.2).

Auth is cookie-based (POST /login sets an HttpOnly cookie; _authenticate() reads
it) — the token never travels as a URL query param anymore. Auth checks (missing/
invalid cookie) never touch the DB, so those pass regardless of migration state.
The "authenticated -> 200" checks do hit the real DB (this project has no separate
test DB) and only pass once the v1.2 schema migration has actually been applied —
see the "Ask first" item in SPEC_FRONTEND.md before running it.
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("DASHBOARD_TOKEN", "test-token-for-pytest")
sys.path.insert(0, str(Path(__file__).parent.parent / "core"))

from fastapi.testclient import TestClient  # noqa: E402

import dashboard  # noqa: E402

TOKEN = dashboard.TOKEN  # whatever was actually set in the environment at import time


def _anon_client():
    # The login cookie is Secure — TestClient's default http://testserver base_url
    # would silently drop it on every subsequent request, breaking every
    # "authenticated" fixture. https:// makes the client treat the connection as
    # secure so the cookie jar actually attaches it (Starlette's documented pattern
    # for testing Secure-cookie apps).
    return TestClient(dashboard.app, base_url="https://testserver")


def _authenticated_client():
    c = _anon_client()
    r = c.post("/login", data={"token": TOKEN}, follow_redirects=False)
    assert r.status_code == 302  # sanity: login itself must succeed for these fixtures to mean anything
    return c


def test_login_form_renders():
    r = _anon_client().get("/login")
    assert r.status_code == 200


def test_login_wrong_token_rejected():
    dashboard._login_failures.clear()
    r = _anon_client().post("/login", data={"token": "definitely-wrong"}, follow_redirects=False)
    assert r.status_code == 403
    dashboard._login_failures.clear()


def test_login_rate_limited_after_repeated_failures():
    # Global in-process counter, not per-client — reset before/after so this test's
    # state doesn't leak into (or get polluted by) any other test in this file.
    dashboard._login_failures.clear()
    c = _anon_client()
    for _ in range(dashboard._LOGIN_MAX_ATTEMPTS):
        r = c.post("/login", data={"token": "wrong"})
        assert r.status_code == 403
    r = c.post("/login", data={"token": "wrong"})
    assert r.status_code == 429
    # locked out even with the correct token, until the window elapses
    r2 = c.post("/login", data={"token": TOKEN})
    assert r2.status_code == 429
    dashboard._login_failures.clear()


def test_login_correct_token_sets_cookie_and_redirects():
    c = _anon_client()
    r = c.post("/login", data={"token": TOKEN}, follow_redirects=False)
    assert r.status_code == 302
    # Kanban is the landing page (design_handoff_review_ui Turn 5) — login lands there,
    # not on /dashboard.
    assert r.headers["location"] == "/"
    assert dashboard.COOKIE_NAME in c.cookies


def test_logout_clears_cookie():
    c = _authenticated_client()
    assert dashboard.COOKIE_NAME in c.cookies
    r = c.get("/logout", follow_redirects=False)
    assert r.status_code == 302
    assert dashboard.COOKIE_NAME not in c.cookies


def test_review_rejects_no_cookie():
    r = _anon_client().get("/review")
    assert r.status_code == 403


def test_kanban_rejects_no_cookie():
    r = _anon_client().get("/kanban")
    assert r.status_code == 403


def test_root_rejects_no_cookie():
    r = _anon_client().get("/")
    assert r.status_code == 403


def test_job_detail_rejects_no_cookie():
    r = _anon_client().get("/jobs/1/detail")
    assert r.status_code == 403


def test_review_rejects_bad_cookie():
    c = _anon_client()
    c.cookies.set(dashboard.COOKIE_NAME, "definitely-wrong")
    r = c.get("/review")
    assert r.status_code == 403


def test_sources_rejects_no_cookie():
    r = _anon_client().get("/sources")
    assert r.status_code == 403


def test_job_status_rejects_no_cookie():
    r = _anon_client().post("/jobs/1/status", data={"new_status": "applied"})
    assert r.status_code == 403


def test_source_toggle_rejects_no_cookie():
    r = _anon_client().post("/sources/Himalayas/toggle")
    assert r.status_code == 403


# --- Below require the v1.2 migration to have been applied against the real DB ---

def test_review_authenticated():
    r = _authenticated_client().get("/review")
    assert r.status_code == 200


def test_kanban_authenticated():
    r = _authenticated_client().get("/kanban")
    assert r.status_code == 200


def test_root_serves_kanban_when_authenticated():
    # "/" and "/kanban" are the same view (Turn 5 — Kanban is the landing page), not
    # a redirect: both must return the board directly.
    r = _authenticated_client().get("/", follow_redirects=False)
    assert r.status_code == 200
    assert "kanban-board" in r.text


def test_kanban_search_param_filters_without_error():
    r = _authenticated_client().get("/kanban", params={"q": "definitely-not-a-real-title-xyz"})
    assert r.status_code == 200
    assert "0 active" in r.text or "kanban-board" in r.text


def test_job_detail_unknown_job_404():
    r = _authenticated_client().get("/jobs/999999999/detail")
    assert r.status_code == 404


def test_job_detail_authenticated_for_a_real_job():
    conn = dashboard.psycopg2.connect(
        dashboard.DATABASE_URL, cursor_factory=dashboard.psycopg2.extras.RealDictCursor, connect_timeout=10
    )
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM jobs WHERE current_status IS NOT NULL LIMIT 1")
            row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        return  # no qualified jobs in this DB yet — nothing to assert against
    r = _authenticated_client().get(f"/jobs/{row['id']}/detail")
    assert r.status_code == 200
    assert "job-detail-dialog" in r.text


def test_sources_authenticated():
    r = _authenticated_client().get("/sources")
    assert r.status_code == 200


def test_stats_rejection_reasons_authenticated():
    r = _authenticated_client().get("/stats/rejection-reasons")
    assert r.status_code == 200


def test_stats_invalid_date_returns_400():
    r = _authenticated_client().get("/stats/rejection-reasons", params={"from": "not-a-date"})
    assert r.status_code == 400


def test_source_toggle_unknown_source_404():
    r = _authenticated_client().post("/sources/definitely-not-a-real-source/toggle")
    assert r.status_code == 404


# --- design_handoff_review_ui Turns 6/8/9 ---

def test_tracker_rejects_no_cookie():
    r = _anon_client().get("/tracker")
    assert r.status_code == 403


def test_tracker_authenticated():
    r = _authenticated_client().get("/tracker")
    assert r.status_code == 200


def test_tracker_invalid_date_returns_400():
    r = _authenticated_client().get("/tracker", params={"from": "not-a-date"})
    assert r.status_code == 400


def test_kanban_filters_by_score_and_domain():
    r = _authenticated_client().get("/kanban", params={"score_min": 70, "domain": "AI/ML,FinTech"})
    assert r.status_code == 200


def test_kanban_blank_score_min_from_filter_form_does_not_422():
    # Regression: the "Min score" filter input submits score_min= (empty string) when
    # left blank, not an omitted param — an int-typed Query param 422s on that before
    # the route body ever runs.
    r = _authenticated_client().get("/kanban", params={"score_min": ""})
    assert r.status_code == 200


def test_kanban_invalid_score_min_400():
    r = _authenticated_client().get("/kanban", params={"score_min": "not-a-number"})
    assert r.status_code == 400


def test_kanban_sort_score():
    r = _authenticated_client().get("/kanban", params={"sort": "score"})
    assert r.status_code == 200


def test_bulk_status_rejects_no_cookie():
    r = _anon_client().post("/jobs/bulk-status", data={"ids": "1,2", "new_status": "applied"})
    assert r.status_code == 403


def test_bulk_status_no_valid_ids_400():
    # Exercises bulk_change_status's own guard (empty job_ids after filtering non-digits) —
    # a bare empty string for "ids" hits FastAPI's own required-field validation (422)
    # before reaching the handler at all, which isn't the guard this test is after.
    r = _authenticated_client().post("/jobs/bulk-status", data={"ids": "abc,xyz", "new_status": "applied"})
    assert r.status_code == 400


def test_bulk_status_unknown_ids_reports_errors_not_crash():
    r = _authenticated_client().post("/jobs/bulk-status", data={"ids": "999999998,999999999", "new_status": "applied"})
    assert r.status_code == 200
    body = r.json()
    assert body["updated"] == 0
    assert len(body["errors"]) == 2


def test_bulk_reject_form_rejects_no_cookie():
    r = _anon_client().get("/jobs/bulk-reject-form", params={"ids": "1,2"})
    assert r.status_code == 403


def test_bulk_reject_form_unknown_ids_404():
    r = _authenticated_client().get("/jobs/bulk-reject-form", params={"ids": "999999998,999999999"})
    assert r.status_code == 404


# --- Release 1 resume features (Generate resume / resume-pdf / Re-score) ---
# DB access is monkeypatched to fakes — these tests must not write rows into the
# real DB (there is no separate test DB) or call the paid generation service.

def _fake_job(**overrides):
    job = {
        "id": 1, "title": "PM", "company": "Acme", "current_status": "found",
        "description": "x" * 500, "resume_run_id": None, "pipeline_run_id": None,
        "ats_score": 70, "rejected_from": None,
    }
    job.update(overrides)
    return job


def test_generate_resume_rejects_no_cookie():
    r = _anon_client().post("/jobs/1/resume")
    assert r.status_code == 403


def test_resume_pdf_rejects_no_cookie():
    r = _anon_client().get("/jobs/1/resume-pdf")
    assert r.status_code == 403


def test_rescore_rejects_no_cookie():
    r = _anon_client().post("/jobs/1/rescore")
    assert r.status_code == 403


def test_generate_resume_unknown_job_404():
    r = _authenticated_client().post("/jobs/999999999/resume")
    assert r.status_code == 404


def test_generate_resume_missing_company_422(monkeypatch):
    monkeypatch.setattr(dashboard.db, "get_job", lambda _id: _fake_job(company=None))
    r = _authenticated_client().post("/jobs/1/resume")
    assert r.status_code == 422
    assert "Company" in r.json()["detail"]


def test_generate_resume_short_jd_422(monkeypatch):
    monkeypatch.setattr(dashboard.db, "get_job", lambda _id: _fake_job(description="too short"))
    r = _authenticated_client().post("/jobs/1/resume")
    assert r.status_code == 422
    assert "JD" in r.json()["detail"]


def test_generate_resume_success_stores_run_id_and_returns_link(monkeypatch):
    stored = {}
    monkeypatch.setattr(dashboard.db, "get_job", lambda _id: _fake_job())
    monkeypatch.setattr(dashboard.db, "set_resume_run_id",
                        lambda job_id, run_id: stored.update({job_id: run_id}))
    monkeypatch.setattr(dashboard.db, "get_resume_skeptic_count", lambda _run_id: 2)
    monkeypatch.setattr(
        dashboard.resume_client, "generate_via_cloud",
        lambda job, prid: (dashboard.resume_client.ResumeOutcome(77, [{}, {}]), "none"),
    )
    r = _authenticated_client().post("/jobs/1/resume")
    assert r.status_code == 200
    assert stored == {1: 77}
    assert "Open resume" in r.text
    assert "/jobs/1/resume-pdf" in r.text


def test_generate_resume_already_generated_returns_link_without_calling_service(monkeypatch):
    monkeypatch.setattr(dashboard.db, "get_job", lambda _id: _fake_job(resume_run_id=77))
    monkeypatch.setattr(dashboard.db, "get_resume_skeptic_count", lambda _run_id: 0)

    def _must_not_be_called(job, prid):
        raise AssertionError("generation must not run again for an existing resume")

    monkeypatch.setattr(dashboard.resume_client, "generate_via_cloud", _must_not_be_called)
    r = _authenticated_client().post("/jobs/1/resume")
    assert r.status_code == 200
    assert "Open resume" in r.text


def test_generate_resume_service_failure_returns_retryable_button(monkeypatch):
    monkeypatch.setattr(dashboard.db, "get_job", lambda _id: _fake_job())
    monkeypatch.setattr(
        dashboard.resume_client, "generate_via_cloud", lambda job, prid: (None, "transient"))
    r = _authenticated_client().post("/jobs/1/resume")
    assert r.status_code == 200
    assert "Generation failed" in r.text
    assert "Generate resume" in r.text  # button still there for a retry


def test_resume_pdf_no_resume_yet_404(monkeypatch):
    monkeypatch.setattr(dashboard.db, "get_job", lambda _id: _fake_job(resume_run_id=None))
    r = _authenticated_client().get("/jobs/1/resume-pdf")
    assert r.status_code == 404


def test_resume_pdf_streams_inline(monkeypatch):
    monkeypatch.setattr(dashboard.db, "get_job", lambda _id: _fake_job(resume_run_id=77))
    monkeypatch.setattr(dashboard.db, "get_resume_pdf", lambda _run_id: (b"%PDF-fake", "Acme"))
    r = _authenticated_client().get("/jobs/1/resume-pdf")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"
    assert "inline" in r.headers["content-disposition"]
    assert r.content == b"%PDF-fake"


def test_rescore_updates_row_and_reports_new_score(monkeypatch):
    updates = {}
    monkeypatch.setattr(dashboard.db, "get_job", lambda _id: _fake_job())
    monkeypatch.setattr(dashboard.db, "update_job_scoring",
                        lambda job_id, result, prid: updates.update({job_id: result.score}))

    class _Result:
        score = 91

    monkeypatch.setattr(
        dashboard.scoring_client, "analyze_via_cloud", lambda job, prid: (_Result(), "none"))
    r = _authenticated_client().post("/jobs/1/rescore")
    assert r.status_code == 200
    assert updates == {1: 91}
    assert "91" in r.text
    assert "re-scored" in r.text


def test_rescore_without_jd_reports_error_not_500(monkeypatch):
    monkeypatch.setattr(dashboard.db, "get_job", lambda _id: _fake_job(description=None))
    r = _authenticated_client().post("/jobs/1/rescore")
    assert r.status_code == 200
    assert "nothing to score" in r.text


# --- 2026-08-17 batch: comments, merged dashboard, applied-today, tracker redirect ---

def test_comments_reject_no_cookie():
    assert _anon_client().post("/jobs/1/comments", data={"body": "x"}).status_code == 403
    assert _anon_client().delete("/jobs/1/comments/1").status_code == 403


def test_applied_today_rejects_no_cookie():
    assert _anon_client().get("/applied-today").status_code == 403


def test_add_comment_unknown_job_404(monkeypatch):
    monkeypatch.setattr(dashboard.db, "get_job", lambda _id: None)
    r = _authenticated_client().post("/jobs/999999999/comments", data={"body": "x"})
    assert r.status_code == 404


def test_add_comment_empty_body_400(monkeypatch):
    monkeypatch.setattr(dashboard.db, "get_job", lambda _id: _fake_job())
    r = _authenticated_client().post("/jobs/1/comments", data={"body": "   "})
    assert r.status_code == 400


def test_add_and_delete_comment_roundtrip(monkeypatch):
    notes = []
    monkeypatch.setattr(dashboard.db, "get_job", lambda _id: _fake_job())
    monkeypatch.setattr(dashboard.db, "add_comment", lambda job_id, body: notes.append(body) or 1)
    monkeypatch.setattr(dashboard.db, "delete_comment", lambda job_id, cid: True)
    monkeypatch.setattr(dashboard.db, "get_comments", lambda _id: [
        {"id": 1, "body": b, "created_at": None} for b in notes
    ])
    c = _authenticated_client()
    r = c.post("/jobs/1/comments", data={"body": "recruiter: Anna"})
    assert r.status_code == 200 and "recruiter: Anna" in r.text
    notes.clear()
    r = c.delete("/jobs/1/comments/1")
    assert r.status_code == 200 and "recruiter: Anna" not in r.text


def test_tracker_redirects_to_dashboard():
    r = _authenticated_client().get("/tracker", params={"from": "2026-08-01"}, follow_redirects=False)
    assert r.status_code == 301
    assert r.headers["location"] == "/dashboard?from=2026-08-01"


def test_dashboard_serves_merged_page():
    r = _authenticated_client().get("/dashboard")
    assert r.status_code == 200
    assert "Applied today" in r.text        # application stats present
    assert "Scraper analytics" in r.text    # scraper section present


def test_applied_today_modal_renders():
    r = _authenticated_client().get("/applied-today")
    assert r.status_code == 200
    assert "applied-today-dialog" in r.text


# --- Merge duplicates (2026-08-19) ---

def test_merge_rejects_no_cookie():
    assert _anon_client().post("/jobs/1/merge-into", data={"target_id": "2"}).status_code == 403


def test_merge_non_numeric_target_400():
    r = _authenticated_client().post("/jobs/1/merge-into", data={"target_id": "abc"})
    assert r.status_code == 400


def test_merge_validation_error_400(monkeypatch):
    def _raise(kept, dup):
        raise ValueError("A card cannot be merged into itself")
    monkeypatch.setattr(dashboard.db, "merge_duplicate", _raise)
    r = _authenticated_client().post("/jobs/5/merge-into", data={"target_id": "#5"})
    assert r.status_code == 400
    assert "itself" in r.json()["detail"]


def test_merge_success_returns_toast(monkeypatch):
    monkeypatch.setattr(dashboard.db, "merge_duplicate",
                        lambda kept, dup: {"kept_id": kept, "dup_id": dup})
    r = _authenticated_client().post("/jobs/7/merge-into", data={"target_id": "#3"})
    assert r.status_code == 200
    assert "merged into #3" in r.text


# --- Card editing (2026-08-19) ---

def test_edit_form_rejects_no_cookie():
    assert _anon_client().get("/jobs/1/edit-form").status_code == 403
    assert _anon_client().post("/jobs/1/edit", data={"title": "x"}).status_code == 403


def test_edit_unknown_job_404(monkeypatch):
    monkeypatch.setattr(dashboard.db, "get_job", lambda _id: None)
    assert _authenticated_client().post("/jobs/1/edit", data={"title": "x"}).status_code == 404


def test_edit_empty_title_400(monkeypatch):
    monkeypatch.setattr(dashboard.db, "get_job", lambda _id: _fake_job())
    r = _authenticated_client().post("/jobs/1/edit", data={"title": "   "})
    assert r.status_code == 400


def test_edit_saves_fields_and_rescores_on_jd_change(monkeypatch):
    saved = {}
    monkeypatch.setattr(dashboard.db, "get_job", lambda _id: _fake_job(resume_run_id=9))
    monkeypatch.setattr(dashboard.db, "update_job_fields",
                        lambda job_id, fields: saved.update(fields))
    cleared = []
    monkeypatch.setattr(dashboard.db, "clear_resume_run_id", lambda job_id: cleared.append(job_id))
    rescored = []
    monkeypatch.setattr(dashboard.threading, "Thread",
                        lambda target, args, daemon: type("T", (), {"start": lambda self: rescored.append(args)})())
    monkeypatch.setattr(dashboard.db, "get_comments", lambda _id: [])
    monkeypatch.setattr(dashboard.db, "get_resume_skeptic_count", lambda _id: 0)

    r = _authenticated_client().post("/jobs/1/edit", data={
        "title": "PM", "company": "Acme", "description": "y" * 500, "url": "https://x.example/a",
    })
    assert r.status_code == 200
    assert saved["description"] == "y" * 500
    assert saved["apply_url"] == "https://x.example/a"  # falls back to url when blank
    assert cleared == [1]      # JD changed -> old resume unlinked
    assert len(rescored) == 1  # background re-score scheduled
    assert "job-detail-dialog" in r.text  # refreshed view mode returned


def test_edit_no_jd_change_keeps_resume(monkeypatch):
    monkeypatch.setattr(dashboard.db, "get_job", lambda _id: _fake_job(resume_run_id=9))
    monkeypatch.setattr(dashboard.db, "update_job_fields", lambda job_id, fields: None)
    cleared = []
    monkeypatch.setattr(dashboard.db, "clear_resume_run_id", lambda job_id: cleared.append(job_id))
    monkeypatch.setattr(dashboard.db, "get_comments", lambda _id: [])
    monkeypatch.setattr(dashboard.db, "get_resume_skeptic_count", lambda _id: 0)
    r = _authenticated_client().post("/jobs/1/edit", data={
        "title": "PM", "company": "Acme", "description": "x" * 500, "salary": "$200k",
    })
    assert r.status_code == 200
    assert cleared == []  # same JD -> resume link untouched


def test_bulk_reject_form_for_real_jobs():
    conn = dashboard.psycopg2.connect(
        dashboard.DATABASE_URL, cursor_factory=dashboard.psycopg2.extras.RealDictCursor, connect_timeout=10
    )
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM jobs WHERE current_status = 'found' LIMIT 2")
            rows = cur.fetchall()
    finally:
        conn.close()
    if len(rows) < 1:
        return  # nothing in 'found' to test against
    ids = ",".join(str(r["id"]) for r in rows)
    r = _authenticated_client().get("/jobs/bulk-reject-form", params={"ids": ids})
    assert r.status_code == 200
    assert "bulk-reject-dialog" in r.text


# --- Mail agent queue (2026-08-22) ---

def test_mail_queue_rejects_no_cookie():
    assert _anon_client().get("/mail").status_code == 403
    assert _anon_client().post("/mail/events/1/resolve", data={"action": "dismissed"}).status_code == 403


def test_mail_queue_renders(monkeypatch):
    monkeypatch.setattr(dashboard.db, "get_pending_mail_events", lambda limit=50: [{
        "id": 7, "subject": "Update on your application", "from_addr": "no-reply@ashbyhq.com",
        "excerpt": "we've decided not to move forward", "classification": "rejection",
        "received_at": None, "company_hint": "Adapty", "job_id": 42,
        "job_title": "Senior PM", "job_company": "Adapty", "job_status": "applied",
        "proposed_status": "rejected", "proposed_reason": "company_rejected",
    }])
    r = _authenticated_client().get("/mail")
    assert r.status_code == 200
    assert "mail-event-7" in r.text and "Adapty" in r.text


def test_mail_confirm_applies_through_normal_status_path(monkeypatch):
    calls = {}
    monkeypatch.setattr(dashboard.db, "resolve_mail_event",
                        lambda eid, action, job_id=None, status=None, reason=None:
                        calls.update(locals()) or {"event_id": eid, "action": action})
    r = _authenticated_client().post("/mail/events/7/resolve", data={
        "action": "confirmed", "job_id": "42", "status": "rejected", "reason": "company_rejected"})
    assert r.status_code == 200
    assert calls["job_id"] == 42 and calls["status"] == "rejected"
    assert "применено" in r.text


def test_mail_confirm_without_card_is_400(monkeypatch):
    monkeypatch.setattr(dashboard.db, "resolve_mail_event", lambda *a, **k: {})
    r = _authenticated_client().post("/mail/events/7/resolve", data={
        "action": "confirmed", "job_id": "", "status": "rejected"})
    assert r.status_code == 400


def test_mail_dismiss_needs_no_card(monkeypatch):
    monkeypatch.setattr(dashboard.db, "resolve_mail_event",
                        lambda *a, **k: {"event_id": 7, "action": "dismissed"})
    r = _authenticated_client().post("/mail/events/7/resolve", data={"action": "dismissed"})
    assert r.status_code == 200 and "пропущено" in r.text
