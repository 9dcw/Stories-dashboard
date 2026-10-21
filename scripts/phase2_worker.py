#!/usr/bin/env python3
"""Run manually requested research jobs; never promote or schedule stories."""
from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path
import signal
import sys
import threading

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

from phase2.google_docs import GoogleDocs
from phase2.hermes_adapter import HermesAdapter
from phase2.orchestrator import Worker
from phase2.store import WorkflowStore
from phase2.telegram import Telegram


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--once',action='store_true')
    args=parser.parse_args()
    required=['STORIES_DB_PATH','PHASE2_GOOGLE_PARENT_ID','PHASE2_GOOGLE_TOKEN','TELEGRAM_BOT_TOKEN','PHASE2_HERMES_PYTHON','PHASE2_HERMES_SOURCE']
    if any(not os.environ.get(key) for key in required):
        parser.error('Missing worker configuration; see docs/phase2-workflow.md')
    if os.environ.get('PHASE2_ENGINE')!='repository':
        parser.error('Worker requires PHASE2_ENGINE=repository')
    store=WorkflowStore(os.environ['STORIES_DB_PATH'])
    store.initialize()
    google=GoogleDocs.authenticated(os.environ['PHASE2_GOOGLE_TOKEN'],os.environ['PHASE2_GOOGLE_PARENT_ID'])
    research=HermesAdapter(os.environ['PHASE2_HERMES_PYTHON'],os.environ['PHASE2_HERMES_SOURCE'])
    telegram=Telegram(os.environ['TELEGRAM_BOT_TOKEN'],os.environ.get('PHASE2_CHAT_ID','-1003984316484'))
    worker=Worker(store,research,google,telegram,os.environ.get('PHASE2_LOCK_DIR',str(Path(store.path).parent/'phase2-locks')))
    if args.once:
        worker.tick()
        return
    stop=threading.Event()
    for sig in [signal.SIGTERM,signal.SIGINT]:
        signal.signal(sig,lambda *_:stop.set())
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
    worker.run(stop)


if __name__=='__main__':
    main()
