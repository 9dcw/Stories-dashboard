#!/usr/bin/env python3
"""Create a timestamped backup of the SQLite database outside the container."""
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path("/srv/stories-dashboard/story-data/stories.sqlite3")
BACKUP_DIR = Path("/srv/stories-dashboard/backups")
KEEP_GENERATIONS = 30

def backup():
    if not DB_PATH.exists():
        print(f"Database not found: {DB_PATH}", file=sys.stderr)
        sys.exit(1)

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = BACKUP_DIR / f"stories-{stamp}.sqlite3"

    import sqlite3
    source = sqlite3.connect(DB_PATH)
    target = sqlite3.connect(dest)

    source.backup(target)
    target.close()
    source.close()

    # Integrity check on backup
    verify = sqlite3.connect(dest)
    verify.execute("PRAGMA integrity_check")
    verify.close()

    print(f"Backup: {dest} ({dest.stat().st_size} bytes)")

    # Prune old backups
    backups = sorted(BACKUP_DIR.glob("stories-*.sqlite3"))
    for old in backups[:-KEEP_GENERATIONS]:
        old.unlink()
        print(f"Pruned: {old}")

if __name__ == "__main__":
    backup()