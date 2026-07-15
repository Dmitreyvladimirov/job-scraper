# requirements.md
> JobScraper — v1.0 — 2026-07-05
> Сгенерировано `/spec` из существующих ROADMAP.md / SPEC.md / BACKLOG.md — не с нуля.

## Overview

JobScraper — автоматизированный бот для поиска PM-вакансий (Product Manager) на
международных job boards и в Telegram-каналах. Запускается по расписанию
(cron, Railway), фильтрует нерелевантные вакансии, оценивает соответствие
резюме через GPT-4o-mini (ATS-скоринг), сохраняет результаты в Notion +
PostgreSQL и отправляет сводку в Telegram. Дополнен FastAPI-дашбордом для
аналитики по прогонам. Единственный пользователь — Dimitry (соло-проект,
инструмент для собственного поиска работы).

## Actors

- **Scraper (cron job)**: автоматический процесс, запускается 4×/день (пн–пт), опрашивает все источники, фильтрует, скорит, пишет в Notion/Postgres, шлёт Telegram-сводку.
- **Job seeker (Dimitry)**: единственный человек-пользователь. Читает Notion-карточки, вручную решает, куда податься; читает дашборд.
- **ATS scoring engine**: GPT-4o-mini, оценивает соответствие вакансии резюме (0–100).
- **Notion**: текущая write-поверхность / UI-слой (карточки вакансий).
- **PostgreSQL**: источник правды для дедупликации и аналитики (Railway managed).
- **ResumeBuilder** (внешний проект): создаёт резюме под конкретную вакансию, в перспективе интегрируется с Postgres напрямую (см. REQ-111+).

## Functional Requirements — Baseline (уже реализовано, зафиксировано как контракт)

> Эти требования уже выполнены в проде. Перечислены явно, чтобы будущие
> изменения не могли их случайно сломать без обновления этого файла.

### Источники вакансий
- **REQ-001**: Scraper shall опрашивать источники Himalayas, Remotive, Jobicy, WeWorkRemotely, RemoteOK, Arbeitnow, Jobgether (JSON-LD) и 21 Telegram-канал на каждом прогоне.
  - _Acceptance_: `scraper.py::sources_data` содержит все перечисленные источники; прогон логирует `source_counts` для каждого.

### Фильтрация
- **REQ-002**: Scraper shall отбрасывать вакансии, чей title не содержит PM-ключевые слова.
  - _Acceptance_: `passes_role_filter()` возвращает False для non-PM title в unit-тесте.
- **REQ-003**: Scraper shall пропускать только вакансии с локацией Remote worldwide / Israel / EMEA.
  - _Acceptance_: US-only вакансия отбрасывается `passes_location_filter()`.
- **REQ-004**: Scraper shall отбрасывать вакансии старше `MAX_JOB_AGE_DAYS` (14 дней по умолчанию).
  - _Acceptance_: вакансия с датой публикации > 14 дней назад не проходит `passes_date_filter()`.
- **REQ-005**: Scraper shall пропускать вакансии на английском, испанском и русском; блокировать немецкий/французский/нидерландский, кроме явного требования русского языка.
  - _Acceptance_: вакансия с маркером `(m/w/d)` блокируется; вакансия с "русский язык обязателен" проходит несмотря на немецкие стоп-слова.

### Дедупликация
- **REQ-006**: Scraper shall дедуплицировать вакансии внутри одного прогона по `normalize_job_key(company, title)`, выбирая источник с лучшим описанием (не-RemoteOK приоритетнее, затем длиннейшее описание).
  - _Acceptance_: две вакансии одной компании/роли из разных источников схлопываются в одну запись.
- **REQ-007**: Scraper shall дедуплицировать вакансии между прогонами по `seen_urls` и `seen_keys`, загруженным из Notion/Postgres при старте.
  - _Acceptance_: повторный прогон с той же вакансией не создаёт вторую Notion-карточку.
- **REQ-008**: Scraper shall помечать `seen_urls.add()` сразу после обработки вакансии независимо от успеха записи в Notion.
  - _Acceptance_: сбой записи в Notion не приводит к повторной попытке создать тот же URL в рамках одного прогона.

### ATS-скоринг
- **REQ-009**: ATS engine shall оценивать вакансию по 4 измерениям (Role Match ≤30, Domain Fit ≤30, Keyword Overlap ≤25, Location ≤15) с возможным штрафом −15, максимум 100 баллов.
  - _Acceptance_: `ats.analyze()` возвращает score в диапазоне [0, 100] с разбивкой по измерениям.
