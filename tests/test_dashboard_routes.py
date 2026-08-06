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
