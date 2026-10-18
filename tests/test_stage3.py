import json
from pathlib import Path

from export_stories import export_json
from gist import MAX_INPUT_CHARS, PROMPT_VERSION, build_bounded_input, extract_html_text, parse_summary_output
from story_store import StoryStore
from export_stories import public_source_url, phase2_action_url


def test_placeholder_source_urls_are_not_published():
    assert public_source_url("https://www.insurancejournal.com/example/business-interruption") == ""
    assert public_source_url("https://legacy.example/stories/old-item") == ""
    assert public_source_url("https://www.justice.gov/real/press-release") == "https://www.justice.gov/real/press-release"


def test_phase2_action_url_opens_hermes_chat_with_exact_drive_topic_handoff():
    from urllib.parse import parse_qs, urlsplit

    url = phase2_action_url(
        42, "A regulator announces a settlement", "https://example.gov/order/42",
        story_id=7, completion_token="capability-token", api_origin="https://stories.example",
    )
    parsed = urlsplit(url)
    query = parse_qs(parsed.query)
    assert parsed.netloc == "t.me"
    assert parsed.path == "/mehyanbot"
    text = query["text"][0]
    assert "item 42" in text
    assert "canonical dedicated Telegram Stories topic" in text
    assert "Research & Treatment Doc" in text
    assert "Images folder" in text
    assert "phase2 row" in text
    assert "Phase 2 pending" in text
    assert "The dashboard has already set this project to Phase 2 pending" in text
    assert '"completion_token":"capability-token"' in text
    assert "phase2-complete" in text
    assert "ONLY after all required Phase 2 work and artifacts are verified" in text
    assert "curl -fsS -X POST" in text
    assert '"story_id":7' in text
    assert "https://stories.example/api/v1/telegram/phase2-complete" in text
    assert "https://example.gov/order/42" in text


HTML_FIXTURE = """
<html><head><title>Ignore</title></head><body>
<nav>Home | Menu | Subscribe</nav>
<article><h1>Agency announces settlement</h1><p>The department announced a $2 million settlement with Example Insurance on Tuesday.</p><p>The order requires refunds to affected policyholders.</p></article>
<aside>Related stories and advertisements</aside><footer>Copyright Example</footer>
</body></html>
"""


def make_store(tmp_path):
    store = StoryStore(tmp_path / "stories.sqlite3")
    source_id = store.add_source("Example Regulator", "C", "US", "https://example.gov")
    item = store.add_item(source_id, "Agency announces settlement", "https://example.gov/order/1", "2026-10-07")
    return store, item


def test_html_extraction_removes_navigation_and_keeps_article():
    text, method = extract_html_text(HTML_FIXTURE)
    assert method == "html_article"
    assert "The department announced" in text
    assert "Subscribe" not in text
    assert "Related stories" not in text


def test_bounded_input_is_deterministic_and_records_counts():
    value = build_bounded_input(
        {"headline": "Headline", "source_name": "Source", "published_at": "2026-10-07", "raw_url": "https://example.test"},
        "x" * (MAX_INPUT_CHARS + 100),
    )
    assert len(value.input_text) == MAX_INPUT_CHARS
    assert value.extracted_char_count == MAX_INPUT_CHARS + 100
    assert value.input_char_count == MAX_INPUT_CHARS
    assert value.input_text.startswith("Headline\nSource\n2026-10-07\nhttps://example.test\n")


def test_bounded_input_never_exceeds_custom_limit_with_long_metadata():
    value = build_bounded_input({"headline": "H" * 100, "source_name": "S" * 100, "raw_url": "https://example.test"}, "body", 20)
    assert value.input_char_count == 20
    assert len(value.input_text) == 20


def test_summary_metadata_is_persisted_and_completed_is_not_pending(tmp_path):
    store, item = make_store(tmp_path)
    store.save_summary(item.item_id, display_headline="New York settles claims case with Example Insurance", gist="What happened: Settlement announced.", summary_prompt_version=PROMPT_VERSION, extracted_char_count=10, input_char_count=10, extraction_method="html_article")
    row = store.get_item(item.item_id)
    assert row["headline"] == "Agency announces settlement"
    assert row["display_headline"] == "New York settles claims case with Example Insurance"
    assert row["gist"] == "What happened: Settlement announced."
    assert row["summary_status"] == "COMPLETE"
    assert row["summary_prompt_version"] == PROMPT_VERSION
    assert store.list_unsummarized_items() == []