- **REQ-010**: Scraper shall создавать qualified-карточку в Notion при score ≥ 60, иначе rejected-карточку (`rejected_by_scraper`).
  - _Acceptance_: вакансия со score 55 создаётся со статусом `rejected_by_scraper`; со score 65 — со статусом `Scraped`.
- **REQ-011**: Scraper shall ограничивать количество GPT-вызовов до `MAX_GPT_CALLS_PER_RUN` (40) за прогон.
  - _Acceptance_: 41-я вакансия в прогоне не отправляется на ATS-скоринг.

### URL-обогащение
- **REQ-012**: Scraper shall искать прямую ссылку на вакансию (Greenhouse → Lever → Ashby) для источников с неполным описанием (в первую очередь RemoteOK).
  - _Acceptance_: RemoteOK-вакансия с валидным company slug получает `apply_url`, ведущий на ATS-платформу, а не на RemoteOK.
- **REQ-013**: Scraper shall валидировать совпадение компании (`_company_match()`) на Greenhouse-ветке enrichment, чтобы избежать ложных совпадений (напр. "Insider" → "Business Insider").
  - _Acceptance_: slug "insider" не матчится с компанией "Business Insider" в unit-тесте `_company_match()`.
  - _Известное ограничение_: см. REQ-101 (Lever/Ashby ветки пока не защищены — открытый баг, не удалять этот REQ при фиксе, только расширить).

### Notion
- **REQ-014**: Scraper shall создавать Notion-карточку с полями Позиция/Компания/Ссылка/ATS Score/Status2/Статус/Date Applied и callout с разбивкой ATS-скоринга.
  - _Acceptance_: созданная карточка содержит все перечисленные поля, ATS Score — число.
- **REQ-015**: Scraper shall помечать вакансию `russia_warning` (callout 🇷🇺 + поле "Тип вакансии") при обнаружении российской локации/ключевых слов.
  - _Acceptance_: вакансия с "Москва" в тексте получает `Тип вакансии = "🇷🇺 Russia"`.
- **REQ-016**: Scraper shall предупреждать (callout) о cooldown, если в Notion уже есть отклик на ту же компанию за последние 90 дней, не блокируя создание карточки.

### Telegram
- **REQ-017**: Scraper shall отправлять сводку прогона в Telegram (qualified, фильтры, топ вакансии, по источникам) после каждого прогона.
- **REQ-018**: Scraper shall алертить, если все источники вернули 0 вакансий за прогон.

### Дашборд
- **REQ-019**: Dashboard shall показывать KPI (прогоны, qualified, GPT-вызовы, total fetched), графики (qualified/fetched по дням, воронка фильтров, ATS distribution, source performance) и таблицу топ-компаний, защищённые токеном.
  - _Acceptance_: `/` без `?token=` возвращает 401/403; с валидным токеном — 200 и данные из Postgres.

## Functional Requirements — Planned (v1.1, приоритизировано по BACKLOG.md / SPEC.md v2)

### Баги (высокий приоритет, независимы от новых фич)
- **REQ-101**: `find_apply_url()` shall применять `_company_match()` на Lever и Ashby ветках, не только на Greenhouse.
  - _Acceptance_: slug с неоднозначным именем компании на Lever/Ashby не даёт `apply_url` на вакансию другой компании (regression-тест на реальном кейсе).
  - _Источник_: BACKLOG.md `[BUG] _company_match() не применяется к Lever и Ashby`.
- **REQ-102**: `send_run_summary()` shall не отправлять Telegram-сообщение, если `qualified == 0`; `send_error()` отправляется всегда.
  - _Acceptance_: прогон с 0 qualified не создаёт сообщение в Telegram; прогон с ошибкой создаёт `send_error()` независимо от qualified.
  - _Источник_: BACKLOG.md `[READY] Telegram: не слать отчёт при каждом прогоне`.
- **REQ-103**: GitHub Actions `workflow_dispatch` shall передавать `DATABASE_URL` в шаг "Run scraper".
  - _Acceptance_: ручной запуск через `gh workflow run` завершается без `EnvironmentError` в `db.init_db()`.
  - _Источник_: SPEC.md v2.4.
- **REQ-121**: `fetch_url_generic()` (`core/utils.py`) shall валидировать хост перед запросом — блокировать loopback/link-local (169.254.0.0/16)/RFC1918-private/ULA/0.0.0.0 адреса и нестандартные схемы, включая проверку на каждом редиректе.
  - _Acceptance_: вызов на URL, резолвящийся в приватный/link-local IP (напр. `169.254.169.254`), не создаёт исходящий запрос; та же проверка применяется к `fetch_jd_from_url()` на нефиксированных хостах.
  - _Источник_: автоматическая security-проверка 2026-07-15 (SSRF, HIGH) — URL приходят из спарсенных вакансий (Telegram-каналы, джоб-борды), потенциально из недоверенного источника.
