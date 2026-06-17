#!/bin/bash
set -e

if [ "$SERVICE_TYPE" = "dashboard" ]; then
    exec uvicorn dashboard:app --host 0.0.0.0 --port "${PORT:-8000}"
else
    printf '%s' "$RESUME_MD" > base_resume.md
    python scraper.py
fi
