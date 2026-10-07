"""Deterministic source collectors used by Stage 2 polling."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit
from xml.etree import ElementTree


@dataclass(frozen=True)
class Candidate:
    source_id: int
    headline: str
    url: str
    published_at: str | None = None


def _date(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip()
    if len(value) == 10 and value[4] == "-" and value[7] == "-":
        return value
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def collect_feed(source: dict, body: str) -> list[Candidate]:
    root = ElementTree.fromstring(body.lstrip("\ufeff \t\r\n"))
    candidates = []
    for entry in root.iter():
        tag = entry.tag.rsplit("}", 1)[-1].lower()
        if tag not in {"item", "entry"}:
            continue
        values = {child.tag.rsplit("}", 1)[-1].lower(): (child.text or "").strip() for child in entry}
        link = values.get("link", "")
        if not link:
            for child in entry:
                if child.tag.rsplit("}", 1)[-1].lower() == "link":
                    link = child.attrib.get("href", "")
                    break
        headline = values.get("title", "")
        if headline and link:
            candidates.append(Candidate(source["source_id"], headline, urljoin(source.get("poll_url") or source.get("source_url", ""), link), _date(values.get("pubdate") or values.get("published") or values.get("updated"))))
    return candidates


class _ListParser(HTMLParser):
    def __init__(self, base_url: str, source_id: int, allowed_path_prefixes: list[str] | None = None):
        super().__init__()
        self.base_url, self.source_id = base_url, source_id
        self.allowed_path_prefixes = allowed_path_prefixes or []
        self.current_href = None
        self.current_text = []
        self.current_date = None
        self.candidates = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "a" and attrs.get("href"):
            self.current_href, self.current_text, self.current_date = attrs["href"], [], None
        elif tag == "time" and attrs.get("datetime"):
            if self.current_href:
                self.current_date = attrs["datetime"]
            elif self.candidates:
                self.candidates[-1] = Candidate(self.candidates[-1].source_id, self.candidates[-1].headline, self.candidates[-1].url, _date(attrs["datetime"]))

    def handle_data(self, data):
        if self.current_href:
            self.current_text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self.current_href:
            headline = " ".join("".join(self.current_text).split())
            resolved_url = urljoin(self.base_url, self.current_href)
            path = urlsplit(resolved_url).path
            allowed = not self.allowed_path_prefixes or any(path.startswith(prefix) for prefix in self.allowed_path_prefixes)
            if headline and resolved_url.lower().startswith(("http://", "https://")) and allowed:
                self.candidates.append(Candidate(self.source_id, headline, resolved_url, _date(self.current_date)))
            self.current_href = None


def collect_html_list(source: dict, body: str) -> list[Candidate]:
    config = json.loads(source.get("collector_config") or "{}")
    parser = _ListParser(
        source.get("poll_url") or source.get("source_url", ""),
        source["source_id"],
        config.get("allowed_path_prefixes"),
    )
    parser.feed(body)
    return parser.candidates


def collect_json(source: dict, body: str) -> list[Candidate]:
    payload = json.loads(body)
    rows = payload if isinstance(payload, list) else payload.get("items", [])
    base = source.get("poll_url") or source.get("source_url", "")
    candidates = []
    for row in rows:
        headline = row.get("title") or row.get("headline")
        link = row.get("url") or row.get("link")
        if headline and link:
            candidates.append(Candidate(source["source_id"], headline.strip(), urljoin(base, link), _date(row.get("date") or row.get("published_at") or row.get("published"))))
    return candidates
