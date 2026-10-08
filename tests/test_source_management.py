import json
from datetime import datetime, timezone, timedelta
from urllib.parse import parse_qs, urlsplit

from export_stories import export_json
from story_store import StoryStore


def test_source_management_export_includes_disabled_sources_and_recent_counts(tmp_path):
    db = tmp_path / 'stories.sqlite3'
    store = StoryStore(db)
    active = store.add_source('Active source', 'A', 'US', 'https://active.example', poll_url='https://active.example/feed')
    disabled = store.add_source('Disabled source', 'C', 'NJ', 'https://disabled.example', poll_url='https://disabled.example/news', active=False)
    store.add_item(active, 'Recent', 'https://active.example/recent', published_at='2026-10-01')
    store.add_item(active, 'Old', 'https://active.example/old', published_at='2026-01-01')
    store.update_source_poll_state(active, checked_at='2026-10-08T00:00:00Z', success_at='2026-10-08T00:00:00Z', error=None)
    store.update_source_poll_state(disabled, checked_at='2026-10-08T00:00:00Z', error='disabled test')

    output = tmp_path / 'stories.json'
    export_json(db, output, generated_at='2026-10-08T00:00:00Z')
    sources = json.loads(output.read_text())['sources']
    assert {row['source_id'] for row in sources} == {active, disabled}
    active_row = next(row for row in sources if row['source_id'] == active)
    disabled_row = next(row for row in sources if row['source_id'] == disabled)
    assert active_row['active'] is True
    assert active_row['publications_last_30_days'] == 1
    assert active_row['last_success_at'] == '2026-10-08T00:00:00Z'
    assert disabled_row['active'] is False
    assert disabled_row['last_error'] == 'disabled test'


def test_source_management_telegram_links_encode_stable_id_name_and_url(tmp_path):
    db = tmp_path / 'stories.sqlite3'
    store = StoryStore(db)
    source_id = store.add_source('A & source', 'A', 'US', 'https://example.test/publication?id=1', poll_url='https://example.test/feed?a=1&b=2')
    output = tmp_path / 'stories.json'
    export_json(db, output, generated_at='2026-10-08T00:00:00Z')
    row = json.loads(output.read_text())['sources'][0]
    query = parse_qs(urlsplit(row['telegram_action_url']).query)
    assert query['url'] == ['https://example.test/feed?a=1&b=2']
    assert f'Disable source {source_id} (A & source)' in query['text'][0]


def test_source_active_toggle_is_idempotent(tmp_path):
    store = StoryStore(tmp_path / 'stories.sqlite3')
    source_id = store.add_source('Source', 'A', 'US', 'https://example.test')
    store.set_source_active(source_id, False)
    store.set_source_active(source_id, False)
    assert store.list_sources()[0]['active'] == 0
    store.set_source_active(source_id, True)
    assert store.list_sources()[0]['active'] == 1
