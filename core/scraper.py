import re
import sys
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import filters
import ats
import scoring_client
import db
import resume_client
import notion_client
import telegram
import sheets
# Disabled 2026-07-14 after source audit (see SOURCES_DECISION.md): himalayas,
# weworkremotely, remotive, remoteok, arbeitnow — API-side issues (ignored search
# params / stale backlog), 21/174 qualified from ~19k rows. Files kept in sources/.
from sources import choicy, company_direct, jobicy, telegram_channels, jobgether
from config import (
    ATS_THRESHOLD, COMPANY_COOLDOWN_DAYS, MAX_GPT_CALLS_PER_RUN, USE_CLOUD_SCORING,
    RESUME_AUTOGEN_MIN_SCORE, RESUME_AUTOGEN_MIN_JD_CHARS, RESUME_AUTOGEN_CAP_PER_RUN,
    COMPANY_REJECTION_COOLDOWN_DAYS,
    validate_secrets,
)
from utils import strip_html, enrich_url, normalize_job_key, fetch_jd_from_url, fetch_url_generic

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

RESUME_PATH = Path(__file__).parent / "base_resume.md"


@dataclass
class Scoring:
    """One vacancy's scoring outcome plus the provenance columns db.log_job() records.
    `result` is what the run acts on; in shadow mode the cloud's opinion rides along in
    shadow_* without touching it."""

    result: ats.ATSResult | None
    source: str  # 'local' | 'cloud' — which scorer produced `result`
    pipeline_run_id: str
    shadow_score: int | None = None
    shadow_payload: dict | None = None
    config_error: str = ""  # set only when cloud auth/deployment is broken


def score_job(job: dict, resume: str, pipeline_run_id: str) -> Scoring:
    """Route one vacancy to the scorer USE_CLOUD_SCORING selects. Off and shadow both
    let ats.analyze() decide, so switching them on cannot change any outcome — shadow
    only adds a second, recorded-but-ignored opinion."""
    if USE_CLOUD_SCORING == "1":
        result, error_kind = scoring_client.analyze_via_cloud(job, pipeline_run_id)
        return Scoring(
            result=result,
            source="cloud",
            pipeline_run_id=pipeline_run_id,
            config_error=(
                f"{scoring_client.SCORING_SERVICE_URL}/v1/score отклонил запрос "
                f"(auth или конфиг сервиса — код статуса в логах). "
                f"Первая вакансия: {job.get('title')} @ {job.get('company')}"
            ) if error_kind == "config" else "",
        )

    result = ats.analyze(job, resume)
    scoring = Scoring(result=result, source="local", pipeline_run_id=pipeline_run_id)

    if USE_CLOUD_SCORING == "shadow":
        cloud_result, error_kind = scoring_client.analyze_via_cloud(job, pipeline_run_id)
        if cloud_result is None:
            # Shadow failures cost nothing — the local score already decided this
            # vacancy — so they warn and are never counted as ats_error.
            logger.warning(f"  ⚠️ Shadow cloud scoring failed ({error_kind}): {job['title']} @ {job['company']}")
        else:
            scoring.shadow_score = cloud_result.score
            scoring.shadow_payload = asdict(cloud_result)
            local = result.score if result else None
            logger.info(f"  shadow: local={local} cloud={cloud_result.score} [{pipeline_run_id}]")

    return scoring


def load_resume() -> str:
    if not RESUME_PATH.exists():
        logger.error(f"Resume not found at {RESUME_PATH.resolve()}")
        sys.exit(1)
    text = RESUME_PATH.read_text(encoding="utf-8")
    logger.info(f"Resume loaded: {len(text)} chars")
    return text


