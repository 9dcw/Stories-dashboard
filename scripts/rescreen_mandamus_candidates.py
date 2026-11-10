#!/usr/bin/env python3
"""Re-screen cached passing candidates for the new mandamus exclusion only."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import fcntl
import json
import os
from pathlib import Path
import re
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from court_pipeline import RULE_VERSION,screen_document
from story_store import StoryStore,utc_now
from summarize_items import command_summarizer


def candidates(connection):
    rows=connection.execute("""SELECT i.*,s.name AS source_name,d.document_json
       FROM items i JOIN sources s USING(source_id) JOIN court_screenings cs USING(item_id)
       JOIN court_documents d USING(item_id)
       WHERE i.status='NEW' AND s.lane='B' AND s.active=1 AND cs.decision='PASS'
       ORDER BY i.item_id""").fetchall()
    return [dict(r) for r in rows if re.search(r'\bmandamus\b',r['headline']+'\n'+'\n'.join(json.loads(r['document_json'])['pages'][:2]),re.I)]


def apply_result(connection,item_id,result,content_hash):
    if result.get('decision')!='EXCLUDED' or result.get('reason_code')!='MANDAMUS' or result.get('needs_review') or not result.get('evidence'):
        return False
    row=connection.execute("SELECT cs.*,i.status FROM court_screenings cs JOIN items i USING(item_id) WHERE item_id=?",(item_id,)).fetchone()
    if not row or row['status']!='NEW' or row['decision']!='PASS' or row['content_hash'] and row['content_hash']!=content_hash:return False
    value=dict(result,policy_migration=dict(previous_rule_version=row['rule_version'],previous_result=json.loads(row['result_json']),migration='mandamus-only cached replay'))
    connection.execute("UPDATE court_screenings SET decision='EXCLUDED',reason_code='MANDAMUS',needs_review=0,result_json=?,content_hash=?,rule_version=?,screened_at=? WHERE item_id=?",(json.dumps(value),content_hash,RULE_VERSION,utc_now(),item_id))
    return True


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db',type=Path,required=True)
    parser.add_argument('--apply',action='store_true')
    parser.add_argument('--limit',type=int)
    parser.add_argument('--report',type=Path)
    args=parser.parse_args()
    if args.limit is not None and args.limit<1:parser.error('--limit must be positive')
    store=StoryStore(args.db)
    with store._connect() as connection:items=candidates(connection)
    if args.limit is not None:items=items[:args.limit]
    print(json.dumps(dict(selected=len(items),apply=args.apply)),flush=True)
    if not args.apply:return
    with (args.db.parent/'court-daily.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        os.environ['STORY_GIST_TOOLSETS']='none'
        agent=command_summarizer(os.environ.get('STORY_GIST_COMMAND',f'{sys.executable} {ROOT / "scripts/hermes_gist_command.py"}'))
        def classify(item):
            try:
                document=json.loads(item['document_json'])
                return item['item_id'],screen_document(item,document,agent),document['content_hash']
            except Exception as exc:return item['item_id'],dict(error_type=type(exc).__name__),''
        records=[];excluded=0
        with ThreadPoolExecutor(max_workers=2) as pool:
            for item_id,result,content_hash in pool.map(classify,items):
                with store._connect() as connection:changed=apply_result(connection,item_id,result,content_hash)
                excluded+=int(changed)
                records.append(dict(item_id=item_id,applied=changed,result=result))
                if len(records)%10==0:print(json.dumps(dict(processed=len(records),excluded=excluded,total=len(items))),flush=True)
                if args.report:
                    args.report.write_text(json.dumps(records,indent=2)+'\n');args.report.chmod(0o600)
        print(json.dumps(dict(processed=len(records),excluded=excluded,errors=sum('error_type' in r['result'] for r in records))),flush=True)


if __name__=='__main__':main()
