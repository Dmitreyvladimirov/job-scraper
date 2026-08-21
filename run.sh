#!/bin/bash
set -e

if [ "$SERVICE_TYPE" = "dashboard" ]; then
    exec uvicorn dashboard:app --app-dir core --host 0.0.0.0 --port "${PORT:-8000}"
elif [ "$SERVICE_TYPE" = "mail" ]; then
    # Separate Railway cron service, not a step inside the scraper: a Gmail auth
    # failure must never be able to take down the live scraping run, and the two
    # have different schedules (mail runs 7 days a week — rejections arrive on
    # weekends; the scraper is Mon-Fri).
    python -c "import sys; sys.path.insert(0, 'core'); import mail_agent; mail_agent.run()"
elif [ "$RUN_MIGRATION" = "1" ]; then
    python scripts/migrate_sqlite_to_pg.py
else
    printf '%s' "$RESUME_MD" > core/base_resume.md
    python core/scraper.py
fi
