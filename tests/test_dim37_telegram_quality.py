import pytest

import filters
from scraper import _quality_gate_failure
from sources.telegram_channels import (
    _extract_title_company,
    _is_absolute_http_url,
    _pick_job_url,
)


FALSE_NEGATIVE_FIXTURES = [
    (
        "careernoborders-540",
        """Привет всем!
Любимая подборка горяченьких вакансий:
Senior Rust Developer
Company - Virtuozzo
Chief Product Officer
Remote-first
Company - NDA""",
    ),
    (
        "careernoborders-547",
        """Привет всем!
Любимая подборка горяченьких вакансий:
Senior Rust Developer
Company - Virtuozzo
Technical Product Manager
Remote
Company - Virtuozzo""",
    ),
    (
        "product_jobs-2063",
        """Вакансия: AI-продакт (с навыками вайбкодинга)
Компания: Балансити и WavyMind
Формат работы: полная занятость, удаленно""",
    ),
    (
        "product_jobs-2084",
        """Вакансия: Продакт-программист-вайбкодер
Компания: marketolog.tech
Формат работы: полная занятость, удаленно""",
    ),
    (
        "pmclub-3887",
        """А по сему можете просто подсказать, где ввести motherlode?
#вакансии_недели
Product Manager в стартап-студию Eyes of Wonder
https://pmclub.pro/tpost/example""",
    ),
    (
        "zarubezhom_jobs-3994",
        """Senior Product Designer в 3Commas
Узнать подробнее и откликнуться: тут
Другие вакансии в компании:
— SEO Specialist
— Senior Product Manager, Trading""",
    ),
    (
        "zarubezhom_jobs-4006",
        """Product Designer в Mira
Узнать подробнее: тут
Другие вакансии в компании:
— Senior Product Manager
— Backend Engineer""",
    ),
    (
        "zarubezhom_jobs-4011",
        """Дайджест вакансий в зарубежных стартапах c русскоговорящими ребятами
Продукт и менеджмент
— Product Manager in AI-Platform for creators
— Lead Product Manager (Data Sources) в Truv
— Technical Product Manager (Payments) в Mindly""",
    ),
]


@pytest.mark.parametrize(("fixture_id", "text"), FALSE_NEGATIVE_FIXTURES)
def test_false_negative_fixture_extracts_pm_role(fixture_id, text):
    title, _ = _extract_title_company(text)

    assert filters.passes_role_filter({"title": title}), f"{fixture_id}: {title!r}"


@pytest.mark.parametrize(
    "url",
    [
        "?q=product",
        "/jobs/123",
        "#vacancies",
        "javascript:void(0)",
        "//example.com/jobs/1",
        "https://example.com/?q=product",
        "https://example.com/#vacancies",
    ],
)
def test_relative_or_navigation_url_is_rejected(url):
    assert _pick_job_url([(url, "Apply")], "Apply") is None


def test_absolute_job_url_is_accepted():
    url = "https://jobs.example.com/jobs/123"
    assert _pick_job_url([(url, "Apply")], "Apply") == url


def test_fallback_title_does_not_inherit_primary_role_url():
    text = """Product Designer в Example
Откликнуться: тут
Другие вакансии:
— Senior Product Manager"""
    links = [("https://jobs.example.com/jobs/designer", "тут")]
    title, _ = _extract_title_company(text)

    assert title == "Senior Product Manager"
    assert _pick_job_url(links, text, title) is None


def test_fallback_title_uses_its_own_link():
    text = """Дайджест вакансий
— Senior Product Manager"""
    url = "https://jobs.example.com/jobs/product-manager"
    links = [(url, "Senior Product Manager")]

    assert _pick_job_url(links, text, "Senior Product Manager") == url


def test_pm_mention_in_prose_is_not_promoted_to_title():
    text = """Бизнес-аналитик
Взаимодействовать с Product Manager, разработчиками и дизайнерами"""
    title, _ = _extract_title_company(text)

    assert title == "Бизнес-аналитик"


def test_quality_gate_rejects_short_jd_before_ats():
    job = {
        "source": "Telegram:example",
        "title": "Product Manager",
        "url": "https://jobs.example.com/jobs/123",
        "apply_url": "https://jobs.example.com/jobs/123",
        "description": "short",
    }
    assert _quality_gate_failure(job) == "jd_too_short:5"


def test_quality_gate_accepts_complete_job():
    job = {
        "source": "Telegram:example",
        "title": "Product Manager",
        "url": "https://jobs.example.com/jobs/123",
        "apply_url": "https://jobs.example.com/jobs/123",
        "description": "x" * 500,
    }
    assert _quality_gate_failure(job) is None
