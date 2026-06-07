import json
import logging
import urllib.request
from config import OPENAI_API_KEY
from utils import retry

logger = logging.getLogger(__name__)

SKEPTIC_BUZZWORDS = [
    "strong track record", "scalable", "high-impact", "fast-paced",
    "measurable outcomes", "robust", "leveraged", "boosted",
    "end-to-end" , "seamlessly", "proactively",
]


def generate(job: dict, ats_result, resume_text: str) -> dict | None:
    """
    Select adapted resume content from base_resume.md for the given vacancy.
    Returns a dict of {placeholder: replacement_text} or None on failure.
    """
    matched = ", ".join(ats_result.matched) if ats_result.matched else "general PM skills"
    missed  = ", ".join(ats_result.missed)  if ats_result.missed  else "none"

    prompt = f"""You are adapting Dimitry Kucher's resume for a specific job vacancy.

JOB: {job['title']} at {job['company']}
LOCATION: {job.get('location', 'Remote')}
ATS SCORE: {ats_result.score}/100
MATCHED KEYWORDS: {matched}
MISSING KEYWORDS: {missed}

JOB DESCRIPTION (first 2500 chars):
{job.get('description', '')[:2500]}

BASE RESUME (source of truth — use ONLY content from here, never invent):
{resume_text[:7000]}

---

TASK: Return a JSON object with these exact keys filled from the base resume above.

Rules:
1. Determine the domain of this vacancy: ai / fintech / saas / edtech / data / cyber / growth
2. Pick the matching SUBTITLE variant tagged with that domain
3. Pick the matching ABOUT ME variant tagged with that domain. Mention {job['company']} or its domain in 1 sentence.
4. Pick the matching SKILLS block tagged with that domain (all 4 lines, keep the "Tools:" line unchanged)
5. ALL THREE INTROS ARE REQUIRED — never leave empty: write IC_INTRO, SF_INTRO, GB_INTRO. Pick the best 1-sentence summary for each company from the base resume that fits this domain.
6. Pick bullets from the base resume: choose ones whose tags match the job domain and that cover the matched keywords. Use ONLY existing bullet text, do not invent or combine.
   - IC: always fill IC_B1–IC_B4. Add IC_B5 only if there's a genuinely relevant bullet not yet used.
   - SF: always fill SF_B1–SF_B5. Add SF_B6 only if there's a genuinely relevant bullet not yet used.
   - GB: always fill GB_B1–GB_B4 (4 bullets).
   - Leave optional slots empty ("") if there's nothing meaningful to add — don't pad with weak content.
7. Word limits (to keep 1 page): About Me ≤ 55 words. Each intro ≤ 20 words. Each bullet ≤ 25 words — trim from the end if the source bullet is longer.
8. Skeptic check — for every bullet and intro sentence: if it contains a vague buzzword without a specific number ({', '.join(SKEPTIC_BUZZWORDS[:6])}...), rewrite it with a concrete number or swap for a different bullet that already has one.

Return ONLY valid JSON, no markdown, no explanation:
{{
  "SUBTITLE": "one-line subtitle matching the domain",
  "ABOUT_ME": "2-3 sentence about me paragraph",
  "SKILL_1": "Category name: skill1, skill2, ...",
  "SKILL_2": "Category name: skill1, skill2, ...",
  "SKILL_3": "Category name: skill1, skill2, ...",
  "SKILL_4": "Category name: skill1, skill2, ...",
  "IC_INTRO": "one sentence IronCircle intro",
  "IC_B1": "bullet text",
  "IC_B2": "bullet text",
  "IC_B3": "bullet text",
  "IC_B4": "bullet text",
  "IC_B5": "bullet text or empty string if not needed",
  "SF_INTRO": "one sentence Skillfactory intro",
  "SF_B1": "bullet text",
  "SF_B2": "bullet text",
  "SF_B3": "bullet text",
  "SF_B4": "bullet text",
  "SF_B5": "bullet text or empty string if not needed",
  "SF_B6": "bullet text or empty string if not needed",
  "GB_INTRO": "one sentence GeekBrains intro",
  "GB_B1": "bullet text",
  "GB_B2": "bullet text",
  "GB_B3": "bullet text",
  "GB_B4": "bullet text or empty string if not needed"
}}"""

    payload = json.dumps({
        "model": "gpt-4o-mini",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 2500,
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
    }).encode("utf-8")

    req = urllib.request.Request("https://api.openai.com/v1/chat/completions", data=payload)
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {OPENAI_API_KEY}")

    try:
        data = retry(lambda: json.loads(
            urllib.request.urlopen(req, timeout=60).read().decode("utf-8")
        ))
        content = data["choices"][0]["message"]["content"]
        result = json.loads(content)
        logger.info(f"Resume content generated for {job['company']} (domain detected)")
        return result
    except Exception as e:
        logger.error(f"Resume generation failed: {e}")
        return None
