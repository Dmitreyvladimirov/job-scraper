# Job Scraper — Техническое задание

## Назначение

Автоматизированный бот для поиска PM-вакансий на международных job boards. Бот запускается 4 раза в день, фильтрует нерелевантные вакансии, оценивает соответствие резюме, сохраняет результаты в Notion и отправляет сводку в Telegram.

---

## Стек

| Компонент | Технология |
|---|---|
| Язык | Python 3.12+ |
| Хостинг | Railway (cron job) |
| Хранилище вакансий | Notion Database |
| Хранилище статистики | PostgreSQL (Railway managed Postgres) |
| LLM | OpenAI GPT-4o-mini |
| Уведомления | Telegram Bot API |
| Обогащение URL | Greenhouse / Lever / Ashby Public API |

---

## Архитектура

```
sources/          ← парсеры job boards (один файл = один источник)
scraper.py        ← главный оркестратор, точка входа
filters.py        ← фильтры: роль, язык, локация, дата
ats.py            ← ATS-скоринг через GPT-4o-mini
utils.py          ← URL-обогащение, fetch JD, дедупликация, стриппинг HTML
notion_client.py  ← запись вакансий в Notion
telegram.py       ← Telegram-уведомления
db.py             ← SQLite: логирование прогонов и вакансий
config.py         ← константы и переменные окружения
run.sh            ← entrypoint для Railway
```

---

## Источники вакансий

| Источник | Тип | Особенности |
|---|---|---|
| Himalayas | JSON API | Полное описание, лучшее качество |
| Remotive | JSON API | Хорошее описание |
| Jobicy | RSS | Обычно полное |
| WeWorkRemotely | RSS | Среднее описание |
| Arbeitnow | JSON API | Много немецких вакансий — отсеиваются языковым фильтром |
| RemoteOK | JSON API | Короткое AI-саммари — обогащается из прямого источника (см. ниже) |
| Telegram каналы | Web scraping (t.me/s/) | 11 каналов, без авторизации, парсинг HTML |

### Telegram каналы (sources/telegram_channels.py)

Скрапит публичные страницы `https://t.me/s/{channel}`. Авторизация не нужна.

**Список каналов:** `TELEGRAM_JOB_CHANNELS` в `.env` (comma-separated).

**URL-классификация сообщения:**
1. Приоритет 1 — ссылка с лейблом "тут / apply / откликнуться"
2. Приоритет 2 — известный ATS-домен (Greenhouse, Lever, Ashby, Workable, Remocate...)
3. Приоритет 3 — первая ссылка в первых 300 символах (Remocate-стиль)
4. Fallback — t.me ссылка на само сообщение

**Listing pages:** URL вида `/careers` или `/jobs` без конкретного job ID распознаётся как листинг. Скрапер пытается открыть страницу и найти внутри отдельные PM-вакансии. Если страница недоступна (403, JS-SPA) — fallback на t.me ссылку.

**Russia detection:** если в location/company/тексте сообщения/описании найден российский город или ключевое слово "россия/russia" — выставляется `russia_warning = True`.

---

## Pipeline обработки вакансии

```
Fetch all sources
    │
    ▼
Strip HTML из descriptions
    │
    ▼
Cross-source dedup (company + title)
  — если дубли: берём не-RemoteOK, потом длиннейшее описание
    │
    ▼
[Для каждой вакансии]
    │
    ├─ passes_role_filter?       — title содержит PM-ключевые слова
    ├─ passes_language_filter?   — English / Spanish / Russian (исключение: требует русский)
    ├─ passes_location_filter?   — Remote worldwide / Israel / EMEA
    ├─ passes_date_filter?       — не старше MAX_JOB_AGE_DAYS (14 дней)
    ├─ URL dedup (seen_urls)?    — не было в Notion по URL
    ├─ Key dedup (seen_keys)?    — не было в Notion по (company, title)
    │
    ▼
enrich_url()                     — для RemoteOK/Arbeitnow/WWR: найти прямую ссылку
    │                              через Greenhouse → Lever → Ashby API по названию компании
    ▼
[Telegram] fetch_jd_from_url() или fetch_url_generic()  — полный JD по ссылке из сообщения
[RemoteOK] fetch_jd_from_url() → fetch_url_generic()    — полный JD; apply_url из API или найденный выше
  (если ни один не дал текст → флаг incomplete_description, callout в Notion)
    │
    ▼
ats.analyze()                    — GPT оценивает вакансию (≤ MAX_GPT_CALLS_PER_RUN = 40)
    │
    ├─ score < 60 → create_rejected_entry() в Notion (не виден в основном фильтре)
    └─ score ≥ 60 → create_entry() в Notion + добавить в top_jobs
    │
    ▼
telegram.send_run_summary()      — итоги прогона + топ вакансии
db.finish_run()                  — сохранить статистику в SQLite
```

---

## ATS-скоринг (ats.py)

Модель: GPT-4o-mini, temperature=0. Лимиты контекста: JD — первые 5000 символов, резюме — первые 7000 символов.

Четыре измерения, максимум 100 баллов:

| Измерение | Макс | Описание |
|---|---|---|
| Role Match | 30 | Senior PM/Head/VP = 25–30; mid PM = 12–22; PO/Associate = 0–10 |
| Domain Fit | 30 | Value (0–15) + Experience (0–15) раздельно |
| Keyword Overlap | 25 | Must Have (2 балла) + Nice to Have (1 балл) по 12 ключевым словам |
| Location | 15 | Remote/Israel = 15; EMEA = 8; US only = 0 |
| **Penalty** | −15 | Если JD требует N лет специфического домена которого нет |

