import re
import logging
import anthropic
from config import ANTHROPIC_API_KEY

logger = logging.getLogger(__name__)

_client: anthropic.Anthropic | None = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    return _client


def score(job: dict, resume_text: str) -> int:
    """Return ATS match score 0–100. Returns 0 on any error."""
    prompt = f"""You are an ATS analyzer. Compare this job description against the candidate's resume.

JOB TITLE: {job['title']}
COMPANY: {job['company']}

JOB DESCRIPTION:
{job['description'][:3000]}

CANDIDATE RESUME:
{resume_text[:3000]}

Analyze: keyword overlap, required skills match, seniority fit, domain relevance.
Reply with ONLY this JSON, no other text:
{{"score": <integer 0-100>, "recommendation": "APPLY" or "DONT_APPLY"}}"""

    try:
        message = _get_client().messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=64,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = message.content[0].text.strip()
        match = re.search(r'"score"\s*:\s*(\d+)', raw)
        if match:
            return min(100, max(0, int(match.group(1))))
        logger.warning(f"Could not parse score from: {raw!r}")
        return 0
    except Exception as e:
        logger.error(f"ATS scoring failed for '{job['title']}': {e}")
        return 0
