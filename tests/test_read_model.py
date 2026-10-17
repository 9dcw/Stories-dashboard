import json

from export_stories import build_read_model, export_json
from story_store import StoryStore


def test_build_read_model_preserves_static_export_contract(tmp_path):
    db = tmp_path / "stories.sqlite3"
    store = StoryStore(db)
    source = store.add_source("Source", "A", "US", "https://source.example")
    item = store.add_item(source, "Candidate", "https://source.example/item")
    store.dismiss_item(item.item_id, "duplicate", "Same event", "sub", "email", "request")

    payload = build_read_model(store, generated_at="2026-10-16T00:00:00Z")
    output = tmp_path / "stories.json"
    export_json(db, output, generated_at="2026-10-16T00:00:00Z")

    assert json.loads(output.read_text()) == payload
    assert set(payload) == {"generated_at", "recent_candidates", "stories", "sources", "source_proposals", "coverage"}
    assert "active_dismissal" not in payload["recent_candidates"][0]
