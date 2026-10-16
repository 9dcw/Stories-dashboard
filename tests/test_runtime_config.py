from pathlib import Path

import pytest

from api.config import ConfigError, RuntimeConfig


def valid_env(tmp_path):
    db = tmp_path / "stories.sqlite3"
    db.touch()
    return {
        "STORIES_DB_PATH": str(db),
        "GOOGLE_CLIENT_ID": "123.apps.googleusercontent.com",
        "GOOGLE_ALLOWED_EMAIL": "Reviewer@Example.COM",
        "GOOGLE_ALLOWED_SUB": "subject-123",
        "SESSION_SECRET": "a" * 48,
        "PUBLIC_ORIGIN": "https://stories.acme-insurance.com",
    }


def test_load_validates_environment_without_changing_email_case(tmp_path):
    config = RuntimeConfig.from_env(valid_env(tmp_path))
    assert config.google_allowed_email == "Reviewer@Example.COM"
    assert config.public_origin == "https://stories.acme-insurance.com"
    assert config.session_max_age_seconds == 3600
    assert config.stories_db_path.is_absolute()


@pytest.mark.parametrize("missing", [
    "STORIES_DB_PATH", "GOOGLE_CLIENT_ID", "GOOGLE_ALLOWED_EMAIL",
    "GOOGLE_ALLOWED_SUB", "SESSION_SECRET", "PUBLIC_ORIGIN",
])
def test_production_config_fails_closed_for_missing_values(tmp_path, missing):
    env = valid_env(tmp_path)
    env.pop(missing)
    with pytest.raises(ConfigError):
        RuntimeConfig.from_env(env)


@pytest.mark.parametrize("name,value", [
    ("GOOGLE_CLIENT_ID", "changeme"),
    ("GOOGLE_ALLOWED_EMAIL", "replace-with-authorized-email"),
    ("GOOGLE_ALLOWED_SUB", "YOUR_GOOGLE_SUB"),
    ("SESSION_SECRET", "short"),
    ("SESSION_SECRET", "replace-with-at-least-32-random-characters"),
    ("PUBLIC_ORIGIN", "https://stories.example.com"),
    ("PUBLIC_ORIGIN", "http://stories.example.com"),
    ("PUBLIC_ORIGIN", "https://stories.example.com/path"),
    ("PUBLIC_ORIGIN", "https://stories.acme-insurance.com:not-a-port"),
    ("PUBLIC_ORIGIN", "https://stories.acme-insurance.com:70000"),
    ("PUBLIC_ORIGIN", "https://STORIES.acme-insurance.com"),
    ("PUBLIC_ORIGIN", "https://stories.acme-insurance.com:443"),
    ("PUBLIC_ORIGIN", "https://stories.acme-insurance.com/"),
    ("SESSION_MAX_AGE_SECONDS", "299"),
    ("SESSION_MAX_AGE_SECONDS", "86401"),
])
def test_production_config_rejects_placeholders_and_unsafe_values(tmp_path, name, value):
    env = valid_env(tmp_path)
    env[name] = value
    with pytest.raises(ConfigError):
        RuntimeConfig.from_env(env)


@pytest.mark.parametrize("name,value", [
    ("GOOGLE_ALLOWED_SUB", "s" * 256),
    ("GOOGLE_ALLOWED_EMAIL", "a" * 309 + "@example.com"),
])
def test_identity_configuration_rejects_values_over_store_limits(tmp_path, name, value):
    env = valid_env(tmp_path)
    env[name] = value
    with pytest.raises(ConfigError, match=name):
        RuntimeConfig.from_env(env)


def test_identity_configuration_accepts_store_limit_boundaries(tmp_path):
    env = valid_env(tmp_path)
    env["GOOGLE_ALLOWED_SUB"] = "s" * 255
    env["GOOGLE_ALLOWED_EMAIL"] = "a" * 308 + "@example.com"
    config = RuntimeConfig.from_env(env)
    assert len(config.google_allowed_sub) == 255
    assert len(config.google_allowed_email) == 320


def test_http_localhost_requires_explicit_testing(tmp_path):
    env = valid_env(tmp_path)
    env["PUBLIC_ORIGIN"] = "http://localhost:8000"
    with pytest.raises(ConfigError):
        RuntimeConfig.from_env(env)
    assert RuntimeConfig.from_env(env, testing=True).public_origin == "http://localhost:8000"


def test_testing_rejects_repository_production_database(monkeypatch):
    production = Path(__file__).resolve().parents[1] / "story-data" / "stories.sqlite3"
    env = {
        "STORIES_DB_PATH": str(production),
        "GOOGLE_CLIENT_ID": "123.apps.googleusercontent.com",
        "GOOGLE_ALLOWED_EMAIL": "reviewer@example.com",
        "GOOGLE_ALLOWED_SUB": "subject-123",
        "SESSION_SECRET": "a" * 48,
        "PUBLIC_ORIGIN": "http://localhost:8000",
    }
    with pytest.raises(ConfigError, match="production database"):
        RuntimeConfig.from_env(env, testing=True)


def test_configuration_is_not_loaded_at_import_time(monkeypatch):
    for key in ("STORIES_DB_PATH", "GOOGLE_CLIENT_ID", "GOOGLE_ALLOWED_EMAIL",
                "GOOGLE_ALLOWED_SUB", "SESSION_SECRET", "PUBLIC_ORIGIN"):
        monkeypatch.delenv(key, raising=False)
    import api.main  # noqa: F401
