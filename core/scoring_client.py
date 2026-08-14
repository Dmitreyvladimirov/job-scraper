"""HTTP client for the resumebuilder-cloud scoring service.

Drop-in alternative to ats.analyze() behind the USE_CLOUD_SCORING flag: it returns
the same ATSResult and keeps the same "never raise" contract, so the caller can swap
one for the other without touching its error handling.

The extra return value is error_kind, which separates two failures the caller must
treat differently: a "transient" blip (timeout, 502) only costs this one vacancy and
is retried on the next run, while "config" (bad token, service missing its API key)
means every remaining call this run will fail the same way — the caller alerts once
and stops rather than hammering the service 40 times.
"""
import json
import logging
import os
import urllib.error
import urllib.request
from dataclasses import dataclass

from ats import ATSResult
from utils import retry

logger = logging.getLogger(__name__)

SCORING_SERVICE_URL = os.environ.get(
    "SCORING_SERVICE_URL", "https://resumebuilder-scoring-production.up.railway.app"
)
SCORING_TOKEN = os.environ.get("SCORING_TOKEN", "")

TIMEOUT = 90  # the service calls Claude synchronously, so it is far slower than a plain API

# Status codes that will fail identically on every subsequent call this run:
# 401/403 = our bearer token is wrong, 503 = the service is up but has no
# ANTHROPIC_API_KEY configured (see services/scoring/app/main.py). Retrying these
# wastes 3x the wall clock and still fails.
_CONFIG_STATUS = (401, 403, 503)


@dataclass
class _ConfigFailure:
    """Returned rather than raised by _post_once(), so utils.retry() sees a normal
    return value and stops immediately instead of retrying a hopeless call."""

    detail: str


def analyze_via_cloud(job: dict, pipeline_run_id: str) -> tuple[ATSResult | None, str]:
    """Score a job through the cloud service instead of the local ats.analyze().

    Returns (result, error_kind) where error_kind is:
      "none"      — result is an ATSResult
      "transient" — this call failed but the next one may work; result is None
      "config"    — auth/deployment is broken, every call will fail; result is None

    Never raises: like ats.analyze(), a scoring failure must not take down the run.
    """
    payload = json.dumps({
        # Full JD, not truncated — the service applies its own limit, and cutting at
        # 5000 chars here (as ats.py does for its prompt) would silently give the
        # cloud scorer less context than it is built to handle.
        "jd_text": job.get("description") or "",
        "company": job.get("company") or None,
        "role_title": job.get("title"),
        "location": job.get("location") or None,
        "pipeline_run_id": pipeline_run_id,
    }).encode("utf-8")

    url = f"{SCORING_SERVICE_URL.rstrip('/')}/v1/score"

    try:
        parsed = retry(lambda: _post_once(url, payload), retries=3)
    except Exception as e:
        logger.error(f"Cloud scoring failed for '{job.get('title')}': {e}")
        return None, "transient"

    if isinstance(parsed, _ConfigFailure):
        logger.error(f"Cloud scoring config error for '{job.get('title')}': {parsed.detail}")
        return None, "config"

    try:
        result = _to_ats_result(parsed)
    except Exception as e:
        logger.error(f"Cloud scoring returned an unexpected payload for '{job.get('title')}': {e}")
        return None, "transient"

    logger.debug(
        f"  cloud scores: role={result.role_score} domain={result.domain_score} "
        f"(value={result.domain_value_score} exp={result.domain_exp_score}) "
        f"keywords={result.keyword_score} location={result.location_score} "
        f"penalty={result.penalty} → {result.score} "
        f"[pipeline_run_id={parsed.get('pipeline_run_id')}]"
    )
    return result, "none"


def _post_once(url: str, payload: bytes) -> dict | _ConfigFailure:
    """One POST attempt. Raises on anything retryable, returns _ConfigFailure on the
    codes that never recover. Built fresh per attempt so a retry can't reuse a
    half-consumed request."""
    req = urllib.request.Request(url, data=payload)
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {SCORING_TOKEN}")

    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        if e.code in _CONFIG_STATUS:
            return _ConfigFailure(f"HTTP {e.code}: {_error_detail(e)}")
        raise

    # A body that isn't valid JSON raises here and is retried as transient — it is
    # usually a proxy/error page rather than the service itself answering.
    return json.loads(body)


def _error_detail(e: urllib.error.HTTPError) -> str:
    """Best-effort human-readable reason from an error response. The service answers
    401 with a plain detail string and 502/503 with a detail dict, so handle both and
    fall back to the raw body when it is neither."""
    try:
        body = e.read().decode("utf-8")
    except Exception:
        return e.reason or ""
    try:
        detail = json.loads(body).get("detail")
    except Exception:
        return body[:200]
    if isinstance(detail, dict):
        return str(detail.get("error") or detail)
    return str(detail) if detail else body[:200]


def _to_ats_result(parsed: dict) -> ATSResult:
    """Map a ScoreResult body onto ATSResult — every field is 1:1. cost_usd and
    pipeline_run_id come back too but have no ATSResult home; they are logged, not
    stored here. Bounds are already enforced service-side by ScoreResult's Field
    constraints, so there is nothing to re-clamp."""
    return ATSResult(
        score=int(parsed["score"]),
        why_apply=parsed.get("why_apply", ""),
        why_not=parsed.get("why_not", ""),
        matched=parsed.get("matched", []),
        missed=parsed.get("missed", []),
        domain=parsed.get("domain", ""),
        role_score=int(parsed["role_score"]),
        domain_score=int(parsed["domain_score"]),
        domain_value_score=int(parsed["domain_value_score"]),
        domain_exp_score=int(parsed["domain_exp_score"]),
        keyword_score=int(parsed["keyword_score"]),
        location_score=int(parsed["location_score"]),
        location_reason=parsed.get("location_reason", ""),
        penalty=int(parsed.get("penalty", 0)),
        penalty_reason=parsed.get("penalty_reason", ""),
    )
