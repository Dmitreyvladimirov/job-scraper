import io
import json
import sys
import urllib.error
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "core"))

import scoring_client  # noqa: E402
import utils  # noqa: E402

JOB = {
    "title": "Senior Product Manager",
    "company": "Acme",
    "description": "We need a PM for our AI platform.",
    "location": "Remote",
}

# A complete ScoreResult body — every ATSResult field plus the two the client drops.
SCORE_BODY = {
    "score": 84,
    "role_score": 28,
    "domain_score": 26,
    "domain_value_score": 14,
    "domain_exp_score": 12,
    "keyword_score": 20,
    "location_score": 15,
    "location_reason": "no restriction found — worldwide/Israel",
    "penalty": 15,
    "penalty_reason": "requires 5+ years IAM experience, candidate has none",
    "domain": "AI/ML",
    "why_apply": "Direct AI platform experience.",
    "why_not": "No IAM background.",
    "matched": ["AI", "B2B SaaS", "APIs"],
    "missed": ["IAM", "SOC2"],
    "cost_usd": 0.0123,
    "pipeline_run_id": "js-7-3",
}


class _FakeResponse:
    """urlopen() is used as a context manager, so the double has to be one too."""

    def __init__(self, body: str):
        self._body = body.encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body


def _http_error(code: int, body: dict | str) -> urllib.error.HTTPError:
    payload = body if isinstance(body, str) else json.dumps(body)
    return urllib.error.HTTPError(
        "https://scoring.test/v1/score", code, "error", {},
        io.BytesIO(payload.encode("utf-8")),
    )


@pytest.fixture
def calls(monkeypatch):
    """Replaces urlopen with a scripted double and counts attempts, so a test can
    assert that non-retryable failures were tried exactly once. Retry backoff is
    stubbed out — otherwise the transient cases would really sleep 1s + 2s."""
    monkeypatch.setattr(utils.time, "sleep", lambda _s: None)
    recorded: list = []

    def install(behaviour):
        def fake_urlopen(req, timeout=None):
            recorded.append(req)
            if callable(behaviour):
                return behaviour(req)
            raise behaviour

        monkeypatch.setattr(scoring_client.urllib.request, "urlopen", fake_urlopen)
        return recorded

    return install


def test_happy_path_maps_every_field(calls):
    calls(lambda req: _FakeResponse(json.dumps(SCORE_BODY)))

    result, error_kind = scoring_client.analyze_via_cloud(JOB, "js-7-3")

    assert error_kind == "none"
    assert result.score == 84
    assert result.role_score == 28
    assert result.domain_score == 26
    assert result.domain_value_score == 14
    assert result.domain_exp_score == 12
    assert result.keyword_score == 20
    assert result.location_score == 15
    assert result.location_reason == "no restriction found — worldwide/Israel"
    assert result.penalty == 15
    assert result.penalty_reason == "requires 5+ years IAM experience, candidate has none"
    assert result.domain == "AI/ML"
    assert result.why_apply == "Direct AI platform experience."
    assert result.why_not == "No IAM background."
    assert result.matched == ["AI", "B2B SaaS", "APIs"]
    assert result.missed == ["IAM", "SOC2"]


def test_request_sends_full_jd_and_bearer_token(calls, monkeypatch):
    monkeypatch.setattr(scoring_client, "SCORING_TOKEN", "test-token")
    recorded = calls(lambda req: _FakeResponse(json.dumps(SCORE_BODY)))

    scoring_client.analyze_via_cloud(JOB, "js-7-3")

    req = recorded[0]
    body = json.loads(req.data.decode("utf-8"))
    assert req.full_url.endswith("/v1/score")
    assert req.get_header("Authorization") == "Bearer test-token"
    # The service does its own truncation — the client must not pre-trim the JD.
    assert body["jd_text"] == JOB["description"]
    assert body["company"] == "Acme"
    assert body["role_title"] == "Senior Product Manager"
    assert body["location"] == "Remote"
    assert body["pipeline_run_id"] == "js-7-3"


def test_missing_location_is_sent_as_null(calls):
    recorded = calls(lambda req: _FakeResponse(json.dumps(SCORE_BODY)))

    scoring_client.analyze_via_cloud({**JOB, "location": ""}, "js-7-4")

    assert json.loads(recorded[0].data.decode("utf-8"))["location"] is None


def test_503_is_config_and_is_not_retried(calls):
    recorded = calls(_http_error(503, {"detail": {"error": "ANTHROPIC_API_KEY not configured"}}))

    result, error_kind = scoring_client.analyze_via_cloud(JOB, "js-7-3")

    assert (result, error_kind) == (None, "config")
    assert len(recorded) == 1


def test_401_is_config_and_is_not_retried(calls):
    recorded = calls(_http_error(401, {"detail": "Invalid or missing bearer token"}))

    result, error_kind = scoring_client.analyze_via_cloud(JOB, "js-7-3")

    assert (result, error_kind) == (None, "config")
    assert len(recorded) == 1


def test_502_is_transient_and_is_retried(calls):
    recorded = calls(_http_error(502, {"detail": {"error": "Upstream returned an unparseable response"}}))

    result, error_kind = scoring_client.analyze_via_cloud(JOB, "js-7-3")

    assert (result, error_kind) == (None, "transient")
    assert len(recorded) == 3


def test_timeout_is_transient(calls):
    recorded = calls(TimeoutError("timed out"))

    result, error_kind = scoring_client.analyze_via_cloud(JOB, "js-7-3")

    assert (result, error_kind) == (None, "transient")
    assert len(recorded) == 3


def test_connection_refused_is_transient(calls):
    recorded = calls(urllib.error.URLError(ConnectionRefusedError("connection refused")))

    result, error_kind = scoring_client.analyze_via_cloud(JOB, "js-7-3")

    assert (result, error_kind) == (None, "transient")
    assert len(recorded) == 3


def test_non_json_body_is_transient(calls):
    calls(lambda req: _FakeResponse("<html>502 Bad Gateway</html>"))

    result, error_kind = scoring_client.analyze_via_cloud(JOB, "js-7-3")

    assert (result, error_kind) == (None, "transient")


def test_json_body_missing_a_required_field_is_transient(calls):
    incomplete = {k: v for k, v in SCORE_BODY.items() if k != "role_score"}
    calls(lambda req: _FakeResponse(json.dumps(incomplete)))

    result, error_kind = scoring_client.analyze_via_cloud(JOB, "js-7-3")

    assert (result, error_kind) == (None, "transient")


def test_never_raises_on_an_unexpected_exception(calls):
    calls(RuntimeError("something nobody predicted"))

    result, error_kind = scoring_client.analyze_via_cloud(JOB, "js-7-3")

    assert (result, error_kind) == (None, "transient")
