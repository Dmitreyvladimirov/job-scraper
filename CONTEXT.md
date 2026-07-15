# CONTEXT.md

> AI session state for all agents. Updated at the end of each session.
> All AI coding tools (Claude Code, Cursor, Copilot, Windsurf, Aider) read this
> before starting work.

## Resume block

**Current task:** `SPEC_FRONTEND.md` — Phase 1 (Specify) done, awaiting Phase 2 (Plan) before task breakdown/implementation. TASK-001 (Lever/Ashby `_company_match()`) still separately open, not active this session.
**Last session:** 2026-07-15
**Status:** 🟡 in progress

## Session log

| Session | Date | Summary | Files changed |
|---------|------|---------|---------------|
| 1 | 2026-07-05 | Project initialized | CONTEXT.md, .great_cto/PROJECT.md |
| 2 | 2026-07-05 | `/spec` retrofit: сгенерированы requirements.md/design.md/tasks.md из существующих ROADMAP.md/SPEC.md/BACKLOG.md | requirements.md, design.md, tasks.md, CONTEXT.md |
| 3 | 2026-07-07 | Закрыл `TASK-002` (`telegram.py::send_run_summary()` больше не шлёт summary при `qualified == 0`); отдельно — по ошибке удалённые рабочие доки (`CLAUDE.md`, `IDEAS.md`, `STATUS.md`, `WORKLOG.md`) были восстановлены после того, как пользователь указал, что они не одноразовые — см. "Standing rules" ниже | telegram.py, tasks.md, CONTEXT.md, CLAUDE.md, IDEAS.md, STATUS.md, WORKLOG.md |
| 4 | 2026-07-15 | Source-audit fixes (XSS в dashboard.py, ats_error-путь, Jobicy/Jobgether фиксы, отключены 5 мёртвых агрегаторов, TG-парсер), ResumeBuilder longlist/shortlist pipeline (109→18 вакансий), разблокирован TASK-022 (фронтенд отдельно от JobPostBot — подтверждено архивным/неиспользуемым), `SPEC_FRONTEND.md` Phase 1 | dashboard.py, ats.py, scraper.py, sources/*, ROADMAP.md, tasks.md, CONTEXT.md, FRONTEND_DESIGN_BRIEF.md, SPEC_FRONTEND.md |
| 5 | 2026-07-15 | Файловый аудит (со Светочкой) + реорганизация: синхронизировал `base_resume.md` с ResumeBuilder (локально + Railway `RESUME_MD` secret — живой скоринг шёл по июньскому профилю без cybersecurity-training фрейминга); переписал `AGENTS.md` под текущий пайплайн (great_cto удалён 2026-07-06); удалил `.venv 2/`, `job_cache.py`, `jobs.db` (безопасно, 0 ссылок/уже в .gitignore); добавил `.DS_Store` в `.gitignore`; консолидировал STATUS/WORKLOG/IDEAS → `archive/` (уникальный контент перенесён в ROADMAP.md/CONTEXT.md); завёл `scripts/` (6 one-off утилит, подтверждено — нигде не импортируются) и `frontend-spec/` (FRONTEND_DESIGN_BRIEF.md, SPEC_FRONTEND.md, reference/) | AGENTS.md, base_resume.md, .gitignore, ROADMAP.md, CONTEXT.md, tasks.md, archive/*, scripts/*, frontend-spec/* |

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
