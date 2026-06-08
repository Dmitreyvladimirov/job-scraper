# Job Scraper — Backlog

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
