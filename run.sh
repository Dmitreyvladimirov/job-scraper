#!/bin/bash
set -e
printf '%s' "$RESUME_MD" > base_resume.md
python scraper.py
