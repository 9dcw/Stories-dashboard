#!/usr/bin/env python3
"""Install or refresh the representative Stage 2 source registry."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from polling_sources import STAGE2_SOURCES
from story_store import StoryStore, canonicalize_url

ROOT = Path(__file__).resolve().parent.parent


def configure(db_path: str | Path = ROOT / "story-data" / "stories.sqlite3") -> list[int]:
    store = StoryStore(db_path)
    existing = {row["name"]: row for row in store.list_sources()}
    ids = []
    for name, lane, jurisdiction, source_url, collector_type, poll_url, collector_config in STAGE2_SOURCES:
        row = existing.get(name)
        if row:
            store.update_source_collector(row["source_id"], source_url=source_url, collector_type=collector_type, poll_url=poll_url, collector_config=collector_config)
            ids.append(row["source_id"])
        else:
            ids.append(store.add_source(name, lane, jurisdiction, source_url, collector_type=collector_type, poll_url=poll_url, collector_config=collector_config, notes="Stage 2 representative source"))
    return ids


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=ROOT / "story-data" / "stories.sqlite3")
    args = parser.parse_args()
    print(f"Configured {len(configure(args.db))} Stage 2 sources")
