# design.md
> JobScraper — v1.0 — 2026-07-05
> Сгенерировано `/spec` из существующих ROADMAP.md / SPEC.md / BACKLOG.md.

## Architecture Overview

Монолитный Python-скрипт, запускаемый по cron на Railway, плюс отдельный
FastAPI-сервис (дашборд) в том же репозитории и Railway-проекте. Postgres —
общая БД для обоих сервисов и единый источник правды для дедупликации и
аналитики. Notion остаётся write-поверхностью/UI для человека (карточки
вакансий), не участвует в дедупликации напрямую с v1.1 (см. REQ-007/112).

**Stack**: Python 3.12+, FastAPI (dashboard), requests/BeautifulSoup (scraping),
OpenAI GPT-4o-mini (ATS), python-telegram-bot (уведомления), psycopg (Postgres).
**Deployment**: Railway — два сервиса в одном проекте: `scraper` (cron
`7 6,9,12,15 * * 1-5`) и `dashboard` (web, FastAPI+Chart.js). GitHub Actions —
только `workflow_dispatch` для ручного запуска (см. REQ-103, известный баг).

## System Diagram

```
Fetch all sources (Himalayas, Remotive, Jobicy, WWR, RemoteOK,
                    Arbeitnow, Jobgether, Telegram×21, [planned: company_direct])
    │
    ▼
Strip HTML из descriptions
    │
    ▼
Cross-source dedup (company + title)  [REQ-006]
    │
    ▼
[Для каждой вакансии]
    ├─ passes_role_filter?       [REQ-002]
    ├─ passes_language_filter?   [REQ-005]
    ├─ passes_location_filter?   [REQ-003]
    ├─ passes_date_filter?       [REQ-004]
    ├─ seen_urls / seen_keys?    [REQ-007, REQ-008]
    │
    ▼
enrich_url()  — Greenhouse → Lever → Ashby  [REQ-012, REQ-013, REQ-101]
    │
    ▼
fetch_jd_from_url() / fetch_url_generic()  — полный JD
    │
    ▼
ats.analyze()  — GPT-4o-mini scoring, ≤40 вызовов/прогон  [REQ-009, NFR-001]
    ├─ score < 60 → create_rejected_entry()  [REQ-010]
    └─ score ≥ 60 → create_entry() + top_jobs
    │
    ▼
telegram.send_run_summary()  [REQ-017, REQ-102]
db.finish_run()  — Postgres
```

## Data Models

### Postgres — существующие таблицы

**`runs`**: id, started_at, finished_at, duration_seconds, filtered_language,
source_counts (jsonb), qualified_count, gpt_calls.

**`jobs`**: id, run_id (FK → runs, планируется nullable — см. REQ-112),
url, company, title, score, outcome (qualified/rejected/low_score), source,
created_at.

### Postgres — планируемые изменения / новые таблицы (v1.1)

**`jobs`** (доп. поля, REQ-111/112):
| Field | Type | Constraints | Notes |
|-------|------|-------------|-------|
| notion_id | TEXT | UNIQUE | связь с Notion page id |
| current_status | TEXT | | канонический статус (enum ниже) |
| source_type | TEXT | DEFAULT 'scraper' | 'scraper' \| 'manual' |
| resume_version | TEXT | nullable | пишется только из `db_manual.py` |
| deleted_at | TIMESTAMP | nullable | tombstone при archived=true в Notion API |

**`status_log`** (REQ-112):
| Field | Type | Constraints |
|-------|------|-------------|
| id | SERIAL | PRIMARY KEY |
| job_id | INT | FK → jobs, индекс `idx_status_log_job_id` |
| old_status | TEXT | |
| new_status | TEXT | |
| changed_at | TIMESTAMP | |
| source | TEXT | 'notion_sync' \| 'manual' |

**`sync_meta`** (REQ-112): key TEXT PRIMARY KEY, value TEXT — хранит `last_sync_at` для инкрементального polling.

