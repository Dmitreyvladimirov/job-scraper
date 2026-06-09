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
    domain: str = ""
    role_score: int = 0
    domain_score: int = 0
    keyword_score: int = 0
    location_score: int = 0


def analyze(job: dict, resume_text: str) -> ATSResult:
    """Score job against resume using explicit rubric. Returns structured analysis."""
    prompt = f"""You are an ATS scorer. Score this job against the candidate's profile using a strict rubric.

CANDIDATE PROFILE:
- Target role: Senior Product Manager
- Strong domains: AI/ML, B2B SaaS, Cybersecurity, FinTech, EdTech
- Location: Tel Aviv, Israel — accepts remote worldwide or Israel-based roles only
- Experience: 7+ years PM, B2B SaaS platforms, AI-driven systems

JOB TITLE: {job['title']}
COMPANY: {job['company']}

JOB DESCRIPTION:
{job['description'][:3000]}

CANDIDATE RESUME:
{resume_text[:2500]}

SCORING RUBRIC — sum all four dimensions:

1. ROLE MATCH (0–30):
   Senior PM / Head of Product / Product Lead / Director / VP Product = 25–30
   Mid-level PM = 10–20
   Product Owner / Associate PM / non-PM = 0–10

2. DOMAIN FIT (0–30):
   AI / ML / LLM = 28–30
   B2B SaaS / Platform / APIs = 25–28
   Cybersecurity / SecOps = 22–25
   FinTech / Payments = 20–23
   EdTech / LMS = 18–22
   Data / Analytics / BI = 15–18
   Growth / Consumer / B2C = 10–15
   Other = 0–10

3. KEYWORD OVERLAP (0–25):
   Extract top 12 keywords from the JD. Count how many the candidate's resume covers.
   9–12 matched = 20–25
   5–8 matched = 12–18
   fewer than 5 matched = 0–10

4. LOCATION / REMOTE (0–15):
   Remote worldwide OR Israel-based = 15
   Europe / EMEA = 8
   US only / LATAM / APAC only = 0

Reply with ONLY this JSON, no other text:
{{
  "score": <integer 0-100, exact sum of the four sub-scores>,
  "role_score": <0-30>,
  "domain_score": <0-30>,
  "keyword_score": <0-25>,
  "location_score": <0-15>,
  "domain": "<detected domain: AI/ML | B2B SaaS | Cybersecurity | FinTech | EdTech | Data/Analytics | Growth/Consumer | Other>",
  "why_apply": "<one sentence: strongest reason to apply>",
  "why_not": "<one sentence: biggest gap or risk>",
  "matched": ["<keyword1>", "<keyword2>", "<keyword3>"],
  "missed": ["<keyword1>", "<keyword2>"]
}}"""

    payload = json.dumps({
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 350,
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
        role_score     = min(30, max(0, int(parsed.get("role_score", 0))))
        domain_score   = min(30, max(0, int(parsed.get("domain_score", 0))))
        keyword_score  = min(25, max(0, int(parsed.get("keyword_score", 0))))
        location_score = min(15, max(0, int(parsed.get("location_score", 0))))
        score = role_score + domain_score + keyword_score + location_score
        logger.debug(
            f"  scores: role={role_score} domain={domain_score} "
            f"keywords={keyword_score} location={location_score} → {score}"
        )
        return ATSResult(
            score=score,
            why_apply=parsed.get("why_apply", ""),
            why_not=parsed.get("why_not", ""),
            matched=parsed.get("matched", [])[:3],
            missed=parsed.get("missed", [])[:2],
            domain=parsed.get("domain", ""),
            role_score=role_score,
            domain_score=domain_score,
            keyword_score=keyword_score,
            location_score=location_score,
        )
    except Exception as e:
        logger.error(f"ATS analysis failed for '{job['title']}': {e}")
        return ATSResult(score=0, why_apply="", why_not="", matched=[], missed=[])
