import re
import json
import logging
import urllib.request
from dataclasses import dataclass
from config import OPENAI_API_KEY
from utils import retry

logger = logging.getLogger(__name__)

MODEL = "gpt-4o-mini"


@dataclass
class ATSResult:
    score: int
    why_apply: str
    why_not: str
    matched: list[str]
    missed: list[str]


def analyze(job: dict, resume_text: str) -> ATSResult:
    """Score job against resume and return structured analysis."""
    prompt = f"""You are an ATS analyzer. Compare this job description against the candidate's resume.

JOB TITLE: {job['title']}
COMPANY: {job['company']}

JOB DESCRIPTION:
{job['description'][:3000]}

CANDIDATE RESUME:
{resume_text[:3000]}

Reply with ONLY this JSON, no other text:
{{
  "score": <integer 0-100>,
  "why_apply": "<one sentence: strongest reason to apply>",
  "why_not": "<one sentence: biggest gap or risk>",
  "matched": ["<keyword1>", "<keyword2>", "<keyword3>"],
  "missed": ["<keyword1>", "<keyword2>"]
}}"""

    payload = json.dumps({
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 200,
        "temperature": 0,
    }).encode("utf-8")

    req = urllib.request.Request("https://api.openai.com/v1/chat/completions", data=payload)
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {OPENAI_API_KEY}")

    try:
        data = retry(lambda: json.loads(
            urllib.request.urlopen(req, timeout=30).read().decode("utf-8")
        ))
        raw = data["choices"][0]["message"]["content"].strip()
        parsed = json.loads(raw)
        return ATSResult(
            score=min(100, max(0, int(parsed.get("score", 0)))),
            why_apply=parsed.get("why_apply", ""),
            why_not=parsed.get("why_not", ""),
            matched=parsed.get("matched", [])[:3],
            missed=parsed.get("missed", [])[:2],
        )
    except Exception as e:
        logger.error(f"ATS analysis failed for '{job['title']}': {e}")
        return ATSResult(score=0, why_apply="", why_not="", matched=[], missed=[])