def run() -> None:
    validate_secrets()
    db.init_db()
    logger.info("=== Job Scraper started ===")
    resume = load_resume()

    sources_data = [
        ("Jobicy",              jobicy.fetch()),
        ("TelegramChannels",    telegram_channels.fetch()),
        ("Jobgether",           jobgether.fetch()),
        ("Choicy",              choicy.fetch()),
        # Last on purpose: when MAX_GPT_CALLS_PER_RUN bites, CompanyDirect's tail is
        # cut first (gpt_limit rows are not marked seen, so the next run retries them).
        ("CompanyDirect",       company_direct.fetch()),
    ]
    source_counts = {name: len(batch) for name, batch in sources_data}
    jobs = [j for _, batch in sources_data for j in batch]

    for job in jobs:
        if job.get("description"):
            job["description"] = strip_html(job["description"])

    # Layer 1: deduplicate within this run by (company, title).
    # When duplicates exist, keep the one with the best description:
    # any source beats RemoteOK (AI summary); among equal-tier sources, longer description wins.
    from collections import defaultdict
    groups: dict[tuple, list[dict]] = defaultdict(list)
    no_key: list[dict] = []
    for job in jobs:
        key = normalize_job_key(job.get("company", ""), job.get("title", ""))
        if key[0] and key[1]:
            groups[key].append(job)
        else:
            no_key.append(job)

    cross_source_dedup = 0
    deduped: list[dict] = list(no_key)
    for key, group in groups.items():
        if len(group) == 1:
            deduped.append(group[0])
        else:
            best = max(group, key=lambda j: (
                0 if j.get("source", "").lower() == "remoteok" else 1,
                len(j.get("description") or ""),
            ))
            cross_source_dedup += len(group) - 1
            sources = [j["source"] for j in group]
            logger.info(f"Cross-source dedup: kept {best['source']} for {best['title']} @ {best['company']} (from {sources})")
            deduped.append(best)
    jobs = deduped

    total_fetched = len(jobs)
    logger.info(
        f"Total fetched: {total_fetched} ({cross_source_dedup} cross-source dupes removed) — "
        + ", ".join(f"{n}:{c}" for n, c in source_counts.items())
    )

    if total_fetched == 0:
        telegram.send_error("⚠️ Все job boards вернули 0 вакансий — возможны проблемы со скрапингом")

    seen_urls, seen_keys = db.load_seen_jobs()
    # Also load from Notion to catch pre-Postgres history (transition period)
    notion_seen_urls, notion_seen_keys = notion_client.load_seen_urls()
    seen_urls |= notion_seen_urls
    seen_keys |= notion_seen_keys
    company_history = notion_client.load_company_applications(COMPANY_COOLDOWN_DAYS)

    counts = {"qualified": 0, "role": 0, "location": 0, "language": 0, "stale": 0, "dedup": 0, "score": 0, "gpt_limit": 0, "ats_error": 0}
    top_jobs: list[dict] = []
    gpt_calls = 0
    started_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    run_id = db.start_run(total_fetched, source_counts)
    score_seq = 0
    cloud_config_alerted = False

    for job in jobs:
        if not job.get("url"):
            continue

        if not filters.passes_role_filter(job):
            counts["role"] += 1
            db.log_job(run_id, job, "role")
            continue

        if not filters.passes_language_filter(job):
            counts["language"] += 1
            db.log_job(run_id, job, "language")
            continue

        if not filters.passes_location_filter(job):
            counts["location"] += 1
            db.log_job(run_id, job, "location")
            continue

        if not filters.passes_date_filter(job):
            counts["stale"] += 1
            db.log_job(run_id, job, "stale")
            continue

        if job["url"] in seen_urls:
            counts["dedup"] += 1
            db.log_job(run_id, job, "dedup")
            continue

        # Layer 2: cross-run dedup by (company, title) — catches same job with different URL
        job_key = normalize_job_key(job.get("company", ""), job.get("title", ""))
        if job_key[0] and job_key[1] and job_key in seen_keys:
            counts["dedup"] += 1
            db.log_job(run_id, job, "dedup")
            continue

        if gpt_calls >= MAX_GPT_CALLS_PER_RUN:
            counts["gpt_limit"] += 1
            db.log_job(run_id, job, "gpt_limit")
            continue

        enrich_url(job)

        # For Telegram jobs: fetch full JD from the linked page; fallback to message text
        if job.get("source", "").startswith("Telegram:"):
            has_url = job.pop("_has_job_url", False)
            message_text = job.pop("_message_text", "")
            if has_url:
                job_url = job.get("url", "")
                description = fetch_jd_from_url(job_url)
                if not description:
                    description = fetch_url_generic(job_url)
                if description:
                    job["description"] = description
                    logger.info(f"  JD fetched: {len(description)} chars for {job['title']} @ {job['company']}")
                else:
                    job["description"] = message_text
                    logger.info(f"  ⚠️ No JD fetched for {job['title']} @ {job['company']}, using message text")
            else:
                job["description"] = message_text

        # For RemoteOK jobs: fetch full JD from the direct URL (RemoteOK only stores AI summary)
        if job.get("source", "").lower() == "remoteok":
            apply_url = job.get("apply_url") or ""
            jd_enriched = False
            if apply_url and apply_url != job.get("url", ""):
                full_jd = fetch_jd_from_url(apply_url) or fetch_url_generic(apply_url)
                if full_jd and len(full_jd) > len(job.get("description") or ""):
                    job["description"] = full_jd
                    jd_enriched = True
                    logger.info(f"  Full JD fetched: {len(full_jd)} chars for {job['title']} @ {job['company']}")
            if not jd_enriched:
                job["incomplete_description"] = True
                logger.info(f"  ⚠️ No direct URL — scoring {job['title']} @ {job['company']} from RemoteOK summary")

        # Try to extract company name from description if missing
        if not job.get("company") and job.get("description"):
            m = re.search(
                r"\b(?:компания|company|работодатель)[:\s]+([A-ZА-ЯЁ][^\n,.(]{2,40})",
                job["description"][:1500], re.IGNORECASE,
            )
            if m:
                job["company"] = m.group(1).strip()

        if filters.is_russia_based(job):
            logger.info(f"  🇷🇺 Russia — rejecting: {job['title']} @ {job['company']}")
            counts["location"] += 1
            db.log_job(run_id, job, "location")
            continue

        score_seq += 1
        pipeline_run_id = f"js-{run_id}-{score_seq}"
        scoring = score_job(job, resume, pipeline_run_id)
        result = scoring.result
        gpt_calls += 1

        # One alert per run, not one per vacancy: a bad token or an unconfigured service
        # fails all ~40 calls identically, so 40 Telegram messages would bury the signal.
        # Each vacancy still takes the ats_error path below, so none are marked seen.
        if scoring.config_error and not cloud_config_alerted:
            telegram.send_error(f"Cloud scoring config error: {scoring.config_error}")
            cloud_config_alerted = True

        # Analysis failure (network/parse) — do NOT mark as seen or rejected,
        # so the job gets re-scored on the next run instead of being lost forever
        if result is None:
            counts["ats_error"] += 1
            db.log_job(run_id, job, "ats_error", pipeline_run_id=pipeline_run_id,
                       scoring_source=scoring.source)
            logger.warning(f"  ⚠️ ATS error — will retry next run: {job['title']} @ {job['company']}")
            continue

        logger.info(f"  {result.score:>3}/100  {job['title']} @ {job['company']}  [{job['source']}]")

        if result.score < ATS_THRESHOLD:
            notion_client.create_rejected_entry(job, result.score)
            seen_urls.add(job["url"])
            seen_keys.add(job_key)
            counts["score"] += 1
            db.log_job(run_id, job, "low_score", ats_score=result.score,
                       domain=result.domain, why_not=result.why_not,
                       why_apply=result.why_apply, matched_keywords=result.matched,
                       missed_keywords=result.missed, penalty_reason=result.penalty_reason or None,
                       role_score=result.role_score, domain_score=result.domain_score,
                       domain_value_score=result.domain_value_score, domain_exp_score=result.domain_exp_score,
                       keyword_score=result.keyword_score, location_score=result.location_score,
                       location_reason=result.location_reason or None,
                       pipeline_run_id=pipeline_run_id, scoring_source=scoring.source,
                       shadow_score=scoring.shadow_score, shadow_payload=scoring.shadow_payload)
            continue

        company_key = job.get("company", "").lower().strip()
        cooldown_match = company_history.get(company_key)
        if cooldown_match:
            logger.info(f"  ⚠️  Cooldown: {cooldown_match['company']} {cooldown_match['days_ago']}d ago")

        ok = notion_client.create_entry(job, result, cooldown_match=cooldown_match)
        seen_urls.add(job["url"])
        seen_keys.add(job_key)
        counts["qualified"] += 1
        db.log_job(run_id, job, "qualified", ats_score=result.score,
                   domain=result.domain, why_not=result.why_not,
                   why_apply=result.why_apply, matched_keywords=result.matched,
                   missed_keywords=result.missed, penalty_reason=result.penalty_reason or None,
                   role_score=result.role_score, domain_score=result.domain_score,
                   domain_value_score=result.domain_value_score, domain_exp_score=result.domain_exp_score,
                   keyword_score=result.keyword_score, location_score=result.location_score,
                   location_reason=result.location_reason or None,
                   pipeline_run_id=pipeline_run_id, scoring_source=scoring.source,
                   shadow_score=scoring.shadow_score, shadow_payload=scoring.shadow_payload)
        top_jobs.append({
            "title": job["title"],
            "company": job["company"],
            "score": result.score,
        })

    top_jobs.sort(key=lambda x: x["score"], reverse=True)
    db.finish_run(run_id, counts, gpt_calls)
    sheets.log_run(counts, gpt_calls, source_counts, started_at=started_at)
    telegram.send_run_summary(counts, top_jobs, source_counts)
    # After the summary on purpose (QA review 2026-08-19): a slow/stuck resume
    # service must never delay or drop the run's primary notification. Autogen
    # reports through its own message instead — which also survives the
    # summary's qualified==0 early-return (backlog cards can generate on a run
    # that found nothing new).
    autogen_resumes()

    logger.info(
        f"=== Done: {counts['qualified']} qualified | GPT calls: {gpt_calls}/{MAX_GPT_CALLS_PER_RUN} | "
        f"role:{counts['role']} language:{counts['language']} location:{counts['location']} "
        f"stale:{counts['stale']} dedup:{counts['dedup']} low_score:{counts['score']} gpt_limit:{counts['gpt_limit']} ats_error:{counts['ats_error']} ==="
    )


