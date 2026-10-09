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

    assert result["sources_checked"] == 2
    assert result["sources_failed"] == 0
    assert result["items_inserted"] == 2
    assert result["rejected_old"] == 0
    assert result["ambiguous"] == 2
    rows = store.list_items()
    assert {row["headline"] for row in rows} == {"First release", "Notice one"}
    assert next(row for row in rows if row["headline"] == "First release")["published_at"] == "2026-10-07T12:00:00Z"


def test_repeated_polling_is_idempotent_and_tracking_variants_deduplicate(tmp_path):
    store, rss_id, _ = make_store(tmp_path)
    responses = {"https://example.gov/feed": RSS}

    first = poll_sources(store, source_ids=[rss_id], fetcher=responses.__getitem__)
    second = poll_sources(store, source_ids=[rss_id], fetcher=responses.__getitem__)

    assert first["items_inserted"] == 1
    assert first["ambiguous"] == 1
    assert second["items_inserted"] == 0
    assert second["ambiguous"] == 1
    assert len(store.list_items()) == 1


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


def test_transient_source_failure_is_retried_with_bound(tmp_path):
    store, rss_id, _ = make_store(tmp_path)
    calls = []

    def fetch(url):
        calls.append(url)
        if len(calls) == 1:
            raise OSError("temporary outage")
        return RSS

    result = poll_sources(store, source_ids=[rss_id], fetcher=fetch, retry_attempts=2)
    assert result["sources_failed"] == 0
    assert result["items_inserted"] == 1
    assert calls.count("https://example.gov/feed") == 2
    assert len(calls) >= 2


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


def test_validation_rejects_archive_navigation_and_generic_headlines():
    from collectors import validate_candidate

    source = {"source_id": 2, "poll_url": "https://agency.gov/notices", "collector_config": "{}"}
    assert validate_candidate(source, Candidate(2, "Learn more", "https://agency.gov/notices/2026", "2026-10-08"), now="2026-10-08T12:00:00Z").decision == "navigation_archive"
    assert validate_candidate(source, Candidate(2, "Order 12", "https://agency.gov/order/12", "2026-08-01"), now="2026-10-08T12:00:00Z").decision == "old"
    accepted = validate_candidate(source, Candidate(2, "Commissioner issues Order 12", "https://agency.gov/order/12", "2026-10-08"), now="2026-10-08T12:00:00Z")
    assert accepted.decision == "accepted"


def test_validation_allows_source_configured_article_path_segments():
    from collectors import validate_candidate

    source = {"source_id": 18, "collector_config": '{"allow_archive_segments":["news"]}'}
    accepted = validate_candidate(
        source,
        Candidate(18, "Insurer announces new coverage", "https://example.com/news/national/2026/10/09/123.htm", "2026-10-09"),
        now="2026-10-09T12:00:00Z",
    )
    assert accepted.decision == "accepted"


def test_html_collector_recovers_headline_from_listing_heading_and_date():
    from collectors import collect_html_list

    html = """<section class='release'><h2>Commissioner issues emergency order</h2>
      <a href='/order/12'>Learn more</a><span class='date'>October 8, 2026</span></section>"""
    candidates = collect_html_list(
        {"source_id": 2, "poll_url": "https://agency.gov/notices", "collector_config": "{}"}, html
    )
    assert candidates[0].headline == "Commissioner issues emergency order"
    assert candidates[0].published_at == "2026-10-08T00:00:00Z"


def test_html_collector_recovers_secondary_header_date():
    from collectors import collect_html_list

    candidates = collect_html_list(
        {"source_id": 14, "poll_url": "https://agency.gov/2026/", "collector_config": "{}"},
        '<span class="secondaryHeader">October 7, 2026</span><a href="/release036-2026.cfm">Release</a>',
    )
    assert candidates[0].published_at == "2026-10-07T00:00:00Z"


def test_poll_report_has_all_quality_buckets_and_ten_source_registry(tmp_path):
    from polling_sources import STAGE2_SOURCES

    assert len(STAGE2_SOURCES) == 10
    expected = {"candidates_extracted", "rejected_navigation_archive", "rejected_old", "accepted_publications", "ambiguous"}
    store, rss_id, html_id = make_store(tmp_path)
    result = poll_sources(store, source_ids=[rss_id, html_id], fetcher={"https://example.gov/feed": RSS, "https://agency.gov/notices": HTML}.__getitem__)
    assert expected <= result.keys()