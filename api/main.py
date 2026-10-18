from __future__ import annotations

import json
import re
import sqlite3
import threading
from pathlib import Path
from typing import NoReturn
from fastapi import Cookie, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse

from api.auth import (
    LoginRateLimiter, SessionCodec, login_client_key, require_csrf, require_origin,
    verify_google_credential,
)
from api.config import RuntimeConfig
from api.models import DismissRequest, GoogleCredential, RestoreRequest
from api.read_model import build_live_read_model, sanitize_feedback
from export_stories import phase2_action_url
from story_store import StoryStore

ROOT = Path(__file__).resolve().parents[1]
SESSION_COOKIE_NAME = "stories_session"
MAX_API_BODY_BYTES = 16 * 1024
LOGIN_GLOBAL_CAPACITY = 50
LOGIN_GLOBAL_PERIOD_SECONDS = 60
LOGIN_VERIFY_CONCURRENCY = 4
LOGIN_CONCURRENCY_RETRY_AFTER_SECONDS = 1

# These guards are shared by every production app object in this interpreter,
# so constructing another app cannot bypass the process-wide bounds.
_PROCESS_GLOBAL_LOGIN_LIMITER = LoginRateLimiter(
    capacity=LOGIN_GLOBAL_CAPACITY,
    period_seconds=LOGIN_GLOBAL_PERIOD_SECONDS,
    max_clients=1,
)
_PROCESS_LOGIN_VERIFICATION_GATE = threading.BoundedSemaphore(LOGIN_VERIFY_CONCURRENCY)

_BODY_LIMITED_PATH = re.compile(
    r"^/api/v1/(?:auth/(?:google|logout)|items/[^/]+/(?:dismiss|restore))$"
)
STATIC_FILES = {
    "app.js": ROOT / "app.js",
    "styles.css": ROOT / "styles.css",
    "sw.js": ROOT / "sw.js",
    "manifest.json": ROOT / "manifest.json",
    "data/stories.json": ROOT / "data" / "stories.json",
    "data/assets.json": ROOT / "data" / "assets.json",
}


class RequestBodyLimitMiddleware:
    """Buffer a small mutation body at the ASGI boundary, including chunked input."""

    def __init__(self, app, max_body_bytes: int = MAX_API_BODY_BYTES):
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope, receive, send):
        if (
            scope["type"] != "http" or scope.get("method") != "POST"
            or not _BODY_LIMITED_PATH.fullmatch(scope.get("path", ""))
        ):
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers", ()))
        try:
            declared = int(headers.get(b"content-length", b"0"))
        except ValueError:
            declared = 0
        if declared > self.max_body_bytes:
            await self._reject(send)
            return
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body.extend(message.get("body", b""))
            if len(body) > self.max_body_bytes:
                await self._reject(send)
                return
            if not message.get("more_body", False):
                break
        delivered = False

        async def replay():
            nonlocal delivered
            if delivered:
                return {"type": "http.request", "body": b"", "more_body": False}
            delivered = True
            return {"type": "http.request", "body": bytes(body), "more_body": False}

        await self.app(scope, replay, send)

    @staticmethod
    async def _reject(send):
        body = json.dumps({"detail": "Request body too large"}, separators=(",", ":")).encode()
        await send({
            "type": "http.response.start", "status": 413,
            "headers": [(b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode()),
                        (b"cache-control", b"no-store")],
        })
        await send({"type": "http.response.body", "body": body})


def _database_unavailable(error: sqlite3.OperationalError) -> NoReturn:
    if "locked" in str(error).lower() or "busy" in str(error).lower():
        raise HTTPException(
            status_code=503, detail="Database temporarily unavailable",
            headers={"Retry-After": "1"},
        ) from None
    raise error


def _store_error(error: ValueError) -> HTTPException:
    message = str(error)
    if message.startswith("Unknown item_id"):
        return HTTPException(status_code=404, detail="Item not found")
    if "request_id" in message or "Only NEW" in message or "Only DISMISSED" in message or "active dismissal" in message:
        return HTTPException(status_code=409, detail=message)
    return HTTPException(status_code=422, detail=message)


