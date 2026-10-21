from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_DB = (ROOT / "story-data" / "stories.sqlite3").resolve()
MIN_SESSION_AGE = 300
MAX_SESSION_AGE = 86400
DEFAULT_SESSION_AGE = 3600
MAX_GOOGLE_ALLOWED_SUB_LENGTH = 255
MAX_GOOGLE_ALLOWED_EMAIL_LENGTH = 320


class ConfigError(ValueError):
    """Raised when runtime configuration is absent or unsafe."""


def _is_placeholder(value: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", value.lower())
    return (
        normalized in {"changeme", "placeholder", "yourvalue", "yourgooglesub", "todo"}
        or normalized.startswith("replacewith")
    )


def _required(environment: Mapping[str, str], name: str) -> str:
    value = environment.get(name, "").strip()
    if not value or _is_placeholder(value):
        raise ConfigError(f"{name} is required and cannot be a placeholder")
    return value


@dataclass(frozen=True)
class RuntimeConfig:
    stories_db_path: Path
    google_client_id: str
    google_allowed_email: str
    google_allowed_sub: str
    session_secret: str
    public_origin: str
    session_max_age_seconds: int = DEFAULT_SESSION_AGE
    testing: bool = False
    trust_x_forwarded_for: bool = False
    hermes_webhook_url: str = ''
    hermes_webhook_secret: str = ''
    phase2_engine: str = 'legacy'
    phase2_service_secret: str = ''
    phase2_allowed_users: str = ''
    phase2_chat_id: str = '-1003984316484'

    def __post_init__(self) -> None:
        if self.phase2_engine not in {'legacy', 'repository'}:
            raise ConfigError('PHASE2_ENGINE must be legacy or repository')
        if self.phase2_engine == 'repository' and (len(self.phase2_service_secret)<32 or not self.phase2_allowed_users.strip() or '*' in self.phase2_allowed_users):
            raise ConfigError('Repository Phase 2 requires a strong service secret and explicit Telegram users')
        db_path = Path(self.stories_db_path).expanduser().resolve()
        email = self.google_allowed_email.strip()
        origin = self.public_origin.strip()
        object.__setattr__(self, "stories_db_path", db_path)
        object.__setattr__(self, "google_allowed_email", email)
        object.__setattr__(self, "public_origin", origin)
        required = {
            "GOOGLE_CLIENT_ID": self.google_client_id,
            "GOOGLE_ALLOWED_EMAIL": email,
            "GOOGLE_ALLOWED_SUB": self.google_allowed_sub,
            "SESSION_SECRET": self.session_secret,
            "PUBLIC_ORIGIN": origin,
        }
        for name, value in required.items():
            if not str(value).strip() or _is_placeholder(str(value)):
                raise ConfigError(f"{name} is required and cannot be a placeholder")
        if len(self.session_secret) < 32:
            raise ConfigError("SESSION_SECRET must contain at least 32 characters")
        if len(self.google_allowed_sub) > MAX_GOOGLE_ALLOWED_SUB_LENGTH:
            raise ConfigError(
                f"GOOGLE_ALLOWED_SUB must be at most {MAX_GOOGLE_ALLOWED_SUB_LENGTH} characters"
            )
        if len(email) > MAX_GOOGLE_ALLOWED_EMAIL_LENGTH:
            raise ConfigError(
                f"GOOGLE_ALLOWED_EMAIL must be at most {MAX_GOOGLE_ALLOWED_EMAIL_LENGTH} characters"
            )
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            raise ConfigError("GOOGLE_ALLOWED_EMAIL must be a valid email address")
        if not isinstance(self.session_max_age_seconds, int) or not MIN_SESSION_AGE <= self.session_max_age_seconds <= MAX_SESSION_AGE:
            raise ConfigError(f"SESSION_MAX_AGE_SECONDS must be between {MIN_SESSION_AGE} and {MAX_SESSION_AGE}")
        try:
            parsed = urlsplit(origin)
            port = parsed.port
        except ValueError as error:
            raise ConfigError("PUBLIC_ORIGIN has an invalid port or host") from error
        local_http = self.testing and parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        if (parsed.scheme != "https" and not local_http) or not parsed.hostname or parsed.username or parsed.password:
            raise ConfigError("PUBLIC_ORIGIN must be an HTTPS origin (HTTP localhost is testing-only)")
        if not self.testing and (
            parsed.hostname in {"example.com", "example.org", "example.net"}
            or parsed.hostname.endswith((".example.com", ".example.org", ".example.net", ".test", ".invalid"))
        ):
            raise ConfigError("PUBLIC_ORIGIN cannot use a reserved placeholder domain")
        if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise ConfigError("PUBLIC_ORIGIN must not include a path, query, or fragment")
        hostname = parsed.hostname or ""
        canonical_host = f"[{hostname}]" if ":" in hostname else hostname
        default_port = (parsed.scheme == "https" and port == 443) or (parsed.scheme == "http" and port == 80)
        canonical = f"{parsed.scheme}://{canonical_host}" + (f":{port}" if port and not default_port else "")
        if origin != canonical:
            raise ConfigError("PUBLIC_ORIGIN must be canonical (lowercase host, no trailing slash or default port)")
        if self.testing and db_path == PRODUCTION_DB:
            raise ConfigError("tests must never use the repository production database")
        if not self.testing and (not db_path.exists() or not db_path.is_file()):
            raise ConfigError("STORIES_DB_PATH must identify an existing database file")

    @classmethod
    def from_env(cls, environment: Mapping[str, str] | None = None, *, testing: bool = False) -> "RuntimeConfig":
        env = os.environ if environment is None else environment
        raw_age = env.get("SESSION_MAX_AGE_SECONDS", str(DEFAULT_SESSION_AGE)).strip()
        try:
            age = int(raw_age)
        except ValueError as error:
            raise ConfigError("SESSION_MAX_AGE_SECONDS must be an integer") from error
        raw_trust_proxy = env.get("TRUST_X_FORWARDED_FOR", "false").strip().lower()
        if raw_trust_proxy not in {"true", "false"}:
            raise ConfigError("TRUST_X_FORWARDED_FOR must be true or false")
        return cls(
            stories_db_path=Path(_required(env, "STORIES_DB_PATH")),
            google_client_id=_required(env, "GOOGLE_CLIENT_ID"),
            google_allowed_email=_required(env, "GOOGLE_ALLOWED_EMAIL"),
            google_allowed_sub=_required(env, "GOOGLE_ALLOWED_SUB"),
            session_secret=_required(env, "SESSION_SECRET"),
            public_origin=_required(env, "PUBLIC_ORIGIN"),
            session_max_age_seconds=age,
            testing=testing,
            trust_x_forwarded_for=raw_trust_proxy == "true",
            hermes_webhook_url=env.get('HERMES_WEBHOOK_URL', '').strip(),
            hermes_webhook_secret=env.get('HERMES_WEBHOOK_SECRET', '').strip(),
            phase2_engine=env.get('PHASE2_ENGINE', 'legacy').strip(),
            phase2_service_secret=env.get('PHASE2_SERVICE_SECRET', '').strip(),
            phase2_allowed_users=env.get('PHASE2_ALLOWED_USERS', '').strip(),
            phase2_chat_id=env.get('PHASE2_CHAT_ID', '-1003984316484').strip(),
        )