**`target_companies`** (REQ-108):
| Field | Type | Constraints |
|-------|------|-------------|
| slug | TEXT | PRIMARY KEY |
| name | TEXT | |
| ats | TEXT | greenhouse \| lever \| ashby \| join |
| status | TEXT | active \| pending \| dormant |
| source | TEXT | manual \| llm \| web-search |
| added_at | TIMESTAMP | |
| last_verified_date | TIMESTAMP | nullable |

**Статус-vocabulary (enum в `config.py`, не в БД)**, маппинг Notion Status →
канонический: `applied → recruiter_reply → screen → interview → offer → rejected`.

**Relationships**: `jobs.run_id` → `runs.id` (LEFT JOIN после REQ-111, т.к.
manual-записи не имеют run_id); `status_log.job_id` → `jobs.id`; `target_companies`
не имеет FK — читается как справочник в `sources/company_direct.py`.

### Notion (текущая схема карточки)
| Поле | Тип | REQ |
|---|---|---|
| Позиция | title | REQ-014 |
| Компания | rich_text | REQ-014 |
| Ссылка на вакансию | url | REQ-014 |
| ATS Score | number | REQ-014 |
| Status2 | select (Scraped / rejected_by_scraper) | REQ-010 |
| Статус | select | REQ-014 |
| Date Applied | date | REQ-014 |
| Тип вакансии | select (🇷🇺 Russia) | REQ-015 |
| Source _(planned)_ | select | REQ-115 |
| Причина отклонения _(planned)_ | select, 4 категории | REQ-116 |

## API / Interface Design

| Method | Path | Auth | REQ | Description |
|--------|------|------|-----|-------------|
| GET | `/` (dashboard) | `?token=` query param | REQ-019 | KPI + графики |
| GET/POST | Greenhouse `boards-api.greenhouse.io` | none | REQ-012, REQ-101 | job listing по slug компании |
| GET/POST | Lever `api.lever.co` | none | REQ-012, REQ-101 | job listing по slug |
| GraphQL | Ashby `jobs.ashbyhq.com` | none | REQ-012, REQ-101 | job listing по slug |
| GET | join.com `/companies/{slug}` | none | REQ-107 | JSON из `__NEXT_DATA__` (Next.js SSR) |
| GET | Telegram `t.me/s/{channel}` | none | REQ-001, REQ-104/105/106 | публичный веб-превью канала |
| REST | Notion API | `NOTION_TOKEN` | REQ-014, REQ-112 | создание карточек, чтение статусов (read-only с v1.1) |
| REST | Telegram Bot API | `TELEGRAM_TOKEN` | REQ-017, REQ-018, REQ-109 | уведомления |
| REST | OpenAI API | `OPENAI_API_KEY` | REQ-009 | GPT-4o-mini scoring |

## File Structure

```
JobScraper/
├── scraper.py            ← оркестратор, точка входа
├── sources/
│   ├── himalayas.py, weworkremotely.py, remotive.py,
│   │   jobicy.py, remoteok.py, arbeitnow.py, jobgether.py
│   ├── telegram_channels.py
│   └── company_direct.py       [planned, REQ-107]
├── filters.py             ← роль/язык/локация/дата
├── ats.py                 ← ATS-скоринг GPT-4o-mini
├── utils.py               ← URL-обогащение, JD fetch, дедуп, HTML strip
│   └── list_greenhouse/list_lever/list_ashby()  [planned refactor, REQ-107]
├── notion_client.py       ← запись вакансий в Notion
├── db.py                  ← Postgres: runs, jobs
├── db_manual.py           ← [planned, REQ-111] ручные подачи
├── sync_notion.py         ← [planned, REQ-112] one-way Notion→Postgres sync
├── telegram.py            ← уведомления
├── config.py              ← константы, env vars, status enum [planned]
├── dashboard.py            ← FastAPI + Chart.js
├── run.sh, railway.toml
└── .github/workflows/scraper.yml   ← workflow_dispatch [REQ-103 нужен фикс]
```

## Security Design

- Секреты только через переменные окружения (Railway UI / `.env` локально, `.env` в `.gitignore`).
- `NOTION_TOKEN`, `OPENAI_API_KEY`, `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`, `DATABASE_URL`,
  `TELEGRAM_JOB_CHANNELS`, `TELEGRAM_API_ID`/`TELEGRAM_API_HASH` (зарезервированы, REQ-118).
