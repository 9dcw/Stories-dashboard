"""Export the SQLite system of record to the GitHub Pages read model."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from story_store import StoryStore


def export_json(db_path: str | Path, output_path: str | Path, generated_at: str | None = None) -> Path:
    store = StoryStore(db_path)
    candidates = []
    for row in store.list_items():
        candidates.append({
            "item_id": row["item_id"],
            "headline": row["headline"],
            "display_headline": row["display_headline"],
            "source": row["source_name"],
            "lane": row["lane"],
            "jurisdiction": row["jurisdiction"],
            "date": row["published_at"] or row["first_seen_at"],
            "source_link": row["raw_url"],
            "status": row["status"],
            "gist": row["gist"],
            "summary_status": row["summary_status"],
            "summary_prompt_version": row["summary_prompt_version"],
            "summarized_at": row["summarized_at"],
        })
    stories = []
    for row in store.list_stories():
        stories.append({
            "story_id": row["story_id"],
            "title": row["title"],
            "status": row["status"],
            "source": row["source_name"],
            "origin_item_id": row["origin_item_id"],
            "origin_url": row["origin_url"],
            "google_doc_url": row["google_doc_url"],
            "telegram_thread_url": row["telegram_thread_url"],
            "research_folder_url": row["research_folder_url"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        })
    sources = [
        {
            "source_id": row["source_id"],
            "name": row["name"],
            "lane": row["lane"],
            "jurisdiction": row["jurisdiction"],
            "source_url": row["source_url"],
            "source_type": row["source_type"],
            "active": bool(row["active"]),
        }
        for row in store.list_sources()
    ]
    payload = {
        "generated_at": generated_at or datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "recent_candidates": candidates,
        "stories": stories,
        "sources": sources,
    }
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    return destination


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("db", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    print(export_json(args.db, args.output))
