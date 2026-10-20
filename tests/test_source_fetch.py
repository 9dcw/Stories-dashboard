import json
from email.message import Message
from urllib.error import HTTPError

import source_fetch
from gist import extract_html_text


class FakeResponse:
    def __init__(self, body, content_type="text/html"):
        self.body = body.encode()
        self.headers = Message()
        self.headers["Content-Type"] = content_type

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, _limit):
        return self.body


def test_sec_policy_adds_contact_to_user_agent(monkeypatch):
    captured = {}
    monkeypatch.setenv("STORIES_SEC_CONTACT_EMAIL", "david@example.test")

    def opener(request, timeout):
        captured.update(request.header_items())
        return FakeResponse("<article><p>SEC release text long enough to extract.</p></article>")

    monkeypatch.setattr(source_fetch, "urlopen", opener)
    source_fetch._LAST_REQUEST.clear()
    source_fetch.fetch_url("https://www.sec.gov/news/press-release/1")
    assert captured["User-agent"] == "StoriesDashboard/3.0 david@example.test"


def test_courtlistener_uses_api_endpoint_and_token(monkeypatch):
    captured = {}
    monkeypatch.setenv("COURTLISTENER_API_TOKEN", "secret-token")

    def opener(request, timeout):
        captured["url"] = request.full_url
        captured["auth"] = request.get_header("Authorization")
        return FakeResponse(json.dumps({"plain_text": "Court opinion text with enough detail for extraction."}), "application/json")

    monkeypatch.setattr(source_fetch, "urlopen", opener)
    source_fetch._LAST_REQUEST.clear()
    body = source_fetch.fetch_url("https://www.courtlistener.com/opinion/12345/example/")
    assert captured == {
        "url": "https://www.courtlistener.com/api/rest/v3/opinions/12345/",
        "auth": "Token secret-token",
    }
    assert extract_html_text(body) == ("Court opinion text with enough detail for extraction.", "api_json_plain_text")


def test_custom_auth_and_rate_policy_are_configurable(monkeypatch):
    captured = {}
    config = {"fetch": {"auth_type": "bearer_env", "credential_env": "EXAMPLE_TOKEN", "min_interval": 0}}
    monkeypatch.setenv("EXAMPLE_TOKEN", "abc")

    def opener(request, timeout):
        captured["auth"] = request.get_header("Authorization")
        return FakeResponse("<article><p>Example source body.</p></article>")

    monkeypatch.setattr(source_fetch, "urlopen", opener)
    source_fetch._LAST_REQUEST.clear()
    source_fetch.fetch_url("https://source.example/story/1", source_config=config)
    assert captured["auth"] == "Bearer abc"


def test_xml_feed_content_types_are_supported(monkeypatch):
    def opener(request, timeout):
        return FakeResponse("<rss><channel><title>Feed</title></channel></rss>", "application/rss+xml")

    monkeypatch.setattr(source_fetch, "urlopen", opener)
    source_fetch._LAST_REQUEST.clear()
    assert "<rss>" in source_fetch.fetch_url("https://source.example/feed.xml")


def test_retry_after_is_honored_for_rate_limit(monkeypatch):
    calls = []
    monkeypatch.setattr(source_fetch.time, "sleep", lambda value: calls.append(value))
    retry_headers = Message()
    retry_headers["Retry-After"] = "0"
    attempts = iter([HTTPError("https://example.test", 429, "rate", retry_headers, None), FakeResponse("<p>ok</p>")])
    def opener(request, timeout):
        value = next(attempts)
        if isinstance(value, Exception):
            raise value
        return value
    monkeypatch.setattr(source_fetch, "urlopen", opener)
    source_fetch._LAST_REQUEST.clear()
    assert source_fetch.fetch_url("https://example.test/story") == "<p>ok</p>"
    assert calls == [0.0]


def test_court_fetch_keeps_redirect_destination_and_rejects_truncation(monkeypatch):
    response = FakeResponse('123456', 'application/pdf')
    response.geturl = lambda: 'https://cdn.ca9.uscourts.gov/final.pdf'
    monkeypatch.setattr(source_fetch, 'urlopen', lambda *a, **kw: response)
    body, kind, final = source_fetch.fetch_resource('https://www.ca9.uscourts.gov/a', max_bytes=10)
    assert (body, kind, final) == (b'123456', 'application/pdf', 'https://cdn.ca9.uscourts.gov/final.pdf')
    import pytest
    with pytest.raises(ValueError, match='not truncated'):
        source_fetch.fetch_resource('https://www.ca9.uscourts.gov/a', max_bytes=5)


def test_second_circuit_uses_date_filtered_post(monkeypatch):
    from urllib.parse import parse_qs
    requests = []
    def opener(request, timeout):
        requests.append(request)
        return FakeResponse('<html>Results</html>')
    monkeypatch.setattr(source_fetch, 'urlopen', opener)
    source_fetch.fetch_resource('https://ww3.ca2.uscourts.gov/dtSearch/dtisapi6.dll',
        source_config={'ca2_date_search': True})
    assert requests[0].get_method() == 'POST'
    fields = parse_qs(requests[0].data.decode())
    assert fields['cmd'] == ['search'] and fields['pageSize'] == ['1000']
    assert fields['fileConditions'][0].startswith('xfilter(date "')
