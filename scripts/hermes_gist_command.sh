#!/usr/bin/env bash
set -euo pipefail
prompt=$(cat)
unset HERMES_SESSION_ID HERMES_SESSION_KEY HERMES_SESSION_PLATFORM HERMES_SESSION_CHAT_ID HERMES_SESSION_CHAT_NAME HERMES_SESSION_THREAD_ID HERMES_SESSION_USER_ID HERMES_SESSION_USER_NAME
hermes chat -Q --source tool --max-turns 1 --toolsets safe --provider "${STORY_GIST_PROVIDER:-openai-codex}" --model "${STORY_GIST_MODEL:-gpt-5.6-luna}" -q "$prompt" | sed '/^session_id:/d'
