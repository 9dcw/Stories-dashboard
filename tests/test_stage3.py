import json
from pathlib import Path

from export_stories import export_json
from gist import MAX_INPUT_CHARS, PROMPT_VERSION, build_bounded_input, extract_html_text, parse_summary_output
from story_store import StoryStore


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
