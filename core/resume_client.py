"""HTTP client for the resumebuilder-cloud resume-generation service.

Same shape as scoring_client.py (never raise, error_kind contract) with one
deliberate difference: retries=1. A retried score costs fractions of a cent; a
retried generation re-runs the whole 3-stage Claude pipeline AND stores a second
artifact row, so a flaky network must cost one failed click, not a double bill.
"""
import json
import logging
import os
import urllib.error
import urllib.request
from dataclasses import dataclass

from utils import retry

logger = logging.getLogger(__name__)

RESUME_SERVICE_URL = os.environ.get(
    "RESUME_SERVICE_URL", "https://resumebuilder-resume-production.up.railway.app"
)
RESUME_TOKEN = os.environ.get("RESUME_TOKEN", "")

# classify -> select -> assemble + Skeptic + PDF render, all synchronous — measured
# runs sit well under this, but a generation is too expensive to kill early.
TIMEOUT = 300

# Same never-recovers set as scoring_client: 401/403 = bad bearer token,
# 503 = service deployed without its DATABASE_URL/ANTHROPIC_API_KEY.
_CONFIG_STATUS = (401, 403, 503)


@dataclass
class ResumeOutcome:
    """What the caller stores/acts on. generation_run_id keys both the shared-DB
    artifact row (dashboard's /jobs/{id}/resume-pdf) and the service's own
    /v1/resume/runs/{id}/pdf; skeptic_findings is surfaced as a count on the card."""

    generation_run_id: int
    skeptic_findings: list


@dataclass
class _ConfigFailure:
    """Returned rather than raised by _post_once(), so utils.retry() sees a normal
    return value and stops immediately instead of retrying a hopeless call."""

    detail: str


def generate_via_cloud(job: dict, pipeline_run_id: str) -> tuple[ResumeOutcome | None, str]:
    """Generate a resume for a job through the cloud service.

    Returns (outcome, error_kind) where error_kind is:
      "none"      — outcome is a ResumeOutcome
      "transient" — this call failed but a later one may work; outcome is None
      "config"    — auth/deployment is broken, every call will fail; outcome is None

    Never raises — a generation failure must not take down the dashboard worker
    or a scraper run (Release 2 calls this in a batch at end of run).
    """
    payload = json.dumps({
        "jd_text": job.get("description") or "",
        "company": job.get("company") or None,
        "role_title": job.get("title"),
        "pipeline_run_id": pipeline_run_id,
    }).encode("utf-8")

    url = f"{RESUME_SERVICE_URL.rstrip('/')}/v1/resume/generate"

    try:
        parsed = retry(lambda: _post_once(url, payload), retries=1)
    except Exception as e:
        logger.error(f"Resume generation failed for '{job.get('title')}': {e}")
        return None, "transient"

    if isinstance(parsed, _ConfigFailure):
        logger.error(f"Resume generation config error for '{job.get('title')}': {parsed.detail}")
        return None, "config"

    try:
        outcome = ResumeOutcome(
            generation_run_id=int(parsed["generation_run_id"]),
            skeptic_findings=parsed.get("skeptic_findings") or [],
        )
    except Exception as e:
        logger.error(f"Resume service returned an unexpected payload for '{job.get('title')}': {e}")
        return None, "transient"

    logger.info(
        f"Resume generated for '{job.get('title')}' @ {job.get('company')}: "
        f"run_id={outcome.generation_run_id} skeptic_findings={len(outcome.skeptic_findings)} "
        f"cost_usd={parsed.get('cost_usd')}"
    )
    return outcome, "none"


def _post_once(url: str, payload: bytes) -> dict | _ConfigFailure:
    """One POST attempt. Raises on anything retryable, returns _ConfigFailure on the
    codes that never recover."""
    req = urllib.request.Request(url, data=payload)
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {RESUME_TOKEN}")

    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        if e.code in _CONFIG_STATUS:
            return _ConfigFailure(f"HTTP {e.code}: {_error_detail(e)}")
        raise

    return json.loads(body)


def _error_detail(e: urllib.error.HTTPError) -> str:
    """Best-effort human-readable reason from an error response (same contract as
    scoring_client: plain string 401s, dict-detail 502/503s, raw body fallback)."""
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
