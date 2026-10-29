#!/usr/bin/env python3
"""Single daily court runner. Never sends messages or promotes stories."""
from __future__ import annotations
import argparse
import fcntl
import json
import os
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from court_pipeline import run_pipeline
from story_store import StoryStore
from summarize_items import command_summarizer

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--db',type=Path,required=True)
    p.add_argument('--limit',type=int)
    p.add_argument('--no-poll',action='store_true')
    p.add_argument('--source',type=int,action='append',help='Limit polling and screening to these source IDs')
    args=p.parse_args()
    if args.limit is not None and args.limit < 1:p.error('--limit must be positive')
    if args.source and any(s<1 for s in args.source):p.error('--source must be positive')
    lock_path=args.db.parent/'court-daily.lock'
    with lock_path.open('a') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            print(json.dumps({'status':'ALREADY_RUNNING'}));return
        os.environ['STORY_GIST_TOOLSETS']='none'
        command=os.environ.get('STORY_GIST_COMMAND',f'{sys.executable} {ROOT / "scripts/hermes_gist_command.py"}')
        result=run_pipeline(StoryStore(args.db),command_summarizer(command),limit=args.limit,poll=not args.no_poll,source_ids=args.source)
        print(json.dumps(result),flush=True)
if __name__=='__main__':main()
