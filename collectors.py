"""Deterministic source collectors used by Stage 2 polling."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
import re
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


@dataclass(frozen=True)
class Validation:
    decision: str
    reason: str = ""
    headline: str = ""


GENERIC_HEADLINES = {"learn more", "read more", "details", "download", "more", "click here", "view", "view current releases", "view release"}
ARCHIVE_SEGMENTS = {"archive", "archives", "category", "categories", "tag", "tags", "page", "calendar", "news"}


def _parse_any_date(value: str | None) -> datetime | None:
    normalized = _date(value)
    if not normalized:
        return None
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _meaningful_headline(value: str) -> bool:
    return bool(value and value.strip().casefold() not in GENERIC_HEADLINES and len(value.strip()) >= 5)


def validate_candidate(source: dict, candidate: Candidate, *, now: str | None = None) -> Validation:
    """Cheap, source-configurable gate; this intentionally does not score relevance."""
    config = json.loads(source.get("collector_config") or "{}")
    parsed = urlsplit(candidate.url)
    path_parts = [part.casefold() for part in parsed.path.split("/") if part]
    allowed_archive_segments = {part.casefold() for part in config.get("allow_archive_segments", [])}
    disallowed_archive_parts = set(path_parts[:-1]) & (ARCHIVE_SEGMENTS - allowed_archive_segments)
    if disallowed_archive_parts or (path_parts and path_parts[-1].isdigit() and len(path_parts[-1]) == 4) or path_parts in (["news"], ["press-releases"], ["news", ""]):
        return Validation("navigation_archive", "archive_or_index", candidate.headline)
    if not _meaningful_headline(candidate.headline):
        return Validation("navigation_archive", "generic_anchor_text", candidate.headline)
    days = int(config.get("max_age_days", 30))
    as_of = _parse_any_date(now) or datetime.now(timezone.utc)
    published = _parse_any_date(candidate.published_at)
    if not published:
        match = re.search(r"(?:^|/)(?:pr)?(20\d{2})(\d{2})(\d{2})(?:\D|$)", parsed.path, re.I)
        if match:
            try:
                published = datetime(int(match.group(1)), int(match.group(2)), int(match.group(3)), tzinfo=timezone.utc)
            except ValueError:
                published = None
        else:
            match = re.search(r"(?:^|/)pr(\d{2})(\d{2})(\d{2})(?:\D|$)", parsed.path, re.I)
            if match:
                published = datetime(2000 + int(match.group(1)), int(match.group(2)), int(match.group(3)), tzinfo=timezone.utc)
    if published and published < as_of - timedelta(days=days):
        return Validation("old", f"older_than_{days}_days", candidate.headline)
    if not published:
        return Validation("ambiguous", "missing_publication_date", candidate.headline)
    return Validation("accepted", "dated_substantive_record", candidate.headline)


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
            for fmt in ("%B %d, %Y", "%b %d, %Y", "%m/%d/%Y"):
                try:
                    parsed = datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
                    break
                except ValueError:
                    parsed = None
            if parsed is None:
                return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def recover_candidate_metadata(candidate: Candidate, body: str) -> Candidate:
    """Recover title/date from one destination page; never scans inventories."""
    title = ""
    for pattern in (r'<meta[^>]+(?:property|name)=["\'](?:og:title|twitter:title)["\'][^>]+content=["\']([^"\']+)', r'<title[^>]*>(.*?)</title>', r'<h1[^>]*>(.*?)</h1>'):
        match = re.search(pattern, body, re.I | re.S)
        if match:
            title = re.sub(r"<[^>]+>", " ", match.group(1))
            title = " ".join(title.split())
            if title:
                break
    date = None
    for pattern in (r'(?i)(?:datePublished|article:published_time|pubdate)["\'\s:=]+([0-9]{4}-[0-9]{2}-[0-9]{2}(?:T[^"\' <]+)?)', r'(?i)<time[^>]+datetime=["\']([^"\']+)'):
        match = re.search(pattern, body)
        if match:
            date = _date(match.group(1))
            if date:
                break
    return Candidate(candidate.source_id, title or candidate.headline, candidate.url, date or candidate.published_at)


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
        self.pending_date = None
        self.current_date_text = []
        self.heading_text = []
        self.in_heading = False
        self.in_date = False
        self.last_heading = ""
        self.last_candidate_index = None
        self.candidates = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = set((attrs.get("class") or "").casefold().split())
        if tag in {"h1", "h2", "h3", "h4"}:
            self.in_heading, self.heading_text = True, []
        elif tag == "a" and attrs.get("href"):
            self.current_href, self.current_text, self.current_date = attrs["href"], [], self.pending_date
            self.pending_date = None
        elif tag == "time" and attrs.get("datetime"):
            if self.current_href:
                self.current_date = attrs["datetime"]
            elif self.last_candidate_index is not None:
                self.candidates[self.last_candidate_index] = Candidate(self.source_id, self.candidates[self.last_candidate_index].headline, self.candidates[self.last_candidate_index].url, _date(attrs["datetime"]))
        if tag in {"time", "span", "div", "p"} and (tag == "time" or "date" in classes or "published" in classes or "secondaryheader" in classes):
            self.in_date, self.current_date_text = True, []

    def handle_data(self, data):
        if self.in_heading:
            self.heading_text.append(data)
        if self.current_href:
            self.current_text.append(data)
        if self.in_date:
            self.current_date_text.append(data)

    def handle_endtag(self, tag):
        if tag in {"h1", "h2", "h3", "h4"} and self.in_heading:
            self.last_heading = " ".join("".join(self.heading_text).split())
            self.in_heading = False
        if self.in_date and tag in {"time", "span", "div", "p"}:
            parsed_date = _date(" ".join("".join(self.current_date_text).split()))
            if self.current_href:
                self.current_date = parsed_date or self.current_date
            elif self.last_candidate_index is not None and parsed_date:
                existing = self.candidates[self.last_candidate_index]
                self.candidates[self.last_candidate_index] = Candidate(existing.source_id, existing.headline, existing.url, parsed_date)
            elif parsed_date:
                self.pending_date = parsed_date
            self.in_date = False
        if tag == "a" and self.current_href:
            anchor = " ".join("".join(self.current_text).split())
            headline = self.last_heading if anchor.casefold() in GENERIC_HEADLINES and self.last_heading else anchor
            resolved_url = urljoin(self.base_url, self.current_href)
            path = urlsplit(resolved_url).path
            allowed = not self.allowed_path_prefixes or any(path.startswith(prefix) for prefix in self.allowed_path_prefixes)
            if headline and resolved_url.lower().startswith(("http://", "https://")) and allowed:
                self.candidates.append(Candidate(self.source_id, headline, resolved_url, _date(self.current_date)))
                self.last_candidate_index = len(self.candidates) - 1
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
