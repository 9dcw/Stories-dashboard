"""Export the SQLite system of record to the GitHub Pages read model."""
from __future__ import annotations
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from story_store import StoryStore

def public_source_url(value: str) -> str:
    lowered = value.lower()
    if "/example/" in lowered or "legacy.example" in lowered or "smoke.example.test" in lowered:
        return ""
    return value

def export_json(db_path: str | Path, output_path: str | Path, generated_at: str | None = None) -> Path:
    store = StoryStore(db_path)
    candidates = []
    for row in store.list_items():
        if row["status"] in {"PROMOTED", "IGNORED"}: continue
        candidates.append({"item_id": row["item_id"], "headline": row["headline"], "display_headline": row["display_headline"],
            "source": row["source_name"], "lane": row["lane"], "jurisdiction": row["jurisdiction"],
            "date": row["published_at"] or row["first_seen_at"], "source_link": public_source_url(row["raw_url"]),
            "status": row["status"], "gist": row["gist"], "summary_status": row["summary_status"],
            "summary_prompt_version": row["summary_prompt_version"], "summarized_at": row["summarized_at"]})
    stories = []
    for row in store.list_stories():
        stories.append({"story_id": row["story_id"], "title": row["title"], "status": row["status"], "source": row["source_name"],
            "origin_item_id": row["origin_item_id"], "origin_url": public_source_url(row["origin_url"]),
            "google_doc_url": row["google_doc_url"], "telegram_thread_url": row["telegram_thread_url"],
            "research_folder_url": row["research_folder_url"], "created_at": row["created_at"], "updated_at": row["updated_at"]})
    export_time = generated_at or datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    sources = []
    for row in store.list_sources_for_management(as_of=export_time):
        publication_url = public_source_url(row["poll_url"] or row["source_url"])
        action = "Enable" if not row["active"] else "Disable"
        instruction = (f"{action} source {row['source_id']} ({row['name']}) in the stories-dashboard source registry. "
                       f"{'Resume future polling' if action == 'Enable' else 'Stop future polling but preserve all existing records'}. "
                       "Update and publish the dashboard.")
        telegram_action_url = "https://t.me/share/url?" + urlencode({"url": publication_url, "text": instruction})
        sources.append({"source_id": row["source_id"], "name": row["name"], "lane": row["lane"], "jurisdiction": row["jurisdiction"],
            "source_url": public_source_url(row["source_url"]), "publication_url": publication_url,
            "source_type": row["source_type"], "collector_type": row["collector_type"], "active": bool(row["active"]),
            "last_success_at": row["last_success_at"], "last_error": row["last_error"] or "",
            "publications_last_30_days": row["publications_last_30_days"],
            "telegram_action_label": f"{action} via Telegram", "telegram_action_url": telegram_action_url})
    proposals = [{"candidate_id": row["candidate_id"], "name": row["name"], "organization": row["organization"],
        "jurisdiction": row["jurisdiction"], "lane": row["lane"], "source_url": public_source_url(row["proposed_url"]),
        "publication_type": row["publication_type"] or row["collector_type"], "collector_type": row["collector_type"],
        "status": row["status"], "validation_status": row["validation_status"], "reason": row["reason"]}
        for row in store.list_source_candidates()]
    payload = {"generated_at": export_time,
        "recent_candidates": candidates, "stories": stories, "sources": sources,
        "source_proposals": proposals, "coverage": store.coverage_report()}
    destination = Path(output_path); destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    return destination

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument("db", type=Path); parser.add_argument("output", type=Path)
    args = parser.parse_args(); print(export_json(args.db, args.output))