def test_failed_summary_is_recorded_and_can_be_retried(tmp_path):
    store, item = make_store(tmp_path)
    store.save_summary(item.item_id, gist="", summary_prompt_version="gist_v1", summary_status="FAILED", summary_error="blocked", extraction_method="fetch_failed")
    assert store.get_item(item.item_id)["summary_error"] == "blocked"
    assert [row["item_id"] for row in store.list_unsummarized_items()] == [item.item_id]


def test_dismissed_status_is_persisted_and_exported_with_telegram_action(tmp_path):
    from urllib.parse import parse_qs, urlsplit

    store, item = make_store(tmp_path)
    store.dismiss_item(
        item.item_id, "other", "", "test", "test@example.test", "stage3-export-dismiss"
    )
    assert store.get_item(item.item_id)["status"] == "DISMISSED"
    output = tmp_path / "stories.json"
    export_json(tmp_path / "stories.sqlite3", output)
    candidate = json.loads(output.read_text())["recent_candidates"][0]
    action_url = urlsplit(candidate["dismissal_action_url"])
    assert candidate["status"] == "DISMISSED"
    assert action_url.netloc == "t.me"
    assert action_url.path == "/mehyanbot"
    text = parse_qs(action_url.query)["text"][0]
    assert "item 1" in text
    assert "DISMISSED" in text
    assert "https://example.gov/order/1" in text


def test_export_marks_new_candidates_available_for_phase2_promotion(tmp_path):
    store, item = make_store(tmp_path)
    output = tmp_path / "stories.json"
    export_json(tmp_path / "stories.sqlite3", output)
    candidate = json.loads(output.read_text())["recent_candidates"][0]
    assert candidate["can_promote_to_phase2"] is True
    assert "phase2_action_url" not in candidate


def test_legacy_items_table_is_migrated_without_changing_item_ids(tmp_path):
    import sqlite3
    db = tmp_path / "legacy.sqlite3"
    connection = sqlite3.connect(db)
    connection.executescript("""
        CREATE TABLE sources (source_id INTEGER PRIMARY KEY, name TEXT NOT NULL, lane TEXT NOT NULL,
            jurisdiction TEXT NOT NULL, source_url TEXT NOT NULL, source_type TEXT NOT NULL DEFAULT 'web',
            active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, notes TEXT NOT NULL DEFAULT '');
        CREATE TABLE items (item_id INTEGER PRIMARY KEY, source_id INTEGER NOT NULL, headline TEXT NOT NULL,
            raw_url TEXT NOT NULL, canonical_url TEXT NOT NULL UNIQUE, url_hash TEXT NOT NULL UNIQUE,
            published_at TEXT, first_seen_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'NEW' CHECK (status IN ('NEW', 'PROMOTED', 'IGNORED')),
            validation_reason TEXT NOT NULL DEFAULT '');
        INSERT INTO sources VALUES (1, 'Legacy', 'A', 'US', 'https://example.gov', 'web', 1, 'now', '');
        INSERT INTO items VALUES (7, 1, 'Old item', 'https://example.gov/old', 'https://example.gov/old', 'hash', NULL, 'now', 'NEW', '');
    """)
    connection.commit(); connection.close()
    store = StoryStore(db)
    assert store.get_item(7)["headline"] == "Old item"
    store.dismiss_item(
        7, "other", "", "test", "test@example.test", "stage3-migration-dismiss"
    )
    assert store.get_item(7)["status"] == "DISMISSED"
    with sqlite3.connect(db) as check:
        assert check.execute("PRAGMA foreign_key_check").fetchall() == []
        child_sql = check.execute("SELECT sql FROM sqlite_master WHERE name='candidate_notes'").fetchone()[0]
        assert 'REFERENCES items' in child_sql


def test_export_includes_gist_and_summary_state(tmp_path):
    store, item = make_store(tmp_path)
    store.save_summary(item.item_id, display_headline="Regulator settles insurance claims case", gist="What happened: Settlement announced.", summary_prompt_version=PROMPT_VERSION, extracted_char_count=10, input_char_count=10, extraction_method="html_article")
    output = tmp_path / "stories.json"
    export_json(tmp_path / "stories.sqlite3", output, generated_at="2026-10-08T00:00:00Z")
    candidate = json.loads(output.read_text())["recent_candidates"][0]
    assert candidate["display_headline"] == "Regulator settles insurance claims case"
    assert candidate["gist"] == "What happened: Settlement announced."
    assert candidate["summary_status"] == "COMPLETE"


