"""Policy-driven source retrieval with auth, pacing, retries, and endpoint adapters."""
from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from urllib.error import HTTPError
from urllib.request import Request, urlopen


@dataclass(frozen=True)
class FetchPolicy:
    auth_type: str = "none"
    credential_env: str = ""
    contact_env: str = "STORIES_CONTACT_EMAIL"
    min_interval: float = 0.0
    endpoint_adapter: str = ""
    api_base: str = ""
    user_agent: str = "StoriesDashboard/3.0"
    max_attempts: int = 3


_LAST_REQUEST: dict[str, float] = {}
_LOCK = threading.Lock()


def _host(url: str) -> str:
    return url.split("/", 3)[2].lower().split(":", 1)[0] if "://" in url else ""


def policy_for(url: str, source_config: str | dict | None = None) -> FetchPolicy:
    config = json.loads(source_config or "{}") if isinstance(source_config, str) else (source_config or {})
    fetch = config.get("fetch", config)
    host = _host(url)
    if not fetch and host == "www.sec.gov":
        fetch = {"user_agent_contact_env": "STORIES_SEC_CONTACT_EMAIL", "min_interval": 0.25}
    if not fetch and host == "www.courtlistener.com":
        fetch = {"auth_type": "token_env", "credential_env": "COURTLISTENER_API_TOKEN",
                 "endpoint_adapter": "courtlistener_opinion" if "/opinion/" in url else "",
                 "api_base": "https://www.courtlistener.com/api/rest/v3",
                 "min_interval": 12.0}
    return FetchPolicy(
        auth_type=fetch.get("auth_type", "none"),
        credential_env=fetch.get("credential_env", ""),
        contact_env=fetch.get("user_agent_contact_env", fetch.get("contact_env", "STORIES_CONTACT_EMAIL")),
        min_interval=float(fetch.get("min_interval", 0.0)),
        endpoint_adapter=fetch.get("endpoint_adapter", ""),
        api_base=fetch.get("api_base", ""),
        user_agent=fetch.get("user_agent", "StoriesDashboard/3.0"),
        max_attempts=int(fetch.get("max_attempts", 3)),
    )


def _adapt_endpoint(url: str, policy: FetchPolicy) -> str:
    if policy.endpoint_adapter != "courtlistener_opinion":
        return url
    match = re.search(r"/opinion/(\d+)(?:/|$)", url)
    if not match:
        raise ValueError(f"CourtListener opinion URL has no numeric opinion id: {url}")
    return f"{policy.api_base.rstrip('/')}/opinions/{match.group(1)}/"


def _retry_delay(response_headers, attempt: int) -> float:
    value = response_headers.get("Retry-After", "")
    try:
        return min(60.0, max(0.0, float(value)))
    except ValueError:
        try:
            return min(60.0, max(0.0, parsedate_to_datetime(value).timestamp() - time.time()))
        except (TypeError, ValueError, OverflowError):
            return min(60.0, 2.0 ** (attempt - 1))


def _headers(url: str, policy: FetchPolicy) -> dict[str, str]:
    headers = {"User-Agent": policy.user_agent, "Accept": "text/html, application/xhtml+xml, application/json, application/rss+xml, application/atom+xml, application/xml, text/xml"}
    if _host(url) == "www.sec.gov":
        contact = os.environ.get(policy.contact_env, "").strip()
        if contact:
            headers["User-Agent"] = f"{policy.user_agent} {contact}"
    if policy.auth_type in {"bearer_env", "token_env", "basic_env", "api_key_env"}:
        credential = os.environ.get(policy.credential_env, "").strip()
        if credential:
            if policy.auth_type == "bearer_env":
                headers["Authorization"] = f"Bearer {credential}"
            elif policy.auth_type == "token_env":
                headers["Authorization"] = f"Token {credential}"
            elif policy.auth_type == "basic_env":
                headers["Authorization"] = f"Basic {credential}"
            else:
                headers["X-API-Key"] = credential
    return headers


def fetch_url_content(url: str, *, source_config: str | dict | None = None, timeout: int = 20) -> tuple[bytes, str]:
    policy = policy_for(url, source_config)
    request_url = _adapt_endpoint(url, policy)
    key = f"{_host(request_url)}:{policy.endpoint_adapter}"
    last_error: Exception | None = None
    for attempt in range(1, max(1, policy.max_attempts) + 1):
        with _LOCK:
            wait = policy.min_interval - (time.monotonic() - _LAST_REQUEST.get(key, 0.0))
            if wait > 0:
                time.sleep(wait)
            _LAST_REQUEST[key] = time.monotonic()
        request = Request(request_url, headers=_headers(request_url, policy))
        try:
            with urlopen(request, timeout=timeout) as response:
                content_type = response.headers.get_content_type()
                allowed = {"text/html", "application/xhtml+xml", "application/json", "application/pdf", "application/rss+xml", "application/atom+xml", "application/xml", "text/xml"}
                if content_type not in allowed:
                    raise ValueError(f"unsupported content type: {content_type}")
                return response.read(2_000_000), content_type
        except HTTPError as exc:
            last_error = exc
            if exc.code not in {429, 500, 502, 503, 504} or attempt >= policy.max_attempts:
                raise
            time.sleep(_retry_delay(exc.headers, attempt))
        except (OSError, TimeoutError) as exc:
            last_error = exc
            if attempt >= policy.max_attempts:
                raise
            time.sleep(min(60.0, 2.0 ** (attempt - 1)))
    raise last_error or RuntimeError("source fetch failed")


def fetch_url(url: str, *, source_config: str | dict | None = None, timeout: int = 20) -> str:
    body, content_type = fetch_url_content(url, source_config=source_config, timeout=timeout)
    if content_type == "application/pdf":
        raise ValueError("PDF content requires first-page image extraction")
    return body.decode("utf-8", errors="replace")
