#!/usr/bin/env python3
"""Audit saved NEW court candidates, then reversibly apply insurance exclusions."""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from insurance_screening import POLICY_VERSION, apply_result, classify_batch, combine, segments
from story_store import utc_now
from summarize_items import command_summarizer


def classify_with_retry(batch, agent):
    try:
        return classify_batch(batch, agent)
    except (ValueError, RuntimeError):
        # Some model replies coalesce multiple segments of the same case.
        # Retry each segment independently rather than accept a partial read.
        results = {}
        for part in batch:
            results.update(classify_batch([part], agent))
        return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, required=True)
    parser.add_argument('--audit', type=Path, required=True)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--workers', type=int, default=4)
    args = parser.parse_args()
    with sqlite3.connect(f'file:{args.db}?mode=ro', uri=True) as connection:
        connection.row_factory = sqlite3.Row
        rows = [dict(row) for row in connection.execute('''
            SELECT i.item_id,i.headline,cs.result_json,cs.content_hash,d.document_json
            FROM items i JOIN sources s USING(source_id)
            JOIN court_screenings cs USING(item_id)
            LEFT JOIN court_documents d USING(item_id)
            WHERE i.status='NEW' AND s.lane='B' AND cs.decision='PASS'
            ORDER BY i.item_id''')]
    saved = {}
    if args.audit.exists():
        for line in args.audit.read_text().splitlines():
            record = json.loads(line)
            saved[(record['key'], record['content_hash'])] = record
    pending, item_parts = [], {}
    for row in rows:
        if not row['document_json']:
            raise ValueError(f"Missing full opinion for item {row['item_id']}")
        parts = segments(row['item_id'], json.loads(row['document_json']))
        item_parts[row['item_id']] = parts
        for part in parts:
            if (part['key'], row['content_hash']) not in saved:
                pending.append(part | {'content_hash': row['content_hash']})
    if not args.apply:
        batches, batch, size = [], [], 0
        for part in pending:
            length = sum(len(p['text']) for p in part['pages'])
            if batch and size + length > 70000:
                batches.append(batch)
                batch, size = [], 0
            batch.append(part)
            size += length
        if batch:
            batches.append(batch)
        os.environ.update(STORY_GIST_TOOLSETS='none', STORY_GIST_MODEL='gpt-5.6-luna', STORY_GIST_PROVIDER='openai-codex')
        agent = command_summarizer(f'{sys.executable} {ROOT / "scripts/hermes_gist_command.py"}')
        print(json.dumps({'candidates':len(rows),'segments_pending':len(pending),'batches':len(batches)}), flush=True)
        with args.audit.open('a') as output, ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = {executor.submit(classify_with_retry, batch, agent): batch for batch in batches}
            for future in as_completed(futures):
                batch = futures[future]
                try:
                    results = future.result()
                except Exception as error:
                    print(json.dumps({'error':str(error)[:500],'keys':[p['key'] for p in batch]}),flush=True)
                    continue
                for part in batch:
                    record = results[part['key']] | {'content_hash':part['content_hash'], 'policy_version':POLICY_VERSION}
                    output.write(json.dumps(record) + '\n')
                    saved[(part['key'], part['content_hash'])] = record
                output.flush()
                print(json.dumps({'segments_completed':len(saved)}),flush=True)
    decisions, incomplete = {}, []
    for row in rows:
        parts = item_parts[row['item_id']]
        values = [saved.get((p['key'], row['content_hash'])) for p in parts]
        if any(value is None or value.get('policy_version') != POLICY_VERSION for value in values):
            incomplete.append(row['item_id'])
        else:
            decisions[row['item_id']] = combine(values)
    counts = {status:sum(v['involvement']==status for v in decisions.values()) for status in ['YES','NO','UNCERTAIN']}
    print(json.dumps({'classification':counts,'incomplete':incomplete}),flush=True)
    if args.apply:
        if incomplete:
            raise ValueError('Complete the audit before applying exclusions')
        originals = []
        with (args.db.parent / 'court-daily.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with sqlite3.connect(args.db) as connection:
                connection.row_factory = sqlite3.Row
                connection.execute('BEGIN IMMEDIATE')
                for row in rows:
                    current = connection.execute('''SELECT cs.* FROM court_screenings cs
                        JOIN items i USING(item_id) WHERE cs.item_id=? AND i.status='NEW'
                        AND cs.decision='PASS' AND cs.content_hash=?''', (row['item_id'],row['content_hash'])).fetchone()
                    if current is None or current['result_json'] != row['result_json']:
                        raise ValueError('Candidate changed during audit; rerun')
                    originals.append(dict(current))
                    result = apply_result(json.loads(current['result_json']), decisions[row['item_id']])
                    connection.execute('''UPDATE court_screenings SET decision=?,reason_code=?,needs_review=?,
                        result_json=?,screened_at=? WHERE item_id=?''',
                        (result['decision'],result.get('reason_code',''),int(result.get('needs_review',False)),
                         json.dumps(result),utc_now(),row['item_id']))
                errors = connection.execute('''SELECT cs.* FROM court_screenings cs
                    JOIN items i USING(item_id) JOIN sources s USING(source_id)
                    WHERE i.status='NEW' AND s.lane='B' AND cs.decision='ERROR' ''').fetchall()
                for current in errors:
                    originals.append(dict(current))
                    insurance = {'policy_version':POLICY_VERSION,'involvement':'UNCERTAIN',
                                 'segments':[], 'reason':'Opinion could not be read or screened'}
                    result = apply_result(json.loads(current['result_json']),insurance)
                    connection.execute('''UPDATE court_screenings SET decision=?,reason_code=?,needs_review=1,
                        result_json=?,screened_at=? WHERE item_id=?''',
                        (result['decision'],result['reason_code'],json.dumps(result),utc_now(),current['item_id']))
                rollback = args.audit.with_suffix('.rollback.json')
                if rollback.exists():
                    raise ValueError('Rollback file already exists; preserve it and use a new audit path')
                rollback.write_text(json.dumps(originals,indent=2))
        print(json.dumps({'applied':len(originals),'unreadable_withheld':len(errors),'rollback':str(rollback)}),flush=True)


if __name__ == '__main__':
    main()