- Dashboard защищён query-токеном (не полноценная auth — приемлемо для соло-проекта, единственный пользователь).
- Синк Notion→Postgres строго read-only со стороны Postgres (NFR-003) — исключает риск порчи Notion-данных багом в скрипте синка.
- Никаких PII третьих лиц не хранится, кроме контактных данных вакансий, публично размещённых компаниями.

## Open Questions

- [ ] Приватные Telegram-каналы (REQ-118) — стоит ли строить telethon-путь ради 1 известного кандидата-канала (GoRemote), или ждать больше кандидатов?
- [ ] Наличие `DATABASE_URL` локально (не только на Railway) — нужно для вызова `db_manual.py` из контекста ResumeBuilder (другой репозиторий/процесс).
- [x] ~~Итоговая архитектура объединения JobScraper+ResumeBuilder+Notion в один сервис~~ — read-write фронтенд для сценариев 1+3 (JobScraper) решён 2026-07-15: строится независимо от `JobPostBot` (подтверждено пользователем — старый проект, не используется, подлежит архивации, не конкурент). Полный вывод Notion из ручной подачи (сценарий 2) остаётся отдельным нерешённым вопросом на будущее, но уже не блокирует эту фазу. См. `SPEC_FRONTEND.md`.
- [ ] Порог сигнала для company_direct (REQ-107): продакт предложил "через 2 месяца замерить, если <3 лида/месяц — список не тот" — нет ещё данных для оценки.

## Cross-project context: WorkSearch umbrella

JobScraper — один из пяти суб-проектов умбрелла-системы **WorkSearch**
(`/Users/DimaKu/Documents/Coding/WorkSearch/`), которая уже связывает их через
Telegram Bot + Notion + Claude Code:

```
Telegram Bot (JobPostBot.nosync, уже на Railway)
    ├── Job URL → Notion card + prep checklist        ← пересекается с REQ-120+ (frontend)
    ├── #post / voice → LinkedIn backlog
    ├── /train ps|vacancy|english → interview training
    └── /stop → session summary → Notion Training Log

Notion (tracking layer)          ← та же Vacancies DB, что использует JobScraper
Claude Code (deep work, local)   ← product-cases/, LinkedIn posting/
```

**Почему это важно для решения "строить кастомный фронтенд" (см. REQ-out-of-scope
выше и ROADMAP.md "Крупная идея"):** `JobPostBot` уже создаёт Notion-карточки
вакансий (из вручную присланного URL) и уже задеплоен на Railway — то есть
часть инфраструктуры "фронтенда для работы с вакансиями" уже существует и
работает, просто с другой стороны процесса (ручной ввод, а не то, что находит
скрапер). `WorkSearch/STATUS.md` (2026-07-04) прямо фиксирует: *"это по сути
будущее WorkSearch — стоит свести эти две линии, когда дойдёт до технической
архитектуры"*.

**Решено (2026-07-15):** отдельный UI внутри JobScraper, не расширение
`JobPostBot`. Пользователь подтвердил, что `JobPostBot` — старый, более не
используемый проект, подлежит архивации — риска построить второй
параллельный "фронтенд для вакансий" нет, потому что первый фактически не
живёт. `tg-job-bot-1` на Railway не проверен вживую (permissions/workspace
mismatch при попытке через MCP), это не заблокировало решение. См.
`SPEC_FRONTEND.md`, `tasks.md` TASK-022.

**Также заметка из WorkSearch/STATUS.md:** WorkSearch не git-репозиторий
(история изменений не отслеживается), и его `ROADMAP.md` устарел (описывает
раннюю версию скрапера, не саму умбрелла-координацию) — не источник правды
для деталей самого скрапера, только для того, как сложены суб-проекты друг с
другом.

## Changelog
| Version | Date | Change |
|---------|------|--------|
| v1.0 | 2026-07-05 | Первичный design, сгенерирован из SPEC.md/ROADMAP.md ретроактивно |
