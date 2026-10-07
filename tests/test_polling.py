import json
import sqlite3

from polling import Candidate, poll_sources
from story_store import StoryStore


RSS = """<?xml version="1.0"?><rss version="2.0"><channel>
<item><title>First release</title><link>https://example.gov/a?utm_source=x</link><pubDate>Tue, 07 Oct 2026 12:00:00 GMT</pubDate></item>
<item><title>Second release</title><link>https://example.gov/b</link></item>
</channel></rss>"""

HTML = """<html><body><ul>
<li><a href="/notice/1">Notice one</a><time datetime="2026-10-07">October 7</time></li>
<li><a href="/notice/2?gclid=abc">Notice two</a></li>
</ul></body></html>"""


def make_store(tmp_path):
    store = StoryStore(tmp_path / "stories.sqlite3")
    rss_id = store.add_source("RSS source", "A", "US", "https://example.gov", collector_type="rss", poll_url="https://example.gov/feed")
    html_id = store.add_source("HTML source", "C", "US", "https://agency.gov", collector_type="html_list", poll_url="https://agency.gov/notices")
    return store, rss_id, html_id


def test_rss_and_html_collectors_normalize_candidates(tmp_path):
    store, rss_id, html_id = make_store(tmp_path)
    responses = {"https://example.gov/feed": RSS, "https://agency.gov/notices": HTML}

    result = poll_sources(store, source_ids=[rss_id, html_id], fetcher=responses.__getitem__)

    assert result == {"sources_checked": 2, "sources_failed": 0, "items_seen": 4, "items_inserted": 4, "items_skipped_existing": 0}
    rows = store.list_items()
    assert {row["headline"] for row in rows} == {"First release", "Second release", "Notice one", "Notice two"}
    assert next(row for row in rows if row["headline"] == "First release")["published_at"] == "2026-10-07T12:00:00Z"


def test_repeated_polling_is_idempotent_and_tracking_variants_deduplicate(tmp_path):
    store, rss_id, _ = make_store(tmp_path)
    responses = {"https://example.gov/feed": RSS}

    first = poll_sources(store, source_ids=[rss_id], fetcher=responses.__getitem__)
    second = poll_sources(store, source_ids=[rss_id], fetcher=responses.__getitem__)

    assert first["items_inserted"] == 2
    assert second["items_inserted"] == 0
    assert second["items_skipped_existing"] == 2
    assert len(store.list_items()) == 2


def test_one_source_failure_does_not_stop_remaining_sources(tmp_path):
    store, rss_id, html_id = make_store(tmp_path)

    def fetch(url):
        if url.endswith("feed"):
            raise OSError("feed unavailable")
        return HTML

    result = poll_sources(store, source_ids=[rss_id, html_id], fetcher=fetch)

    assert result["sources_checked"] == 2
    assert result["sources_failed"] == 1
    assert result["items_inserted"] == 2
    source = next(row for row in store.list_sources() if row["source_id"] == rss_id)
    assert source["last_error"] == "feed unavailable"
    assert source["last_success_at"] is None


def test_feed_parser_accepts_leading_whitespace_before_xml_declaration():
    from collectors import collect_feed

    candidates = collect_feed(
        {"source_id": 4, "poll_url": "https://example.gov/feed"},
        "\ufeff  \n" + RSS,
    )

    assert len(candidates) == 2


def test_html_collector_skips_non_http_navigation_links():
    from collectors import collect_html_list

    candidates = collect_html_list(
        {"source_id": 2, "poll_url": "https://agency.gov/notices", "collector_config": '{"allowed_path_prefixes":["/notice/"]}'},
        '<a href="/notice/1">Real notice</a><a href="/other">Navigation</a><a href="mailto:info@agency.gov">Email</a><a href="javascript:void(0)">Menu</a>',
    )

    assert [candidate.url for candidate in candidates] == ["https://agency.gov/notice/1"]


def test_json_collector_and_candidate_shape():
    from collectors import collect_json

    candidates = collect_json(
        {"source_id": 9, "poll_url": "https://example.gov/api"},
        '{"items":[{"title":"Order","url":"/orders/1","date":"2026-10-08"}]}',
    )

    assert candidates == [Candidate(9, "Order", "https://example.gov/orders/1", "2026-10-08")]