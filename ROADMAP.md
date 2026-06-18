# Job Scraper — Roadmap

## Сделано

### Инфраструктура
- [x] Railway deployment: cron job пн–пт 4×/день
- [x] `run.sh` entrypoint, `railway.toml`
- [x] Postgres (Railway addon) как единая БД для скрапера и дашборда
- [x] Postgres как источник правды для дедупа — `load_seen_jobs()` вместо Notion lookup
- [x] `filtered_language` добавлен в таблицу `runs`; индексы на `jobs.url`, `jobs.outcome`
- [x] Два сервиса в одном Railway-проекте: `scraper` (cron) + `dashboard` (web)
- [x] Переменные окружения через Railway UI

### Источники
- [x] Himalayas, Remotive, Jobicy, WeWorkRemotely, RemoteOK, Arbeitnow

### Фильтрация
- [x] Фильтр по роли (PM-ключевые слова в title)
- [x] Фильтр по локации (Remote / Israel / EMEA, исключения US-only и т.п.)
- [x] Фильтр по дате публикации (MAX_JOB_AGE_DAYS = 14)
- [x] Языковой фильтр: EN/ES/RU проходят, DE/FR/NL блокируются
  - Хард-блок: маркеры `(m/w/d)`, `:in` суффикс
  - Мягкий блок: 3+ стоп-слова из заблокированных языков
  - Исключение: вакансии с явным требованием русского языка всегда проходят

### Дедупликация
- [x] **SQLite как источник правды** — `load_seen_jobs()` загружает seen URLs и (company, title) из БД
- [x] Cross-source деdup внутри прогона по (company, title): из дублей выбирается источник с лучшим описанием (не-RemoteOK приоритетнее, затем длиннейшее описание)
- [x] `normalize_job_key()`: fuzzy-матчинг, стрипает Inc/Ltd/GmbH и пунктуацию

### ATS-скоринг
- [x] GPT-4o-mini, temperature=0, 4 измерения: Role / Domain / Keywords / Location
- [x] Domain Fit разделён на Value (0–15) + Experience (0–15)
- [x] Penalty −15 за жёсткие требования по домену
- [x] Must Have (2 балла) / Nice to Have (1 балл) для keyword overlap
- [x] Рубрика синхронизирована с ResumeBuilder
- [x] Калибровка: большинство вакансий 50–70; выше 80 — только при подтверждённом senior-уровне + прямом доменном опыте; generic PM background = 5–8 по Domain Experience

### URL-обогащение
- [x] Поиск прямой ссылки через Greenhouse / Lever / Ashby API
- [x] Валидация компании через `_company_match()` (защита от ложных совпадений типа Insider → Business Insider)
- [x] Для RemoteOK: загрузка полного JD из прямого источника (`fetch_jd_from_url`) после обогащения
- [x] Предупреждение ⚠️ в Notion callout если прямая ссылка не найдена
- [x] `enrich_existing.py` — ретроактивное обновление уже созданных карточек

### Notion
- [x] Карточки qualified (Scraped) и rejected (rejected_by_scraper)
- [x] ATS Score как отдельное числовое поле (сортировка)
- [x] Callout с ATS breakdown: Role / Domain (Value·Exp) / Keywords / Location + penalty
- [x] Cooldown-предупреждение при повторной вакансии той же компании (90 дней)
- [x] Компания извлекается из title "Position (Company)" если поле пустое

### Telegram
- [x] Сводка после каждого прогона: qualified, фильтры, топ вакансии, по источникам
- [x] Алерт если все источники вернули 0 вакансий

### Аналитический дашборд
- [x] FastAPI + Chart.js на Railway (отдельный web-сервис, тот же репо)
- [x] KPI: всего прогонов, qualified, GPT-вызовы, total fetched
- [x] Графики: qualified/fetched по дням, воронка фильтров, ATS score distribution, эффективность источников
- [x] Таблица топ-компаний и последних прогонов
- [x] Защита по токену (`?token=xxx`)
- [x] Ссылка на дашборд в Telegram-сводке после каждого прогона

### Документация
- [x] `SPEC.md` — полное техническое задание
- [x] `ROADMAP.md` — этот файл

---

## В планах

### Высокий приоритет

#### Прямые источники по компаниям
Опрашивать Greenhouse/Lever/Ashby напрямую для списка целевых компаний.
Даёт вакансии которые не попадают на агрегаторы вообще.
- Составить список 30–50 целевых компаний (Israeli tech, remote-first стартапы, AI-компании)
- Написать `sources/greenhouse_direct.py`, `sources/lever_direct.py`
- Добавить в pipeline наравне с другими источниками

### Средний приоритет

#### Feedback loop
Когда статус карточки в Notion меняется на "applied" или "interview" — бот должен знать.
- Webhook или polling Notion API
- Хранить историю статусов в SQLite
- Показывать в дашборде: conversion rate по источникам и доменам

#### WeWorkRemotely и Arbeitnow: fetch full JD
Аналогично RemoteOK — после обогащения URL загружать полный JD из прямого источника.
Сейчас `fetch_jd_from_url` вызывается только для RemoteOK.

#### Еженедельный дайджест
Вместо 4 уведомлений в день — один дайджест в пятницу.
Топ-5 за неделю, статистика прогонов, сравнение с прошлой неделей.

### Низкий приоритет / идеи

#### Company research при высоком ATS
Когда score ≥ 75 — автоматически добавлять в Notion 2–3 факта о компании (размер, стадия, продукт).

#### Salary tracking
Парсить диапазон зарплат из JD, хранить как числовое поле в Notion для фильтрации.

#### Навыковый gap-анализ
По отклонённым вакансиям — какие keywords стабильно в missed?
Показывать в дашборде: «Вот 5 скиллов которых не хватает чаще всего».

---

## Отложено

| Задача | Причина | Статус |
|---|---|---|
| SQLite → Postgres миграция исторических данных | Volume был пуст — данные не персистировались до подключения Volume | Закрыто, данные потеряны |
| Google Sheets логирование | Отклонено — заменено на Postgres | Код есть (`sheets.py`), не активен |
