#!/usr/bin/env python3
"""Small deterministic CLI for the Stage 1 story inventory."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from export_stories import export_json
from story_store import StoryStore

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "story-data" / "stories.sqlite3"
DEFAULT_JSON = ROOT / "data" / "stories.json"


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", type=Path, default=DEFAULT_DB)
    sub = p.add_subparsers(dest="command", required=True)
    source = sub.add_parser("add-source")
    source.add_argument("name"); source.add_argument("lane"); source.add_argument("jurisdiction"); source.add_argument("source_url")
    source.add_argument("--source-type", default="web"); source.add_argument("--notes", default="")
    source.add_argument("--collector-type", default="")
    source.add_argument("--poll-url")
    source.add_argument("--collector-config", default="{}")
    item = sub.add_parser("add-item")
    item.add_argument("source_id", type=int); item.add_argument("headline"); item.add_argument("raw_url"); item.add_argument("--published-at")
    listing = sub.add_parser("list-items"); listing.add_argument("--limit", type=int, default=25); listing.add_argument("--status")
    status = sub.add_parser("update-status"); status.add_argument("item_id", type=int); status.add_argument("status", choices=["NEW", "PROMOTED", "IGNORED"])
    promote = sub.add_parser("promote"); promote.add_argument("item_id", type=int); promote.add_argument("title"); promote.add_argument("--status", default="ACTIVE")
    links = sub.add_parser("update-links"); links.add_argument("story_id", type=int); links.add_argument("--google-doc-url"); links.add_argument("--telegram-thread-url"); links.add_argument("--research-folder-url"); links.add_argument("--status")
    export = sub.add_parser("export"); export.add_argument("--output", type=Path, default=DEFAULT_JSON); export.add_argument("--generated-at")
    proposals = sub.add_parser("source-proposals"); proposals.add_argument("--status")
    approve = sub.add_parser("approve-source"); approve.add_argument("candidate_id", type=int)
    reject = sub.add_parser("reject-source"); reject.add_argument("candidate_id", type=int)
    enroll = sub.add_parser("enroll-source"); enroll.add_argument("candidate_id", type=int)
    coverage = sub.add_parser("source-coverage")
    disable = sub.add_parser("disable-source"); disable.add_argument("source_id", type=int)
    enable = sub.add_parser("enable-source"); enable.add_argument("source_id", type=int)
    return p


def main() -> None:
    args = parser().parse_args()
    store = StoryStore(args.db)
    if args.command == "add-source":
        print(store.add_source(args.name, args.lane, args.jurisdiction, args.source_url, args.source_type, notes=args.notes, collector_type=args.collector_type, poll_url=args.poll_url, collector_config=args.collector_config))
    elif args.command == "add-item":
        print(store.add_item(args.source_id, args.headline, args.raw_url, args.published_at).item_id)
    elif args.command == "list-items":
        for row in store.list_items(args.limit, args.status):
            print(f'{row["item_id"]}\t{row["status"]}\t{row["source_name"]}\t{row["headline"]}\t{row["raw_url"]}')
    elif args.command == "update-status":
        store.update_item_status(args.item_id, args.status)
    elif args.command == "promote":
        print(store.promote_item(args.item_id, args.title, args.status))
    elif args.command == "update-links":
        store.update_story_links(args.story_id, google_doc_url=args.google_doc_url, telegram_thread_url=args.telegram_thread_url, research_folder_url=args.research_folder_url, status=args.status)
    elif args.command == "export":
        print(export_json(args.db, args.output, args.generated_at))
    elif args.command == "source-proposals":
        import json
        print(json.dumps(store.list_source_candidates(args.status), separators=(",", ":")))
    elif args.command == "approve-source":
        store.update_source_candidate(args.candidate_id, status="APPROVED"); print(args.candidate_id)
    elif args.command == "reject-source":
        store.update_source_candidate(args.candidate_id, status="REJECTED"); print(args.candidate_id)
    elif args.command == "enroll-source":
        print(store.enroll_source_candidate(args.candidate_id))
    elif args.command == "source-coverage":
        import json
        print(json.dumps(store.coverage_report(), separators=(",", ":")))
    elif args.command == "disable-source":
        store.set_source_active(args.source_id, False)
    elif args.command == "enable-source":
        store.set_source_active(args.source_id, True)


if __name__ == "__main__":
    main()
