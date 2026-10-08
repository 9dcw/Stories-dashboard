#!/usr/bin/env python3
"""Run and review the independent Stage 4 source-discovery process."""
from __future__ import annotations
import argparse, json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from source_discovery import discovery_prompt, load_json, run_discovery
from story_store import StoryStore, utc_now

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "story-data" / "stories.sqlite3"
TARGETS = ROOT / "discovery" / "coverage_targets.json"

def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__); p.add_argument("--db",type=Path,default=DEFAULT_DB)
    sub=p.add_subparsers(dest="command",required=True)
    run=sub.add_parser("run"); run.add_argument("--input",type=Path,required=True); run.add_argument("--target-limit",type=int,default=10); run.add_argument("--no-validate",action="store_true")
    prompt=sub.add_parser("prompt"); prompt.add_argument("--target-limit",type=int,default=10)
    ls=sub.add_parser("list"); ls.add_argument("--status")
    for name in ("approve","reject","test","enroll"):
        cmd=sub.add_parser(name); cmd.add_argument("candidate_id",type=int)
    sub.add_parser("coverage")
    args=p.parse_args(argv); store=StoryStore(args.db)
    if args.command == "run":
        targets=json.loads(TARGETS.read_text()) if TARGETS.exists() else []
        store.seed_discovery_targets(targets)
        print(json.dumps(run_discovery(store, load_json(args.input), validate=not args.no_validate), separators=(",",":")))
    elif args.command == "prompt":
        targets=json.loads(TARGETS.read_text()) if TARGETS.exists() else []
        store.seed_discovery_targets(targets)
        print(discovery_prompt(store.select_discovery_targets(args.target_limit)))
    elif args.command == "list":
        print(json.dumps(store.list_source_candidates(args.status), separators=(",",":")))
    elif args.command == "approve":
        store.update_source_candidate(args.candidate_id,status="APPROVED"); print(args.candidate_id)
    elif args.command == "reject":
        store.update_source_candidate(args.candidate_id,status="REJECTED"); print(args.candidate_id)
    elif args.command == "test":
        from source_discovery import validate_source
        row=store.get_source_candidate(args.candidate_id); result=validate_source(row)
        store.update_source_candidate(args.candidate_id,validation_status=result["status"],validation_result=result); print(json.dumps(result,separators=(",",":")))
    elif args.command == "enroll": print(store.enroll_source_candidate(args.candidate_id))
    elif args.command == "coverage": print(json.dumps(store.coverage_report(),separators=(",",":")))

if __name__ == "__main__": main()
