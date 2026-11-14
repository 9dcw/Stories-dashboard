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
    assert set(payload) == {"generated_at", "recent_candidates", "stories", "sources", "source_proposals", "coverage", "court_pipeline", "court_inventory", "originating_court_activity"}
    assert "active_dismissal" not in payload["recent_candidates"][0]


def test_live_model_omits_unused_payload_and_preserves_screening_evidence(tmp_path, monkeypatch):
    from api.read_model import build_live_read_model
    import court_pipeline
    import originating_courts

    store = StoryStore(tmp_path / "stories.sqlite3")
    source = store.add_source("Source", "A", "US", "https://source.example")
    item = store.add_item(source, "Candidate", "https://source.example/item")
    screening = dict(decision="PASS", reason="Relevant", reason_code="retained", needs_review=True,
                     evidence=[dict(page=2, quote="Evidence remains visible")],
                     rule_version="v1", screened_at="2026-11-14", unused_raw_data="x" * 10000)
    monkeypatch.setattr(court_pipeline, "read_court_state", lambda _: ({item.item_id: screening}, {}, {}))
    def unused_report(*args):
        raise AssertionError("Live requests must not build the unused court activity report")
    monkeypatch.setattr(originating_courts, "load_courts", unused_report)
    monkeypatch.setattr(originating_courts, "activity_report", unused_report)
    model = build_live_read_model(store)
    candidate = model["recent_candidates"][0]
    assert candidate["screening"] == {k: v for k, v in screening.items() if k != "unused_raw_data"}
    assert candidate["appeal_documents"][0]["item_id"] == item.item_id
    assert "dismissal_action_url" not in candidate
    assert "originating_court_activity" not in model
