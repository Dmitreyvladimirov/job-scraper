"""ats_boards listers against real captured fixtures (tests/fixtures/ats/*.json,
snapped live 2026-08-19). HTTP goes through the single _get_json seam."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "core"))

import ats_boards  # noqa: E402

_FIXTURES = Path(__file__).parent / "fixtures" / "ats"


def _fixture(name):
    return json.loads((_FIXTURES / name).read_text())


def _seam(monkeypatch, status, payload):
    calls = []

    def fake(url, *, timeout, params=None):
        calls.append(url)
        return status, payload

    monkeypatch.setattr(ats_boards, "_get_json", fake)
    return calls


# --- Greenhouse ---

def test_greenhouse_maps_fields(monkeypatch):
    _seam(monkeypatch, 200, _fixture("greenhouse_board.json"))
    postings = ats_boards.list_greenhouse("anthropic")
    assert len(postings) == 3
    pm = postings[0]
    assert "Product Manager" in pm["title"]
    assert pm["url"].startswith("http")
    assert pm["location"]  # location.name flattened
    assert pm["description"] == ""  # listing carries none by design
    assert pm["published"] and len(pm["published"]) == 10  # first_published date part


def test_greenhouse_404_is_not_found(monkeypatch):
    _seam(monkeypatch, 404, None)
    with pytest.raises(ats_boards.AtsBoardNotFound):
        ats_boards.list_greenhouse("nope")


def test_greenhouse_500_is_board_error(monkeypatch):
    _seam(monkeypatch, 500, None)
    with pytest.raises(ats_boards.AtsBoardError):
        ats_boards.list_greenhouse("acme")


# --- Lever ---

def test_lever_maps_fields(monkeypatch):
    _seam(monkeypatch, 200, _fixture("lever_board.json"))
    postings = ats_boards.list_lever("spotify")
    assert len(postings) == 3
    pm = postings[0]
    assert "Product Manager" in pm["title"]
    assert pm["url"].startswith("http")
    assert pm["location"]  # categories.location
    assert pm["description"]  # descriptionPlain ships in the listing
    assert pm["published"] and len(pm["published"]) == 10  # createdAt ms -> ISO date


def test_lever_ok_false_dict_is_not_found(monkeypatch):
    # Lever's actual unknown-slug behavior: HTTP 200 + {"ok": false, ...}
    _seam(monkeypatch, 200, _fixture("lever_not_found.json"))
    with pytest.raises(ats_boards.AtsBoardNotFound):
        ats_boards.list_lever("figma")


# --- Ashby ---

def test_ashby_maps_fields(monkeypatch):
    _seam(monkeypatch, 200, _fixture("ashby_board.json"))
    postings = ats_boards.list_ashby("openai")
    assert len(postings) == 3
    pm = postings[0]
    assert "Product Manager" in pm["title"]
    assert pm["url"].startswith("https://jobs.ashbyhq.com/")
    assert pm["description"]  # descriptionHtml stripped
    assert pm["published"] and len(pm["published"]) == 10


def test_ashby_missing_jobs_key_is_not_found(monkeypatch):
    _seam(monkeypatch, 200, {"apiVersion": "1"})
    with pytest.raises(ats_boards.AtsBoardNotFound):
        ats_boards.list_ashby("ghost")


# --- shared behavior ---

def test_empty_board_is_empty_list_not_error(monkeypatch):
    _seam(monkeypatch, 200, {"jobs": []})
    assert ats_boards.list_greenhouse("quiet") == []


def test_invalid_slug_never_makes_a_request(monkeypatch):
    calls = _seam(monkeypatch, 200, {"jobs": []})
    for bad in ("acme/../x", "acme?x=1", "", "a b"):
        with pytest.raises(ats_boards.AtsBoardNotFound):
            ats_boards.list_greenhouse(bad)
    assert calls == []  # the gate fired before any HTTP


def test_network_exception_becomes_board_error(monkeypatch):
    def boom(url, *, timeout, params=None):
        raise ConnectionError("dns")

    monkeypatch.setattr(ats_boards, "_get_json", boom)
    with pytest.raises(ats_boards.AtsBoardError):
        ats_boards.list_lever("acme")


def test_fetch_description_greenhouse_only(monkeypatch):
    _seam(monkeypatch, 200, {"content": "&lt;p&gt;Real JD&lt;/p&gt;"})
    assert "Real JD" in ats_boards.fetch_description("greenhouse", "acme", "123")
    assert ats_boards.fetch_description("lever", "acme", "123") == ""


def test_fetch_description_swallows_errors(monkeypatch):
    def boom(url, *, timeout, params=None):
        raise ConnectionError("down")

    monkeypatch.setattr(ats_boards, "_get_json", boom)
    assert ats_boards.fetch_description("greenhouse", "acme", "123") == ""
