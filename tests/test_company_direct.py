"""company_direct source — failure isolation, role pre-filter parity, the
deliberate published='' decision, and health-recording semantics."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "core"))

import ats_boards  # noqa: E402
from sources import company_direct  # noqa: E402


def _company(i, ats="greenhouse", name=None):
    return {"id": i, "name": name or f"Co{i}", "ats": ats, "slug": f"co{i}"}


def _posting(title, **kw):
    p = {"id": "1", "title": title, "url": "https://x/1", "location": "Remote",
         "description": "full jd text", "salary": "", "published": "2026-08-01"}
    p.update(kw)
    return p


def _install(monkeypatch, companies, lister_map, recorded):
    monkeypatch.setattr(company_direct.db, "get_active_target_companies", lambda: companies)
    monkeypatch.setattr(company_direct.db, "record_target_company_results",
                        lambda results: recorded.setdefault("results", results))
    monkeypatch.setattr(company_direct.db, "degrade_target_companies",
                        lambda threshold=6: recorded.setdefault("degraded", []) or [])
    monkeypatch.setattr(company_direct.telegram, "send_error",
                        lambda msg: recorded.setdefault("alerts", []).append(msg))
    monkeypatch.setattr(company_direct.ats_boards, "LISTERS", lister_map)
    monkeypatch.setattr(company_direct.ats_boards, "fetch_description",
                        lambda ats, slug, pid, timeout=10: "fetched-gh-jd")
    monkeypatch.setattr(company_direct.time, "sleep", lambda s: None)


def test_pm_prefilter_and_contract(monkeypatch):
    recorded = {}
    _install(monkeypatch, [_company(1, name="Acme")], {
        "greenhouse": lambda slug, timeout: [
            _posting("Senior Product Manager, Platform"),
            _posting("Staff Software Engineer"),
            _posting("Account Executive"),
        ],
    }, recorded)
    jobs = company_direct.fetch()
    assert len(jobs) == 1  # only the PM title survives
    j = jobs[0]
    assert j["source"] == "CompanyDirect"
    assert j["company"] == "Acme"          # seed name, not ATS-reported
    assert j["apply_url"] == j["url"]
    assert j["published"] == ""            # DELIBERATE — see module docstring


def test_one_broken_company_does_not_kill_the_sweep(monkeypatch):
    recorded = {}

    def broken(slug, timeout):
        raise ats_boards.AtsBoardError("http_500")

    _install(monkeypatch, [_company(1), _company(2), _company(3)], {
        "greenhouse": lambda slug, timeout: (
            broken(slug, timeout) if slug == "co2"
            else [_posting("Product Manager")]),
    }, recorded)
    jobs = company_direct.fetch()
    assert len(jobs) == 2
    by_id = {r["id"]: r for r in recorded["results"]}
    assert by_id[2]["ok"] is False and by_id[1]["ok"] and by_id[3]["ok"]


def test_not_found_recorded_distinctly(monkeypatch):
    recorded = {}

    def gone(slug, timeout):
        raise ats_boards.AtsBoardNotFound("404")

    _install(monkeypatch, [_company(1)], {"greenhouse": gone}, recorded)
    assert company_direct.fetch() == []
    assert recorded["results"][0]["error"] == "not_found"


def test_greenhouse_empty_description_fetched_lazily(monkeypatch):
    recorded = {}
    _install(monkeypatch, [_company(1)], {
        "greenhouse": lambda slug, timeout: [_posting("Product Manager", description="")],
    }, recorded)
    jobs = company_direct.fetch()
    assert jobs[0]["description"] == "fetched-gh-jd"


def test_mass_failure_sends_one_aggregated_alert(monkeypatch):
    recorded = {}

    def gone(slug, timeout):
        raise ats_boards.AtsBoardError("timeout")

    _install(monkeypatch, [_company(i) for i in range(1, 5)], {"greenhouse": gone}, recorded)
    company_direct.fetch()
    assert len(recorded["alerts"]) == 1
    assert "4/4 boards failed" in recorded["alerts"][0]


def test_db_read_failure_returns_empty_not_raise(monkeypatch):
    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(company_direct.db, "get_active_target_companies", boom)
    assert company_direct.fetch() == []


def test_health_write_failure_does_not_lose_jobs(monkeypatch):
    recorded = {}
    _install(monkeypatch, [_company(1)], {
        "greenhouse": lambda slug, timeout: [_posting("Product Manager")],
    }, recorded)

    def boom(results):
        raise RuntimeError("db down")

    monkeypatch.setattr(company_direct.db, "record_target_company_results", boom)
    jobs = company_direct.fetch()
    assert len(jobs) == 1  # telemetry loss is not vacancy loss


def test_time_budget_defers_tail(monkeypatch):
    recorded = {}
    clock = {"t": 0.0}
    _install(monkeypatch, [_company(i) for i in range(1, 4)], {
        "greenhouse": lambda slug, timeout: [_posting("Product Manager")],
    }, recorded)

    def fake_monotonic():
        clock["t"] += 200  # every check advances well past the budget after 2 iterations
        return clock["t"]

    monkeypatch.setattr(company_direct.time, "monotonic", fake_monotonic)
    jobs = company_direct.fetch()
    assert len(jobs) < 3          # tail deferred
    assert len(recorded["results"]) == len(jobs)  # deferred companies not marked checked
