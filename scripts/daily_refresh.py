#!/usr/bin/env python3
"""Run SQLite source polling and Stage 3 gisting without deploying a site.

The process intentionally prints only compact JSON statistics. Article bodies and
model prompts are kept inside this process and the Hermes adapter subprocess.
"""
from __future__ import annotations

import argparse
import json
import os

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from polling import poll_sources
from story_store import StoryStore
from summarize_items import command_summarizer, summarize_items


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=ROOT / "story-data" / "stories.sqlite3")

    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--retry-delay", type=float, default=2.0)
    parser.add_argument("--limit", type=int, help="limit gisting for a bounded validation run; daily default is all pending items")

    parser.add_argument("--log-file", type=Path, default=ROOT / "story-data" / "daily-refresh.jsonl")
    args = parser.parse_args(argv)
    if args.max_attempts < 1 or args.max_attempts > 5:
        parser.error("--max-attempts must be between 1 and 5")

    command = os.environ.get(
        "STORY_GIST_COMMAND",
        f"python3 {ROOT / 'scripts' / 'hermes_gist_command.py'}",
    )
    store = StoryStore(args.db)
    poll = poll_sources(store, retry_attempts=args.max_attempts, retry_delay=args.retry_delay)
    summary = {"attempts": 0, "items_checked": 0, "summaries_created": 0, "summaries_failed": 0}
    summarizer = command_summarizer(command)
    pending = store.list_unsummarized_items(limit=args.limit, active_only=True)
    item_ids = [row["item_id"] for row in pending]
    for attempt in range(args.max_attempts):
        current = summarize_items(store, item_ids=item_ids, summarizer=summarizer)
        summary["attempts"] += 1
        for key in ("items_checked", "summaries_created", "summaries_failed"):
            summary[key] += current[key]
        if current["summaries_failed"] == 0:
            break
        if attempt + 1 < args.max_attempts:
            time.sleep(args.retry_delay)
    summary["remaining_unsummarized"] = len(store.list_unsummarized_items(active_only=True))
    stats = {"poll": poll, "summaries": summary}
    args.log_file.parent.mkdir(parents=True, exist_ok=True)
    with args.log_file.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(stats, separators=(",", ":")) + "\n")
    print(json.dumps(stats, separators=(",", ":")))


if __name__ == "__main__":
    main()