def autogen_resumes() -> int:
    """Release 2 (2026-08-19): batch resume generation at the end of a run for the
    review-queue cards that pass the quality gates (see db.get_autogen_candidates).
    Never raises — a broken resume service must not fail an otherwise-good run.
    A config failure (bad token / undeployed service) aborts the batch after one
    Telegram alert: every remaining call would fail identically. Transient failures
    just skip the card; resume_run_id stays NULL, so the next run retries it."""
    try:
        candidates = db.get_autogen_candidates(
            RESUME_AUTOGEN_MIN_SCORE, RESUME_AUTOGEN_MIN_JD_CHARS, RESUME_AUTOGEN_CAP_PER_RUN,
    COMPANY_REJECTION_COOLDOWN_DAYS,
            cooldown_days=COMPANY_REJECTION_COOLDOWN_DAYS)
    except Exception as e:
        logger.error(f"Resume autogen: candidate query failed: {e}")
        return 0
    generated: list[dict] = []
    for job in candidates:
        # The whole loop body is guarded (QA review 2026-08-19): a DB blip in
        # set_resume_run_id after a PAID generation must neither kill the run
        # nor abort the rest of the batch. The known cost of that blip is one
        # possible re-generation next run (~$0.005) — accepted. The read-check-
        # generate race with the dashboard's own Generate button is likewise
        # accepted (same tiny cost, single user); an atomic claim would trade it
        # for a stuck-sentinel risk, which is worse.
        try:
            # Provenance marker: the dashboard's Generate button reuses the card's
            # scoring pipeline_run_id verbatim, so without the -autogen suffix a
            # batch generation is indistinguishable from a manual click in
            # resume.generation_run (bit us live on 2026-08-19).
            base_prid = job.get("pipeline_run_id") or f"js-{job['id']}"
            outcome, error_kind = resume_client.generate_via_cloud(
                job, f"{base_prid}-autogen")
            if outcome is None:
                if error_kind == "config":
                    telegram.send_error("Resume autogen config error — batch aborted (check RESUME_TOKEN / service)")
                    break
                continue
            db.set_resume_run_id(job["id"], outcome.generation_run_id)
            generated.append({
                "title": job["title"], "company": job["company"],
                "score": job["ats_score"], "flags": len(outcome.skeptic_findings),
            })
            logger.info(
                f"  📄 resume generated: {job['title']} @ {job['company']} "
                f"(score {job['ats_score']}, run {outcome.generation_run_id}, "
                f"{len(outcome.skeptic_findings)} flags)")
        except Exception:
            logger.exception(f"Resume autogen failed for job {job.get('id')} — continuing batch")
    if candidates:
        logger.info(f"Resume autogen: {len(generated)}/{len(candidates)} generated")
    if generated:
        try:
            telegram.send_autogen_summary(generated)
        except Exception:
            logger.exception("Resume autogen: telegram notification failed (non-fatal)")
    return len(generated)


if __name__ == "__main__":
    run()
