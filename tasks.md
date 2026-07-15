# tasks.md
> JobScraper — v1.0 — 2026-07-05
> Сгенерировано `/spec` из существующих ROADMAP.md / BACKLOG.md.

## Legend
- [ ] Not started
- [~] In progress
- [x] Complete
- [!] Blocked — reason noted inline

---

## Phase 1: Баги (независимы от новых фич, чинить в первую очередь)
*Goal*: закрыть известные открытые баги, которые касаются уже работающей в проде логики.

- [ ] **TASK-001** [REQ-101]: добавить `_company_match()` на Lever и Ashby ветки в `utils.py::find_apply_url()`.
  - _Output_: обе ветки отклоняют slug при несовпадении имени компании.
  - _Verify_: regression-тест на реальном неоднозначном slug (напр. общее слово), `apply_url` не ведёт на вакансию другой компании.

- [x] **TASK-002** [REQ-102]: убрать ветку `if qualified == 0` в `telegram.py::send_run_summary()` — просто `return` без отправки.
  - _Output_: прогон с 0 qualified не создаёт сообщение; `send_error()` не тронут.
  - _Verify_: локальный прогон с моком 0 qualified — Telegram API не вызывается.

- [ ] **TASK-003** [REQ-103]: передать `DATABASE_URL` в шаг "Run scraper" в `.github/workflows/scraper.yml`.
  - _Output_: секрет добавлен в workflow env.
  - _Verify_: `gh workflow run` завершается без `EnvironmentError` в `db.init_db()`.

- [ ] **TASK-024** [REQ-121]: SSRF-защита в `fetch_url_generic()` (`core/utils.py:218`) — резолвить хост через `socket.getaddrinfo`, блокировать loopback/link-local/RFC1918/ULA/`0.0.0.0`, ограничить схему `http`/`https`, `allow_redirects=False` + ре-валидация `Location` на каждом хопе. Тот же guard — на нефиксированные хосты в `fetch_jd_from_url()`.
  - _Output_: helper-функция валидации хоста, применённая в обеих функциях.
  - _Verify_: запрос на URL, резолвящийся в `169.254.169.254` (или другой приватный/link-local IP), не создаёт исходящий HTTP-запрос — тест с моком DNS-резолва.
  - _Источник_: security review 2026-07-15, HIGH.

- [ ] **TASK-025** [REQ-122]: убрать fail-open в `_check_token()` (`core/dashboard.py:23-24`) — при незаданном `DASHBOARD_TOKEN` отклонять запросы (500/503, не пропускать как сейчас), сравнение токена через `hmac.compare_digest`.
  - _Output_: `_check_token()` требует непустой `TOKEN`; `validate_secrets()`/startup явно проверяет `DASHBOARD_TOKEN`.
  - _Verify_: с пустым `DASHBOARD_TOKEN` защищённый эндпоинт возвращает ошибку конфигурации, а не 200; с валидным токеном сравнение идёт через `hmac.compare_digest`.
  - _Источник_: security review 2026-07-15, MEDIUM.

---

## Phase 2: Telegram-парсинг (высокий приоритет — реальная потеря лидов)
*Goal*: удвоить полезный выход Telegram-источников, чиня конкретные каналы, найденные аудитом 2026-07-02.

- [ ] **TASK-004** [REQ-104]: в `_extract_title_company()` (`sources/telegram_channels.py`) добавить извлечение компании из multi-bullet формата (канал `forproducts`) — компания на отдельной строке ниже списка ролей.
  - _Output_: `forproducts` даёт непустую компанию для каждой роли в списке.
  - _Verify_: прогнать `_fetch_channel('forproducts', ...)` на реальных сохранённых сообщениях, company не пустая.

