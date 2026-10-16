#!/usr/bin/env python3
"""Create and verify a timestamped online SQLite backup."""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path("/srv/stories-dashboard/story-data/stories.sqlite3")
BACKUP_DIR = Path("/srv/stories-dashboard/backups")
KEEP_GENERATIONS = 30


def _snapshot(connection: sqlite3.Connection) -> tuple[dict[str, int], dict[str, int]]:
    tables = [
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]
    counts = {
        table: connection.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
        for table in tables
    }
    item_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(items)")
    } if "items" in tables else set()
    statuses = dict(connection.execute(
        "SELECT status, count(*) FROM items GROUP BY status ORDER BY status"
    )) if "status" in item_columns else {}
    return counts, statuses


def _verify_backup(
    destination: Path,
    expected_counts: dict[str, int],
    expected_statuses: dict[str, int],
) -> None:
    with sqlite3.connect(destination) as verify:
        integrity = verify.execute("PRAGMA integrity_check").fetchall()
        if integrity != [("ok",)]:
            raise RuntimeError(f"Backup integrity check failed: {integrity}")
        verify.execute("PRAGMA foreign_keys = ON")
        violations = verify.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RuntimeError(f"Backup foreign key check failed: {violations}")
        actual_counts, actual_statuses = _snapshot(verify)
    if actual_counts != expected_counts:
        raise RuntimeError(
            f"Backup row counts differ: expected {expected_counts}, got {actual_counts}"
        )
    if actual_statuses != expected_statuses:
        raise RuntimeError(
            f"Backup status counts differ: expected {expected_statuses}, got {actual_statuses}"
        )


def backup(
    db_path: Path = DB_PATH,
    backup_dir: Path = BACKUP_DIR,
    *,
    keep_generations: int = KEEP_GENERATIONS,
) -> Path:
    db_path = Path(db_path)
    backup_dir = Path(backup_dir)
    if not db_path.is_file():
        raise FileNotFoundError(f"Database not found: {db_path}")
    if keep_generations < 1:
        raise ValueError("keep_generations must be at least 1")

    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    destination = backup_dir / f"stories-{stamp}.sqlite3"

    try:
        with sqlite3.connect(db_path) as source:
            expected_counts, expected_statuses = _snapshot(source)
            with sqlite3.connect(destination) as target:
                source.backup(target)
        _verify_backup(destination, expected_counts, expected_statuses)
    except Exception:
        destination.unlink(missing_ok=True)
        raise

    print(f"Backup: {destination} ({destination.stat().st_size} bytes)")
    backups = sorted(backup_dir.glob("stories-*.sqlite3"))
    for old in backups[:-keep_generations]:
        old.unlink()
        print(f"Pruned: {old}")
    return destination


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DB_PATH)
    parser.add_argument("--backup-dir", type=Path, default=BACKUP_DIR)
    parser.add_argument("--keep-generations", type=int, default=KEEP_GENERATIONS)
    args = parser.parse_args()
    try:
        backup(args.db, args.backup_dir, keep_generations=args.keep_generations)
    except (FileNotFoundError, RuntimeError, ValueError) as error:
        print(error, file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
