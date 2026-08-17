# CONTEXT.md

> AI session state for all agents. Updated at the end of each session.
> All AI coding tools (Claude Code, Cursor, Copilot, Windsurf, Aider) read this
> before starting work.

## Resume block

**Current task:** Объединение JobScraper + resumebuilder-cloud. Фазы 0-2 сделаны, Фаза 3 (shadow-скоринг) идёт. Следующие шаги по порядку:
1. **Катовер скоринга** — проверить письмо субботнего отчёта (облачная рутина отработала 2026-08-15 09:00, отчёт на почте Дмитрия; рутина trig_01HK2BhpFwuFD75aFvM2TVr5). Если рекомендация «go»: на сервисе "Scrapper itself" выставить `USE_CLOUD_SCORING=1`, поднять `ATS_THRESHOLD` 60→70 (core/config.py:44 — под cloud-шкалу, измерено 80% совпадения с ручной разметкой Дмитрия против 44% у local), сутки наблюдения (ats_error в Telegram-сводке).
2. **Релиз 1 резюме-фич** — план синтезирован из отчётов продакт/фуллстек/QA, Дмитрий сказал «кнопка должна быть», формальное «стартуй» на весь релиз НЕ получено + не отвечен вопрос порога автогенерации (>80 или ≥80; рекомендация ≥80). Состав: core/resume_client.py (копия паттерна scoring_client.py, но retries=1 — ретрай генерации платный!), колонка jobs.resume_run_id, кнопка Generate resume в карточке (hx-indicator, повторный клик открывает готовое), GET /jobs/{id}/resume-pdf прямым SELECT из resume.artifact (база теперь ОБЩАЯ — share-ссылки с TTL не нужны), гейты (company обязательна — иначе 422; JD <200 символов — блок), автоскоринг ручных карточек сразу после Add-job (сейчас они не скорятся никогда), кнопка Re-score в дубль-диалоге. ~6ч.
3. **Релиз 2 (после катовера)** — автогенерация резюме батчем в конце прогона скрапера: cloud-скор ≥80 (порог подтвердить) AND location_score>0 AND len(description)≥500 AND resume_run_id IS NULL, кап 5/прогон, на карточке счётчик Skeptic-флагов. Стоимость генерации $0.003 (замерено по 17 прогонам) — бюджет не фактор, гейты — про качество.
4. **Фаза 4** — фиксы входных данных скоринга: подмена Jobgether-JD реальным текстом по прямой ATS-ссылке (boilerplate «offer available from» травит location в обе стороны — см. db-backups/label_comparison_2026-08-14.md), языковой чек (Portuguese/Uzbek невидимы для location-оси), инженерные вакансии под PM-тайтлом, единый лимит обрезки description.
5. **Фаза 5 (~2026-08-28, после 2 недель стабильности)** — удалить core/ats.py, OPENAI_API_KEY из validate_secrets, запись RESUME_MD в run.sh, Postgres-hQr0 (пустая точка отката), решить про отключение записи скрапера в Notion (Дмитрий уже работает только через фронтенд).

**Ключевые артефакты:** db-backups/ (бэкап jobscraper_pg_20260814_0320.dump 50MB, scoring_baseline_2026-08-14.md, labels_dimitry_2026-08-14.json — ручная разметка 25 вакансий, label_comparison_2026-08-14.md — итог сравнения local 44% vs cloud 80%@70, rescore_labeled_2026-08-14.json, unrequested_addjob_ui.patch).
**Last session:** 2026-08-17 (консолидация доков + уборка папок; рабочий трек без изменений)
**Status:** 🟡 in progress — shadow копит пары с 2026-08-14, ждём решения Дмитрия по катоверу (субботний отчёт лежит на почте с 15.08) и по Релизу 1

## Session log