def test_fixture_batch_failure_isolated(tmp_path):
    store, first = make_store(tmp_path)
    second = store.add_item(first.source_id, "Second", "https://example.gov/order/2")
    from summarize_items import summarize_items

    def fetcher(url):
        if url.endswith("/2"):
            raise OSError("blocked")
        return HTML_FIXTURE

    def summarizer(text):
        return "What happened: Settlement announced."

    result = summarize_items(store, item_ids=[first.item_id, second.item_id], fetcher=fetcher, summarizer=summarizer)
    assert result == {"items_checked": 2, "summaries_created": 1, "summaries_failed": 1}
    assert store.get_item(first.item_id)["summary_status"] == "COMPLETE"
    assert store.get_item(second.item_id)["summary_status"] == "FAILED"


def test_force_regenerates_completed_summary(tmp_path):
    store, item = make_store(tmp_path)
    calls = []

    def fetcher(url):
        return HTML_FIXTURE

    def summarizer(text):
        calls.append(text)
        return "What happened: New wording."

    from summarize_items import summarize_items
    store.save_summary(item.item_id, display_headline="Old display", gist="old", summary_prompt_version=PROMPT_VERSION, extraction_method="html_article")
    assert summarize_items(store, item_ids=[item.item_id], fetcher=fetcher, summarizer=summarizer) == {"items_checked": 0, "summaries_created": 0, "summaries_failed": 0}
    assert summarize_items(store, item_ids=[item.item_id], force=True, fetcher=fetcher, summarizer=summarizer)["summaries_created"] == 1
    assert calls


def test_force_without_item_regenerates_completed_batch(tmp_path):
    store, item = make_store(tmp_path)
    store.save_summary(item.item_id, display_headline="Old display", gist="old", summary_prompt_version=PROMPT_VERSION, extraction_method="html_article")
    from summarize_items import summarize_items
    result = summarize_items(store, force=True, fetcher=lambda _: HTML_FIXTURE, summarizer=lambda _: "What happened: regenerated.")
    assert result["items_checked"] == 1
    assert result["summaries_created"] == 1


def test_summary_output_parses_compact_json_with_both_fields():
    parsed = parse_summary_output('{"display_headline":"Insurer fined over claim delays","gist":"What happened: The regulator fined the insurer."}')
    assert parsed == {
        "display_headline": "Insurer fined over claim delays",
        "gist": "What happened: The regulator fined the insurer.",
    }


def test_completed_summary_without_display_headline_is_pending(tmp_path):
    store, item = make_store(tmp_path)
    store.save_summary(item.item_id, gist="old", summary_prompt_version="gist_v1", extraction_method="html_article")
    assert [row["item_id"] for row in store.list_unsummarized_items()] == [item.item_id]


def test_pdf_uses_first_three_pages_image_and_cleans_up(tmp_path, monkeypatch):
    import summarize_items as summarizer_module

    store, item = make_store(tmp_path)
    image_path = tmp_path / "first-page.png"
    image_path.write_bytes(b"png")
    calls = {}

    monkeypatch.setattr(summarizer_module, "fetch_url_content", lambda url, source_config=None: (b"pdf", "application/pdf"))
    monkeypatch.setattr(summarizer_module, "render_pdf_first_three_pages", lambda body: str(image_path))

    def summarizer(prompt, attached_image):
        calls["image"] = attached_image
        return '{"display_headline":"PDF-backed settlement","gist":"What happened: A PDF source was summarized from its first page image."}'

    from summarize_items import summarize_items
    result = summarize_items(store, item_ids=[item.item_id], summarizer=summarizer)
    assert result == {"items_checked": 1, "summaries_created": 1, "summaries_failed": 0}
    assert calls["image"] == str(image_path)
    assert not image_path.exists()
    assert store.get_item(item.item_id)["extraction_method"] == "pdf_first_three_pages_image"


def test_pdf_renderer_stacks_at_most_three_pages(tmp_path):
    import pymupdf
    from PIL import Image

    document = pymupdf.open()
    for index in range(4):
        page = document.new_page(width=200, height=300)
        page.insert_text((20, 40), f"Page {index + 1}")
    pdf_bytes = document.tobytes()
    document.close()

    from gist import render_pdf_first_three_pages

    image_path = Path(render_pdf_first_three_pages(pdf_bytes))
    try:
        image = Image.open(image_path)
        assert image.width == 300
        assert image.height == 1350
    finally:
        image_path.unlink(missing_ok=True)
