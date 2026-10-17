import asyncio
import sqlite3
import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

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


def test_deployment_rollback_uses_the_previously_running_immutable_image():
    root = Path(__file__).resolve().parents[1]
    compose = (root / "docker-compose.yml").read_text()
    workflow = (root / ".github/workflows/deploy-contabo.yml").read_text()

    assert "${STORIES_IMAGE:-" in compose
    assert "CURRENT_IMAGE=$(docker inspect" in workflow
    assert 'docker-compose.rollback.yml' in workflow
    assert 'docker-compose.rollback.yml' in workflow
    assert '-f docker-compose.yml -f docker-compose.rollback.yml up -d' in workflow
    assert "export IMAGE_TAG=latest" not in workflow


def test_container_user_matches_documented_volume_owner_and_ci_db_is_writable():
    root = Path(__file__).resolve().parents[1]
    dockerfile = (root / "Dockerfile").read_text()
    workflow = (root / ".github/workflows/test.yml").read_text()

    assert "groupadd -g 1000 stories" in dockerfile
    assert "useradd -u 1000 -g stories" in dockerfile
    assert 'chmod 666 "$TMP_DB"' in workflow
    assert "PUBLIC_ORIGIN=https://stories.localhost" in workflow


def test_production_compose_passes_domain_and_persistent_backup_mount():
    root = Path(__file__).resolve().parents[1]
    compose = (root / "docker-compose.yml").read_text()
    entrypoint = (root / "deploy/entrypoint.sh").read_text()

    assert "caddy:" in compose
    caddy_section = compose.split("  caddy:", 1)[1]
    assert "/etc/stories/stories.env" not in caddy_section
    assert "DOMAIN: ${DOMAIN:?DOMAIN must be set}" in caddy_section
    assert "/srv/stories-dashboard/backups:/app/backups" in compose
    assert "cp \"$STORIES_DB_PATH\"" not in entrypoint
    assert "scripts/backup_db.py" in entrypoint


def test_deploy_runs_only_after_tests_with_package_permission_and_host_pin():
    root = Path(__file__).resolve().parents[1]
    workflow = (root / ".github/workflows/deploy-contabo.yml").read_text()

    assert "workflow_run:" in workflow
    assert 'workflows: ["Test"]' in workflow
    assert "workflow_dispatch:" not in workflow
    assert "github.event.workflow_run.conclusion == 'success'" in workflow
    assert "github.event.workflow_run.event == 'push'" in workflow
    assert 'git fetch origin main:refs/remotes/origin/main' in workflow
    assert 'git rev-parse origin/main' in workflow
    assert "packages: write" in workflow
    assert "fingerprint: ${{ secrets.CONTABO_HOST_FINGERPRINT }}" in workflow
    assert "known_hosts:" not in workflow
    assert "vars.DEPLOY_PATH" in workflow
    assert "secrets.DEPLOY_PATH" not in workflow
    assert "DOMAIN: ${{ vars.DOMAIN }}" in workflow
    assert "envs: DOMAIN" in workflow
    assert "GitHub Actions variable DOMAIN must be set" in workflow
    assert 'ROLLBACK_ASSETS=$(mktemp -d)' in workflow
    assert 'cp -a "$ROLLBACK_ASSETS/docker-compose.yml" docker-compose.yml' in workflow


def test_deploy_docs_pin_the_host_key_algorithm_used_by_ssh_action():
    root = Path(__file__).resolve().parents[1]
    deployment_guide = (root / "deploy/README.md").read_text()

    assert "verified ECDSA fingerprint" in deployment_guide
    assert "ssh-keyscan -t ecdsa" in deployment_guide


def test_static_host_declares_mode_explicitly():
    root = Path(__file__).resolve().parents[1]
    assert json.loads((root / "deployment-mode.json").read_text()) == {"mode": "static"}


def test_deployment_mode_is_never_service_worker_cached():
    root = Path(__file__).resolve().parents[1]
    worker = (root / "sw.js").read_text()
    assert "url.pathname==='/deployment-mode.json'" in worker


def test_hidden_overlays_are_not_forced_visible_by_component_css():
    root = Path(__file__).resolve().parents[1]
    stylesheet = (root / "styles.css").read_text()
    assert "[hidden]{display:none!important}" in stylesheet
