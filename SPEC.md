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
| Хранилище статистики | SQLite (`/data/jobs.db` на Railway Volume) |
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
| RemoteOK | JSON API | Короткое AI-саммари — обогащается из прямого источника |

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
    │
    ▼
fetch_jd_from_url()              — для RemoteOK: загрузить полный JD из GH/Lever/Ashby
  (если не нашли → флаг incomplete_description)
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

Модель: GPT-4o-mini, temperature=0.

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

Для вакансий с платформ-агрегаторов (RemoteOK, Arbeitnow, WeWorkRemotely) ищем прямую ссылку на ATS компании:

1. Генерируем slug-варианты из названия компании
2. Пробуем Greenhouse API → Lever API → Ashby GraphQL
3. Валидируем название компании через `_company_match()` (защита от ложных совпадений)
4. Если нашли → `job["apply_url"]` = прямая ссылка

Для RemoteOK дополнительно: после нахождения URL загружаем полный JD через `fetch_jd_from_url()` (Greenhouse/Lever/Ashby возвращают полный текст).

Если прямую ссылку не нашли → в Notion callout добавляется предупреждение `⚠️ Описание неполное`.

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

---

## Дедупликация

**Слой 1 — внутри прогона (cross-source):**
Группируем по `normalize_job_key(company, title)`. Из дублей выбираем лучший источник: не-RemoteOK приоритетнее, затем длиннейшее описание.

**Слой 2 — между прогонами (Notion):**
При старте загружаем из Notion все URL (`seen_urls`) и все пары (company, title) (`seen_keys`). Вакансия пропускается если совпадает по любому из двух.

`normalize_job_key()` стрипает суффиксы (Inc, Ltd, GmbH, LLC...) и пунктуацию для fuzzy-матчинга.

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
