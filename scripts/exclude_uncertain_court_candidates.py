#!/usr/bin/env python3
"""Apply the v4 uncertainty policy to saved NEW court candidates, without model calls."""
import argparse
import fcntl
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from court_pipeline import RULE_VERSION
from story_store import utc_now


def migrate(connection):
    rows = connection.execute('''SELECT cs.item_id,cs.result_json,cs.rule_version,cs.screened_at
        FROM court_screenings cs JOIN items i USING(item_id) JOIN sources s USING(source_id)
        WHERE i.status='NEW' AND s.lane='B' AND cs.decision='PASS' AND cs.needs_review=1''').fetchall()
    for item_id, raw, previous_version, previous_time in rows:
        previous = json.loads(raw)
        result = previous | {
            'decision': 'EXCLUDED', 'reason_code': 'SCREENING_UNCERTAIN',
            'reason': 'Uncertain screening; excluded from candidate feed',
            'needs_review': True,
            'policy_migration': {'previous_rule_version': previous_version,
                                 'previous_screened_at': previous_time,
                                 'previous_result': previous},
        }
        connection.execute('''UPDATE court_screenings SET decision='EXCLUDED',
            reason_code='SCREENING_UNCERTAIN',result_json=?,rule_version=?,screened_at=?
            WHERE item_id=?''', (json.dumps(result), RULE_VERSION, utc_now(), item_id))
    return len(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    with (args.db.parent / 'court-daily.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with sqlite3.connect(args.db) as connection:
            connection.execute('BEGIN IMMEDIATE')
            count = migrate(connection)
            if not args.apply:
                connection.rollback()
        print(json.dumps({'affected': count, 'applied': args.apply, 'rule_version': RULE_VERSION}))


if __name__ == '__main__':
    main()
