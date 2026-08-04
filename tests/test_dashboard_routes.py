"""Smoke tests for the Review UI routes (SPEC_FRONTEND.md v1.2).

Auth checks (missing/invalid token) never touch the DB — _check_token() runs
before any query, so those pass regardless of migration state. The "valid
token -> 200" checks do hit the real DB (this project has no separate test
DB) and only pass once the v1.2 schema migration has actually been applied —
see the "Ask first" item in SPEC_FRONTEND.md before running it.
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("DASHBOARD_TOKEN", "test-token-for-pytest")
sys.path.insert(0, str(Path(__file__).parent.parent / "core"))

from fastapi.testclient import TestClient  # noqa: E402

import dashboard  # noqa: E402

client = TestClient(dashboard.app)
TOKEN = dashboard.TOKEN  # whatever was actually set in the environment at import time


def test_review_rejects_missing_token():
    r = client.get("/review")
    assert r.status_code == 403


def test_kanban_rejects_missing_token():
    r = client.get("/kanban")
    assert r.status_code == 403


def test_review_rejects_wrong_token():
    r = client.get("/review", params={"token": "definitely-wrong"})
    assert r.status_code == 403


def test_sources_rejects_missing_token():
    r = client.get("/sources")
    assert r.status_code == 403


def test_job_status_rejects_missing_token():
    r = client.post("/jobs/1/status", data={"new_status": "applied"})
    assert r.status_code == 403


def test_source_toggle_rejects_missing_token():
    r = client.post("/sources/Himalayas/toggle")
    assert r.status_code == 403


# --- Below require the v1.2 migration to have been applied against the real DB ---

def test_review_with_valid_token():
    r = client.get("/review", params={"token": TOKEN})
    assert r.status_code == 200


def test_kanban_with_valid_token():
    r = client.get("/kanban", params={"token": TOKEN})
    assert r.status_code == 200


def test_sources_with_valid_token():
    r = client.get("/sources", params={"token": TOKEN})
    assert r.status_code == 200


def test_stats_rejection_reasons_with_valid_token():
    r = client.get("/stats/rejection-reasons", params={"token": TOKEN})
    assert r.status_code == 200


def test_stats_invalid_date_returns_400():
    r = client.get("/stats/rejection-reasons", params={"token": TOKEN, "from": "not-a-date"})
    assert r.status_code == 400


def test_source_toggle_unknown_source_404():
    r = client.post("/sources/definitely-not-a-real-source/toggle", params={"token": TOKEN})
    assert r.status_code == 404
