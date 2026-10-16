#!/usr/bin/env bash
set -euo pipefail

echo "=== Stories Dashboard entrypoint ==="

# Validate required configuration
: "${STORIES_DB_PATH:?STORIES_DB_PATH must be set}"
: "${GOOGLE_CLIENT_ID:?GOOGLE_CLIENT_ID must be set}"
: "${GOOGLE_ALLOWED_EMAIL:?GOOGLE_ALLOWED_EMAIL must be set}"
: "${GOOGLE_ALLOWED_SUB:?GOOGLE_ALLOWED_SUB must be set}"
: "${SESSION_SECRET:?SESSION_SECRET must be set}"
: "${PUBLIC_ORIGIN:?PUBLIC_ORIGIN must be set}"

# Verify the database file exists and is writable
if [ ! -f "$STORIES_DB_PATH" ]; then
  echo "ERROR: Database not found at $STORIES_DB_PATH"
  exit 1
fi

# Create a timestamped backup before migration
BACKUP_DIR="${STORIES_DB_PATH%/*}/backups"
mkdir -p "$BACKUP_DIR"
BACKUP_FILE="$BACKUP_DIR/stories-$(date -u +%Y%m%dT%H%M%SZ).sqlite3"
cp "$STORIES_DB_PATH" "$BACKUP_FILE"
echo "Backup created: $BACKUP_FILE"

# Python startup validates config, opens DB (migrations run automatically),
# verifies integrity (foreign keys, etc.)
python3 -c "
from api.config import RuntimeConfig
c = RuntimeConfig.from_env()
print('Config OK')
from story_store import StoryStore
s = StoryStore(c.stories_db_path)
print(f'DB OK — {len(s.list_items())} items, {len(s.list_sources())} sources')
# Integrity checks
import sqlite3
conn = sqlite3.connect(c.stories_db_path)
conn.execute('PRAGMA foreign_keys=ON')
fk = conn.execute('PRAGMA foreign_key_check').fetchall()
if fk:
    raise SystemExit(f'Foreign key violations: {fk}')
print('Integrity checks passed')
"

echo "=== Ready, starting uvicorn ==="
exec "$@"