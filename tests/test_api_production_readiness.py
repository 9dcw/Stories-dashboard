import asyncio
import sqlite3
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
from fastapi import FastAPI, Request
from fastapi import HTTPException
from fastapi.testclient import TestClient

from api.auth import SessionCodec
from api.main import (
    LOGIN_GLOBAL_CAPACITY,
    LOGIN_VERIFY_CONCURRENCY,
    MAX_API_BODY_BYTES,
    RequestBodyLimitMiddleware,
    create_app,
)
from tests.conftest import login

ORIGIN = {"Origin": "http://localhost:8000"}


def test_login_is_bounded_and_does_not_trust_forwarded_for(runtime_config, monkeypatch, valid_claims):
    from api import auth

    monkeypatch.setattr(auth.id_token, "verify_oauth2_token", lambda *_a, **_k: valid_claims)
    with TestClient(create_app(runtime_config), base_url="https://localhost") as client:
        for attempt in range(10):
            response = client.post(
                "/api/v1/auth/google", json={"credential": "x"},
                headers={**ORIGIN, "X-Forwarded-For": f"198.51.100.{attempt}"},
            )
            assert response.status_code == 200
        denied = client.post(
            "/api/v1/auth/google", json={"credential": "x"},
            headers={**ORIGIN, "X-Forwarded-For": "203.0.113.99"},
        )
    assert denied.status_code == 429
    assert int(denied.headers["Retry-After"]) >= 1


def test_login_has_aggregate_bound_across_unique_client_keys(runtime_config, monkeypatch, valid_claims):
    from api import auth

    monkeypatch.setattr(auth.time, "monotonic", lambda: 1000.0)
    monkeypatch.setattr(auth.id_token, "verify_oauth2_token", lambda *_a, **_k: valid_claims)
    config = replace(runtime_config, trust_x_forwarded_for=True)
    with TestClient(create_app(config), base_url="https://localhost") as client:
        for attempt in range(LOGIN_GLOBAL_CAPACITY):
            response = client.post(
                "/api/v1/auth/google", json={"credential": "x"},
                headers={**ORIGIN, "X-Forwarded-For": f"198.51.{attempt // 256}.{attempt % 256}"},
            )
            assert response.status_code == 200
        denied = client.post(
            "/api/v1/auth/google", json={"credential": "x"},
            headers={**ORIGIN, "X-Forwarded-For": "203.0.113.250"},
        )

    assert denied.status_code == 429
    assert int(denied.headers["Retry-After"]) >= 1


def test_login_verification_has_nonblocking_concurrency_bound(runtime_config, monkeypatch, valid_claims):
    from api import auth

    entered = threading.Event()
    release = threading.Event()
    calls = 0
    calls_lock = threading.Lock()

    def blocked_verifier(*_args, **_kwargs):
        nonlocal calls
        with calls_lock:
            calls += 1
            if calls == LOGIN_VERIFY_CONCURRENCY:
                entered.set()
        assert release.wait(3)
        return valid_claims

    monkeypatch.setattr(auth.id_token, "verify_oauth2_token", blocked_verifier)
    config = replace(runtime_config, trust_x_forwarded_for=True)
    with TestClient(create_app(config), base_url="https://localhost") as client:
        def request(number):
            return client.post(
                "/api/v1/auth/google", json={"credential": "x"},
                headers={**ORIGIN, "X-Forwarded-For": f"198.51.100.{number}"},
            )

        with ThreadPoolExecutor(max_workers=LOGIN_VERIFY_CONCURRENCY) as pool:
            futures = [pool.submit(request, number) for number in range(LOGIN_VERIFY_CONCURRENCY)]
            assert entered.wait(3)
            denied = request(250)
            assert denied.status_code == 429
            assert int(denied.headers["Retry-After"]) >= 1
            assert calls == LOGIN_VERIFY_CONCURRENCY
            release.set()
            assert all(future.result().status_code == 200 for future in futures)


def test_login_verification_gate_releases_after_verifier_failure(runtime_config, monkeypatch, valid_claims):
    from api import auth

    calls = 0

    def verifier(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls <= LOGIN_VERIFY_CONCURRENCY:
            raise ValueError("verification failed")
        return valid_claims

    monkeypatch.setattr(auth.id_token, "verify_oauth2_token", verifier)
    with TestClient(create_app(runtime_config), base_url="https://localhost") as client:
        for _ in range(LOGIN_VERIFY_CONCURRENCY):
            assert client.post(
                "/api/v1/auth/google", json={"credential": "x"}, headers=ORIGIN,
            ).status_code == 401
        recovered = client.post(
            "/api/v1/auth/google", json={"credential": "x"}, headers=ORIGIN,
        )

    assert recovered.status_code == 200


def test_google_verification_uses_one_cached_short_timeout_transport():
    from api import auth

    assert auth.GOOGLE_REQUEST is auth.GOOGLE_REQUEST
    assert auth.GOOGLE_HTTP_TIMEOUT_SECONDS <= 10
    assert auth.GOOGLE_HTTP_TIMEOUT_SECONDS >= 5
    assert any("cachecontrol" in type(adapter).__module__.lower()
               for adapter in auth.GOOGLE_REQUEST.session.adapters.values())


def test_api_mutation_body_limit_rejects_content_length(client, api_db):
    _db, _store, item_id, _second = api_db
    csrf = login(client)
    response = client.post(
        f"/api/v1/items/{item_id}/dismiss",
        content=b"{" + b" " * MAX_API_BODY_BYTES + b"}",
        headers={**ORIGIN, "X-CSRF-Token": csrf, "Content-Type": "application/json"},
    )
    assert response.status_code == 413


def test_body_limit_counts_chunked_messages_before_json_parser():
    parsed = False
    inner = FastAPI()

    @inner.post("/api/v1/auth/google")
    async def endpoint(request: Request):
        nonlocal parsed
        parsed = True
        await request.json()
        return {"bad": True}

    app = RequestBodyLimitMiddleware(inner, max_body_bytes=8)
    messages = iter([
        {"type": "http.request", "body": b"12345", "more_body": True},
        {"type": "http.request", "body": b"67890", "more_body": False},
    ])
    sent = []

    async def receive():
        return next(messages)

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http", "http_version": "1.1", "method": "POST",
        "scheme": "https", "path": "/api/v1/auth/google",
        "raw_path": b"/api/v1/auth/google", "query_string": b"",
        "headers": [(b"content-type", b"application/json")],
        "client": ("127.0.0.1", 1234), "server": ("localhost", 443),
    }
    asyncio.run(app(scope, receive, send))
    assert next(m for m in sent if m["type"] == "http.response.start")["status"] == 413
    assert parsed is False


