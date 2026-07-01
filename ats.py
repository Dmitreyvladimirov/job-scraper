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
    domain_value_score: int = 0
    domain_exp_score: int = 0
    keyword_score: int = 0
    location_score: int = 0
    penalty: int = 0


def analyze(job: dict, resume_text: str) -> ATSResult:
    """Score job against resume using explicit rubric. Returns structured analysis."""
    prompt = f"""You are a strict ATS scorer. Score this job against the candidate's profile.

CALIBRATION — read before scoring:
- Score distribution: 30–55 = poor fit, 56–69 = weak fit, 70–79 = decent, 80–89 = strong, 90+ = exceptional (very rare)
- Most jobs should score 50–70. Score above 80 requires: confirmed senior title + strong direct domain experience + most must-haves covered + perfect location
- When uncertain between two values, always pick the lower one
- "Years of PM experience in B2B SaaS" is NOT domain-specific experience. Only count explicit work IN the exact domain (built ML features, worked at FinTech, ran cybersecurity product line, etc.)
- A generic PM background scores 5–8 on Domain Experience, NOT 12–15

CANDIDATE PROFILE:
- Target role: Senior Product Manager
- Strong domains: AI/ML, B2B SaaS, Cybersecurity, FinTech, EdTech, Data/Analytics
- Location: Tel Aviv, Israel — accepts remote worldwide or Israel-based roles only
- Experience: 7+ years PM, B2B SaaS platforms, AI-driven systems, payments, edtech

JOB TITLE: {job['title']}
COMPANY: {job.get('company') or '(see description)'}

JOB DESCRIPTION:
{job['description'][:5000]}

CANDIDATE RESUME:
{resume_text[:7000]}

SCORING RUBRIC — sum all four dimensions, then apply penalty if triggered:

1. ROLE MATCH (0–30):
   Senior PM / Head of Product / Product Lead / Director / VP Product = 25–30
   PM without "Senior" / mid-level = 12–22
   Product Owner / Associate PM / non-PM title = 0–10

2. DOMAIN FIT (0–30) — two sub-factors, each 0–15:

   A) DOMAIN VALUE (0–15): how strategically valuable is this domain for the candidate's profile:
      AI / ML / LLM = 14–15
      B2B SaaS / Platform / APIs = 12–14
      Cybersecurity / SecOps = 11–12
      FinTech / Payments = 10–11
      EdTech / HRTech / WorkTech = 9–11
      Data / Analytics / BI = 7–9
      Growth / Consumer / B2C = 5–7
      Other = 0–5
      +2 bonus if role explicitly requires AI/ML PM AND candidate has shipped ≥1 ML/AI feature (cap at 15)

   B) DOMAIN EXPERIENCE (0–15): how much of the candidate's actual background covers the required domain:
      Full coverage — AI/ML, data analytics, B2B SaaS, EdTech = 12–15
      Partial coverage — e.g. FinTech via payments experience, cybersecurity adjacent = 7–11
      Minimal / adjacent only = 2–5
      No coverage at all = 0–2

   DOMAIN FIT = A + B (cap at 30)

3. KEYWORD OVERLAP (0–25):
   Extract exactly 12 keywords from the JD. Classify each as Must Have or Nice to Have.
   Must Have: ✅ full match = 2 pts | ⚠️ partial/reframeable = 1 pt | ❌ not covered = 0 pts
   Nice to Have: ✅ full match = 1 pt | ⚠️ partial = 0.5 pts | ❌ not covered = 0 pts
   Map raw total → 0–25 scale (max raw ≈ 28 → 25 pts cap)

4. LOCATION (0–15):
   Remote worldwide OR Israel-based = 15
   Europe / EMEA = 8
   US only / LATAM / APAC only = 0

HARD REQUIREMENT PENALTY: −15 (applied to final total, floor at 0)
Apply ONLY when ALL three are true:
1. JD explicitly requires N years in a specific technical domain (cybersecurity, IAM, fraud, healthcare, legal, etc.)
2. Candidate has less than 50% of that domain-specific experience
3. The requirement is domain-specific — NOT general PM tenure ("8+ years as PM")

Reply with ONLY this JSON, no other text:
{{
  "role_score": <0-30>,
  "domain_value_score": <0-15>,
  "domain_exp_score": <0-15>,
  "keyword_score": <0-25>,
  "location_score": <0-15>,
  "penalty": <0 or 15>,
  "domain": "<detected domain: AI/ML | B2B SaaS | Cybersecurity | FinTech | EdTech | Data/Analytics | Growth/Consumer | Other>",
  "why_apply": "<one sentence: strongest reason to apply>",
  "why_not": "<one sentence: biggest gap or risk>",
  "matched": ["<keyword1>", "<keyword2>", "<keyword3>"],
  "missed": ["<keyword1>", "<keyword2>"]
}}"""

    payload = json.dumps({
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 400,
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

        role_score         = min(30, max(0, int(parsed.get("role_score", 0))))
        domain_value_score = min(15, max(0, int(parsed.get("domain_value_score", 0))))
        domain_exp_score   = min(15, max(0, int(parsed.get("domain_exp_score", 0))))
        domain_score       = min(30, domain_value_score + domain_exp_score)
        keyword_score      = min(25, max(0, int(parsed.get("keyword_score", 0))))
        location_score     = min(15, max(0, int(parsed.get("location_score", 0))))
        penalty            = 15 if int(parsed.get("penalty", 0)) > 0 else 0

        score = max(0, role_score + domain_score + keyword_score + location_score - penalty)

        logger.debug(
            f"  scores: role={role_score} domain={domain_score} "
            f"(value={domain_value_score} exp={domain_exp_score}) "
            f"keywords={keyword_score} location={location_score} penalty={penalty} → {score}"
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
            domain_value_score=domain_value_score,
            domain_exp_score=domain_exp_score,
            keyword_score=keyword_score,
            location_score=location_score,
            penalty=penalty,
        )
    except Exception as e:
        logger.error(f"ATS analysis failed for '{job['title']}': {e}")
        return ATSResult(score=0, why_apply="", why_not="", matched=[], missed=[])
