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
