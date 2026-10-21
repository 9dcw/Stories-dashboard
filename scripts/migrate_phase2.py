#!/usr/bin/env python3
"""Inventory by default; --apply adopts an explicitly reconciled identity manifest."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from phase2.migration import inventory, verify_entry, adopt
from phase2.store import WorkflowStore


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db',required=True,type=Path)
    parser.add_argument('--manifest',type=Path)
    parser.add_argument('--apply',action='store_true')
    args=parser.parse_args()
    if not args.apply:
        print(json.dumps(inventory(args.db),indent=2))
        return
    if not args.manifest:
        parser.error('--apply requires a reviewed identity manifest')
    from phase2.google_docs import GoogleDocs
    from phase2.telegram import Telegram
    store=WorkflowStore(args.db)
    store.initialize()
    google=GoogleDocs.authenticated(os.environ['PHASE2_GOOGLE_TOKEN'],os.environ['PHASE2_GOOGLE_PARENT_ID'])
    telegram=Telegram(os.environ['TELEGRAM_BOT_TOKEN'],os.environ.get('PHASE2_CHAT_ID','-1003984316484'))
    entries=json.loads(args.manifest.read_text())['stories']
    # Verify every entry before changing a mapping. Conflicts are explicit, never inferred away.
    verified=[verify_entry(store,e,google,telegram.chat_id) for e in entries]
    for entry in entries:
        telegram.verify_topic(entry['thread_id'])
    for entry,proof in zip(entries,verified):
        adopt(store,entry,proof)
        run=store.enqueue(entry['story_id'],'migration:'+str(entry['story_id']),operation='migrate',requester='reviewed-migration')
        print(json.dumps({'story_id':entry['story_id'],'run_id':run}))


if __name__=='__main__':
    main()
