"""SQLite system of record for the insurance story discovery workflow."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parent
SCHEMA_PATH = ROOT / "schema.sql"
VALID_ITEM_STATUSES = {"NEW", "PROMOTED", "IGNORED"}
VALID_SUMMARY_STATUSES = {"PENDING", "COMPLETE", "FAILED", "SKIPPED"}
_TRACKING_KEYS = {"fbclid", "gclid", "mc_cid", "mc_eid", "ref_src"}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def canonicalize_url(url: str) -> str:
    """Return a deterministic URL representation for cheap exact deduplication."""
    value = url.strip()
    if not value:
        raise ValueError("URL cannot be empty")
    parsed = urlsplit(value)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("URL must be an absolute HTTP(S) URL")
    hostname = parsed.hostname.lower()
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    port = parsed.port
    netloc = hostname
    if port and not ((parsed.scheme.lower() == "http" and port == 80) or (parsed.scheme.lower() == "https" and port == 443)):
        netloc += f":{port}"
    path = parsed.path or "/"
    if path != "/":
        path = path.rstrip("/")
    query_pairs = [
        (key, val)
        for key, val in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in _TRACKING_KEYS
    ]
    query_pairs.sort()
    return urlunsplit((parsed.scheme.lower(), netloc, path, urlencode(query_pairs, doseq=True), ""))


def url_hash(canonical_url: str) -> str:
    return hashlib.sha256(canonical_url.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Item:
    item_id: int
    source_id: int
    headline: str
    raw_url: str
    canonical_url: str
    url_hash: str
    published_at: str | None
    first_seen_at: str
    status: str
    validation_reason: str = ""


class StoryStore:
    @staticmethod
    def _item_id(value: int | Item) -> int:
        return value.item_id if isinstance(value, Item) else value

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(SCHEMA_PATH.read_text())
            note_columns = {row[1] for row in connection.execute("PRAGMA table_info(candidate_notes)")}
            if "display_headline" not in note_columns:
                connection.execute("ALTER TABLE candidate_notes ADD COLUMN display_headline TEXT NOT NULL DEFAULT ''")
            columns = {row[1] for row in connection.execute("PRAGMA table_info(sources)")}
            migrations = {
                "collector_type": "TEXT NOT NULL DEFAULT ''",
                "poll_url": "TEXT NOT NULL DEFAULT ''",
                "collector_config": "TEXT NOT NULL DEFAULT '{}'",
                "last_checked_at": "TEXT",
                "last_success_at": "TEXT",
                "last_error": "TEXT",
            }
            for name, definition in migrations.items():
                if name not in columns:
                    connection.execute(f"ALTER TABLE sources ADD COLUMN {name} {definition}")
            item_columns = {row[1] for row in connection.execute("PRAGMA table_info(items)")}
            if "validation_reason" not in item_columns:
                connection.execute("ALTER TABLE items ADD COLUMN validation_reason TEXT NOT NULL DEFAULT ''")

    def add_source(self, name: str, lane: str, jurisdiction: str, source_url: str,
                   source_type: str = "web", active: bool = True, notes: str = "",
                   collector_type: str = "", poll_url: str | None = None,
                   collector_config: str = "{}") -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                """INSERT INTO sources
                   (name, lane, jurisdiction, source_url, source_type, active, created_at, notes,
                    collector_type, poll_url, collector_config)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (name, lane, jurisdiction, canonicalize_url(source_url), source_type, int(active), utc_now(), notes,
                 collector_type, canonicalize_url(poll_url or source_url), collector_config),
            )
            return int(cursor.lastrowid or 0)

    def add_item(self, source_id: int, headline: str, raw_url: str,
                 published_at: str | None = None, first_seen_at: str | None = None) -> Item:
        canonical = canonicalize_url(raw_url)
        digest = url_hash(canonical)
        seen = first_seen_at or utc_now()
        with self._connect() as connection:
            if connection.execute("SELECT 1 FROM sources WHERE source_id = ?", (source_id,)).fetchone() is None:
                raise ValueError(f"Unknown source_id: {source_id}")
            connection.execute(
                """INSERT OR IGNORE INTO items
                   (source_id, headline, raw_url, canonical_url, url_hash, published_at, first_seen_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (source_id, headline, raw_url, canonical, digest, published_at, seen),
            )
            row = connection.execute("SELECT * FROM items WHERE canonical_url = ?", (canonical,)).fetchone()
            return Item(**dict(row))

    def add_item_with_result(self, source_id: int, headline: str, raw_url: str,
                             published_at: str | None = None,
                             first_seen_at: str | None = None) -> tuple[Item, bool]:
        canonical = canonicalize_url(raw_url)
        digest = url_hash(canonical)
        seen = first_seen_at or utc_now()
        with self._connect() as connection:
            if connection.execute("SELECT 1 FROM sources WHERE source_id = ?", (source_id,)).fetchone() is None:
                raise ValueError(f"Unknown source_id: {source_id}")
            cursor = connection.execute(
                """INSERT OR IGNORE INTO items
                   (source_id, headline, raw_url, canonical_url, url_hash, published_at, first_seen_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (source_id, headline, raw_url, canonical, digest, published_at, seen),
            )
            row = connection.execute("SELECT * FROM items WHERE canonical_url = ?", (canonical,)).fetchone()
            return Item(**dict(row)), cursor.rowcount == 1

    def update_source_collector(self, source_id: int, *, source_url: str | None = None,
                                collector_type: str, poll_url: str, collector_config: str = "{}") -> None:
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE sources SET source_url = COALESCE(?, source_url), collector_type = ?, poll_url = ?, collector_config = ? WHERE source_id = ?",
                (canonicalize_url(source_url) if source_url else None, collector_type, canonicalize_url(poll_url), collector_config, source_id),
            )
            if cursor.rowcount == 0:
                raise ValueError(f"Unknown source_id: {source_id}")

    def update_source_poll_state(self, source_id: int, *, checked_at: str,
                                 success_at: str | None = None, error: str | None = None) -> None:
        with self._connect() as connection:
            if connection.execute("SELECT 1 FROM sources WHERE source_id = ?", (source_id,)).fetchone() is None:
                raise ValueError(f"Unknown source_id: {source_id}")
            if error is None:
                connection.execute(
                    "UPDATE sources SET last_checked_at = ?, last_success_at = ?, last_error = '' WHERE source_id = ?",
                    (checked_at, success_at, source_id),
                )
            else:
                connection.execute(
                    "UPDATE sources SET last_checked_at = ?, last_error = ? WHERE source_id = ?",
                    (checked_at, error, source_id),
                )

    def list_items(self, limit: int | None = None, status: str | None = None) -> list[dict]:
        query = """SELECT i.*, s.name AS source_name, s.lane, s.jurisdiction,
                         COALESCE(n.display_headline, '') AS display_headline, COALESCE(n.gist, '') AS gist, COALESCE(n.summary_status, 'PENDING') AS summary_status,
                         n.summary_prompt_version, n.summarized_at, n.extracted_char_count,
                         n.input_char_count, n.extraction_method, COALESCE(n.summary_error, '') AS summary_error
                  FROM items i JOIN sources s ON s.source_id = i.source_id
                  LEFT JOIN candidate_notes n ON n.item_id = i.item_id"""
        params: list[object] = []
        if status:
            query += " WHERE i.status = ?"
            params.append(status)
        query += " ORDER BY COALESCE(i.published_at, i.first_seen_at) DESC, i.item_id DESC"
        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)
        with self._connect() as connection:
            return [dict(row) for row in connection.execute(query, params)]

    def get_item(self, item_id: int | Item) -> dict:
        item_id = self._item_id(item_id)
        with self._connect() as connection:
            row = connection.execute(
                """SELECT i.*, s.name AS source_name, s.lane, s.jurisdiction,
                          COALESCE(n.display_headline, '') AS display_headline, COALESCE(n.gist, '') AS gist, COALESCE(n.summary_status, 'PENDING') AS summary_status,
                          n.summary_prompt_version, n.summarized_at, n.extracted_char_count,
                          n.input_char_count, n.extraction_method, COALESCE(n.summary_error, '') AS summary_error
                   FROM items i JOIN sources s ON s.source_id = i.source_id
                   LEFT JOIN candidate_notes n ON n.item_id = i.item_id WHERE i.item_id = ?""",
                (item_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"Unknown item_id: {item_id}")
            return dict(row)

    def list_unsummarized_items(self, limit: int | None = None, *, active_only: bool = False) -> list[dict]:
        query = """SELECT i.*, s.name AS source_name, s.lane, s.jurisdiction,
                          COALESCE(n.summary_status, 'PENDING') AS summary_status,
                          COALESCE(n.display_headline, '') AS display_headline
                   FROM items i JOIN sources s ON s.source_id = i.source_id
                   LEFT JOIN candidate_notes n ON n.item_id = i.item_id
                   WHERE (COALESCE(n.summary_status, 'PENDING') != 'COMPLETE'
                      OR (COALESCE(n.summary_status, 'PENDING') = 'COMPLETE' AND COALESCE(n.display_headline, '') = '')
                   )"""
        if active_only:
            query += " AND i.status = 'NEW'"
        query += " ORDER BY COALESCE(i.published_at, i.first_seen_at) DESC, i.item_id DESC"
        params: list[object] = []
        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)
        with self._connect() as connection:
            return [dict(row) for row in connection.execute(query, params)]

    def save_summary(self, item_id: int | Item, *, display_headline: str = "", gist: str = "", summary_status: str = "COMPLETE",
                     summary_prompt_version: str = "", summarized_at: str | None = None,
                     extracted_char_count: int | None = None, input_char_count: int | None = None,
                     extraction_method: str = "", summary_error: str = "") -> None:
        item_id = self._item_id(item_id)
        if summary_status not in VALID_SUMMARY_STATUSES:
            raise ValueError(f"Invalid summary status: {summary_status}")
        with self._connect() as connection:
            if connection.execute("SELECT 1 FROM items WHERE item_id = ?", (item_id,)).fetchone() is None:
                raise ValueError(f"Unknown item_id: {item_id}")
            connection.execute("""INSERT INTO candidate_notes
                (item_id, display_headline, gist, summary_status, summary_prompt_version, summarized_at,
                 extracted_char_count, input_char_count, extraction_method, summary_error)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(item_id) DO UPDATE SET display_headline=excluded.display_headline, gist=excluded.gist,
                  summary_status=excluded.summary_status, summary_prompt_version=excluded.summary_prompt_version,
                  summarized_at=excluded.summarized_at, extracted_char_count=excluded.extracted_char_count,
                  input_char_count=excluded.input_char_count, extraction_method=excluded.extraction_method,
                  summary_error=excluded.summary_error""",
                (item_id, display_headline, gist, summary_status, summary_prompt_version,
                 summarized_at or (utc_now() if summary_status == "COMPLETE" else None),
                 extracted_char_count, input_char_count, extraction_method, summary_error))

    def get_validation_cache(self, cache_key: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute("SELECT decision, reason, headline FROM candidate_validation_cache WHERE cache_key=?", (cache_key,)).fetchone()
            return dict(row) if row else None

    def save_validation_cache(self, cache_key: str, decision: str, reason: str, headline: str) -> None:
        with self._connect() as connection:
            connection.execute("""INSERT INTO candidate_validation_cache(cache_key, decision, reason, headline, checked_at)
                VALUES (?, ?, ?, ?, ?) ON CONFLICT(cache_key) DO UPDATE SET decision=excluded.decision,
                reason=excluded.reason, headline=excluded.headline, checked_at=excluded.checked_at""",
                (cache_key, decision, reason, headline, utc_now()))

    def quarantine_invalid_items(self, *, now: str | None = None) -> dict[str, int]:
        """Quarantine only unpromoted invalid inventory rows; project rows are preserved."""
        from collectors import Candidate, validate_candidate

        counts = {"checked": 0, "quarantined": 0, "preserved_projects": 0}
        with self._connect() as connection:
            rows = connection.execute("""SELECT i.*, s.collector_config FROM items i JOIN sources s ON s.source_id=i.source_id
                WHERE i.status != 'PROMOTED'""").fetchall()
            for row in rows:
                counts["checked"] += 1
                if connection.execute("SELECT 1 FROM story_projects WHERE origin_item_id = ?", (row["item_id"],)).fetchone():
                    counts["preserved_projects"] += 1
                    continue
                result = validate_candidate(dict(row), Candidate(row["source_id"], row["headline"], row["raw_url"], row["published_at"]), now=now)
                if result.decision in {"navigation_archive", "old"}:
                    connection.execute("UPDATE items SET status='IGNORED', validation_reason=? WHERE item_id=?", (result.reason, row["item_id"]))
                    counts["quarantined"] += 1
        return counts

    def update_item_status(self, item_id: int | Item, status: str) -> None:
        item_id = self._item_id(item_id)
        if status not in VALID_ITEM_STATUSES:
            raise ValueError(f"Invalid item status: {status}")
        with self._connect() as connection:
            cursor = connection.execute("UPDATE items SET status = ? WHERE item_id = ?", (status, item_id))
            if cursor.rowcount == 0:
                raise ValueError(f"Unknown item_id: {item_id}")

    def promote_item(self, item_id: int | Item, title: str, status: str = "ACTIVE") -> int:
        item_id = self._item_id(item_id)
        now = utc_now()
        with self._connect() as connection:
            item = connection.execute("SELECT 1 FROM items WHERE item_id = ?", (item_id,)).fetchone()
            if item is None:
                raise ValueError(f"Unknown item_id: {item_id}")
            existing = connection.execute(
                "SELECT story_id FROM story_projects WHERE origin_item_id = ?", (item_id,)
            ).fetchone()
            if existing:
                return int(existing[0])
            cursor = connection.execute(
                """INSERT INTO story_projects
                   (origin_item_id, title, status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (item_id, title, status, now, now),
            )
            connection.execute("UPDATE items SET status = 'PROMOTED' WHERE item_id = ?", (item_id,))
            return int(cursor.lastrowid or 0)

    def update_story_links(self, story_id: int, *, google_doc_url: str | None = None,
                           telegram_thread_url: str | None = None,
                           research_folder_url: str | None = None, status: str | None = None) -> None:
        fields = {"updated_at": utc_now()}
        if google_doc_url is not None:
            fields["google_doc_url"] = google_doc_url
        if telegram_thread_url is not None:
            fields["telegram_thread_url"] = telegram_thread_url
        if research_folder_url is not None:
            fields["research_folder_url"] = research_folder_url
        if status is not None:
            fields["status"] = status
        assignments = ", ".join(f"{key} = ?" for key in fields)
        with self._connect() as connection:
            cursor = connection.execute(
                f"UPDATE story_projects SET {assignments} WHERE story_id = ?",
                [*fields.values(), story_id],
            )
            if cursor.rowcount == 0:
                raise ValueError(f"Unknown story_id: {story_id}")

    def get_story(self, story_id: int) -> dict:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT p.*, i.headline AS origin_headline, i.raw_url AS origin_url,
                          i.published_at, s.name AS source_name, s.lane
                   FROM story_projects p
                   JOIN items i ON i.item_id = p.origin_item_id
                   JOIN sources s ON s.source_id = i.source_id
                   WHERE p.story_id = ?""",
                (story_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"Unknown story_id: {story_id}")
            return dict(row)

    def list_stories(self) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT p.*, i.headline AS origin_headline, i.raw_url AS origin_url,
                          i.published_at, s.name AS source_name, s.lane
                   FROM story_projects p
                   JOIN items i ON i.item_id = p.origin_item_id
                   JOIN sources s ON s.source_id = i.source_id
                   ORDER BY p.updated_at DESC, p.story_id DESC"""
            ).fetchall()
            return [dict(row) for row in rows]

    def list_sources(self) -> list[dict]:
        with self._connect() as connection:
            return [dict(row) for row in connection.execute("SELECT * FROM sources ORDER BY name")]

    def set_source_active(self, source_id: int, active: bool) -> None:
        with self._connect() as connection:
            cursor = connection.execute("UPDATE sources SET active=? WHERE source_id=?", (int(active), source_id))
            if cursor.rowcount == 0:
                raise ValueError(f"Unknown source_id: {source_id}")

    def list_sources_for_management(self, *, as_of: str | None = None) -> list[dict]:
        reference = datetime.fromisoformat((as_of or utc_now()).replace("Z", "+00:00"))
        cutoff = (reference - timedelta(days=30)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        with self._connect() as connection:
            rows = connection.execute("""SELECT s.*, COALESCE((SELECT COUNT(*) FROM items i
                WHERE i.source_id=s.source_id AND i.published_at IS NOT NULL AND i.published_at >= ?), 0) AS publications_last_30_days
                FROM sources s ORDER BY s.name, s.source_id""", (cutoff,)).fetchall()
            return [dict(row) for row in rows]

    def propose_source(self, *, name: str, organization: str, jurisdiction: str, lane: str,
                       proposed_url: str, collector_type: str = "", poll_url: str | None = None,
                       collector_config: str = "{}", publication_type: str = "", reason: str = "") -> tuple[int, bool]:
        normalized = canonicalize_url(proposed_url)
        normalized_poll = canonicalize_url(poll_url or proposed_url)
        with self._connect() as connection:
            existing = connection.execute("SELECT candidate_id FROM source_candidates WHERE normalized_url IN (?, ?)", (normalized, normalized_poll)).fetchone()
            if existing:
                return int(existing[0]), False
            source = connection.execute("SELECT source_id FROM sources WHERE source_url IN (?, ?) OR poll_url IN (?, ?)",
                                       (normalized, normalized_poll, normalized, normalized_poll)).fetchone()
            if source:
                return int(source[0]), False
            cursor = connection.execute("""INSERT INTO source_candidates
                (name, organization, jurisdiction, lane, proposed_url, normalized_url, collector_type, poll_url,
                 collector_config, publication_type, discovered_at, reason)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (name, organization, jurisdiction, lane, proposed_url, normalized, collector_type,
                 canonicalize_url(poll_url or proposed_url), collector_config, publication_type, utc_now(), reason))
            return int(cursor.lastrowid or 0), True

    def list_source_candidates(self, status: str | None = None) -> list[dict]:
        query = "SELECT * FROM source_candidates"
        params: list[object] = []
        if status:
            query += " WHERE status=?"; params.append(status)
        query += " ORDER BY discovered_at DESC, candidate_id DESC"
        with self._connect() as connection:
            return [dict(row) for row in connection.execute(query, params)]

    def get_source_candidate(self, candidate_id: int) -> dict:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM source_candidates WHERE candidate_id=?", (candidate_id,)).fetchone()
            if not row:
                raise ValueError(f"Unknown candidate_id: {candidate_id}")
            return dict(row)

    def update_source_candidate(self, candidate_id: int, *, status: str | None = None,
                                validation_status: str | None = None, validation_result: dict | None = None,
                                reason: str | None = None) -> None:
        fields, values = [], []
        if status is not None:
            if status not in {"PROPOSED", "APPROVED", "REJECTED", "ENROLLED"}:
                raise ValueError(f"Invalid source candidate status: {status}")
            fields.append("status=?"); values.append(status)
        if validation_status is not None:
            fields.append("validation_status=?"); values.append(validation_status)
        if validation_result is not None:
            fields.append("validation_result=?"); values.append(json.dumps(validation_result, sort_keys=True))
        if reason is not None:
            fields.append("reason=?"); values.append(reason)
        fields.append("last_checked_at=?"); values.append(utc_now())
        values.append(candidate_id)
        with self._connect() as connection:
            cursor = connection.execute(f"UPDATE source_candidates SET {', '.join(fields)} WHERE candidate_id=?", values)
            if cursor.rowcount == 0:
                raise ValueError(f"Unknown candidate_id: {candidate_id}")

    def enroll_source_candidate(self, candidate_id: int) -> int:
        candidate = self.get_source_candidate(candidate_id)
        if candidate["status"] == "ENROLLED" and candidate.get("enrolled_source_id"):
            return int(candidate["enrolled_source_id"])
        if candidate["status"] != "APPROVED":
            raise ValueError("Only APPROVED source candidates can be enrolled")
        with self._connect() as connection:
            existing = connection.execute("SELECT source_id FROM sources WHERE source_url=? OR poll_url=?",
                                          (candidate["normalized_url"], candidate["poll_url"])).fetchone()
            if existing:
                source_id = int(existing[0])
            else:
                cursor = connection.execute("""INSERT INTO sources
                    (name, lane, jurisdiction, source_url, source_type, active, created_at, notes,
                     collector_type, poll_url, collector_config)
                    VALUES (?, ?, ?, ?, 'web', 1, ?, ?, ?, ?, ?)""",
                    (candidate["name"], candidate["lane"], candidate["jurisdiction"], candidate["normalized_url"], utc_now(),
                     candidate["reason"], candidate["collector_type"], candidate["poll_url"], candidate["collector_config"]))
                source_id = int(cursor.lastrowid or 0)
            connection.execute("UPDATE source_candidates SET status='ENROLLED', enrolled_source_id=?, last_checked_at=? WHERE candidate_id=?",
                               (source_id, utc_now(), candidate_id))
            return source_id

    def seed_discovery_targets(self, targets: list[dict]) -> int:
        with self._connect() as connection:
            count = 0
            for target in targets:
                cursor = connection.execute("""INSERT OR IGNORE INTO discovery_targets(name, lane, jurisdiction, query, metadata)
                    VALUES (?, ?, ?, ?, ?)""", (target["name"], target["lane"], target["jurisdiction"], target.get("query", ""), json.dumps(target.get("metadata", {}), sort_keys=True)))
                count += cursor.rowcount
            return count

    def select_discovery_targets(self, limit: int = 5) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM discovery_targets ORDER BY COALESCE(last_selected_at, ''), selection_count, target_id LIMIT ?", (limit,)).fetchall()
            now = utc_now()
            for row in rows:
                connection.execute("UPDATE discovery_targets SET last_selected_at=?, selection_count=selection_count+1 WHERE target_id=?", (now, row["target_id"]))
            return [dict(row) for row in rows]

    def record_discovery_run(self, *, started_at: str, completed_at: str, targets_checked: int,
                             proposals_created: int, sources_validated: int, sources_failed: int, summary: dict) -> int:
        with self._connect() as connection:
            cursor = connection.execute("""INSERT INTO discovery_runs(started_at, completed_at, targets_checked, proposals_created, sources_validated, sources_failed, summary)
                VALUES (?, ?, ?, ?, ?, ?, ?)""", (started_at, completed_at, targets_checked, proposals_created, sources_validated, sources_failed, json.dumps(summary, sort_keys=True)))
            return int(cursor.lastrowid or 0)

    def coverage_report(self) -> dict:
        with self._connect() as connection:
            def rows(query): return [dict(row) for row in connection.execute(query)]
            return {
                "active_by_lane": rows("SELECT lane, COUNT(*) AS count FROM sources WHERE active=1 GROUP BY lane ORDER BY lane"),
                "active_by_jurisdiction": rows("SELECT jurisdiction, COUNT(*) AS count FROM sources WHERE active=1 GROUP BY jurisdiction ORDER BY jurisdiction"),
                "proposal_status": rows("SELECT status, COUNT(*) AS count FROM source_candidates GROUP BY status ORDER BY status"),
                "poll_health": rows("SELECT name, lane, jurisdiction, last_success_at, last_error, (SELECT COUNT(*) FROM items i WHERE i.source_id=s.source_id) AS publications FROM sources s WHERE active=1 ORDER BY name"),
                "coverage_gaps": rows("SELECT lane, jurisdiction, COUNT(*) AS targets, SUM(CASE WHEN last_selected_at IS NULL THEN 1 ELSE 0 END) AS unvisited_targets FROM discovery_targets GROUP BY lane, jurisdiction ORDER BY lane, jurisdiction"),
                "discovery_runs": rows("SELECT * FROM discovery_runs ORDER BY run_id DESC LIMIT 10"),
            }
