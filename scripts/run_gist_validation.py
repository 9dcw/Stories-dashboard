#!/usr/bin/env python3
"""Run a representative Stage 3 gist validation batch."""
from __future__ import annotations

import argparse
import json
import os
from collections import defaultdict
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from gist import extract_html_text, fetch_url
from story_store import StoryStore
from summarize_items import command_summarizer, summarize_items


def representative_ids(store: StoryStore, count: int, accessible_only: bool = False) -> list[int]:
    rows = [row for row in store.list_items() if row["source_name"] != "Smoke Test Source"]
    if accessible_only:
        usable = []
        for row in rows:
            if "/example/" in row["raw_url"] or row["raw_url"].lower().endswith(".pdf"):
                continue
            try:
                text, _ = extract_html_text(fetch_url(row["raw_url"], timeout=8))
                if len(text.strip()) >= 40:
                    usable.append(row)
            except Exception:
                continue
            if len(usable) >= count * 2:
                break
        rows = usable
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        groups[row["source_name"]].append(row)
    selected: list[dict] = []
    group_names = sorted(groups)
    while len(selected) < min(count, len(rows)):
        progressed = False
        for name in group_names:
            if groups[name]:
                selected.append(groups[name].pop(0))
                progressed = True
                if len(selected) == count:
                    break
        if not progressed:
            break
    return [row["item_id"] for row in selected]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=ROOT / "story-data" / "stories.sqlite3")
    parser.add_argument("--count", type=int, default=40)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--accessible-only", action="store_true", help="preflight ordinary HTML pages and skip unavailable fixtures")
    args = parser.parse_args()
    command = os.environ.get("STORY_GIST_COMMAND")
    if not command:
        raise SystemExit("STORY_GIST_COMMAND must be configured")
    store = StoryStore(args.db)
    ids = representative_ids(store, args.count, accessible_only=args.accessible_only)
    result = summarize_items(store, item_ids=ids, force=args.force, summarizer=command_summarizer(command))
    output = {**result, "selected_item_ids": ids}
    print(json.dumps(output, separators=(",", ":")))


if __name__ == "__main__":
    main()