def test_non_ascii_security_values_fail_closed(client, monkeypatch, valid_claims):
    from api import auth

    malformed_origin = Request({
        "type": "http", "headers": [(b"origin", "http://localhost/\N{SNOWMAN}".encode())],
    })
    with pytest.raises(HTTPException) as origin_error:
        auth.require_origin(malformed_origin, client.app.state.config)
    assert origin_error.value.status_code == 403

    for field in ("email", "sub"):
        claims = dict(valid_claims, **{field: "\N{SNOWMAN}"})
        monkeypatch.setattr(auth.id_token, "verify_oauth2_token", lambda *_a, _claims=claims, **_k: _claims)
        assert client.post(
            "/api/v1/auth/google", json={"credential": "x"}, headers=ORIGIN,
        ).status_code == 401

    malformed_csrf = Request({
        "type": "http", "headers": [(b"x-csrf-token", "\N{SNOWMAN}".encode())],
    })
    with pytest.raises(HTTPException) as csrf_error:
        auth.require_csrf(malformed_csrf, {"csrf": "ascii-token"})
    assert csrf_error.value.status_code == 403


def test_sqlite_busy_on_mutation_is_generic_retryable_503(authenticated, monkeypatch):
    client, csrf = authenticated
    monkeypatch.setattr(
        client.app.state.store, "dismiss_item",
        lambda *_a, **_k: (_ for _ in ()).throw(sqlite3.OperationalError("database is locked: secret")),
    )
    response = client.post(
        "/api/v1/items/1/dismiss",
        json={"reason_code": "other", "note": "", "request_id": str(uuid.uuid4())},
        headers={**ORIGIN, "X-CSRF-Token": csrf},
    )
    assert response.status_code == 503
    assert response.headers["Retry-After"] == "1"
    assert response.json() == {"detail": "Database temporarily unavailable"}
    assert "secret" not in response.text


@pytest.mark.parametrize("endpoint", [
    "/api/v1/dashboard",
    "/api/v1/items/1/feedback",
])
def test_sqlite_busy_on_authenticated_reads_is_generic_retryable_503(
    authenticated, monkeypatch, endpoint,
):
    client, _csrf = authenticated

    if endpoint == "/api/v1/dashboard":
        monkeypatch.setattr(
            "api.main.build_live_read_model",
            lambda *_a, **_k: (_ for _ in ()).throw(
                sqlite3.OperationalError("database is busy: secret")
            ),
        )
    else:
        monkeypatch.setattr(
            client.app.state.store, "list_item_feedback",
            lambda *_a, **_k: (_ for _ in ()).throw(
                sqlite3.OperationalError("database is locked: secret")
            ),
        )

    response = client.get(endpoint)
    assert response.status_code == 503
    assert response.headers["Retry-After"] == "1"
    assert response.json() == {"detail": "Database temporarily unavailable"}
    assert "secret" not in response.text


def test_live_dashboard_uses_one_snapshot_during_concurrent_dismissal(api_db, monkeypatch):
    db, store, _first, second = api_db
    with sqlite3.connect(db) as connection:
        connection.execute("PRAGMA journal_mode=WAL")

    from api import read_model
    original = read_model.build_read_model
    snapshot_read = threading.Event()
    writer_finished = threading.Event()

    def paused_build(snapshot, *args, **kwargs):
        model = original(snapshot, *args, **kwargs)
        snapshot_read.set()
        assert writer_finished.wait(3)
        return model

    monkeypatch.setattr(read_model, "build_read_model", paused_build)

    def writer():
        assert snapshot_read.wait(3)
        store.dismiss_item(second, "too_old", "concurrent", "sub", "email@example.com", "concurrent")
        writer_finished.set()

    thread = threading.Thread(target=writer)
    thread.start()
    model = read_model.build_live_read_model(store)
    thread.join(3)
    assert not thread.is_alive()
    candidate = next(row for row in model["recent_candidates"] if row["item_id"] == second)
    assert candidate["status"] == "NEW"
    assert candidate["active_dismissal"] is None
    assert store.get_item(second)["status"] == "DISMISSED"


def test_session_expiration_uses_real_timestamp_validation(runtime_config, monkeypatch):
    import itsdangerous.timed

    codec = SessionCodec(runtime_config)
    now = itsdangerous.timed.time.time()
    monkeypatch.setattr(itsdangerous.timed.time, "time", lambda: now - 7200)
    token, _csrf = codec.issue()
    monkeypatch.undo()
    with pytest.raises(Exception) as caught:
        codec.serializer.loads(token, max_age=runtime_config.session_max_age_seconds)
    assert caught.type.__name__ == "SignatureExpired"