| Session | Date | Summary | Files changed |
|---------|------|---------|---------------|
| 1 | 2026-07-05 | Project initialized | CONTEXT.md, .great_cto/PROJECT.md |
| 2 | 2026-07-05 | `/spec` retrofit: сгенерированы requirements.md/design.md/tasks.md из существующих ROADMAP.md/SPEC.md/BACKLOG.md | requirements.md, design.md, tasks.md, CONTEXT.md |
| 3 | 2026-07-07 | Закрыл `TASK-002` (`telegram.py::send_run_summary()` больше не шлёт summary при `qualified == 0`); отдельно — по ошибке удалённые рабочие доки (`CLAUDE.md`, `IDEAS.md`, `STATUS.md`, `WORKLOG.md`) были восстановлены после того, как пользователь указал, что они не одноразовые — см. "Standing rules" ниже | telegram.py, tasks.md, CONTEXT.md, CLAUDE.md, IDEAS.md, STATUS.md, WORKLOG.md |
| 4 | 2026-07-15 | Source-audit fixes (XSS в dashboard.py, ats_error-путь, Jobicy/Jobgether фиксы, отключены 5 мёртвых агрегаторов, TG-парсер), ResumeBuilder longlist/shortlist pipeline (109→18 вакансий), разблокирован TASK-022 (фронтенд отдельно от JobPostBot — подтверждено архивным/неиспользуемым), `SPEC_FRONTEND.md` Phase 1 | dashboard.py, ats.py, scraper.py, sources/*, ROADMAP.md, tasks.md, CONTEXT.md, FRONTEND_DESIGN_BRIEF.md, SPEC_FRONTEND.md |
| 5 | 2026-07-15 | Файловый аудит (со Светочкой) + реорганизация: синхронизировал `base_resume.md` с ResumeBuilder (локально + Railway `RESUME_MD` secret — живой скоринг шёл по июньскому профилю без cybersecurity-training фрейминга); переписал `AGENTS.md` под текущий пайплайн (great_cto удалён 2026-07-06); удалил `.venv 2/`, `job_cache.py`, `jobs.db` (безопасно, 0 ссылок/уже в .gitignore); добавил `.DS_Store` в `.gitignore`; консолидировал STATUS/WORKLOG/IDEAS → `archive/` (уникальный контент перенесён в ROADMAP.md/CONTEXT.md); завёл `scripts/` (6 one-off утилит, подтверждено — нигде не импортируются) и `frontend-spec/` (FRONTEND_DESIGN_BRIEF.md, SPEC_FRONTEND.md, reference/) | AGENTS.md, base_resume.md, .gitignore, ROADMAP.md, CONTEXT.md, tasks.md, archive/*, scripts/*, frontend-spec/* |
| 6 | 2026-08-14 | **Объединение с resumebuilder-cloud, Фазы 0-3.** План от 6-агентного пайплайна (продакт/архитектор/фуллстек/QA + 2 факт-агента). Подтверждены 3 причины плохого скоринга: резюме в промпт обрезано до 7000/21645 символов (GeekBrains/Skills/AI-consulting не видны скореру), рубрики разъехались (6 фиксов калибровки 10.08 только в облаке), location вычисляется источниками но не передаётся в промпт. Baseline: 90% оценённого проходило порог 60, Jobgether location 1.6/15. Фаза 0: бэкап 50MB, baseline-снэпшот, разметка 25 вакансий Дмитрием. Фаза 1: базы сведены — 3 облачных сервиса переключены на Postgres скрапера (bootstrap получил ledger pipeline.schema_migrations), hQr0 = точка отката до ~28.08. Фаза 2: core/scoring_client.py за флагом USE_CLOUD_SCORING (off/shadow/1), retries только на transient, config-ошибки → 1 Telegram-алерт/прогон; колонки pipeline_run_id/location/scoring_source/shadow_*; ats_error в сводке. Фаза 3: shadow включён (прогон 181: 21 local-pass → 12 cloud-pass, 0 флипов вверх, 0 ошибок), суббот. отчёт-рутина в облаке. Трёхстороннее сравнение на разметке: cloud 80% @порог 70 vs local 44%. Отдельно: словарь причин → location_mismatch+low_salary (код+22 строки БД+UI; инцидент — суб-агент самовольно менял прод-данные, откачено и переприменено решением Дмитрия), Add-job фича (QA 14/14, UX-фиксы: дедуп-предупреждение, toast вместо reload, Esc/клик-фон, XSS-фикс). План резюме-фич (Релизы 1-2) синтезирован, ждёт ОК. | scoring_client.py, scraper.py, db.py, config.py, telegram.py, dashboard.py, templates/partials/*, tests/test_scoring_client.py, CONTEXT.md; в resumebuilder-cloud: bootstrap.py, share_link.py, main.py |
| 7 | 2026-08-17 | **Консолидация доков + уборка папок.** (1) Все планировочные доки слиты в единый `PROJECT.md` (354 строки вместо ~2000): текущая архитектура, активный трек, живой роадмап по областям, «Сделано»/«Отменено», карта документов, открытые вопросы. ROADMAP/BACKLOG/tasks/requirements + FRONTEND_DESIGN_BRIEF + дубль ats-dashboard-redesign → `archive/`; ручной перенос дизайн-доков в `Design/` узаконен git-renames; CLAUDE.md обновлён (стухшие open threads убраны). Попутные находки: 30% qualified без атрибуции источника (NULL source — в роадмапе), тумблер Sources panel не читается скрапером (в роадмапе), мёртвые TG-каналы уже вырезаны из env (проверено). (2) Решение: репозитории и папки НЕ объединять — Railway привязан к двум репо, общего кода нет, объединение достигнуто на уровне Railway-проекта/Postgres/PROJECT.md; критерий пересмотра — появление общего кода. (3) Уборка: worktree `JobScraper-postgres-tracker` удалён (незакоммиченный хвост DIM-30 закоммичен и запушен в ветку `feature/postgres-tracker`), `WorkSearch/` (внутри живые Google-креды — sodium-wall-331321, при случае отозвать ключ), `JobPostBot/`, `JobPostBot.nosync/` (0 незапушенных) → `Coding/_archived/`. | PROJECT.md (новый), CLAUDE.md, archive/*, Design/*, CONTEXT.md |

## Standing rules

_(перенесено из WORKLOG.md 2026-07-15 при консолидации в единый лог — правило действует постоянно, не только для той сессии)_

- Не удалять рабочие/плановые доки проекта без явного подтверждения пользователя, что они одноразовые. 7 июля 2026 доки (`CLAUDE.md`, `IDEAS.md`, `STATUS.md`, `WORKLOG.md`) были удалены как «мусор» и их пришлось восстанавливать — пользователь явно указал, что это не были одноразовые файлы.
- В сомнительных случаях — сохранять рабочие заметки, удалять только явно генерируемый кэш (`__pycache__/`, `.DS_Store` и т.п.).
- Если сессия трогает документы верхнего уровня (архивирует/мержит/переносит) — фиксировать причину в Session log ниже, чтобы следующая сессия понимала, что изменилось и почему.

## Open questions

- Приватные Telegram-каналы (REQ-118) — строить ли telethon-путь ради 1 известного кандидата?
- Итоговая архитектура объединения JobScraper+ResumeBuilder+Notion в один сервис — решение частичное (см. design.md Open Questions). TASK-022 (фронтенд UI сам по себе) решён 2026-07-15 — независимо от JobPostBot.
- Порог сигнала для company_direct (REQ-107) — нет данных для оценки до истечения 2 месяцев после запуска.
- `SPEC_FRONTEND.md` Open Questions: drag-and-drop на мобильном, detail view vs list-only, визуализация ATS breakdown, JSONB-отклонение от TEXT-конвенции.
- Мультипользовательский доступ (друзья используют скрапер для себя) — идея зафиксирована в ROADMAP.md, не спроектирована.

## Divergences from spec

- **2026-07-15**: TASK-022 (кастомный фронтенд, Phase 6) разблокирован. Решение:
  строить фронтенд для сценариев 1+3 (пересмотр карточек скрапера, канбан,
  причины отклонения, статистика) отдельно от `JobPostBot` (ручной ввод URL,
  сценарий 2) — подтверждено пользователем, `JobPostBot` больше не
  используется, подлежит архивации. `tg-job-bot-1` на Railway не проверен
  (permissions/workspace mismatch), не блокирует решение. См.
  `frontend-spec/FRONTEND_DESIGN_BRIEF.md`, `frontend-spec/SPEC_FRONTEND.md`, `tasks.md` TASK-022.
