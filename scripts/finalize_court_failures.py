#!/usr/bin/env python3
"""Dry-run by default; --apply closes historical uncertainty as explicit failure."""
import argparse,json,sys,fcntl
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from court_failures import finalize_existing
from story_store import StoryStore
p=argparse.ArgumentParser(description=__doc__)
p.add_argument('--db',type=Path,required=True);p.add_argument('--apply',action='store_true');a=p.parse_args()
with (a.db.parent/'court-daily.lock').open('a') as lock:
    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    print(json.dumps(finalize_existing(StoryStore(a.db),apply=a.apply)))