Порог для Notion qualified: **60+**. Ниже — `rejected_by_scraper`.

Domain Value шкала: AI/ML=14–15, B2B SaaS=12–14, Cybersecurity=11–12, FinTech=10–11, EdTech=9–11, Data/BI=7–9, Growth/B2C=5–7.

---

## URL-обогащение (utils.py)

### RemoteOK — правило обогащения

**Правило:** для каждой вакансии из RemoteOK скрапер ищет прямую ссылку на вакансию в компании на внешних ATS-платформах.

Порядок действий:
1. `enrich_url(job)` — генерирует slug-варианты из названия компании, ищет через:
   - Greenhouse API (`boards-api.greenhouse.io`)
   - Lever API (`api.lever.co`)
   - Ashby GraphQL (`jobs.ashbyhq.com`)
   - Валидирует через `_company_match()` (защита от "Insider" → "Business Insider")
2. Если нашёл → `job["apply_url"]` = прямая ссылка
3. Если не нашёл через ATS → берём `apply_url` из RemoteOK API (прямая ссылка на сайт компании)
4. По найденному URL: `fetch_jd_from_url()` (ATS API) → fallback `fetch_url_generic()` (HTML scraping)
5. Если JD получить не удалось → `incomplete_description = True`, callout в Notion `⚠️`

### Все источники
`fetch_jd_from_url()` умеет парсить Greenhouse / Lever / Ashby через их API (возвращают чистый текст).
`fetch_url_generic()` — универсальный HTML scraper для всех остальных страниц (strip script/style/nav).

---

## Notion-карточка (notion_client.py)

Поля в карточке:
- `Позиция` — "Role Title (Company)"
- `Компания` — отдельное поле (plain text)
- `Ссылка на вакансию` — прямая ссылка если нашли, иначе платформа
- `ATS Score` — число 0–100 (сортировка/фильтрация)
- `Status2` — "Scraped" для новых, "rejected_by_scraper" для низкого скора
- `Статус` — "Активно"
- `Date Applied` — дата создания карточки

Тело страницы: callout с ATS breakdown (Role/Domain/Keywords/Location + Value·Exp детализация), `why_apply`, `why_not`, matched/missed keywords. При cooldown и неполном описании — дополнительные callout-предупреждения.

**Russia warning:** если `russia_warning = True` — добавляется callout 🇷🇺 и поле `Тип вакансии = "🇷🇺 Russia"`. В Telegram-сводке такая вакансия помечается флагом 🇷🇺.

**UTF-16:** Notion считает длину текста в UTF-16 code units (как JavaScript). Emoji вне BMP (🌎🚀 и др.) занимают 2 единицы. Все rich_text блоки нарезаются через `_split_for_notion()` с лимитом 1990 UTF-16 единиц (не символов Python).

---

## Дедупликация

**Слой 1 — внутри прогона (cross-source):**
Группируем по `normalize_job_key(company, title)`. Из дублей выбираем лучший источник: не-RemoteOK приоритетнее, затем длиннейшее описание.

**Слой 2 — между прогонами (Notion + Postgres):**
При старте загружаем из Notion все URL (`seen_urls`) и все пары (company, title) (`seen_keys`). Postgres также хранит историю прогонов. Вакансия пропускается если совпадает по любому из двух.

`normalize_job_key()` стрипает суффиксы (Inc, Ltd, GmbH, LLC...) и пунктуацию для fuzzy-матчинга.

**Важно:** `seen_urls.add()` вызывается всегда после обработки вакансии, независимо от успеха записи в Notion. Это предотвращает дубли внутри одного прогона (в предыдущей версии URL добавлялся только при успехе → один и тот же URL мог попасть в Notion несколько раз если первые попытки падали с ошибкой).

---

## Фильтры

### Языковой фильтр
Пропускаются: **английский, испанский, русский**.
Блокируются: немецкий, французский, нидерландский и другие.

Детекция:
1. Хард-блок: маркеры `(m/w/d)`, `:in` суффикс (немецкая гендерно-нейтральная форма)
2. Мягкий блок: 3+ стоп-слова из списка немецкого/французского/нидерландского

Исключение: любая вакансия с явным требованием русского языка проходит фильтр.

### Cooldown компаний
Если в Notion уже есть отклик на эту компанию за последние 90 дней — вакансия создаётся с предупреждением в callout, но не блокируется.

---

## Scheduling

Railway cron: `7 6,9,12,15 * * 1-5` — пн–пт, 4 прогона в день (09:07, 12:07, 15:07, 18:07 по Израилю).

Лимит GPT-вызовов: 40 на прогон (защита от перерасхода при большом потоке).

---

## Переменные окружения

| Переменная | Назначение |
|---|---|
| `NOTION_TOKEN` | API токен Notion |
| `OPENAI_API_KEY` | OpenAI |
| `TELEGRAM_TOKEN` | Telegram Bot |
| `TELEGRAM_CHAT_ID` | ID чата для уведомлений |
| `RESUME_MD` | Текст резюме (base64 или plain, записывается в `base_resume.md`) |
| `DATABASE_URL` | PostgreSQL connection string (Railway) |
| `TELEGRAM_JOB_CHANNELS` | Comma-separated список Telegram каналов (напр. `@evacuatejobs,@forproducts`) |
| `TELEGRAM_API_ID` | Telegram User API ID (my.telegram.org) — зарезервировано, не используется активно |
| `TELEGRAM_API_HASH` | Telegram User API Hash — зарезервировано |
