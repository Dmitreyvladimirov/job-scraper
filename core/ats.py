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
    location_reason: str = ""
    penalty: int = 0
    penalty_reason: str = ""


def analyze(job: dict, resume_text: str) -> ATSResult | None:
    """Score job against resume using explicit rubric. Returns structured analysis,
    or None when the analysis itself failed (network/parse error) — a failure must
    not be conflated with a genuine low score, or the job gets permanently skipped."""
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

4. LOCATION (0–15) — read the full JD, not just a "Remote" headline:
   Many listings say "Remote" or "Full Remote job" but actually restrict eligibility to
   residents/citizens of one country or region. Treat ALL of the following as an authoritative
   restriction — score LOCATION based on the named country/region, NOT 15, regardless of any
   "Remote"/"Full Remote job" boilerplate elsewhere in the text:
     - Explicit residency/work-authorization wording: "must be based in the US", "must be
       authorized to work in Germany", "this is a UK-based remote role", "candidates must
       reside in France"
     - Aggregator boilerplate naming a single country, e.g. "the offer is available from:
       <country>", "hiring only in <country>", "open to candidates in <country>", "Remote
       Position: false" — this is a literal per-posting country field, not a stylistic remark;
       whatever country/region follows it is the actual restriction, even if the same JD also
       says "Full Remote job" or "Remote" elsewhere
     - "U.S. Citizen required", "eVerify participant", active security clearance requirement
       (all imply onsite/US-only eligibility)
     - On-site/hybrid role type stated directly — "Position Role Type: Hybrid", "on-site",
       "in-office", or a named specific office city/location with no remote option offered
   For an on-site/hybrid role tied to a specific office, score LOCATION as if that office's
   country/region were the residency restriction (0, or 8 if the office is in EMEA) — UNLESS the
   office is in Israel, in which case score 15 (candidate is Tel Aviv-based, so an Israel-based
   on-site/hybrid role is fine).
   Before writing location_reason, explicitly scan the JD for the phrase "available from",
   "offer is available", or any single named country/region — if found, that is the restriction;
   do not default to 15 just because "remote" also appears. When still uncertain, re-read the JD
   once before deciding; do not default to 15.
   Remote with NO country/region restriction (genuinely worldwide) OR Israel-based (remote,
   on-site, or hybrid) = 15
   Restricted to Europe / EMEA (residency OR on-site/hybrid office) = 8
     e.g. "offer available from: Germany/France/Poland/UK/Portugal/UAE/etc." → 8, not 0
   Restricted to US only / LATAM / APAC only (residency OR on-site/hybrid office) = 0
     e.g. "offer available from: United States/Canada/Brazil/India/Philippines/etc." → 0
   Russia-based (company office or HQ in Russia) = 0

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
  "location_reason": "<REQUIRED. <20 words: quote or paraphrase the specific JD text that justifies the location_score (residency/citizenship/clearance requirement, or the on-site/hybrid office location). If scoring 15, write 'no restriction found — worldwide/Israel'.>",
  "penalty": <0 or 15>,
  "penalty_reason": "<REQUIRED if penalty is 15: <20 words naming the specific domain requirement and the gap (e.g. 'requires 5+ years IAM experience, candidate has none'). Empty string if penalty is 0.>",
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
        # Force valid JSON — without this the model occasionally wraps the reply
        # in markdown fences, which used to fail json.loads() below
        "response_format": {"type": "json_object"},
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
        location_reason    = parsed.get("location_reason", "") or ""
        penalty            = 15 if int(parsed.get("penalty", 0)) > 0 else 0
        penalty_reason     = (parsed.get("penalty_reason", "") or "") if penalty else ""

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
            location_reason=location_reason,
            penalty=penalty,
            penalty_reason=penalty_reason,
        )
    except Exception as e:
        logger.error(f"ATS analysis failed for '{job['title']}': {e}")
        return None