- **REQ-122**: `_check_token()` (`core/dashboard.py`) shall отклонять запросы, если `DASHBOARD_TOKEN` не задан, вместо fail-open поведения; сравнение токена shall использовать `hmac.compare_digest`.
  - _Acceptance_: при незаданном `DASHBOARD_TOKEN` дашборд возвращает ошибку конфигурации (не 200) на всех защищённых эндпоинтах; таймингово-безопасное сравнение используется для валидного токена.
  - _Источник_: автоматическая security-проверка 2026-07-15 (Authentication Bypass, MEDIUM) — пустой `DASHBOARD_TOKEN` сейчас открывает дашборд для всех без предупреждения.

### Telegram-парсинг (высокий приоритет)
- **REQ-104**: `_extract_title_company()` shall извлекать компанию из multi-bullet сообщений (канал `forproducts`), где компания указана в отдельной строке ниже списка ролей.
  - _Acceptance_: тестовое сообщение канала `forproducts` даёт непустое поле `company` для каждой из перечисленных ролей.
  - _Источник_: SPEC.md v2.2, BACKLOG.md.
- **REQ-105**: `_extract_title_company()` shall распознавать явно лейблированный формат `🏢 Company: X` (канал `remotejobss`) и брать title со строки роли, а не с generic-заголовка первой строки.
  - _Acceptance_: тестовое сообщение канала `remotejobss` даёт title = реальное название роли (не "JOB OPPORTUNITY") и company = значение после "Company:".
  - _Источник_: SPEC.md v2.2, BACKLOG.md.
- **REQ-106**: `_extract_title_company()` shall отделять суффикс "(Компания)" от title, когда компания встроена в скобки (`smartremotejobs`, `productjobgo`).
  - _Acceptance_: title "Senior PM (CyberNut)" даёт title="Senior PM", company="CyberNut".
  - _Приоритет_: ниже REQ-104/105.
- **REQ-120**: `_expand_listing_page()`'s listing-URL selection shall исключать `linkedin.com` (и остальные домены из `_SKIP_URL_PATTERNS`), не только `_is_listing_page()`-проверку пути.
  - _Acceptance_: сообщение, где единственная listing-подобная ссылка — на `linkedin.com`, не вызывает `_expand_listing_page()` и не даёт LinkedIn auth-wall текст в `company`/`description`.
  - _Источник_: найдено 2026-07-15 через скриншот дашборда — 69 записей с пустым `company` + 27 записей с текстом LinkedIn cookie-баннера вместо названия компании в "Top companies found". Root cause: `telegram_channels.py:386-387`, `listing_url = next(...)` проверяет только `_is_listing_page(url)`, не `_SKIP_URL_PATTERNS`.

### Company-direct источник (высокий приоритет, крупнейшая фича v1.1)
- **REQ-107**: Scraper shall опрашивать список целевых компаний (~80, EdTech/LMS + израильские) напрямую через Greenhouse/Lever/Ashby/join.com API, в три слоя по частоте (Слой 1: 4×/день, Слой 2: 1×/день, Слой 3: fallback/не строится отдельно).
  - _Acceptance_: компания из Слоя 1 с активным ATS даёт вакансию в `sources/company_direct.py::fetch()`, проходящую через существующий pipeline (dedup/фильтры/ATS/Notion) без изменений в этих модулях.
  - _Источник_: ROADMAP.md "Высокий приоритет #1", SPEC.md v2.3.
- **REQ-108**: Postgres shall хранить список целевых компаний в таблице `target_companies(slug, name, ats, status, source, added_at, last_verified_date)` со статусами active/pending/dormant.
  - _Acceptance_: таблица существует, засеяна из курированного списка; `status=pending` компании не опрашиваются до ручной активации.
- **REQ-109**: Вакансия с `target_company_match=True` (Слой 1) и score ≥ 60 shall создавать Notion-карточку немедленно (не ждать батч 4×/день) и отправлять отдельное instant Telegram-сообщение.
  - _Acceptance_: находка Слоя 1 появляется в Notion и Telegram в течение одного цикла проверки (не следующего батч-прогона).
- **REQ-110**: Weekly discovery job shall писать кандидатов в `target_companies` со статусом `pending`; активация — только вручную (review queue).
  - _Acceptance_: ни один кандидат не переходит в `active` без явного ручного действия.