- [ ] **TASK-005** [REQ-105]: распознать лейблированный формат `🏢 Company: X` (канал `remotejobss`) — title со строки роли (не первой generic-строки), company после "Company:".
  - _Output_: `remotejobss` даёт осмысленный title (не "JOB OPPORTUNITY") и корректную компанию.
  - _Verify_: прогнать `_fetch_channel('remotejobss', ...)`, title/company совпадают с реальным текстом сообщения.

- [ ] **TASK-006** [REQ-106]: отделять суффикс "(Компания)" от title (`smartremotejobs`, `productjobgo`).
  - _Output_: title "Senior PM (CyberNut)" → title="Senior PM", company="CyberNut".
  - _Verify_: unit-тест на строке с суффиксом в скобках.

- [ ] **TASK-023** [REQ-120]: `_expand_listing_page()` — в `_fetch_channel()` (`sources/telegram_channels.py:386-387`), при выборе `listing_url` фильтровать по `_SKIP_URL_PATTERNS`, не только `_is_listing_page()`.
  - _Output_: `listing_url = next((url for url, _ in msg["links"] if _is_listing_page(url) and "t.me/" not in url and not any(p in url.lower() for p in _SKIP_URL_PATTERNS)), None)` (или эквивалент).
  - _Verify_: regression-тест на сообщении, где единственная listing-ссылка — `linkedin.com/company/x/jobs/`: `_expand_listing_page()` не вызывается, `company`/`description` не содержат текста LinkedIn auth-wall.
  - _Доп. проверка_: пройтись по уже существующим Postgres-записям с `company IS NULL` или `company ILIKE '%linkedin%'` — оценить масштаб уже накопленного мусора (69+27 на момент находки 2026-07-15), решить отдельно, чистить ли задним числом.

---

## Phase 3: Company-direct источник (высокий приоритет, крупнейшая фича)
*Goal*: получать вакансии от целевых компаний (EdTech/LMS + израильские) напрямую через ATS API, в обход агрегаторов.

- [ ] **TASK-007** [REQ-107]: рефакторинг ATS-блоков в `utils.py` в переиспользуемые `list_greenhouse(slug)` / `list_lever(slug)` / `list_ashby(slug)`, возвращающие все постинги компании.
  - _Output_: три новые функции, покрывающие все посты (не только enrichment одной вакансии).
  - _Verify_: вызов `list_greenhouse('known-slug')` возвращает список вакансий компании.

- [ ] **TASK-008** [REQ-108]: создать таблицу `target_companies` в Postgres + seed из курированного списка (~80 компаний, EdTech/LMS + израильские, после чистки дублей/мёртвых компаний — см. design.md).
  - _Output_: таблица создана, Слой 1 (~33 израильские + 8 глобальных) сразу `active`, остальные `pending`.
  - _Verify_: SQL `SELECT COUNT(*) FROM target_companies WHERE status='active'` соответствует ожидаемому размеру Слоя 1.

- [ ] **TASK-009** [REQ-107]: новый `sources/company_direct.py` с `fetch()`, читающий `active`-компании из `target_companies`, добавлен в `sources_data` в `scraper.py`.
  - _Output_: источник работает наравне с остальными — dedup/фильтры/ATS/Notion без изменений в этих модулях.
  - _Verify_: end-to-end прогон с одной тестовой active-компанией создаёт Notion-карточку через штатный pipeline.

- [ ] **TASK-010** [REQ-109]: instant-путь — вакансия с `target_company_match=True` и score ≥ 60 создаёт карточку и Telegram-алерт немедленно, не дожидаясь батч-прогона.
  - _Output_: отдельная функция вне обычного батч-цикла, вызываемая сразу после скоринга Слоя-1 вакансии.
  - _Verify_: находка Слоя 1 приходит в Telegram/Notion в течение текущего цикла проверки.

- [ ] **TASK-011** [REQ-110]: weekly discovery job — пишет кандидатов в `target_companies` со статусом `pending`, без авто-активации.
  - _Output_: cron/job, отдельный от основного 4×/день скрапера.
  - _Verify_: после запуска job новые записи имеют `status='pending'`, ни одна не активна автоматически.

