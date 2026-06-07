import re
import json
import logging
import urllib.request
from config import OPENAI_API_KEY

logger = logging.getLogger(__name__)

MODEL = "gpt-4o-mini"


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

    payload = json.dumps({
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 64,
        "temperature": 0,
    }).encode("utf-8")

    req = urllib.request.Request("https://api.openai.com/v1/chat/completions", data=payload)
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {OPENAI_API_KEY}")

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        raw = data["choices"][0]["message"]["content"].strip()
        match = re.search(r'"score"\s*:\s*(\d+)', raw)
        if match:
            return min(100, max(0, int(match.group(1))))
        logger.warning(f"Could not parse score from: {raw!r}")
        return 0
    except Exception as e:
        logger.error(f"ATS scoring failed for '{job['title']}': {e}")
        return 0