### ResumeBuilder ↔ Postgres синхронизация (средний приоритет)
- **REQ-111**: `db_manual.py` shall писать запись о ручной подаче (через ResumeBuilder) в Postgres `jobs` с `source_type='manual'` и `resume_version`, сразу после создания Notion-карточки.
  - _Acceptance_: ручная подача через ResumeBuilder создаёт строку в Postgres `jobs` с непустым `resume_version`.
  - _Источник_: ROADMAP.md "Высокий приоритет #2", SPEC.md v2.5.
- **REQ-112**: `sync_notion.py` shall односторонне читать статусы из Notion (`last_edited_time >= last_sync_at`) и писать в `status_log`; Postgres shall никогда не писать обратно в Notion.
  - _Acceptance_: смена статуса Applied→Screen в Notion отражается в `status_log` после следующего sync-цикла; ни один Notion-объект не изменяется кодом синка.
- **REQ-113**: Перед созданием Notion-карточки в ResumeBuilder Step 2.5, код shall проверять существование записи по URL/company+title и делать UPDATE вместо INSERT при совпадении (reapplication guard против `position_closed`).
  - _Acceptance_: повторная попытка подать резюме на вакансию со статусом `position_closed` в Postgres не создаёт новую Notion-карточку.

### Прочее (средний/низкий приоритет)
- **REQ-114**: WeWorkRemotely и Arbeitnow shall получать полный JD через `fetch_jd_from_url()` (сейчас только RemoteOK).
- **REQ-115**: Notion DB shall иметь отдельное select-свойство `Source`, заполняемое из `job["source"]`.
- **REQ-116**: Notion shall предоставлять select-поле с 4 категориями причины отклонения (плохой скоринг / не глобально-удалённая / неактивна-закрыта / плохая в принципе), заполняемое вручную.
- **REQ-117**: Dashboard shall показывать Telegram-каналы отдельной таблицей (сообщений спарсено → прошло role-фильтр → qualified) с per-channel счётчиками из `telegram_channels.fetch()`.
- **REQ-118**: Scraper shall опционально поддерживать приватные (invite-only) Telegram-каналы через telethon Client API (`TELEGRAM_API_ID`/`TELEGRAM_API_HASH`), не как расширение HTML-скрапера.
  - _Не начинать без явного решения_ (см. Open Questions в design.md) — известен 1 кандидат-канал.
- **REQ-119**: Scraper shall опционально отправлять еженедельный дайджест по пятницам (топ-5 за неделю + сравнение с прошлой) вместо/вместе с ежедневными сообщениями.

## Non-Functional Requirements

- **NFR-001**: ATS-скоринг shall укладываться в лимит 40 GPT-вызовов за прогон (защита от перерасхода).
  - _Measurement_: счётчик `MAX_GPT_CALLS_PER_RUN` в `config.py`, проверяемый в `ats.py`.
- **NFR-002**: Дедупликация shall быть консистентной между прогонами — 0 дублирующих Notion-карточек по одному URL за 30 дней наблюдения.
  - _Measurement_: SQL-запрос по Postgres `jobs`, группировка по нормализованному URL, count > 1 = нарушение.
- **NFR-003**: Postgres shall оставаться единственным источником правды для read-side аналитики; Notion — только UI/write-поверхность для ручных статусов.
  - _Measurement_: код синка (`sync_notion.py`) не содержит вызовов Notion API на запись.
- **NFR-004**: Скрапер shall укладываться в окно одного прогона (между двумя cron-тиками, т.е. < 3 часа) при нормальной нагрузке источников.
  - _Measurement_: `runs.duration_seconds` в Postgres, alert если > 3600с.

## Out of Scope (v1.1)

- Two-way sync между Postgres и Notion (Postgres только читает).
- Полный вывод Notion из процесса ручной подачи (сценарий 2 из ROADMAP "Крупная идея") — отдельное решение позже.
- Continuous auto-discovery новых компаний (только ручной quarterly review).
- Парсинг JS-SPA карьерных страниц без headless-браузера (Playwright) — remoteworldwide.net и подобные явно исключены, покрываются через их Telegram-канал.
- Синхронизация email-переписки с рекрутерами.
- 100%-покрытие компаний через ATS (реалистичная цель ~53%).

## Changelog
| Version | Date | Change |
|---------|------|--------|
| v1.0 | 2026-07-05 | Первичный spec, сгенерирован из ROADMAP.md/SPEC.md/BACKLOG.md ретроактивно |
