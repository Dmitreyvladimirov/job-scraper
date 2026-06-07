import json
import logging
import urllib.request
from config import OPENAI_API_KEY
from utils import retry

logger = logging.getLogger(__name__)


def generate(job: dict, ats_result, resume_text: str) -> str:
    """
    Generate an adapted resume as simple HTML for the given vacancy.
    GPT selects the right About Me variant and most relevant bullets.
    Returns HTML string ready for Google Docs upload.
    """
    matched = ", ".join(ats_result.matched) if ats_result.matched else "general PM skills"
    missed = ", ".join(ats_result.missed) if ats_result.missed else "none"

    prompt = f"""You are helping Dimitry Kucher adapt his resume for a specific job.

JOB: {job['title']} at {job['company']}
LOCATION: {job.get('location', 'Remote')}
ATS SCORE: {ats_result.score}/100
MATCHED KEYWORDS: {matched}
MISSING KEYWORDS: {missed}

JOB DESCRIPTION (first 2000 chars):
{job['description'][:2000]}

CANDIDATE'S FULL RESUME (source of truth):
{resume_text[:5000]}

TASK:
1. Choose the most appropriate ABOUT ME variant from the resume (or write a new one based on existing variants, tailored to this company and role — mention the company's domain)
2. Select the most relevant 3-4 bullet points per role (IronCircle, Skillfactory, GeekBrains)
3. Keep Education and Skills sections unchanged

OUTPUT: Simple HTML only. Use <h1> for name, <h2> for section headers, <h3> for job titles, <ul><li> for bullets, <p> for paragraphs. No CSS, no style attributes. Simple semantic HTML only.

Structure:
<h1>Dimitry Kucher</h1>
<p>Senior Product Manager | [subtitle line matching the role]</p>
<p>dmitreyvladimirovic@gmail.com | linkedin.com/in/dmitreyvladimirovic</p>

<h2>About Me</h2>
<p>[chosen/adapted About Me]</p>

<h2>Experience</h2>
<h3>[Company] — [Title] ([Dates])</h3>
<ul><li>...</li></ul>

<h2>Education</h2>
...

<h2>Skills</h2>
..."""

    payload = json.dumps({
        "model": "gpt-4o-mini",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 2000,
        "temperature": 0.2,
    }).encode("utf-8")

    req = urllib.request.Request("https://api.openai.com/v1/chat/completions", data=payload)
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {OPENAI_API_KEY}")

    try:
        data = retry(lambda: json.loads(
            urllib.request.urlopen(req, timeout=45).read().decode("utf-8")
        ))
        html = data["choices"][0]["message"]["content"].strip()
        # Strip markdown code fences if GPT adds them
        if html.startswith("```"):
            html = html.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        logger.info(f"Resume generated: {len(html)} chars")
        return html
    except Exception as e:
        logger.error(f"Resume generation failed: {e}")
        return ""
