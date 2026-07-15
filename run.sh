#!/bin/bash
set -e

if [ "$SERVICE_TYPE" = "dashboard" ]; then
    exec uvicorn dashboard:app --app-dir core --host 0.0.0.0 --port "${PORT:-8000}"
elif [ "$RUN_MIGRATION" = "1" ]; then
    python scripts/migrate_sqlite_to_pg.py
else
    printf '%s' "$RESUME_MD" > core/base_resume.md
    python core/scraper.py
fi
