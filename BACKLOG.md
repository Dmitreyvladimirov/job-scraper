# Job Scraper — Backlog & Progress

## Статус деплоя (2026-06-07)

Railway cron: `7 6,9,12,15 * * 1-5` (пн–пт, 4× в день в :07)
GitHub Actions: только `workflow_dispatch` для ручного запуска

---

## [PARTIAL] Аудит качества парсинга Telegram-каналов — сделан, часть багов почищена, часть осталась

**Запрошено пользователем:** 2026-07-02. Аудит проведён по всем 21 каналу (2 страницы, 14-дневное окно).

**Уже почищено 2026-07-02:**
- LinkedIn (весь домен) исключён из фетчащихся/apply-URL — раньше давал auth-wall текст ("Sign in to LinkedIn... By clicking Continue...") вместо JD/компании
- regex бэкфилла компании в `scraper.py` сужен до явных лейблов ("company"/"компания"/"работодатель"), убраны слишком общие триггеры "at"/"in"
- `get_text(separator="\n")` в `_parse_messages()` (`sources/telegram_channels.py`) заменён на `<br>`→`\n` + `get_text()` без разделителя — раньше вставлял перенос строки на границе КАЖДОГО тега (не только `<br>`), из-за чего "Title в **Company**" (компания жирным — частый формат) разваливалось на разные "строки" и компания терялась. Проверено на реальных данных: `evacuatejobs` 37→0 пустых компаний, `worldwideremote` 40→3.

**Осталось (найдено аудитом, не починено):**
- **`forproducts` — крупнейший источник релевантных вакансий канала (18/40 за прогон), 0% с извлечённой компанией.** Формат: список из нескольких ролей через 🔹, компания указана в отдельной строке ниже ("🔹 Role1\n🔹 Role2\nв ВкусВилл — ..."). `_extract_title_company()` смотрit только на `lines[0]`, компания физически есть в тексте, но не на первой строке.
- **`remotejobss` — вероятно, теряет 100% вакансий канала.** Явно структурированный формат ("💼 JOB OPPORTUNITY\n🚀 Manager Growth Marketing & Content\n🏢 Company: X"), но title берётся с первой строки ("JOB OPPORTUNITY" — generic заголовок), из-за чего role-фильтр не матчит НИЧЕГО. Реальный title — на второй строке, компания — явно лейблирована "Company: X" на третьей. Самый простой для парсинга формат из всех каналов, но сейчас даёт 0 полезных вакансий.
- Суффикс "(Компания)" в title не отделяется — `smartremotejobs`, `productjobgo`, частично `worldwideremote`: title = "Senior PM (CyberNut)", company остаётся пустой.
- `workewco` — компания физически отсутствует в тексте (только title/локация/ссылка) — не баг, ограничение источника.
- `agile_jobs`, `cryptojobswork` — не отдают список сообщений через `t.me/s/` вообще (публичное веб-превью отключено владельцем канала) — нужен Telegram Client API (telethon), не текущий HTML-скрапинг.
- `jobstobefound` — жив, но не постил ничего последние ~3.5 месяца — не баг, просто вне 14-дневного окна.

**Что делать дальше:** починить `forproducts` (компания на отдельной строке после multi-bullet списка) и `remotejobss` (лейблированный "Company:" формат + title со второй строки, не первой) — вместе, по оценке, это удвоит полезный выход Telegram-источника. "(Компания)"-суффикс — более простой точечный фикс.

**Приоритет:** высокий для `forproducts`/`remotejobss` (реальная потеря лидов прямо сейчас), средний для остального.

---

## [READY] Telegram: не слать отчёт при каждом прогоне — только когда есть находки

**Запрошено пользователем:** 2026-07-01.

**Что не так:** `telegram.send_run_summary()` (`telegram.py`) сейчас шлёт сообщение на **каждый** прогон скрапера, включая случай `qualified == 0` ("Новых вакансий не найдено..."). При 4 прогонах в день это в основном шум — большинство прогонов ничего не находят.

**Что делать:** в `send_run_summary()` (`telegram.py:29`) убрать ветку `if qualified == 0: ...` — просто `return` без отправки, если `qualified == 0`. Отправлять сообщение только когда реально найдены новые вакансии (текущая ветка `else`). `send_error()` (алерты об ошибках) не трогать — они должны приходить всегда.

**Точка вызова:** `scraper.py:231`, без изменений — логика фильтрации остаётся внутри `telegram.py`.

**Приоритет:** низкий/тривиальный, но явно запрошено — можно взять в любой момент.

---

## [BUG] `_company_match()` не применяется к Lever и Ashby

**Найдено:** QA-ревью 2026-07-01 (при обсуждении EdTech/LMS company-sourcing идеи).

**Что не так:** В `utils.py::find_apply_url()` проверка `_company_match()` (защита от ложных совпадений вроде "Insider" → "Business Insider") применяется **только на Greenhouse-ветке** (строка ~91). Lever-ветка (~110) и Ashby-ветка (~132) принимают первый ответивший slug и матчат только по названию вакансии (`_title_match`) — без проверки, что это вообще та же компания.

**Риск:** если slug компании случайно совпадает с slug другой компании на Lever/Ashby (например, generic-словом), и там же нашлась вакансия с похожим title — получаем `apply_url`, ведущий на вакансию совсем другой компании. Тихая ошибка, не видна без ручной проверки.

