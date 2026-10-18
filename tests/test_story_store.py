import json
import sqlite3
from pathlib import Path

import pytest

from story_store import StoryStore, canonicalize_url, url_hash
from export_stories import export_json


def test_canonicalize_url_removes_tracking_and_fragment():
    assert canonicalize_url(
        "HTTPS://Example.COM/article/?utm_source=newsletter&ref=home&id=42#comments"
    ) == "https://example.com/article?id=42&ref=home"


def test_duplicate_urls_are_rejected_after_normalization(tmp_path):
    store = StoryStore(tmp_path / "stories.sqlite3")
    source_id = store.add_source(
        name="Example News", lane="A", jurisdiction="US", source_url="https://example.com/feed"
    )

    first = store.add_item(
        source_id=source_id,
        headline="A candidate",
        raw_url="https://Example.com/story/?utm_medium=email",
        published_at="2026-10-01",
    )
    second = store.add_item(
        source_id=source_id,
        headline="Same candidate",
        raw_url="https://example.com/story/",
        published_at="2026-10-01",
    )

    assert first.item_id > 0
    assert second.item_id == first.item_id
    assert len(store.list_items()) == 1


def test_promote_and_update_links(tmp_path):
    store = StoryStore(tmp_path / "stories.sqlite3")
    source_id = store.add_source("Court", "B", "US", "https://court.example")
    item_id = store.add_item(source_id, "Court order", "https://court.example/order")

    story_id = store.promote_item(item_id, title="Court order story")
    store.update_story_links(
        story_id,
        google_doc_url="https://docs.google.com/document/d/example",
        telegram_thread_url="https://t.me/c/123/456?thread=455&topic",
        research_folder_url="https://drive.google.com/drive/folders/example",
    )

    story = store.get_story(story_id)
    assert story["origin_item_id"] == item_id.item_id
    assert story["google_doc_url"].endswith("example")
    assert story["telegram_thread_url"].startswith("https://t.me/c/")
    assert store.get_item(item_id)["status"] == "PROMOTED"


def test_request_phase2_marks_story_pending_and_is_idempotent(tmp_path):
    store = StoryStore(tmp_path / "stories.sqlite3")
    source_id = store.add_source("Court", "B", "US", "https://court.example")
    item = store.add_item(source_id, "Court order", "https://court.example/order")

    story_id = store.request_phase2(item.item_id, title="Court order story")
    replayed_story_id = store.request_phase2(item.item_id, title="Changed on retry")

    assert replayed_story_id == story_id
    assert store.get_story(story_id)["status"] == "Phase 2 pending"
    assert store.get_story(story_id)["title"] == "Court order story"
    assert store.get_item(item.item_id)["status"] == "PROMOTED"


def test_request_phase2_upgrades_legacy_story_stub(tmp_path):
    store = StoryStore(tmp_path / "stories.sqlite3")
    source_id = store.add_source("Court", "B", "US", "https://court.example")
    item = store.add_item(source_id, "Court order", "https://court.example/order")
    story_id = store.promote_item(item.item_id, title="Legacy stub", status="Story stub")

    assert store.request_phase2(item.item_id, title="Legacy stub") == story_id
    assert store.get_story(story_id)["status"] == "Phase 2 pending"


def test_telegram_completion_capability_transitions_pending_to_phase2_once(tmp_path):
    store = StoryStore(tmp_path / "stories.sqlite3")
    source_id = store.add_source("Court", "B", "US", "https://court.example")
    item = store.add_item(source_id, "Court order", "https://court.example/order")
    story_id = store.request_phase2(item.item_id, title="Court order story")
    token = store.issue_phase2_completion_token(story_id)

    with pytest.raises(ValueError, match="Invalid Phase 2 completion token"):
        store.complete_phase2(story_id, "wrong-token")
    completed = store.complete_phase2(story_id, token)
    replayed = store.complete_phase2(story_id, token)

    assert completed["status"] == "Phase 2"
    assert replayed["status"] == "Phase 2"
    assert store.get_item(item.item_id)["status"] == "PROMOTED"


def test_phase2_completion_does_not_downgrade_phase3(tmp_path):
    store = StoryStore(tmp_path / "stories.sqlite3")
    source_id = store.add_source("Court", "B", "US", "https://court.example")
    item = store.add_item(source_id, "Court order", "https://court.example/order")
    story_id = store.request_phase2(item.item_id, title="Court order story")
    token = store.issue_phase2_completion_token(story_id)
    store.update_story_links(story_id, status="Phase 3")

    assert store.complete_phase2(story_id, token)["status"] == "Phase 3"


def test_export_has_read_model_sections_and_is_deterministic(tmp_path):
    db_path = tmp_path / "stories.sqlite3"
    store = StoryStore(db_path)
    source_id = store.add_source("Regulator", "C", "NJ", "https://regulator.example")
    item_id = store.add_item(source_id, "Regulatory item", "https://regulator.example/item")
    store.promote_item(item_id, title="Regulatory story", status="Phase 2")
    output = tmp_path / "data" / "stories.json"

    export_json(db_path, output, generated_at="2026-10-07T00:00:00Z")
    first = output.read_text()
    export_json(db_path, output, generated_at="2026-10-07T00:00:00Z")
    second = output.read_text()

    assert first == second
    payload = json.loads(first)
    assert {"generated_at", "recent_candidates", "stories", "sources", "source_proposals", "coverage"} <= set(payload)
    assert payload["recent_candidates"] == []
    assert payload["stories"][0]["title"] == "Regulatory story"
    assert payload["sources"][0]["name"] == "Regulator"


def test_pending_and_complete_phase2_work_stay_out_of_phase1_candidates(tmp_path):
    db_path = tmp_path / "stories.sqlite3"
    store = StoryStore(db_path)
    source_id = store.add_source("Regulator", "C", "NJ", "https://regulator.example")
    stub = store.add_item(source_id, "Still a candidate", "https://regulator.example/stub")
    pending = store.add_item(source_id, "Phase 2 pending work", "https://regulator.example/pending")
    final = store.add_item(source_id, "Phase 2 work", "https://regulator.example/phase2")
    active = store.add_item(source_id, "Still active Phase 1 work", "https://regulator.example/active")
    store.promote_item(stub.item_id, title="Legacy story stub", status="Story stub")
    pending_story_id = store.request_phase2(pending.item_id, title="Pending project")
    completion_token = store.issue_phase2_completion_token(pending_story_id)
    store.complete_phase2(pending_story_id, completion_token)
    store.promote_item(final.item_id, title="Phase 2 project", status="Phase 2")

    payload = json.loads(export_json(
        db_path, tmp_path / "stories.json", generated_at="2026-10-07T00:00:00Z"
    ).read_text())

    assert {story["title"] for story in payload["stories"]} == {"Phase 2 project", "Pending project"}
    assert {candidate["item_id"] for candidate in payload["recent_candidates"]} == {stub.item_id, active.item_id}
    assert {row["item_id"] for row in store.list_unsummarized_items(active_only=True)} == {active.item_id}


def test_schema_foreign_key_and_status_validation(tmp_path):
    store = StoryStore(tmp_path / "stories.sqlite3")
    with pytest.raises(ValueError):
        store.update_item_status(999, "NOPE")
    with pytest.raises(ValueError):
        store.add_item(999, "Bad", "https://example.com/bad")
