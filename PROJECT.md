# JobScraper + ResumeBuilder Cloud - единый проект

Jobs-пайплайн: скрапинг -> скоринг -> трекинг -> генерация резюме.

Черновик консолидации (2026-08-17). Собран из ROADMAP.md, BACKLOG.md, tasks.md,
requirements.md, SPEC.md, CONTEXT.md, SOURCES_DECISION.md, Design/*. Решение об
архивации исходников - за Дмитрием.

---

## Как устроено сейчас

Два репозитория, один Railway-проект ("Job scrapper NEW"), одна общая Postgres.

| Компонент | Где лежит | Railway-сервис | Что делает |
|---|---|---|---|
| Скрапер | `JobScraper/core/` | Scrapper itself (cron `7 6,9,12,15 * * 1-5`) | Опрашивает источники, фильтрует, скорит, пишет в Postgres + Notion, шлёт Telegram-сводку |
| Дашборд / фронтенд | `JobScraper/core/dashboard.py` + `core/templates/` | Dashboard (job-scraper) | Review, Kanban, Tracker, Add-job, очередь почты, статистика, Sources panel. FastAPI + Jinja2 + HTMX, cookie-сессия |
| Внешний intake | `JobScraper/core/intake.py`, `core/tg_bot.py` | Dashboard (job-scraper) | Ручная отправка вакансии: `POST /api/intake` (bearer) и Telegram-бот (`POST /tg/{secret}`). Ссылка или текст → карточка → скоринг; резюме — по явной команде |
| Почтовый агент | `JobScraper/core/mail_agent.py`, `core/gmail_client.py` | Mail agent (без крона) | Читает jobhunt-почту, предлагает смену статуса карточки. Сервис создан 23.08, спит до прохождения Gmail OAuth |
| Scoring service | `resumebuilder-cloud/services/scoring/` | scoring | `POST /v1/score`, `/v1/score/batch`. Claude Haiku 4.5, та же 4-осевая рубрика |
| Cards service | `resumebuilder-cloud/services/cards/` | cards | `POST /v1/cards` - Notion-карточка трекинга |
| Resume service | `resumebuilder-cloud/services/resume/` | resume | `POST /v1/resume/generate`, `GET /v1/resume/runs/{id}/pdf`. 3 стадии: домен -> буллеты из банка -> сборка + Skeptic + PDF |
| Postgres | Railway addon | Postgres | Единая БД: `runs`, `jobs`, `status_log`, `sources_config`, `resume.*`, `scoring.score_run`, `pipeline.event` |
| Postgres-hQr0 | Railway addon | - | Пустая точка отката после сведения баз 2026-08-14. Удаляется в Фазе 5 |

Схема данных сведена 2026-08-14: три облачных сервиса переключены на Postgres
скрапера, миграции ведутся через `pipeline.schema_migrations` (`db/bootstrap.py`,
`preDeployCommand` сервиса resume).

Скоринг: с 2026-08-17 решает облачный `core/scoring_client.py`
(`USE_CLOUD_SCORING=1` на Scrapper itself, `ATS_THRESHOLD=70`). Локальный
`core/ats.py` остаётся в дереве как путь отката до Фазы 5. Основание катовера:
3 дня shadow (70 пар, 6 прогонов) — 0 ошибок, 0 флипов вверх, cloud 80%
совпадения с ручной разметкой Дмитрия против 44% у local.

Notion: скрапер продолжает писать карточки односторонне, но Дмитрий как рабочей
поверхностью им больше не пользуется - весь просмотр и движение по воронке идёт
через дашборд. Отключение записи запланировано в Фазе 5.

Источники: активны Jobgether, Jobicy, Choicy (choicy.work, добавлен 2026-08-17 — открытый Bubble API) и Telegram-каналы. Пять агрегаторов
(Himalayas, Remotive, RemoteOK, Arbeitnow, WeWorkRemotely) отключены по
`SOURCES_DECISION.md` - код пока не удалён.

---

## Активный трек

Из `CONTEXT.md` (Resume block, обновлён 2026-08-17). Это единственный живой
рабочий поток; всё остальное в роадмапе ждёт его.

### 1. Катовер скоринга — СДЕЛАН 2026-08-17

Субботняя рутина 15.08 упала (Railway OAuth MCP не отдаёт значения переменных —
до БД не дошла), отчёт посчитан локально 17.08: 70 shadow-пар за 14–17.08,
0 ошибок, 46 flip down / 0 flip up, все cloud-pass входят в local-pass.
`USE_CLOUD_SCORING=1` выставлен на Scrapper itself, `ATS_THRESHOLD` 60 -> 70
запушен. Проверено вживую: прогон 197 — 18 оценок cloud, 0 ошибок, 17 отклонено,
1 qualified (PhotoRoom 85). Субботняя
рутина shadow-отчёта устарела — кандидат на удаление (решение Дмитрия).

### 2. Релиз 1 резюме-фич — СДЕЛАН и ПРОВЕРЕН ВЖИВУЮ 2026-08-17

Реализовано по плану, 85 тестов зелёные (24 новых):
- `core/resume_client.py` - паттерн `scoring_client.py`, но `retries=1`
  (ретрай генерации платный).
- Колонка `jobs.resume_run_id` (применена в БД + идемпотентно в `init_db`).
- Кнопка Generate resume в peek-модалке (`hx-disabled-elt` + `htmx-indicator`;
  повторный клик открывает уже готовое, вторая генерация не оплачивается).
- `GET /jobs/{id}/resume-pdf` - прямым SELECT из `resume.artifact`, inline PDF.
- Гейты: `company` обязательна (422), JD короче 200 символов - блок; в UI
  кнопка задизейблена с причиной в title.
- Автоскоринг ручных карточек: daemon-поток после Add-job, тост не ждёт.
- Кнопка Re-score в дубль-диалоге (+ показ текущего ATS старой карточки).
- Счётчик Skeptic-флагов на ссылке Open resume (из `resume.generation_run`).

Порог автогенерации решён Дмитрием 2026-08-17: `>=80`.
Env: на Dashboard-сервис добавлены `SCORING_TOKEN`/`RESUME_TOKEN`
(Railway-референсы на сервисы Scoring/Resume, применились с деплоем).

### 3. Релиз 2 - автогенерация резюме - СДЕЛАН 2026-08-19

Батч в конце прогона скрапера (`scraper.autogen_resumes`), гейты по плану:
cloud-скор >=80 AND `location_score > 0` AND `len(description) >= 500` AND
company AND `resume_run_id IS NULL`, кап 5/прогон. Прошёл QA-ревью (qa-expert):
цикл полностью защищён (сбой БД после платной генерации не роняет прогон),
батч идёт ПОСЛЕ основной телеграм-сводки, результаты — отдельным
Telegram-сообщением (переживает early-return сводки при 0 qualified).
Отложено осознанно: микро-гонка с ручной кнопкой Generate (~$0.005).

Стоимость генерации $0.003 (замер по 17 прогонам) - бюджет не фактор, гейты
нужны для качества.

### 4. Фаза 4 - фиксы входных данных скоринга - СДЕЛАНА 2026-08-19

- ~~Подмена Jobgether-JD по прямой ATS-ссылке~~ НЕ ПОНАДОБИЛАСЬ: данные показали,
  что JSON-LD уже отдаёт полный текст (311/312 JD, средняя длина 6029 символов,
  0 boilerplate-only) - ядовитым был только вводный `<p>`-абзац "offer available
  from". Он вырезается по границе тега (`jobgether.clean_description`); чистая
  локация и так парсится из applicantLocationRequirements.
- Языковой чек: добавлены португальский и узбекский стоп-листы + word-regex стал
  accent-aware (старый `\b[a-z]+\b` молча выбрасывал все акцентованные слова -
  потому португальский и проходил). Испанский по-прежнему разрешён.
- Единый лимит обрезки: `config.DESCRIPTION_MAX_CHARS = 8000` (использует db.log_job;
  облачный скорер получает полный текст ДО обрезки).
- Инженерные вакансии под PM-тайтлом - ОТЛОЖЕНО осознанно: после катовера их
  отсеивает role-ось облачного скорера (порог 70); keyword-фильтр добавил бы
  риск ложных отсевов без измеримой выгоды. Вернуться, если в очереди появятся
  инженерные карточки с 70+.

### 5. Фаза 5 - зачистка (~2026-08-28, после 2 недель стабильности)

Удалить `core/ats.py`, `OPENAI_API_KEY` из `validate_secrets`, запись `RESUME_MD`
в `run.sh`, Postgres-hQr0. Решить про отключение записи скрапера в Notion.

### 6. Почтовый агент - КОД ГОТОВ, НЕ РАЗВЁРНУТ (2026-08-22)

Три коммита в main (`f435879`, `7f51248`, `1bdd2b0`): таксономия из 9 классов
(выведена разметкой ~80 реальных писем, а не угадана - гипотеза про 4 категории
не подтвердилась), `core/mail_agent.py` + `core/gmail_client.py`, ветка `mail`
в `run.sh`, очередь ревью `/mail` в дашборде, `scripts/mint_gmail_token.py`.
Безопасность: LLM возвращает метку из закрытого множества, не статус; маппинг
метка->переход - обычный Python; `MAIL_AUTO_APPLY = False`, всё через
подтверждение. Это снимает противоречие с правилом "Never auto-transition" из
`SPEC_FRONTEND.md` - авто-перехода нет.

Сделано 2026-08-23: создан сервис «Mail agent» (`SERVICE_TYPE=mail`, restart
policy NEVER, крона пока нет), `ANTHROPIC_API_KEY`/`DATABASE_URL`/Telegram
выставлены референсами; первый запуск прислал в Telegram «нет Google-креденшла» -
то есть сборка, БД и Telegram у сервиса работают. Таблицы `mail_event` и
`google_credential` в проде проверены и существуют.

Осталось (требует браузера, за Дмитрием):
- OAuth-приложение в Google Cloud Console: Gmail API, consent screen External и
  **опубликованный** (в Testing refresh-токен умирает через 7 дней и это выглядит
  как «мне никто не писал»), клиент типа Desktop app
- `python scripts/mint_gmail_token.py` локально -> строка в `google_credential`;
  напечатанный `GMAIL_TOKEN_KEY` + client id/secret выставить на сервисе
- ярлык `jobhunt` в Gmail с фильтром (`MAIL_GMAIL_QUERY` по умолчанию)
- после этого выставить сервису `cron_schedule` (7 дней в неделю)

### 7. Внешний intake - СДЕЛАН 2026-08-23

Ручная отправка вакансии "куда угодно с телефона" - решения Дмитрия: транспорта
два сразу, резюме только по явной команде, вход любой (ATS-ссылка, агрегатор,
LinkedIn, текст).

- `core/intake.py` - единственное место, где живёт логика: фетч (ATS API ->
  обычный HTML -> ScrapingBee), извлечение title/company/location/salary через
  Haiku, дедуп, карточка, скоринг. Оба транспорта - тонкие.
- `core/tg_bot.py` + `POST /tg/{secret}` - бот принимает ссылку или текст,
  отвечает скором по осям и кнопками "Сделать резюме" / "Перескорить", присылает
  PDF файлом (не ссылкой - дашборд за логином).
- `POST /api/intake` (bearer `DASHBOARD_TOKEN`) - тот же пайплайн для iOS
  Shortcut и любых внешних вызовов; `POST /api/jobs/{id}/resume` - явная команда
  на генерацию.
- Гейт качества: JD короче `INTAKE_MIN_JD_CHARS = 400` не скорится вообще -
  LinkedIn отдаёт логин-стену, SPA агрегатора отдаёт пустую оболочку, и то и
  другое выглядит как "описание на 200 символов"; это ровно та инфляция коротких
  JD, из-за которой сняли локальный скорер. Вместо оценки - просьба прислать
  текст, ссылка запоминается на 30 минут и привязывается к карточке.
- Резюме автоматически не генерится (решение Дмитрия 2026-08-23): ручная
  отправка смещена в сторону вакансий, которые и так нравятся, - автогенерация
  тратила бы на каждой.
- Тесты: 25 новых в `tests/test_intake.py` + 10 маршрутных, вся сюита 218 зелёных.

Артефакты трека: `db-backups/` в `/Users/DimaKu/Documents/Coding/db-backups`
(дамп `jobscraper_pg_20260814_0320.dump` 50MB, `scoring_baseline_2026-08-14.md`,
`labels_dimitry_2026-08-14.json`, `label_comparison_2026-08-14.md`,
`rescore_labeled_2026-08-14.json`, `unrequested_addjob_ui.patch`).

---

## Роадмап

Только нереализованное и не отменённое.

### Источники

- ~~**NULL/empty source - расследование.**~~ ЗАКРЫТО 2026-08-19: все 54 строки -
  разовый июньский импорт старого Notion-трекера (прогон #8), не живая утечка.
  Бэкфилл source='NotionImport', import_notion_csv.py починен. Процентам по
  источникам в SOURCES_DECISION.md теперь можно доверять без оговорок.
- **`sources/company_direct.py` - СДЕЛАН (MVP) 2026-08-19.** Полный агентный
  пайплайн: продакт (скоуп/ребаланс списка) -> решения Дмитрия (дополнить старый
  список AI-компаниями; instant-алерт позже; обновление по событию) -> архитектор
  -> имплементация -> QA (2 раунда фиксов). Что в проде: core/ats_boards.py
  (list_* по трём ATS, фикстуры с живых бордов), таблица target_companies
  (health-колонки, авто-dormant после 6 отказов, строки не удаляются),
  seed 73 компании (35 израильских реконструированы - исходный research-док
  утерян; сетевая верификация: 27 ok / 42 not_found - половина целей на
  Comeet/Workday), 12 Layer-1 бордов активны (Lightricks, Riskified, Fireblocks,
  Melio, Similarweb, Taboola, Yotpo, Payoneer, Optimove, Bringg, D2L, Litmos).
  PM-предфильтр в источнике, published='' намеренно, бюджет 240с, источник
  последним в sources_data. Метрика 2-мес. чекпоинта (продакт): >=3 net-new
  qualified/мес, которых не нашёл ни один другой источник. Дальше по сигналу:
  Layer 2 (15 верифицированных pending: Anthropic, OpenAI, PhotoRoom, Cohere...),
  разбор 42 not_found через --probe-all, instant-алерт. Kill-switch: перевести
  строки в pending (тумблер Sources на прогон не влияет - существующий долг).
- **Удалить код 5 отключённых агрегаторов** (`core/sources/himalayas.py`,
  `remotive.py`, `remoteok.py`, `arbeitnow.py`, `weworkremotely.py`). Отключены,
  но файлы на месте - лишняя maintenance surface. [SOURCES_DECISION.md п.5]
- **Проверить 4 источника-кандидата:** `remocate.pro`, `hirehi.ru`, `hirify.me`,
  `wantapply.com`. Нужен вердикт в формате SOURCES_DECISION (KEEP/FIX/DROP +
  числа) до написания кода: есть ли API/RSS, доля PM-релевантных, гео-фокус.
  [BACKLOG.md "TO INVESTIGATE"]
- **Приватные Telegram-каналы через telethon.** `TELEGRAM_API_ID`/
  `TELEGRAM_API_HASH` уже в `.env`, не используются. Известен 1 кандидат
  (GoRemote, 13.7k подписчиков). Не начинать без явного решения - принципиально
  другой код-путь, не расширение HTML-скрапера. [tasks.md TASK-020, REQ-118]

### Скоринг

- Катовер, Фаза 4, Релиз 2 - см. "Активный трек".
- **Company research при score >=75** - авто-добавление 2-3 фактов о компании
  (размер, стадия, продукт) в карточку. Идея, не спроектирована. [ROADMAP.md]
- **Salary tracking как число.** Поле `jobs.salary TEXT` уже пишется
  (SPEC_FRONTEND v1.2), но парсинг диапазона в числовое поле для фильтрации -
  нет. [ROADMAP.md]
- **Skill gap-анализ по missed keywords.** Какие ключевые слова стабильно в
  missed по отклонённым вакансиям - "вот 5 скиллов, которых не хватает чаще
  всего". Использует ту же разметку отклонений, что уже собирается. [ROADMAP.md]
- **Retry-friendly source fetching** - отличать транзиентный 403 от мёртвого
  источника. [ROADMAP.md]
- **Canary source rollout** - новый источник сначала на малой доле, промоушен
  после пары недель сигнала. [ROADMAP.md]

### Фронтенд

- **Per-channel разбивка Telegram в дашборде.** Сейчас `scraper.py` кладёт в
  `source_counts` один агрегат `TelegramChannels` на все каналы - сырые
  до-фильтровые числа по каналу не видны. Нужно: `telegram_channels.fetch()`
  возвращает per-channel счётчики + отдельная секция (канал -> спарсено ->
  прошло role-фильтр -> qualified). [tasks.md TASK-019, REQ-117]
- **Sources panel: тумблер ни на что не влияет.** Read-часть и сам тумблер
  реализованы (`db.get_sources_config()`, `db.toggle_source()`, таблица
  `sources_config`), но `core/scraper.py` эту таблицу не читает - выключенный
  источник всё равно опрашивается на следующем прогоне. Оставалось пунктом
  "Ask first", т.к. трогает прод-cron. [SPEC_FRONTEND.md §Data Model,
  SPEC_UPDATES.md п.3]
- **Мобильные swipe-actions (1k)** - альтернатива bottom-sheet-пикеру (1j,
  реализован). Нужна undo-механика, которая не спроектирована. Решено не
  строить в текущем проходе. [SPEC_FRONTEND.md Open Questions]
- **Reapplication guard.** Проверка перед созданием карточки: есть ли уже запись
  по company + normalized title со статусом "позиция закрыта". Изначально
  задумывалась против Notion (REQ-113); в текущей архитектуре это SELECT по
  Postgres перед Add-job / генерацией резюме. Не реализовано. [ROADMAP.md
  "Высокий приоритет #2", tasks.md TASK-015]

### Резюме-генерация

- Релиз 1 и Релиз 2 - см. "Активный трек".
- **About Me не кастомизируется под JD** - облачный сервис берёт один из ~7
  готовых абзацев дословно. Ручной workflow берёт тот же базовый абзац и делает
  1-3 точечные подмены слов под лексику JD. Разрыв меньше, чем кажется, но в
  side-by-side видно. Задокументировано как принятое упрощение, не баг.
  [resumebuilder-cloud/docs/resume-service-spec.md §2]
- **Skeptic флагит, но не переписывает** - by design, нет надёжного LLM-free
  рерайта. Флаги видны, текст остаётся как есть. [там же]

### Инфра

- **`DATABASE_URL` в GitHub Actions.** `.github/workflows/scraper.yml`
  (`workflow_dispatch`) не передаёт секрет в шаг "Run scraper" -> `db.init_db()`
  падает с `EnvironmentError` при ручном запуске. Тривиально. [tasks.md
  TASK-003, REQ-103]
- **Фаза 5 - зачистка** (см. "Активный трек"): удалить `core/ats.py`,
  `OPENAI_API_KEY`, `RESUME_MD` в `run.sh`, Postgres-hQr0.
- **Отключить запись скрапера в Notion.** Notion больше не рабочая поверхность.
  Требует решения: что делать с историей карточек и нужен ли экспорт.
  [CONTEXT.md Фаза 5]
- **Еженедельный дайджест** вместо/вместе с 4 сообщениями в день: топ-5 за
  неделю, статистика прогонов, сравнение с прошлой. Опционально. [tasks.md
  TASK-021, REQ-119]
- **Архивация `tg-job-bot-1` на Railway.** Предположительно старый JobPostBot,
  не подтверждено (permissions/workspace mismatch при проверке через MCP
  2026-07-15). Ждёт явного запроса. [tasks.md TASK-022 follow-up]

### Идеи (зафиксированы намеренно, не спроектированы)

- **Мультипользовательский доступ - друзья скрапят для себя.** Не шаринг
  дашборда на просмотр, а мультитенантность: своё резюме на человека (сейчас
  `base_resume.md` один и захардкожен), свой ATS-контекст и калибровка рубрики
  (рубрика жёстко описывает профиль Дмитрия), свои фильтры (сейчас константы в
  `config.py`), куда пишутся результаты, чей бюджет GPT-вызовов. Требует
  отдельного раунда продакт/архитектор. Не путать с SPEC_FRONTEND - та спека
  solo-scoped. [ROADMAP.md, зафиксировано 2026-07-15]
- ~~**Gmail-агент - автодвижение карточек по статусам из писем рекрутеров.**~~
  РЕАЛИЗОВАН 2026-08-22, см. "Активный трек" п.6 (не развёрнут). Противоречие с
  правилом "Never auto-transition" снято конструкцией: авто-перехода нет,
  агент только предлагает, решение подтверждает Дмитрий в очереди `/mail`.
- **Авторизация дашборда через Google-аккаунт вместо токена.** OAuth-приложение
  в Google Cloud Console, `/auth/google` + `/auth/google/callback`, allowlist из
  одного адреса, та же cookie-сессия. `authlib` закрывает компактно. Трейдофф
  обсуждён: заметно больше поверхности ради UX, не безопасности. Дмитрий явно
  решил не делать сейчас. [ROADMAP.md, зафиксировано 2026-08-06]

---

## Сделано

Инфра и пайплайн: Railway cron 4x/день пн-пт, два сервиса в одном проекте,
Postgres как источник правды для дедупа и аналитики, `run.sh` + `railway.toml`.

Фильтры: роль (PM-ключевые слова), локация (Remote/Israel/EMEA), дата
(`MAX_JOB_AGE_DAYS=14`), язык (EN/ES/RU проходят, DE/FR/NL блок, исключение для
явного требования русского).

Дедупликация: cross-source внутри прогона по `normalize_job_key(company, title)`,
между прогонами по `seen_urls`/`seen_keys` из Postgres.

Скоринг: 4 оси (Role 30 / Domain 30 как Value+Exp / Keywords 25 / Location 15),
penalty -15, калибровка синхронизирована с ResumeBuilder. LOCATION-фикс с
`location_reason` (TASK-026, коммиты `d0ed68a`/`39f38ef`).

URL-обогащение: Greenhouse/Lever/Ashby с `_company_match()` на всех трёх ветках
(TASK-001), `jobgether.com`/`jobicy.com` в `_PLATFORM_HOSTS` (TASK-027, 35%
находят прямую ссылку), ScrapingBee-фолбэк для остальных ATS (TASK-028),
`remoteworldwide.net` через прямой anchor.

Telegram-парсинг: фиксы `forproducts`/`remotejobss`/`smartremotejobs`
(TASK-004/005/006), LinkedIn исключён из fetch и apply-URL, listing-URL
skip-list (TASK-023), `<br>`->`\n` вместо `get_text(separator)`.

Безопасность: SSRF-guard в `fetch_url_generic()` (TASK-024), fail-open в
`_check_token()` убран + `hmac.compare_digest` (TASK-025), XSS-фиксы в
дашборде, cookie-сессия вместо `?token=` в URL.

Telegram-уведомления: сводка прогона, алерт на 0 вакансий по всем источникам,
пропуск сводки при `qualified == 0` (TASK-002), `ats_error` в сводке.

Фронтенд (SPEC_FRONTEND v1.2 - реализован): Card Review с инлайн-раскрытием и
полным ATS breakdown, Kanban с drag-and-drop и Active/Rejected-полосами,
Tracker, форма причины отклонения с серверной валидацией, `status_log`, Add-job
(ручной ввод вакансии, QA 14/14), дубль-предупреждение, статистика по причинам
отклонения и conversion, Sources panel (read), период-фильтр, тёмная тема,
поиск, CSV-экспорт, bulk-действия, error/empty-состояния, дизайн-система
Classical.

Облачные сервисы: три сервиса на Railway (scoring/cards/resume), батч-скоринг
(50% цены), сквозной `pipeline_run_id` и `pipeline.event` как cross-service
аудит-лог, миграции через `pipeline.schema_migrations`.

Объединение (2026-08-14): базы сведены, `scoring_client.py` за флагом,
колонки `pipeline_run_id`/`location`/`scoring_source`/`shadow_*`, shadow-прогоны
идут (прогон 181: 21 local-pass -> 12 cloud-pass, 0 флипов вверх, 0 ошибок),
трёхстороннее сравнение на ручной разметке.

Источники: Jobicy location-фикс, Jobgether encoding-фикс (2026-07-15), 5
агрегаторов отключены по аудиту, мёртвые Telegram-каналы вырезаны из
`TELEGRAM_JOB_CHANNELS` (проверено 2026-08-17: в переменной 10 живых каналов,
`agile_jobs`/`cryptojobswork`/`jobstobefound` отсутствуют).

---

## Отменено / устарело

| Пункт | Причина |
|---|---|
| REQ-114 / TASK-016: полный JD для WeWorkRemotely и Arbeitnow | Оба источника отключены по `SOURCES_DECISION.md` |
| REQ-115 / TASK-017: select-поле `Source` в Notion | Notion больше не рабочая поверхность, отключение записи в планах |
| REQ-116 / TASK-018: причины отклонения как select в Notion | Реализовано во фронтенде (`jobs.rejection_reason` + форма), Notion-версия не нужна |
| BACKLOG "[READY] Поле ATS Score в Notion DB" | Давно в проде |
| BACKLOG "[NEXT] Фильтрация по дате публикации" | Реализовано, `MAX_JOB_AGE_DAYS=14` |
| BACKLOG "[MANUAL CHECK] проверить Google Doc" | Резюме генерируются облачным сервисом в PDF, Google Docs из пайплайна ушли |
| REQ-112 / TASK-014: `sync_notion.py`, односторонний polling статусов из Notion | Статусы живут в дашборде и `status_log`, Notion читать незачем |
| REQ-111 / TASK-013: `db_manual.py` + `resume_version` из ResumeBuilder Step 2.5 | Заменено на Add-job в дашборде + `jobs.resume_run_id` (Релиз 1) |
| ROADMAP "Крупная идея: единый сервис вместо JobScraper + ResumeBuilder + Notion" (обсуждение 2026-07-02, ~50 строк дискуссии продакт/архитектор) | Решение принято и реализовано - фронтенд построен, сервисы объединены. Ценность только историческая |
| TASK-022: свести фронтенд с `WorkSearch/JobPostBot` | Решено 2026-07-15: не объединять, JobPostBot не используется |
| BACKLOG "[DONE] Прямая ссылка для Jobicy/Jobgether" - headless-инфраструктура | Закрыто через ScrapingBee (TASK-028), Playwright заводить не пришлось |
| Google Sheets логирование | Отклонено, заменено на Postgres. Код `sheets.py` есть, не активен |
| SQLite -> Postgres миграция исторических данных | Volume был пуст, данные потеряны. Закрыто |
| Порог ATS 60 как константа | Под cloud-шкалу поднимается до 70 при катовере |

---

## Карта документов

### JobScraper

| Документ | Роль | Статус |
|---|---|---|
| `CONTEXT.md` | Состояние сессий, активная задача, session log, standing rules | Живой, обновлять |
| `SPEC.md` | Техспека скрапера: архитектура, пайплайн обработки вакансии, ATS-рубрика, дедупликация, env vars | Справочный. Разделы "v2" поглощены этим документом |
| `SOURCES_DECISION.md` | Аудит 8 источников с числами (26 дней логов + live-тест), вердикты KEEP/FIX/DROP | Справочный, шаблон формата для новых источников |
| `AGENTS.md` | Конституция проекта: hard constraints, dev-пайплайн, subagent routing, стиль кода | Живой |
| `CLAUDE.md` | Рабочая заметка для сессий, указатели на доки | Живой, требует обновления (ссылается на закрытые TASK) |
| `Design/design.md` | Схема Postgres (существующая + планируемая), API-таблица, файловая структура, WorkSearch-контекст | Справочный по схеме данных |
| `Design/frontend-spec/SPEC_FRONTEND.md` | Спека дашборда v1.2: маршруты, колонки, категории отклонения, тесты, boundaries | Справочный, в основном реализован |
| `Design/frontend-spec/FRONTEND_DESIGN_BRIEF.md` | Бриф для дизайн-агента (2026-07-15) | Исторический |
| `Design/design_handoff_review_ui/` | Хендофф Claude Design: HTML-прототипы, Classical CSS, скриншоты, `SPEC_UPDATES.md` | Справочный по визуалу |
| `Design/ats-dashboard-redesign/` | Тот же хендофф в другой упаковке | Дубль |
| `ROADMAP.md`, `BACKLOG.md`, `tasks.md`, `requirements.md` | Поглощены этим документом | Кандидаты в `archive/` |
| `archive/` | `IDEAS.md`, `STATUS.md`, `WORKLOG.md` - консолидированы 2026-07-15 | Историческое |

### resumebuilder-cloud

| Документ | Роль |
|---|---|
| `README.md` | Что построено (Phase 1-3), запуск локально, батч-скоринг, аудит-трейл, env vars, Railway-деплой |
| `docs/scoring-service-spec.md` | Спека и test record scoring-сервиса: API-контракт, рубрика, принципы grounding кандидатского профиля, метод тестирования |
| `docs/resume-service-spec.md` | Спека и test record resume-сервиса: контракт, принятые упрощения, 4 найденных и починенных бага Skeptic'а |
| `services/*/README.md` | Env vars по каждому сервису |
| `db/migrations/`, `db/bootstrap.py` | Схема и накат миграций |

### Внешние

- `/Users/DimaKu/Documents/Coding/db-backups/` - дамп Postgres, baseline-снэпшот
  скоринга, ручная разметка Дмитрия и итог сравнения local vs cloud.
- `ResumeBuilder/` - локальный ручной workflow резюме, источник рубрики и банка
  буллетов для облачных сервисов.

---

## Открытые вопросы

- Порог автогенерации резюме: `>80` или `>=80` (рекомендация `>=80`).
- Формальный "стартуй" на весь Релиз 1 не получен - Дмитрий сказал только
  "кнопка должна быть".
- Порог сигнала для `company_direct`: продакт предложил замерить через 2 месяца
  (<3 лида/месяц = список не тот), данных пока нет.
- Строить ли telethon-путь ради 1 известного приватного канала.
- ~~Gmail-агент против правила "Never auto-transition"~~ - снято 2026-08-22:
  агент только предлагает, применяет Дмитрий.
- Когда разворачивать почтового агента (нужен сервис + Gmail OAuth + ключ Anthropic).
- Нужен ли intake-путь для писем (переслать вакансию на адрес) - сейчас
  транспорта два, почтовый третьим не делали.
- Что делать с историей Notion-карточек при отключении записи.
