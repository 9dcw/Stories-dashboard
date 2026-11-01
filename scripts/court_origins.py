#!/usr/bin/env python3
"""Report observed originating courts; optionally backfill cached captions only."""
import argparse
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from story_store import StoryStore
from originating_courts import VERSION, activity_report, save_courts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, required=True)
    parser.add_argument('--backfill', action='store_true')
    args = parser.parse_args()
    if not args.db.is_file():parser.error('Database must already exist')
    store = StoryStore(args.db)
    changed = 0
    if args.backfill:
        with store._connect() as c:
            rows = list(c.execute('''SELECT d.item_id,d.document_json,s.jurisdiction,o.identity_json
                FROM court_documents d JOIN items i USING(item_id) JOIN sources s USING(source_id)
                LEFT JOIN court_origin_identity o USING(item_id)'''))
            for row in rows:
                if row['identity_json'] and json.loads(row['identity_json']).get('version') == VERSION:continue
                save_courts(c, row['item_id'], json.loads(row['document_json'])['pages'], row['jurisdiction'])
                changed += 1
    print(json.dumps({'backfilled_documents':changed, **activity_report(store)}, indent=2))


if __name__ == '__main__':main()