def _mutation_response(event: dict) -> dict:
    """Reconstruct a mutation response solely from its immutable event."""
    return {
        "item": {"item_id": event["item_id"], "status": event["resulting_status"]},
        "feedback": sanitize_feedback(event),
        "active_dismissal": sanitize_feedback(event) if event["action"] == "DISMISS" else None,
    }


def create_app(config: RuntimeConfig | None = None) -> FastAPI:
    """Application factory: configuration is intentionally loaded only when called."""
    runtime = config or RuntimeConfig.from_env()
    store = StoryStore(runtime.stories_db_path)
    codec = SessionCodec(runtime)
    login_limiter = LoginRateLimiter()
    if runtime.testing:
        # Test apps are isolated from one another while exercising identical bounds.
        global_login_limiter = LoginRateLimiter(
            capacity=LOGIN_GLOBAL_CAPACITY,
            period_seconds=LOGIN_GLOBAL_PERIOD_SECONDS,
            max_clients=1,
        )
        login_verification_gate = threading.BoundedSemaphore(LOGIN_VERIFY_CONCURRENCY)
    else:
        global_login_limiter = _PROCESS_GLOBAL_LOGIN_LIMITER
        login_verification_gate = _PROCESS_LOGIN_VERIFICATION_GATE
    app = FastAPI(
        title="Stories Dashboard API",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.config = runtime
    app.state.store = store
    app.state.login_rate_limiter = login_limiter
    app.state.global_login_rate_limiter = global_login_limiter
    app.state.login_verification_gate = login_verification_gate
    app.add_middleware(RequestBodyLimitMiddleware, max_body_bytes=MAX_API_BODY_BYTES)

    def session_dependency(
        stories_session: str | None = Cookie(default=None, alias=SESSION_COOKIE_NAME),
    ) -> dict[str, str]:
        return codec.load(stories_session)

    def mutation_session(
        request: Request,
        session: dict[str, str] = Depends(session_dependency),
    ) -> dict[str, str]:
        require_origin(request, runtime)
        require_csrf(request, session)
        return session

    @app.middleware("http")
    async def api_cache_policy(request: Request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/api/v1/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/deployment-mode.json", include_in_schema=False)
    def deployment_mode(response: Response) -> dict[str, str]:
        response.headers["Cache-Control"] = "no-store"
        return {"mode": "interactive"}

    @app.get("/api/v1/config")
    def public_config() -> dict[str, str]:
        return {"google_client_id": runtime.google_client_id}

    @app.post("/api/v1/auth/google")
    def google_login(payload: GoogleCredential, request: Request, response: Response) -> dict:
        require_origin(request, runtime)
        retry_after = global_login_limiter.consume("all-clients")
        if retry_after is None:
            retry_after = login_limiter.consume(
                login_client_key(request, runtime.trust_x_forwarded_for)
            )
        if retry_after is not None:
            raise HTTPException(
                status_code=429, detail="Too many login attempts",
                headers={"Retry-After": str(retry_after)},
            )
        if not login_verification_gate.acquire(blocking=False):
            raise HTTPException(
                status_code=429, detail="Too many login attempts",
                headers={"Retry-After": str(LOGIN_CONCURRENCY_RETRY_AFTER_SECONDS)},
            )
        try:
            verify_google_credential(payload.credential, runtime)
        finally:
            login_verification_gate.release()
        token, csrf = codec.issue()
        response.set_cookie(
            SESSION_COOKIE_NAME,
            token,
            max_age=runtime.session_max_age_seconds,
            httponly=True,
            secure=True,
            samesite="strict",
            path="/",
        )
        return {"authenticated": True, "csrf_token": csrf}

    @app.get("/api/v1/auth/session")
    def auth_session(session: dict[str, str] = Depends(session_dependency)) -> dict:
        return {"authenticated": True, "csrf_token": session["csrf"]}

    @app.post("/api/v1/auth/logout")
    def logout(
        response: Response,
        session: dict[str, str] = Depends(mutation_session),
    ) -> dict[str, bool]:
        response.delete_cookie(
            SESSION_COOKIE_NAME, path="/", secure=True, httponly=True, samesite="strict"
        )
        return {"authenticated": False}

    @app.get("/api/v1/dashboard")
    def dashboard(session: dict[str, str] = Depends(session_dependency)) -> dict:
        try:
            return build_live_read_model(store)
        except sqlite3.OperationalError as error:
            _database_unavailable(error)

    @app.post("/api/v1/items/{item_id}/phase2", status_code=201)
    def request_phase2(
        item_id: int,
        session: dict[str, str] = Depends(mutation_session),
    ) -> dict:
        try:
            item = store.get_item(item_id)
            title = item["display_headline"] or item["headline"]
            story_id = store.request_phase2(item_id, title)
            return {
                "story": store.get_story(story_id),
                "telegram_handoff_url": phase2_action_url(item_id, title, item["raw_url"]),
            }
        except ValueError as error:
            raise _store_error(error) from None
        except sqlite3.OperationalError as error:
            _database_unavailable(error)

    @app.get("/api/v1/items/{item_id}/feedback")
    def feedback(item_id: int, session: dict[str, str] = Depends(session_dependency)) -> list[dict]:
        try:
            return [dict(sanitize_feedback(event) or {}) for event in store.list_item_feedback(item_id)]
        except ValueError as error:
            raise _store_error(error) from None
        except sqlite3.OperationalError as error:
            _database_unavailable(error)

    @app.post("/api/v1/items/{item_id}/dismiss", status_code=201)
    def dismiss(
        item_id: int,
        payload: DismissRequest,
        response: Response,
        session: dict[str, str] = Depends(mutation_session),
    ) -> dict:
        try:
            event, replayed = store.dismiss_item(
                item_id, payload.reason_code, payload.note,
                runtime.google_allowed_sub, runtime.google_allowed_email,
                str(payload.request_id), return_replay_status=True,
            )
            if replayed:
                response.status_code = 200
            return _mutation_response(event)
        except ValueError as error:
            raise _store_error(error) from None
        except sqlite3.OperationalError as error:
            _database_unavailable(error)

    @app.post("/api/v1/items/{item_id}/restore", status_code=201)
    def restore(
        item_id: int,
        payload: RestoreRequest,
        response: Response,
        session: dict[str, str] = Depends(mutation_session),
    ) -> dict:
        try:
            event, replayed = store.restore_item(
                item_id, payload.dismissal_feedback_id,
                runtime.google_allowed_sub, runtime.google_allowed_email,
                str(payload.request_id), return_replay_status=True,
            )
            if replayed:
                response.status_code = 200
            return _mutation_response(event)
        except ValueError as error:
            raise _store_error(error) from None
        except sqlite3.OperationalError as error:
            _database_unavailable(error)

    @app.get("/{requested_path:path}", include_in_schema=False)
    def static_frontend(requested_path: str):
        if requested_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="Not found")
        if requested_path.split("/", 1)[0] in {
            "api", "docs", "tests", "story-data", "scripts", "discovery", "samples", "deploy"
        }:
            raise HTTPException(status_code=404, detail="Not found")
        if requested_path in STATIC_FILES:
            return FileResponse(STATIC_FILES[requested_path])
        # Safe SPA fallback: route-like paths only. Dotfiles, extensions, traversal,
        # and repository directories are never translated into filesystem paths.
        segments = requested_path.split("/") if requested_path else []
        if all(segment and segment not in {".", ".."} and "." not in segment for segment in segments):
            return FileResponse(ROOT / "index.html")
        if requested_path == "":
            return FileResponse(ROOT / "index.html")
        raise HTTPException(status_code=404, detail="Not found")

    return app
