#!/usr/bin/env python3
"""Check the scheduled court poll from the host without changing its database."""
import argparse
from datetime import datetime, timedelta, timezone
import fcntl
import json
from pathlib import Path
import sqlite3
from zoneinfo import ZoneInfo

START_GRACE = timedelta(minutes=45)
RUN_LIMIT = timedelta(hours=8)


def runner_active(path):
    with path.open('rb') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        return False


def check(db, launcher, now):
    if not launcher.is_file():
        return 'unhealthy', f'Court intake launcher is missing: {launcher}'
    active = runner_active(db.parent / 'court-daily.lock')
    with sqlite3.connect(f'{db.resolve().as_uri()}?mode=ro', uri=True, timeout=5) as conn:
        schedule = conn.execute('SELECT enabled, timezone, hour, minute FROM court_schedule WHERE singleton=1').fetchone()
        if not schedule or not schedule[0]:
            return 'unhealthy', 'Daily court intake schedule is disabled or missing'
        local = now.astimezone(ZoneInfo(schedule[1]))
        due = local.replace(hour=schedule[2], minute=schedule[3], second=0, microsecond=0)
        if now < due.astimezone(timezone.utc) + START_GRACE:
            due -= timedelta(days=1)
        due = due.astimezone(timezone.utc)
        # Include failed starts with zero source counters and skip successful
        # screening-only replays, which cannot satisfy the daily poll.
        run = conn.execute("""SELECT run_id, started_at, finished_at, status, stats_json
            FROM court_runs
            WHERE status IN ('RUNNING','FAILED','INTERRUPTED')
              OR COALESCE(json_extract(stats_json,'$.sources_checked'),0)>0
            ORDER BY run_id DESC LIMIT 1""").fetchone()
    if run is None:
        return 'unhealthy', f'No court poll recorded for the {due.isoformat()} schedule'
    run_id, started, finished, state, raw_stats = run
    started = datetime.fromisoformat(started)
    stats = json.loads(raw_stats)
    # Allow an early catch-up on the same local calendar day.
    window = due.astimezone(local.tzinfo).replace(hour=0, minute=0).astimezone(timezone.utc)
    if started < window:
        return 'unhealthy', f'Court poll missed for {due.date()}; latest run {run_id} started {started.isoformat()}'
    if state == 'RUNNING':
        if now - started > RUN_LIMIT:
            return 'unhealthy', f'Court run {run_id} has been running for more than eight hours'
        if not active:
            return 'unhealthy', f'Court run {run_id} is recorded as running but no runner holds the lock'
        return 'running', f'Court run {run_id} is active; {stats.get("sources_checked", 0)} sources checked'
    if state not in {'COMPLETE', 'COMPLETE_WITH_FAILURES', 'PARTIAL'} or not finished:
        return 'unhealthy', f'Court run {run_id} ended with status {state}'
    if stats.get('sources_failed', 0) >= stats.get('sources_checked', 0):
        return 'unhealthy', f'Every source failed in court run {run_id}'
    return 'healthy', f'Court run {run_id} finished {finished}; {stats.get("sources_checked", 0)} sources checked, {stats.get("sources_failed", 0)} failed'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, required=True)
    parser.add_argument('--launcher', type=Path, required=True)
    args = parser.parse_args()
    try:
        status, message = check(args.db, args.launcher, datetime.now(timezone.utc))
    except (OSError, ValueError, sqlite3.Error, KeyError) as exc:
        status, message = 'unhealthy', f'Court intake check could not read operational state: {exc}'
    print(f'{status.upper()}: {message}')
    return int(status == 'unhealthy')


if __name__ == '__main__':
    raise SystemExit(main())
