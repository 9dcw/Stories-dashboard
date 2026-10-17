from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from story_store import StoryStore


@pytest.fixture
def api_db(tmp_path):
    db = tmp_path / "stories.sqlite3"
    store = StoryStore(db)
    source_id = store.add_source("Example News", "A", "US", "https://news.example.test")
    first = store.add_item(
        source_id, "First candidate", "https://news.example.test/first",
        published_at="2026-10-15T10:00:00Z",
    )
    second = store.add_item(
        source_id, "Second candidate", "https://news.example.test/second",
        published_at="2026-10-14T10:00:00Z",
    )
    return db, store, first.item_id, second.item_id


@pytest.fixture
def runtime_config(api_db):
    from api.config import RuntimeConfig

    db, *_ = api_db
    return RuntimeConfig(
        stories_db_path=db,
        google_client_id="client-id.apps.googleusercontent.com",
        google_allowed_email="reviewer@example.com",
        google_allowed_sub="google-subject-123",
        session_secret="s" * 48,
        public_origin="http://localhost:8000",
        session_max_age_seconds=3600,
        testing=True,
    )


@pytest.fixture
def valid_claims():
    return {
        "aud": "client-id.apps.googleusercontent.com",
        "iss": "https://accounts.google.com",
        "sub": "google-subject-123",
        "email": "reviewer@example.com",
        "email_verified": True,
    }


@pytest.fixture
def client(runtime_config, monkeypatch, valid_claims):
    from api import auth
    from api.main import create_app

    monkeypatch.setattr(auth.id_token, "verify_oauth2_token", lambda *_args, **_kwargs: dict(valid_claims))
    with TestClient(create_app(runtime_config), base_url="https://localhost") as test_client:
        yield test_client


def login(client, credential="valid-token"):
    response = client.post(
        "/api/v1/auth/google",
        json={"credential": credential},
        headers={"Origin": "http://localhost:8000"},
    )
    assert response.status_code == 200, response.text
    return response.json()["csrf_token"]


@pytest.fixture
def authenticated(client):
    return client, login(client)