*Через 2 недели после TASK-009/010 — замерить сигнал (false positive/negative, скорость alert → подача), затем решить о расширении на Слой 2. Явно не делать: continuous auto-discovery, Playwright для JS-SPA на этом этапе.*

---

## Phase 4: ResumeBuilder ↔ Postgres синхронизация (средний приоритет)
*Goal*: устранить дублирование логики создания Notion-карточки между JobScraper и ResumeBuilder, дать сквозную статистику по ручным подачам.

- [ ] **TASK-012** [REQ-111]: схема — добавить в `jobs`: `notion_id`, `current_status`, `source_type`, `resume_version`, `deleted_at`; `run_id` → nullable. Новая таблица `status_log` + индекс. Новая `sync_meta`.
  - _Output_: миграция применена, `db.py` JOIN'ы к `runs` переведены на LEFT JOIN.
  - _Verify_: миграция идемпотентна (повторный запуск не падает), существующие запросы дашборда не ломаются.

- [ ] **TASK-013** [REQ-111]: `db_manual.py` — INSERT ручной подачи + `resume_version`, вызывается из ResumeBuilder Step 2.5 сразу после создания Notion-карточки (fire-and-forget, не блокирует PDF).
  - _Output_: новый файл `db_manual.py`, вызов добавлен в ResumeBuilder CLAUDE.md workflow.
  - _Verify_: ручная подача создаёт строку в Postgres с `source_type='manual'` и непустым `resume_version`.

- [ ] **TASK-014** [REQ-112]: `sync_notion.py` — polling `last_edited_time >= last_sync_at` (частота = частота cron скрапера), обновляет `current_status`, пишет в `status_log`. Никогда не пишет в Notion.
  - _Output_: новый файл, читает/обновляет `sync_meta.last_sync_at`.
  - _Verify_: смена статуса в Notion отражается в `status_log` после следующего запуска синка; код синка не содержит Notion write-вызовов.

- [ ] **TASK-015** [REQ-113]: reapplication guard — перед созданием карточки в ResumeBuilder Step 2.5, SELECT по company+normalized title на `status='position_closed'`; при совпадении — UPDATE вместо INSERT.
  - _Output_: guard-проверка в `db_manual.py`, нормализация URL для матчинга (переиспользовать/расширить `normalize_job_key()`).
  - _Verify_: повторная подача на `position_closed`-вакансию не создаёт вторую Notion-карточку.

*Pre-ship checklist (из ROADMAP.md): идемпотентность upsert, sync failure не блокирует PDF, 3+ реальных статус-перехода end-to-end проверены вручную, Notion "Status" property существует.*

---

## Phase 5: Средний/низкий приоритет — по мере наличия времени
*Goal*: точечные улучшения, не блокирующие остальной roadmap.

- [ ] **TASK-016** [REQ-114]: `fetch_jd_from_url()` для WeWorkRemotely и Arbeitnow (сейчас только RemoteOK).
  - _Verify_: вакансия с WWR/Arbeitnow имеет полный `description`, не короткий RSS-сниппет.

- [ ] **TASK-017** [REQ-115]: добавить select-свойство `Source` в Notion DB (ручной шаг в Notion + код в `_make_properties`).
  - _Verify_: новая карточка имеет заполненное поле `Source`.

- [ ] **TASK-018** [REQ-116]: select-поле с 4 категориями причины отклонения в Notion (ручной шаг + документирование категорий в config.py-комментарии).
  - _Verify_: поле существует в Notion DB, значения соответствуют 4 категориям из design.md.

- [ ] **TASK-019** [REQ-117]: `telegram_channels.fetch()` возвращает per-channel счётчики; отдельная таблица/секция в `dashboard.py` (канал → спарсено → прошло role-фильтр → qualified).
  - _Verify_: дашборд показывает разбивку минимум по 3 каналам с ненулевыми числами на каждом этапе воронки.