**Что делать:** добавить `_company_match()` (или эквивалент) на Lever/Ashby ветки. У Lever в ответе `/v0/postings/{slug}` нет прямого поля с названием компании — нужен доп. lookup или fuzzy-match по team/posting тексту. У Ashby — GraphQL-ответ уже содержит `jobBoard`, можно достать организацию оттуда для сверки.

**Приоритет:** независим от EdTech/LMS-фичи (roadmap) — актуален уже сейчас для существующего enrichment RemoteOK/Arbeitnow/WWR.

---

## [READY] Поле ATS Score в Notion DB

**Что делает:** ATS score (0–100) становится отдельной колонкой в Notion DB, не только текстом внутри страницы. Позволяет сортировать и фильтровать вакансии по рейтингу прямо в таблице.

### Шаг 1 — Пользователь добавляет поле в Notion (вручную)
1. Открыть Notion DB (Vacancies)
2. Добавить новое свойство: **тип Number**, **название точно: `ATS Score`**
3. Формат числа: Number (без процентов и прочего)

### Шаг 2 — Изменения в коде

**`notion_client.py` — функция `_make_properties`:**
```python
def _make_properties(job: dict, status2: str, status: str, score: int | None = None) -> dict:
    today = date.today().isoformat()
    company = job.get("company", "")
    title = f"{job['title']} ({company})" if company else job["title"]
    props = {
        "Позиция": {"title": [{"text": {"content": title[:255]}}]},
        "Ссылка на вакансию": {"url": job["url"]},
        "Status2": {"select": {"name": status2}},
        "Статус": {"select": {"name": status}},
        "Date Applied": {"date": {"start": today}},
        "Подался сам": {"checkbox": False},
    }
    if company:
        props["Компания"] = {"rich_text": [{"text": {"content": company[:255]}}]}
    if score is not None:
        props["ATS Score"] = {"number": score}   # <-- добавить эту строку
    return props
```

**`notion_client.py` — обновить вызовы `_make_properties`:**
```python
# В create_entry:
props = _make_properties(job, "Scraped", "Активно", score=result.score)

# В create_rejected_entry:
props = _make_properties(job, "rejected_by_scraper", "🚫 Отклонено", score=score)
```

### Важно
- Деплоить код только ПОСЛЕ того как поле добавлено в Notion
- Если поле не существует, Notion вернёт 400 и карточка не создастся совсем
- Имя поля должно совпадать точно: `ATS Score`

---

## [NEXT] Фильтрация вакансий по дате публикации

**Зачем:** Источники возвращают старые вакансии (месяцы), а актуальны только свежие — не старше 2 недель, лучше до 7 дней.

**Что сделать:**
1. Парсить поле даты публикации из каждого источника (у всех оно есть: `published_at`, `date`, `pubDate` и т.п.)
2. Хранить как `job["published_at"]` (ISO datetime string)
3. Добавить фильтр в `filters.py`: пропускать вакансии старше `MAX_JOB_AGE_DAYS` (default = 14)
4. Логировать отдельный счётчик: `stale: N`
5. Добавить `MAX_JOB_AGE_DAYS` в `config.py`
6. Опционально: писать дату публикации в Notion карточку (поле `Published`)

**Где смотреть дату по источникам:**
- Himalayas: `published_at` в JSON
- RemoteOK: `date` (unix timestamp)
- Remotive: `publication_date`
- Jobicy: `jobGeo` + `pubDate` (RSS)
- WeWorkRemotely: `pubDate` (RSS)

**Ожидаемый эффект:** Сильно сократит шум — не будет показывать вакансии за январь в июне.

---

## [MANUAL CHECK] После следующей вакансии с высоким ATS

После того как придёт первая хорошая вакансия (score ≥ 70), открыть сгенерированный Google Doc и проверить:
- Нет пустого пространства между секциями (hidden bullets)
- Скиллы: категория жирная, список — нормальный размер
- About Me: размер шрифта ≈ 9.5pt
- GPT не повторяет одни и те же bullets в разных компаниях
- Все 3 интро заполнены (IC_INTRO, SF_INTRO, GB_INTRO)

---

## [DONE] Выполненные задачи

- [x] Node.js 24 для GitHub Actions (checkout@v6, setup-python@v6)
- [x] `_hide_empty_bullets`: добавлен `updateParagraphStyle` spaceAbove/Below=0
- [x] `_format_skills`: убран fontSize override (был 8.5pt)
- [x] `_format_skills`: guard — colon + comma (не бить по "Launch X: result")
- [x] `seen_urls.add` — только после успешного Notion write (notion функции → bool)
- [x] `About Me` font: добавлена `_format_about_me()` → 9.5pt
- [x] `strip_html()` в utils.py — чистим HTML из job descriptions
- [x] Per-source counts + 0-vacancy alert в scraper.py
- [x] source_counts breakdown в Telegram summary
- [x] GPT prompt: все 3 интро обязательны (IC/SF/GB)
- [x] Railway deployment: railway.toml + run.sh
- [x] GitHub Actions: schedule удалён, только workflow_dispatch
- [x] Все 8 env vars установлены в Railway CLI
