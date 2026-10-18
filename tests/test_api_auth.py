from api.auth import SessionCodec
from api.main import SESSION_COOKIE_NAME


def test_public_health_and_config_disclose_only_safe_values(client):
    health = client.get("/api/v1/health")
    assert health.status_code == 200
    assert health.json() == {"status": "ok"}
    assert client.get("/deployment-mode.json").status_code == 404
    config = client.get("/api/v1/config")
    assert config.json() == {"google_client_id": "client-id.apps.googleusercontent.com"}
    assert "STORIES" not in config.text


def test_valid_google_token_sets_secure_timed_session(client, runtime_config):
    response = client.post(
        "/api/v1/auth/google", json={"credential": "google-token"},
        headers={"Origin": "http://localhost:8000"},
    )
    assert response.status_code == 200
    assert response.json()["authenticated"] is True
    assert response.json()["csrf_token"]
    cookie = response.headers["set-cookie"].lower()
    assert SESSION_COOKIE_NAME in cookie
    assert "httponly" in cookie
    assert "secure" in cookie
    assert "samesite=strict" in cookie
    assert "path=/" in cookie
    assert "max-age=3600" in cookie

    cookie_payload = SessionCodec(runtime_config).serializer.loads(
        response.cookies[SESSION_COOKIE_NAME]
    )
    assert cookie_payload.keys() == {"v", "session_id", "csrf"}
    assert runtime_config.google_allowed_sub not in repr(cookie_payload)
    assert runtime_config.google_allowed_email not in repr(cookie_payload)

    session = client.get("/api/v1/auth/session")
    assert session.status_code == 200
    assert session.json() == response.json()
    assert "email" not in session.text and "subject" not in session.text


def test_google_email_authorization_is_case_sensitive(client, monkeypatch, valid_claims):
    from api import auth

    claims = dict(valid_claims, email="Reviewer@example.com")
    monkeypatch.setattr(auth.id_token, "verify_oauth2_token", lambda *_args, **_kwargs: claims)

    response = client.post(
        "/api/v1/auth/google", json={"credential": "google-token"},
        headers={"Origin": "http://localhost:8000"},
    )

    assert response.status_code == 401


def test_google_verifier_receives_configured_audience(client, monkeypatch, valid_claims):
    from api import auth
    seen = {}
    def verify(token, request, audience):
        seen.update(token=token, request=request, audience=audience)
        return valid_claims
    monkeypatch.setattr(auth.id_token, "verify_oauth2_token", verify)
    response = client.post("/api/v1/auth/google", json={"credential": "secret-token"},
                           headers={"Origin": "http://localhost:8000"})
    assert response.status_code == 200
    assert seen["token"] == "secret-token"
    assert seen["audience"] == "client-id.apps.googleusercontent.com"
    assert seen["request"].__class__.__module__.startswith("google.auth.transport")


def test_logout_requires_csrf_and_clears_cookie(authenticated):
    client, csrf = authenticated
    denied = client.post("/api/v1/auth/logout", headers={"Origin": "http://localhost:8000"})
    assert denied.status_code == 403
    response = client.post("/api/v1/auth/logout", headers={
        "Origin": "http://localhost:8000", "X-CSRF-Token": csrf,
    })
    assert response.status_code == 200
    assert response.json() == {"authenticated": False}
    assert "max-age=0" in response.headers["set-cookie"].lower()
    assert client.get("/api/v1/auth/session").status_code == 401
