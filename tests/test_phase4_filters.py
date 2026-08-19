"""Phase 4 input-data fixes (2026-08-19): the Jobgether boilerplate strip and the
Portuguese/Uzbek extension of the language filter. Both were found via the
2026-08-14 shadow-scoring label comparison."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "core"))

import filters  # noqa: E402
from sources import jobgether  # noqa: E402


# --- Jobgether boilerplate strip ---

def test_clean_description_strips_leading_boilerplate():
    raw = ('<p>This a Full Remote job, the offer is available from: Texas (USA)</p> '
           '<p>Overview: real JD text here.</p>')
    assert jobgether.clean_description(raw) == '<p>Overview: real JD text here.</p>'


def test_clean_description_only_strips_at_start():
    raw = '<p>Real intro.</p><p>This a Full Remote job, the offer is available from: X</p>'
    assert jobgether.clean_description(raw) == raw  # mid-document mention untouched


def test_clean_description_noop_without_boilerplate():
    raw = '<p>Just a normal JD.</p>'
    assert jobgether.clean_description(raw) == raw


def test_clean_description_multi_country_prefix():
    raw = ('<p>This a Full Remote job, the offer is available from: Portugal, Spain, '
           'Italy</p><p>We are hiring.</p>')
    assert jobgether.clean_description(raw) == '<p>We are hiring.</p>'


# --- Language filter: Portuguese / Uzbek (accent-aware) ---

_PT_JOB = {
    "title": "Product Manager",
    "description": ("Você será responsável pela gestão do produto. Não é necessário "
                    "experiência prévia na área, mas conhecimentos de mercado são um diferencial. "
                    "A vaga inclui benefícios e equipe internacional."),
}

_UZ_JOB = {
    "title": "Product Manager",
    "description": ("Kompaniya uchun mahsulot menejeri kerak. Lavozim talablari: ishlash "
                    "tajribasi hamda yuqori malaka. Jamoa bilan ishlash muhim."),
}

_ES_JOB = {
    "title": "Product Manager",
    "description": ("Buscamos un product manager para nuestra empresa. Trabajarás con "
                    "nosotros en un equipo internacional con grandes beneficios."),
}


def test_portuguese_jd_is_blocked():
    assert filters.passes_language_filter(_PT_JOB) is False


def test_uzbek_jd_is_blocked():
    assert filters.passes_language_filter(_UZ_JOB) is False


def test_spanish_stays_allowed():
    assert filters.passes_language_filter(_ES_JOB) is True


def test_english_stays_allowed():
    job = {"title": "Senior PM", "description": "We are looking for a product manager "
           "to own our roadmap and work with engineering."}
    assert filters.passes_language_filter(job) is True


def test_russian_requirement_exception_still_wins():
    job = dict(_PT_JOB, description=_PT_JOB["description"] + " Russian language required.")
    assert filters.passes_language_filter(job) is True
