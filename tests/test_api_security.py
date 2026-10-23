import uuid

import pytest

from tests.conftest import login


def test_wrong_identity_and_invalid_tokens_are_rejected(client, monkeypatch, valid_claims):
    from api import auth
    origin = {"Origin": "http://localhost:8000"}
    for changed in (
        {"email": "intruder@example.com"},
        {"email": "REVIEWER@example.com", "sub": "wrong-sub"},
        {"email_verified": False},
        {"iss": "https://evil.example"},
    ):
        claims = dict(valid_claims, **changed)
        monkeypatch.setattr(auth.id_token, "verify_oauth2_token", lambda *_a, _claims=claims, **_k: _claims)
        assert client.post("/api/v1/auth/google", json={"credential": "x"}, headers=origin).status_code == 401

    def invalid(*_args, **_kwargs):
        raise ValueError("invalid token secret detail")
    monkeypatch.setattr(auth.id_token, "verify_oauth2_token", invalid)
    response = client.post("/api/v1/auth/google", json={"credential": "do-not-echo"}, headers=origin)
    assert response.status_code == 401
    assert "do-not-echo" not in response.text and "secret detail" not in response.text


def test_origin_and_csrf_are_enforced(client, api_db):
    _db, _store, item_id, _ = api_db
    assert client.post("/api/v1/auth/google", json={"credential": "x"}).status_code == 403
    assert client.post("/api/v1/auth/google", json={"credential": "x"}, headers={"Origin": "https://evil.example"}).status_code == 403
    csrf = login(client)
    body = {"reason_code": "other", "note": "", "request_id": str(uuid.uuid4())}
    endpoint = f"/api/v1/items/{item_id}/dismiss"
    assert client.post(endpoint, json=body).status_code == 403
    assert client.post(endpoint, json=body, headers={"Origin": "http://localhost:8000"}).status_code == 403
    assert client.post(endpoint, json=body, headers={"Origin": "http://localhost:8000", "X-CSRF-Token": "wrong"}).status_code == 403
    assert client.post(endpoint, json=body, headers={"Origin": "https://evil.example", "X-CSRF-Token": csrf}).status_code == 403


def test_tampered_and_expired_sessions_are_unauthenticated(client, monkeypatch):
    login(client)
    cookie = client.cookies.get("stories_session")
    client.cookies.set("stories_session", cookie + "tamper")
    assert client.get("/api/v1/dashboard").status_code == 401

    login(client)
    from api import auth
    monkeypatch.setattr(auth.URLSafeTimedSerializer, "loads", lambda *_a, **_k: (_ for _ in ()).throw(auth.SignatureExpired("expired")))
    assert client.get("/api/v1/dashboard").status_code == 401


def test_no_cors_and_production_docs_disabled(client):
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404
    response = client.options("/api/v1/dashboard", headers={
        "Origin": "https://evil.example", "Access-Control-Request-Method": "GET",
    })
    assert "access-control-allow-origin" not in response.headers


@pytest.mark.parametrize("path", [
    "/.env", "/.git/config", "/story-data/stories.sqlite3", "/story_store.py",
    "/tests/test_api_security.py", "/schema.sql", "/requirements.txt", "/docs/anything",
    "/data/../story_store.py",
])
def test_static_server_never_exposes_repository_files(client, path):
    response = client.get(path)
    assert response.status_code == 404


def test_static_server_serves_only_allowlisted_frontend_and_spa(client):
    assert "<!doctype html>" in client.get("/").text.lower()
    assert client.get("/app.js").status_code == 200
    assert client.get("/styles.css").status_code == 200
    assert client.get("/manifest.json").status_code == 200
    assert client.get("/data/stories.json").status_code == 404
    assert "<!doctype html>" in client.get("/candidate/123").text.lower()
    assert client.get("/api/v1/not-real").status_code == 404


@pytest.mark.parametrize('path',['/','/index.html','/candidate/123','/app.js?v=one-click-dismiss-v2','/styles.css?v=one-click-dismiss-v2','/sw.js','/manifest.json'])
def test_frontend_assets_require_revalidation_across_deployments(client,path):
    response=client.get(path)
    assert response.status_code==200
    assert response.headers['Cache-Control']=='no-cache'
    if path.startswith('/app.js'):
        assert 'async function dismissItem' in response.text


def test_versioned_assets_exist_and_match_the_service_worker_precache(client):
    import re
    from urllib.parse import urlsplit,parse_qs
    html=client.get('/').text
    worker=client.get('/sw.js').text
    urls=re.findall(r'(?:src|href)="(\./(?:app\.js|styles\.css)\?[^"]+)"',html)
    assert len(urls)==2
    versions={parse_qs(urlsplit(url).query)['v'][0] for url in urls}
    assert len(versions)==1
    version=versions.pop()
    assert "ASSET_VERSION='"+version+"'" in worker
    for url in urls:
        assert client.get('/'+url.removeprefix('./')).status_code==200
        assert './'+urlsplit(url).path.removeprefix('./')+'?v=${ASSET_VERSION}' in worker


def test_every_service_worker_precache_url_is_served(client):
    import re
    worker=client.get('/sw.js').text
    version=re.search(r"ASSET_VERSION='([^']+)'",worker).group(1)
    files=re.search(r'const FILES=\[(.*?)\];',worker).group(1)
    urls=re.findall(r"['`](\./[^'`]*)['`]",files)
    assert len(urls)>=5
    for url in urls:
        path='/'+url.removeprefix('./').replace('${ASSET_VERSION}',version)
        response=client.get(path)
        assert response.status_code==200,(path,response.status_code)
