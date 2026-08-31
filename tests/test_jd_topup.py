"""The scraper must not score a listing stub as if it were a job ad.

Chili Piper #84808 (2026-08-31): remoteworldwide.net renders its postings in the
browser, so a plain fetch returned 232 characters ending in "Loading...". The card
was scored 84 on that, and the resume generated from it had no AI-consulting block —
on a JD whose third requirement is hands-on AI. enrich_url had already resolved the
real Ashby posting; nobody read it.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "core"))

import scraper  # noqa: E402
from config import SCORING_MIN_JD_CHARS  # noqa: E402

_STUB = ("Product Manager, Orchestrator - Remoteworldwide Job Description Posted: "
         "27/08/2026 Anywhere in the world Remote Senior Loading... Apply Now")


def _job(**kw):
    return dict({"source": "Telegram:worldwideremote", "description": _STUB,
                 "url": "https://www.remoteworldwide.net/jobs/product-manager-orchestrator",
                 "apply_url": "https://jobs.ashbyhq.com/chilipiper/edfa0d70"}, **kw)


def test_a_stub_with_a_resolved_posting_is_topped_up():
    assert scraper.jd_needs_topup(_job())


def test_a_real_jd_is_left_alone():
    assert not scraper.jd_needs_topup(_job(description="x" * (SCORING_MIN_JD_CHARS + 1)))


def test_remoteok_is_topped_up_however_long_its_summary_is():
    # RemoteOK stores an AI summary, not the posting — length says nothing there.
    assert scraper.jd_needs_topup(_job(source="RemoteOK", description="x" * 5000))


def test_nothing_to_fetch_when_the_apply_url_is_the_page_we_already_read():
    page = "https://www.remoteworldwide.net/jobs/product-manager-orchestrator"
    assert not scraper.jd_needs_topup(_job(url=page, apply_url=page))
    assert not scraper.jd_needs_topup(_job(apply_url=""))
