import os
import uuid

import psycopg2
import pytest


TEST_DATABASE_URL = os.environ.get("JOBSCRAPER_TEST_DATABASE_URL")


pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="JOBSCRAPER_TEST_DATABASE_URL is required for Postgres route tests",
)


@pytest.fixture()
def test_database(monkeypatch):
    db_name = f"jobscraper_dim30_{uuid.uuid4().hex}"
    admin_url = TEST_DATABASE_URL.rsplit("/", 1)[0] + "/postgres"
    conn = psycopg2.connect(admin_url)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(f'CREATE DATABASE "{db_name}"')
    finally:
        conn.close()

    db_url = TEST_DATABASE_URL.rsplit("/", 1)[0] + f"/{db_name}"
    monkeypatch.setenv("DATABASE_URL", db_url)

    import db
    import dashboard

    db.DATABASE_URL = db_url
    dashboard.DATABASE_URL = db_url
    db.init_db()

    try:
        yield db_url, dashboard
    finally:
        conn = psycopg2.connect(admin_url)
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s",
                    (db_name,),
                )
                cur.execute(f'DROP DATABASE "{db_name}"')
        finally:
            conn.close()


def test_dashboard_tracker_analytics_uses_status_log_without_n_plus_one(test_database):
    db_url, dashboard = test_database
    conn = psycopg2.connect(db_url)
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO runs (started_at, qualified, total_fetched) VALUES ('2026-07-06T10:00:00+00:00', 4, 4) RETURNING id")
                run_id = cur.fetchone()[0]
                cur.execute(
                    """
                    INSERT INTO jobs
                        (run_id, title, company, source, outcome, ats_score, current_status, rejection_reason)
                    VALUES
                        (%s, 'PM One', 'Acme', 'fixture', 'qualified', 90, 'qualified', NULL),
                        (%s, 'PM Two', 'Beta', 'fixture', 'qualified', 85, 'applied', NULL),
                        (%s, 'PM Three', 'Cygnus', 'fixture', 'qualified', 83, 'interview', NULL),
                        (%s, 'PM Four', 'Delta', 'fixture', 'qualified', 95, 'offer', NULL),
                        (%s, 'PM Five', 'Echo', 'fixture', 'qualified', 80, 'rejected', 'position_closed'),
                        (%s, 'PM Six', 'Fox', 'fixture', 'low_score', 45, 'rejected', 'low_score')
                    RETURNING id
                    """,
                    (run_id, run_id, run_id, run_id, run_id, run_id),
                )
                job_ids = [row[0] for row in cur.fetchall()]
                cur.execute(
                    """
                    INSERT INTO status_log (job_id, old_status, new_status, source)
                    VALUES
                        (%s, 'qualified', 'applied', 'test'),
                        (%s, 'applied', 'interview', 'test'),
                        (%s, 'interview', 'offer', 'test'),
                        (%s, 'applied', 'rejected', 'test')
                    """,
                    (job_ids[2], job_ids[3], job_ids[3], job_ids[4]),
                )
    finally:
        conn.close()

    analytics = dashboard._tracker_analytics()

    assert analytics["funnel"] == {
        "qualified": 5,
        "applied": 4,
        "interview": 2,
        "offer": 1,
    }
    assert analytics["applied_rate"] == 80.0
    assert {r["reason"]: r["cnt"] for r in analytics["rejection_reasons"]} == {
        "position_closed": 1,
        "low_score": 1,
    }

    response = dashboard.dashboard(token="")
    html = response.body.decode()
    assert response.status_code == 200
    assert "Tracker funnel" in html
    assert "Top rejection reasons" in html
    assert "80.0%" in html
