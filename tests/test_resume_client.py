"""resume_client mirrors scoring_client's contract; the tests mirror
test_scoring_client.py's doubles. The one behavioral difference under test:
retries=1 — a transient failure must be tried exactly once, because a retried
generation re-runs the whole paid Claude pipeline."""
import io
import json
import sys
import urllib.error
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "core"))

import resume_client  # noqa: E402
import utils  # noqa: E402

JOB = {
    "title": "Senior Product Manager",
    "company": "Acme",
    "description": "We need a PM for our AI platform." * 10,
}

# A complete ResumeGenerateResult body (rbc_common.schemas) — the client keeps
# generation_run_id and skeptic_findings, the rest is logged or ignored.
GENERATE_BODY = {
    "generation_run_id": 42,
    "domain": "ai",
    "geekbrains_framing": "financial",
    "include_ai_consulting": True,
    "subtitle": "AI Platforms | SaaS | APIs",
    "about_me": "...",
    "skills_block": "...",
    "jobs": [],
    "skeptic_findings": [{"category": "buzzword", "text": "scalable"}],
    "pdf_url": "https://resume.test/v1/resume/runs/42/pdf?expires=1&sig=x",
    "cost_usd": 0.0031,
    "pipeline_run_id": "js-7-3",
}


class _FakeResponse:
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
        "https://resume.test/v1/resume/generate", code, "error", {},
        io.BytesIO(payload.encode("utf-8")),
    )


@pytest.fixture
def calls(monkeypatch):
    monkeypatch.setattr(utils.time, "sleep", lambda _s: None)
    recorded: list = []

    def install(behaviour):
        def fake_urlopen(req, timeout=None):
            recorded.append(req)
            if callable(behaviour):
                return behaviour(req)
            raise behaviour

        monkeypatch.setattr(resume_client.urllib.request, "urlopen", fake_urlopen)
        return recorded

    return install


def test_happy_path_returns_run_id_and_findings(calls):
    calls(lambda req: _FakeResponse(json.dumps(GENERATE_BODY)))

    outcome, error_kind = resume_client.generate_via_cloud(JOB, "js-7-3")

    assert error_kind == "none"
    assert outcome.generation_run_id == 42
    assert outcome.skeptic_findings == [{"category": "buzzword", "text": "scalable"}]


def test_request_sends_full_jd_and_bearer_token(calls, monkeypatch):
    monkeypatch.setattr(resume_client, "RESUME_TOKEN", "test-token")
    recorded = calls(lambda req: _FakeResponse(json.dumps(GENERATE_BODY)))

    resume_client.generate_via_cloud(JOB, "js-7-3")

    req = recorded[0]
    body = json.loads(req.data.decode("utf-8"))
    assert req.full_url.endswith("/v1/resume/generate")
    assert req.get_header("Authorization") == "Bearer test-token"
    assert body["jd_text"] == JOB["description"]
    assert body["company"] == "Acme"
    assert body["role_title"] == "Senior Product Manager"
    assert body["pipeline_run_id"] == "js-7-3"


def test_null_skeptic_findings_becomes_empty_list(calls):
    calls(lambda req: _FakeResponse(json.dumps({**GENERATE_BODY, "skeptic_findings": None})))

    outcome, error_kind = resume_client.generate_via_cloud(JOB, "js-7-3")

    assert error_kind == "none"
    assert outcome.skeptic_findings == []


def test_transient_failure_is_tried_exactly_once(calls):
    # THE resume-specific rule: retries=1, a retry re-runs the whole paid pipeline.
    recorded = calls(_http_error(502, {"detail": {"error": "upstream burp"}}))

    outcome, error_kind = resume_client.generate_via_cloud(JOB, "js-7-3")

    assert (outcome, error_kind) == (None, "transient")
    assert len(recorded) == 1


def test_timeout_is_transient_and_single_attempt(calls):
    recorded = calls(TimeoutError("timed out"))

    outcome, error_kind = resume_client.generate_via_cloud(JOB, "js-7-3")

    assert (outcome, error_kind) == (None, "transient")
    assert len(recorded) == 1


def test_503_is_config(calls):
    recorded = calls(_http_error(503, {"detail": {"error": "ANTHROPIC_API_KEY not configured"}}))

    outcome, error_kind = resume_client.generate_via_cloud(JOB, "js-7-3")

    assert (outcome, error_kind) == (None, "config")
    assert len(recorded) == 1


def test_401_is_config(calls):
    recorded = calls(_http_error(401, {"detail": "Invalid or missing bearer token"}))

    outcome, error_kind = resume_client.generate_via_cloud(JOB, "js-7-3")

    assert (outcome, error_kind) == (None, "config")
    assert len(recorded) == 1


def test_non_json_body_is_transient(calls):
    calls(lambda req: _FakeResponse("<html>502 Bad Gateway</html>"))

    outcome, error_kind = resume_client.generate_via_cloud(JOB, "js-7-3")

    assert (outcome, error_kind) == (None, "transient")


def test_body_missing_run_id_is_transient(calls):
    incomplete = {k: v for k, v in GENERATE_BODY.items() if k != "generation_run_id"}
    calls(lambda req: _FakeResponse(json.dumps(incomplete)))

    outcome, error_kind = resume_client.generate_via_cloud(JOB, "js-7-3")

    assert (outcome, error_kind) == (None, "transient")


def test_never_raises_on_an_unexpected_exception(calls):
    calls(RuntimeError("something nobody predicted"))

    outcome, error_kind = resume_client.generate_via_cloud(JOB, "js-7-3")

    assert (outcome, error_kind) == (None, "transient")
