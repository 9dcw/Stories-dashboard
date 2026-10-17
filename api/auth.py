from __future__ import annotations

import hmac
import secrets
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Mapping

import requests
from cachecontrol import CacheControl
from fastapi import HTTPException, Request
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2 import id_token
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from api.config import RuntimeConfig

SESSION_SALT = "stories-dashboard-session-v1"
GOOGLE_HTTP_TIMEOUT_SECONDS = 7


class ShortTimeoutSession(requests.Session):
    """A pooled requests session with a hard short default timeout."""

    def request(self, *args, **kwargs):
        timeout = kwargs.pop("timeout", GOOGLE_HTTP_TIMEOUT_SECONDS)
        kwargs["timeout"] = min(timeout, GOOGLE_HTTP_TIMEOUT_SECONDS)
        return super().request(*args, **kwargs)


# Google's signing certificates carry HTTP cache metadata. Keep one pooled,
# cache-aware transport for the process instead of constructing one per login.
_google_session = CacheControl(ShortTimeoutSession())
GOOGLE_REQUEST = GoogleRequest(session=_google_session)


def _constant_time_text_equal(left: object, right: object) -> bool:
    """Compare possibly hostile Unicode values without compare_digest TypeError."""
    if not isinstance(left, str) or not isinstance(right, str):
        return False
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


class LoginRateLimiter:
    """Small bounded per-client token bucket; a reverse proxy may add a wider layer."""

    def __init__(self, capacity: int = 10, period_seconds: float = 60, max_clients: int = 1024):
        self.capacity = capacity
        self.refill_per_second = capacity / period_seconds
        self.max_clients = max_clients
        self._clients: OrderedDict[str, tuple[float, float]] = OrderedDict()
        self._lock = threading.Lock()

    def consume(self, client_key: str) -> int | None:
        now = time.monotonic()
        with self._lock:
            tokens, updated = self._clients.pop(client_key, (float(self.capacity), now))
            tokens = min(float(self.capacity), tokens + (now - updated) * self.refill_per_second)
            if tokens < 1:
                self._clients[client_key] = (tokens, now)
                return max(1, int((1 - tokens) / self.refill_per_second + 0.999))
            self._clients[client_key] = (tokens - 1, now)
            while len(self._clients) > self.max_clients:
                self._clients.popitem(last=False)
        return None


def login_client_key(request: Request, trust_forwarded_for: bool) -> str:
    if trust_forwarded_for:
        forwarded = request.headers.get("x-forwarded-for", "").split(",", 1)[0].strip()
        if forwarded:
            return forwarded
    return request.client.host if request.client else "unknown"


@dataclass(frozen=True)
class Identity:
    sub: str
    email: str


class SessionCodec:
    def __init__(self, config: RuntimeConfig):
        self.config = config
        self.serializer = URLSafeTimedSerializer(config.session_secret, salt=SESSION_SALT)

    def issue(self) -> tuple[str, str]:
        csrf = secrets.token_urlsafe(32)
        # itsdangerous authenticates this payload but does not encrypt it. Keep
        # account identifiers server-side and issue only opaque session data.
        token = self.serializer.dumps({
            "v": "1", "session_id": secrets.token_urlsafe(32), "csrf": csrf,
        })
        return token, csrf

    def load(self, token: str | None) -> dict[str, str]:
        if not token:
            raise HTTPException(status_code=401, detail="Authentication required")
        try:
            payload = self.serializer.loads(token, max_age=self.config.session_max_age_seconds)
        except (BadSignature, SignatureExpired, TypeError):
            raise HTTPException(status_code=401, detail="Authentication required") from None
        if (
            not isinstance(payload, dict)
            or set(payload) != {"v", "session_id", "csrf"}
            or payload.get("v") != "1"
            or not all(
                isinstance(payload.get(key), str) and payload[key]
                for key in ("session_id", "csrf")
            )
        ):
            raise HTTPException(status_code=401, detail="Authentication required")
        return payload


def verify_google_credential(credential: str, config: RuntimeConfig) -> Identity:
    """Verify a Google ID token and apply the local single-user allowlist."""
    try:
        claims: Mapping[str, Any] = id_token.verify_oauth2_token(
            credential, GOOGLE_REQUEST, config.google_client_id
        )
    except Exception:
        # Google verifier errors can contain token details. Return one generic failure.
        raise HTTPException(status_code=401, detail="Invalid Google identity") from None
    issuer = claims.get("iss")
    audience = claims.get("aud")
    email = claims.get("email")
    subject = claims.get("sub")
    if issuer not in {"accounts.google.com", "https://accounts.google.com"}:
        raise HTTPException(status_code=401, detail="Invalid Google identity")
    if audience != config.google_client_id:
        raise HTTPException(status_code=401, detail="Invalid Google identity")
    if claims.get("email_verified") is not True:
        raise HTTPException(status_code=401, detail="Invalid Google identity")
    if (
        not isinstance(email, str)
        or not email
        or email.strip() != email
        or any(character.isspace() for character in email)
        or not _constant_time_text_equal(email, config.google_allowed_email)
    ):
        raise HTTPException(status_code=401, detail="Invalid Google identity")
    if not isinstance(subject, str) or not _constant_time_text_equal(subject, config.google_allowed_sub):
        raise HTTPException(status_code=401, detail="Invalid Google identity")
    return Identity(subject, email)


def require_origin(request: Request, config: RuntimeConfig) -> None:
    origin = request.headers.get("origin", "")
    if not _constant_time_text_equal(origin, config.public_origin):
        raise HTTPException(status_code=403, detail="Invalid request origin")


def require_csrf(request: Request, session: dict[str, str]) -> None:
    supplied = request.headers.get("x-csrf-token", "")
    if not supplied or not _constant_time_text_equal(supplied, session["csrf"]):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")
