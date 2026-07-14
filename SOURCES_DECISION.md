# Sources Decision — 2026-07-14

Основа: Postgres-логи (26 дней, 29,874 rows, 174 qualified) + live-тест QA всех 8 fetchers (~920 fetched → 73 survive filters, 68 из Telegram).

## 1. Вердикт по источникам

| Источник | Вердикт | Причина (числа) |
|---|---|---|
| TG: forproducts | **KEEP** | 1,175→63 qualified (5.4%), 36% всех qualified — главный источник |
| TG: hireproproduct | **KEEP** | 59→3 (5.1%) |
| TG: productjobgo | **KEEP** | 390→6 (1.5%) |
| TG: evacuatejobs | **KEEP** | 1,354→12 (0.9%) |
| TG: worldwideremote / hiring_by_lukina / smartremotejobs / zarubezhom_jobs | **KEEP** | 8 / 4 / 3 / 2 qualified — скромно, но near-zero maintenance (env var) |
| TG: theyseeku, israjobs, product_jobs, pmclub, israeljobopps, itworksolim, remotejobss, careernoborders, hightechforolims | **DROP** | 0 qualified из 2,600 rows за 26 дней. QA независимо подтвердил hightechforolims (последний пост 20д назад) + agile_jobs/cryptojobswork (структурно мертвы, `/s/` пуст) + jobstobefound (54д без постов) |
| Jobicy | **FIX** (1 строка) | 2,813 rows, 72% умирают на location — баг: `jobGeo` возвращает "USA"/"Europe", никогда "remote", хотя все вакансии удалённые. QA: 52% PM-title relevance — лучшая из всех источников. Фикс: `location = f"Remote — {jobGeo}"` в `sources/jobicy.py` |
| Jobgether | **FIX** (1 строка) | QA: ~100% PM relevance, лучший по дизайну (curated), но UTF-8 encoding баг корраптит title/description. Фикс: `resp.encoding = resp.apparent_encoding` в `_fetch_page`. Проверить, что подключён в `scraper.py` `sources_data` (файл сейчас untracked) |
| Remotive | **DROP** | 2,367 rows, 98% умирают на role filter, 0 qualified за всё время. QA подтвердил: `category`/`search` — no-ops на стороне API, не чинится |
| Himalayas | **DROP** | 1,554→1 qualified. QA: `q` игнорируется API, лимит жёстко капнут на 20 из 102k firehose, 0% PM-title relevance. Не чинится параметрами |
| RemoteOK | **DROP** | 4,743→12 (0.3%), всего 104 уникальных URL (массивный re-churn). QA: median age вакансий 70 дней vs cutoff 14 дней — API не поддерживает date filter, не чинится |
| Arbeitnow | **DROP** | 2,846→3 (0.1%). QA: search-параметр нерабочий, 55% борда — немецкие вакансии, собственная пагинация триггерит свой же rate-limit (429) в рамках одного fetch |
| WeWorkRemotely | **DROP** | 4,478→5 (0.1%). QA live-тест не нашёл фикса; выхлоп ничтожен относительно fetch/dedup overhead |
| **NULL/empty source** | **РАССЛЕДОВАТЬ (блокер)** | 54 rows → 52 qualified (**30% всех qualified!**). Атрибуция сломана — неясно, какой источник это реально приносит. Пока не закрыто, доверять точным % выше нужно с оговоркой |

## 2. Приоритизированный список действий

1. **NULL-source расследование** — обязательный первый шаг: 30% qualified не атрибутированы, это искажает весь остальной анализ выше. Effort: часы, найти fetcher/путь, где `job["source"]` не проставляется.
2. **Jobicy: `location = f"Remote — {jobGeo}"`** — 1 строка, `sources/jobicy.py`. Ожидаемый эффект: при 52% raw PM-relevance (лучший показатель среди всех источников) и текущей 72%-й гибели на location, по оценке QA становится вероятным **#2 источником после Telegram**.
3. **Jobgether: `resp.encoding = resp.apparent_encoding`** — 1 строка в `_fetch_page`, плюс проверить подключение к `scraper.py`. QA: лучший по дизайну источник (~100% PM relevance, curated) — сейчас теряется на encoding-баге, не на плохом сорсинге.
4. **Отрезать 9 мёртвых TG-каналов** (0 qualified из 2,600 rows) + подтверждённые QA structurally-dead (agile_jobs, cryptojobswork, jobstobefound, hightechforolims) — near-zero effort (env var), снижает шум/dedup overhead, риска нет.
5. **Удалить код 5 сломанных aggregator-источников** (Himalayas, Remotive, Arbeitnow, RemoteOK, WWR) — вместе дают 21/174 (12%) qualified ценой ~19,000+ rows fetch/фильтрации; все баги API-side, QA подтвердил — не чинятся. Снижает maintenance surface.

## 3. Стратегическая рекомендация

Следующая единица усилий → **`sources/company_direct.py` (EdTech/LMS), не больше TG-каналов и не докручивание сломанных агрегаторов.**

Обоснование числами: Telegram уже даёт 68/73 (93%) survivors в live-тесте QA и 101/122 (58%) атрибутированных qualified в 26-дневных логах — источник близок к насыщению, и этот же аудит уже нашёл 9 из ~17 протестированных каналов мёртвыми (diminishing returns на поиск новых каналов). Aggregator-источники (Himalayas/Remotive/RemoteOK/Arbeitnow) заблокированы на уровне API, не параметров — тратить время на них дальше бессмысленно. `company_direct.py` — единственное направление, целящее в структурно другой пул вакансий (не попадающие на агрегаторы вообще), а не в переразбор уже покрытого пула. Спека уже готова (roadmap, high priority #1), fetch-код по большей части существует в `utils.py`.

Порядок: закрыть пункты 1–5 (дёшево, дни) → затем `company_direct.py` Layer 1 (израильские + high-confidence global EdTech/LMS ATS, ~2 недели по уже согласованному плану в `ROADMAP.md`).
