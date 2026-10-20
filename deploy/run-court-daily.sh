#!/bin/bash
set -euo pipefail
export PATH="/home/david/.local/bin:/usr/local/bin:/usr/bin:/bin"
export STORY_GIST_TOOLSETS=none
export STORY_GIST_MODEL=gpt-5.6-luna
export STORY_GIST_PROVIDER=openai-codex
cd /srv/stories-dashboard/court-runner
exec /home/david/.hermes/hermes-agent/venv/bin/python3 scripts/court_daily.py --db /srv/stories-dashboard/story-data/stories.sqlite3 "$@"
