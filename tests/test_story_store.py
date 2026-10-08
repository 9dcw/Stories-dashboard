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


def test_export_has_read_model_sections_and_is_deterministic(tmp_path):
    db_path = tmp_path / "stories.sqlite3"
    store = StoryStore(db_path)
    source_id = store.add_source("Regulator", "C", "NJ", "https://regulator.example")
    item_id = store.add_item(source_id, "Regulatory item", "https://regulator.example/item")
    store.promote_item(item_id, title="Regulatory story")
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


def test_schema_foreign_key_and_status_validation(tmp_path):
    store = StoryStore(tmp_path / "stories.sqlite3")
    with pytest.raises(ValueError):
        store.update_item_status(999, "NOPE")
    with pytest.raises(ValueError):
        store.add_item(999, "Bad", "https://example.com/bad")
