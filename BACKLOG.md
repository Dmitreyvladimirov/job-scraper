# Job Scraper — Backlog & Progress

## Статус деплоя (2026-06-07)

Railway cron: `7 6,9,12,15 * * 1-5` (пн–пт, 4× в день в :07)
GitHub Actions: только `workflow_dispatch` для ручного запуска

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