- [ ] **TASK-020** [REQ-118] _(не начинать без явного решения — см. Open Questions в design.md)_: telethon-клиент для приватных Telegram-каналов.

- [ ] **TASK-021** [REQ-119] _(опционально)_: еженедельный дайджест по пятницам вместо/вместе с ежедневными сообщениями.

---

## Phase 6: Кастомный фронтенд (разблокировано 2026-07-15 — осознанное решение)
*Goal*: реализовать rollout-решение из ROADMAP.md "Крупная идея" (сценарии 1+3:
ручной пересмотр карточек скрапера, канбан по статусам, причины отклонения,
статистика) — намеренно отдельно от WorkSearch/JobPostBot.

- [x] **TASK-022**: свести архитектуру "кастомный фронтенд для JobScraper" и
  существующий `WorkSearch/JobPostBot.nosync` в одно решение.
  - _Решение (2026-07-15)_: **не объединять**. Подтверждено пользователем
    2026-07-15: `JobPostBot` — старый проект, больше не используется, подлежит
    архивации (не активный конкурент новому фронтенду). Новый фронтенд —
    пересмотр находок скрапера + канбан по статусам + причины отклонения +
    статистика (сценарии 1+3), полностью независимо от `JobPostBot`.
  - _Output_: см. `frontend-spec/FRONTEND_DESIGN_BRIEF.md`, `frontend-spec/SPEC_FRONTEND.md`.
  - _Follow-up (не сделано)_: `tg-job-bot-1` на Railway — предположительно
    JobPostBot, не подтверждено (permissions/workspace mismatch при попытке
    проверить через MCP 2026-07-15). Архивация/остановка сервиса — отдельное
    действие, ждёт явного запроса пользователя.

---

## Completed Tasks Archive

*Перенесено из ROADMAP.md "Сделано" — уже в проде, соответствует REQ-001…REQ-019 в requirements.md.*

- [x] Railway deployment: cron job пн–пт 4×/день, `run.sh`, `railway.toml`
- [x] Postgres (Railway addon) как единая БД для скрапера и дашборда
- [x] Postgres как источник правды для дедупа (`load_seen_jobs()`)
- [x] Два сервиса в одном Railway-проекте: `scraper` + `dashboard`
- [x] Источники: Himalayas, Remotive, Jobicy, WeWorkRemotely, RemoteOK, Arbeitnow, Jobgether, Telegram×21
- [x] Фильтры: роль, локация, дата (MAX_JOB_AGE_DAYS=14), язык (EN/ES/RU, блок DE/FR/NL)
- [x] Дедупликация: cross-source (внутри прогона) + между прогонами (seen_urls/seen_keys)
- [x] ATS-скоринг: GPT-4o-mini, 4 измерения, калибровка с ResumeBuilder
- [x] URL-обогащение: Greenhouse/Lever/Ashby, `_company_match()` на Greenhouse-ветке, `enrich_existing.py`
- [x] Notion: qualified/rejected карточки, ATS Score поле, callout breakdown, cooldown-предупреждение, russia_warning
- [x] Telegram: сводка после прогона, алерт на 0 вакансий по всем источникам
- [x] Дашборд: FastAPI+Chart.js, KPI, графики, топ-компании, токен-защита
- [x] LinkedIn исключён из fetch/apply-URL (auth-wall баг)
- [x] Telegram-парсинг: `<br>`→`\n` + `get_text()` без разделителя (был баг с разбиением "Title в **Company**")
- [x] Regex бэкфилла компании сужен до явных лейблов ("company"/"компания"/"работодатель")

## Changelog
| Version | Date | Change |
|---------|------|--------|
| v1.0 | 2026-07-05 | Первичный tasks.md, сгенерирован из ROADMAP.md/BACKLOG.md ретроактивно |
