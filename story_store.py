"""SQLite system of record for the insurance story discovery workflow."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import copy
import secrets
import hmac
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parent
SCHEMA_PATH = ROOT / "schema.sql"
VALID_ITEM_STATUSES = {"NEW", "PROMOTED", "IGNORED", "DISMISSED"}
VALID_SUMMARY_STATUSES = {"PENDING", "COMPLETE", "FAILED", "SKIPPED"}
MAX_FEEDBACK_REASON_LENGTH = 64
MAX_FEEDBACK_NOTE_LENGTH = 1000
FEEDBACK_MIGRATION_VERSION = "feedback_v1"
VALID_DISMISSAL_REASONS = {
    "not_relevant", "duplicate", "too_old", "weak_source", "other", "manual_cli"
}
_TRACKING_KEYS = {"fbclid", "gclid", "mc_cid", "mc_eid", "ref_src"}


class _BorrowedConnection:
    """A context wrapper that does not commit or close its snapshot connection."""

    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def __enter__(self) -> sqlite3.Connection:
        return self.connection

    def __exit__(self, *_args) -> None:
        return None


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
        connection = sqlite3.connect(self.db_path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    @contextmanager
    def read_snapshot(self):
        """Yield a read-only store facade pinned to one SQLite transaction."""
        connection = self._connect()
        try:
            connection.execute("BEGIN")
            snapshot = copy.copy(self)
            snapshot._connect = lambda: _BorrowedConnection(connection)  # type: ignore[method-assign]
            yield snapshot
        finally:
            connection.rollback()
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(SCHEMA_PATH.read_text())
            item_schema = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='items'"
            ).fetchone()[0]
            if "DISMISSED" not in item_schema:
                # SQLite cannot alter a CHECK constraint in place. Rebuild only
                # this small table while preserving all item IDs and metadata.
                connection.execute("PRAGMA foreign_keys = OFF")
                connection.execute("PRAGMA legacy_alter_table = ON")
                connection.execute("DROP INDEX IF EXISTS idx_items_first_seen")
                connection.execute("DROP INDEX IF EXISTS idx_items_status")
                connection.execute("ALTER TABLE items RENAME TO items_legacy")
                connection.execute("""CREATE TABLE items (
                    item_id INTEGER PRIMARY KEY,
                    source_id INTEGER NOT NULL REFERENCES sources(source_id),
                    headline TEXT NOT NULL,
                    raw_url TEXT NOT NULL,
                    canonical_url TEXT NOT NULL UNIQUE,
                    url_hash TEXT NOT NULL UNIQUE,
                    published_at TEXT,
                    first_seen_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'NEW' CHECK (status IN ('NEW', 'PROMOTED', 'IGNORED', 'DISMISSED')),
                    validation_reason TEXT NOT NULL DEFAULT ''
                )""")
                connection.execute("""INSERT INTO items
                    (item_id, source_id, headline, raw_url, canonical_url, url_hash,
                     published_at, first_seen_at, status, validation_reason)
                    SELECT item_id, source_id, headline, raw_url, canonical_url, url_hash,
                           published_at, first_seen_at, status, validation_reason
                    FROM items_legacy""")
                connection.execute("DROP TABLE items_legacy")
                connection.execute("CREATE INDEX idx_items_first_seen ON items(first_seen_at DESC)")
                connection.execute("CREATE INDEX idx_items_status ON items(status)")
                connection.execute("PRAGMA legacy_alter_table = OFF")
                connection.execute("PRAGMA foreign_keys = ON")
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
            project_columns = {row[1] for row in connection.execute("PRAGMA table_info(story_projects)")}
            if "phase2_completion_token_hash" not in project_columns:
                connection.execute("ALTER TABLE story_projects ADD COLUMN phase2_completion_token_hash TEXT NOT NULL DEFAULT ''")

            # Commit unrelated historical schema upgrades before opening the
            # all-or-nothing feedback migration transaction.
            connection.commit()
            self._ensure_feedback_migration(connection)

    def _ensure_feedback_migration(self, connection: sqlite3.Connection) -> None:
        if connection.execute(
            "SELECT 1 FROM schema_migrations WHERE version = ?",
            (FEEDBACK_MIGRATION_VERSION,),
        ).fetchone():
            return

        # Recheck after acquiring the write lock so concurrent Store
        # constructors cannot both apply/backfill this migration.
        connection.execute("BEGIN IMMEDIATE")
        if connection.execute(
            "SELECT 1 FROM schema_migrations WHERE version = ?",
            (FEEDBACK_MIGRATION_VERSION,),
        ).fetchone():
            return
        self._migrate_feedback_v1(connection)
        connection.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            (FEEDBACK_MIGRATION_VERSION, utc_now()),
        )

    def _migrate_feedback_v1(self, connection: sqlite3.Connection) -> None:
        connection.execute("""CREATE TABLE IF NOT EXISTS item_feedback_events (
            feedback_id INTEGER PRIMARY KEY,
            item_id INTEGER NOT NULL REFERENCES items(item_id),
            action TEXT NOT NULL CHECK (action IN ('DISMISS', 'RESTORE')),
            reason_code TEXT NOT NULL DEFAULT '' CHECK (length(reason_code) <= 64),
            note TEXT NOT NULL DEFAULT '' CHECK (length(note) <= 1000),
            prior_status TEXT NOT NULL CHECK (prior_status IN ('NEW', 'PROMOTED', 'IGNORED', 'DISMISSED')),
            resulting_status TEXT NOT NULL CHECK (resulting_status IN ('NEW', 'PROMOTED', 'IGNORED', 'DISMISSED')),
            reverts_feedback_id INTEGER REFERENCES item_feedback_events(feedback_id),
            actor_sub TEXT NOT NULL CHECK (
                length(actor_sub) <= 255 AND
                length(trim(actor_sub, ' ' || char(9) || char(10) || char(11) || char(12) || char(13))) > 0
            ),
            actor_email TEXT NOT NULL CHECK (
                length(actor_email) <= 320 AND
                length(trim(actor_email, ' ' || char(9) || char(10) || char(11) || char(12) || char(13))) > 0
            ),
            request_id TEXT NOT NULL UNIQUE CHECK (
                length(request_id) <= 255 AND
                length(trim(request_id, ' ' || char(9) || char(10) || char(11) || char(12) || char(13))) > 0
            ),
            created_at TEXT NOT NULL,
            CHECK (
                (action = 'DISMISS' AND reason_code != '' AND prior_status = 'NEW'
                    AND resulting_status = 'DISMISSED' AND reverts_feedback_id IS NULL)
                OR
                (action = 'RESTORE' AND reason_code = '' AND prior_status = 'DISMISSED'
                    AND resulting_status = 'NEW' AND reverts_feedback_id IS NOT NULL)
            )
        )""")
        connection.execute("""CREATE INDEX IF NOT EXISTS idx_item_feedback_item_created
            ON item_feedback_events(item_id, feedback_id)""")
        connection.execute("""CREATE UNIQUE INDEX IF NOT EXISTS idx_item_feedback_reverts
            ON item_feedback_events(reverts_feedback_id)""")
        connection.execute("""INSERT INTO item_feedback_events
            (item_id, action, reason_code, note, prior_status, resulting_status,
             reverts_feedback_id, actor_sub, actor_email, request_id, created_at)
            SELECT i.item_id, 'DISMISS', 'legacy_backfill', '', 'NEW', 'DISMISSED',
                   NULL, 'migration', 'migration',
                   'migration-dismissed-' || i.item_id, i.first_seen_at
            FROM items i
            WHERE i.status = 'DISMISSED'
              AND NOT EXISTS (
                  SELECT 1 FROM item_feedback_events f
                  WHERE f.item_id = i.item_id AND f.action = 'DISMISS'
              )
            ON CONFLICT(request_id) DO NOTHING""")
        self._install_feedback_triggers(connection)

    def _install_feedback_triggers(self, connection: sqlite3.Connection) -> None:
        connection.execute("""CREATE TRIGGER IF NOT EXISTS item_feedback_events_validate_identity
            BEFORE INSERT ON item_feedback_events
            WHEN length(NEW.actor_sub) > 255
              OR length(trim(NEW.actor_sub, ' ' || char(9) || char(10) || char(11) || char(12) || char(13))) = 0
              OR length(NEW.actor_email) > 320
              OR length(trim(NEW.actor_email, ' ' || char(9) || char(10) || char(11) || char(12) || char(13))) = 0
              OR length(NEW.request_id) > 255
              OR length(trim(NEW.request_id, ' ' || char(9) || char(10) || char(11) || char(12) || char(13))) = 0
            BEGIN
                SELECT RAISE(ABORT, 'feedback identity values must be nonblank and bounded');
            END""")
        connection.execute("""CREATE TRIGGER IF NOT EXISTS item_feedback_events_validate_restore
            BEFORE INSERT ON item_feedback_events
            WHEN NEW.action = 'RESTORE' AND NOT EXISTS (
                SELECT 1 FROM item_feedback_events dismissed
                WHERE dismissed.feedback_id = NEW.reverts_feedback_id
                  AND dismissed.action = 'DISMISS'
                  AND dismissed.item_id = NEW.item_id
            )
            BEGIN
                SELECT RAISE(ABORT, 'restore must reference a dismissal for the same item');
            END""")
        connection.execute("""CREATE TRIGGER IF NOT EXISTS item_feedback_events_no_update
            BEFORE UPDATE ON item_feedback_events
            BEGIN
                SELECT RAISE(ABORT, 'item feedback events are immutable');
            END""")
        connection.execute("""CREATE TRIGGER IF NOT EXISTS item_feedback_events_no_delete
            BEFORE DELETE ON item_feedback_events
            BEGIN
                SELECT RAISE(ABORT, 'item feedback events are immutable');
            END""")

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
                         n.input_char_count, n.extraction_method, COALESCE(n.summary_error, '') AS summary_error,
                         s.collector_config
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
                          n.input_char_count, n.extraction_method, COALESCE(n.summary_error, '') AS summary_error,
                          s.collector_config
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
                          COALESCE(n.display_headline, '') AS display_headline,
                          s.collector_config
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
                WHERE i.status NOT IN ('PROMOTED', 'DISMISSED')""").fetchall()
            for row in rows:
                counts["checked"] += 1
                if connection.execute("SELECT 1 FROM story_projects WHERE origin_item_id = ?", (row["item_id"],)).fetchone():
                    counts["preserved_projects"] += 1
                    continue
                result = validate_candidate(dict(row), Candidate(row["source_id"], row["headline"], row["raw_url"], row["published_at"]), now=now)
                if result.decision in {"navigation_archive", "old"}:
                    cursor = connection.execute(
                        """UPDATE items SET status='IGNORED', validation_reason=?
                           WHERE item_id=? AND status != 'DISMISSED'""",
                        (result.reason, row["item_id"]),
                    )
                    counts["quarantined"] += cursor.rowcount
        return counts

    def update_item_status(self, item_id: int | Item, status: str) -> None:
        item_id = self._item_id(item_id)
        if status not in VALID_ITEM_STATUSES:
            raise ValueError(f"Invalid item status: {status}")
        if status == "DISMISSED":
            raise ValueError("DISMISSED status requires dismiss_item so an audit event is recorded")
        with self._connect() as connection:
            cursor = connection.execute(
                """UPDATE items SET status = ?
                   WHERE item_id = ? AND status != 'DISMISSED'""",
                (status, item_id),
            )
            if cursor.rowcount == 0:
                item = connection.execute(
                    "SELECT status FROM items WHERE item_id = ?", (item_id,)
                ).fetchone()
                if item is None:
                    raise ValueError(f"Unknown item_id: {item_id}")
                raise ValueError("DISMISSED items must be transitioned with restore_item")

    @staticmethod
    def _validate_feedback_identity(actor_sub: str, actor_email: str, request_id: str) -> None:
        for name, value, maximum in (
            ("actor_sub", actor_sub, 255),
            ("actor_email", actor_email, 320),
            ("request_id", request_id, 255),
        ):
            if not isinstance(value, str) or not value.strip() or len(value) > maximum:
                raise ValueError(f"{name} must be a non-empty string of at most {maximum} characters")

    @staticmethod
    def _feedback_row(connection: sqlite3.Connection, request_id: str) -> sqlite3.Row | None:
        return connection.execute(
            "SELECT * FROM item_feedback_events WHERE request_id = ?", (request_id,)
        ).fetchone()

    @staticmethod
    def _return_replay_or_conflict(existing: sqlite3.Row | None, expected: dict) -> dict | None:
        if existing is None:
            return None
        row = dict(existing)
        if all(row[key] == value for key, value in expected.items()):
            return row
        raise ValueError("request_id was already used with a different payload")

    def dismiss_item(self, item_id: int | Item, reason_code: str, note: str | None,
                     actor_sub: str, actor_email: str, request_id: str, *,
                     return_replay_status: bool = False) -> dict | tuple[dict, bool]:
        """Atomically append a dismissal event and project the item to DISMISSED."""
        item_id = self._item_id(item_id)
        note = "" if note is None else note
        if (not isinstance(reason_code, str)
                or reason_code not in VALID_DISMISSAL_REASONS
                or not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", reason_code)
                or len(reason_code) > MAX_FEEDBACK_REASON_LENGTH):
            raise ValueError(
                "reason_code must be one of: " + ", ".join(sorted(VALID_DISMISSAL_REASONS))
            )
        if not isinstance(note, str) or len(note) > MAX_FEEDBACK_NOTE_LENGTH:
            raise ValueError(f"note must be at most {MAX_FEEDBACK_NOTE_LENGTH} characters")
        self._validate_feedback_identity(actor_sub, actor_email, request_id)
        expected = {
            "item_id": item_id, "action": "DISMISS", "reason_code": reason_code,
            "note": note, "prior_status": "NEW", "resulting_status": "DISMISSED",
            "reverts_feedback_id": None, "actor_sub": actor_sub,
            "actor_email": actor_email, "request_id": request_id,
        }
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            replay = self._return_replay_or_conflict(
                self._feedback_row(connection, request_id), expected
            )
            if replay is not None:
                return (replay, True) if return_replay_status else replay
            item = connection.execute(
                "SELECT status FROM items WHERE item_id = ?", (item_id,)
            ).fetchone()
            if item is None:
                raise ValueError(f"Unknown item_id: {item_id}")
            if item["status"] != "NEW":
                raise ValueError("Only NEW items can be dismissed")
            cursor = connection.execute("""INSERT INTO item_feedback_events
                (item_id, action, reason_code, note, prior_status, resulting_status,
                 reverts_feedback_id, actor_sub, actor_email, request_id, created_at)
                VALUES (?, 'DISMISS', ?, ?, 'NEW', 'DISMISSED', NULL, ?, ?, ?, ?)""",
                (item_id, reason_code, note, actor_sub, actor_email, request_id, utc_now()),
            )
            connection.execute(
                "UPDATE items SET status = 'DISMISSED' WHERE item_id = ?", (item_id,)
            )
            row = connection.execute(
                "SELECT * FROM item_feedback_events WHERE feedback_id = ?", (cursor.lastrowid,)
            ).fetchone()
            event = dict(row)
            return (event, False) if return_replay_status else event

    def restore_item(self, item_id: int | Item, dismissal_feedback_id: int,
                     actor_sub: str, actor_email: str, request_id: str, *,
                     return_replay_status: bool = False) -> dict | tuple[dict, bool]:
        """Atomically append a restore event for the active referenced dismissal."""
        item_id = self._item_id(item_id)
        if not isinstance(dismissal_feedback_id, int) or isinstance(dismissal_feedback_id, bool):
            raise ValueError("dismissal_feedback_id must be an integer")
        self._validate_feedback_identity(actor_sub, actor_email, request_id)
        expected = {
            "item_id": item_id, "action": "RESTORE", "reason_code": "", "note": "",
            "prior_status": "DISMISSED", "resulting_status": "NEW",
            "reverts_feedback_id": dismissal_feedback_id, "actor_sub": actor_sub,
            "actor_email": actor_email, "request_id": request_id,
        }
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            replay = self._return_replay_or_conflict(
                self._feedback_row(connection, request_id), expected
            )
            if replay is not None:
                return (replay, True) if return_replay_status else replay
            item = connection.execute(
                "SELECT status FROM items WHERE item_id = ?", (item_id,)
            ).fetchone()
            if item is None:
                raise ValueError(f"Unknown item_id: {item_id}")
            if item["status"] != "DISMISSED":
                raise ValueError("Only DISMISSED items can be restored")
            active = connection.execute("""SELECT d.*
                FROM item_feedback_events d
                WHERE d.item_id = ? AND d.action = 'DISMISS'
                  AND NOT EXISTS (
                      SELECT 1 FROM item_feedback_events r
                      WHERE r.action = 'RESTORE' AND r.reverts_feedback_id = d.feedback_id
                  )
                ORDER BY d.feedback_id DESC LIMIT 1""", (item_id,)).fetchone()
            if active is None or active["feedback_id"] != dismissal_feedback_id:
                raise ValueError("dismissal_feedback_id is not the active dismissal")
            resulting_status = active["prior_status"]
            if resulting_status != "NEW":
                raise ValueError("Active dismissal has an invalid prior status")
            cursor = connection.execute("""INSERT INTO item_feedback_events
                (item_id, action, reason_code, note, prior_status, resulting_status,
                 reverts_feedback_id, actor_sub, actor_email, request_id, created_at)
                VALUES (?, 'RESTORE', '', '', 'DISMISSED', ?, ?, ?, ?, ?, ?)""",
                (item_id, resulting_status, dismissal_feedback_id, actor_sub,
                 actor_email, request_id, utc_now()),
            )
            connection.execute(
                "UPDATE items SET status = ? WHERE item_id = ?", (resulting_status, item_id)
            )
            row = connection.execute(
                "SELECT * FROM item_feedback_events WHERE feedback_id = ?", (cursor.lastrowid,)
            ).fetchone()
            event = dict(row)
            return (event, False) if return_replay_status else event

    def list_item_feedback(self, item_id: int | Item) -> list[dict]:
        item_id = self._item_id(item_id)
        with self._connect() as connection:
            if connection.execute(
                "SELECT 1 FROM items WHERE item_id = ?", (item_id,)
            ).fetchone() is None:
                raise ValueError(f"Unknown item_id: {item_id}")
            return [dict(row) for row in connection.execute(
                "SELECT * FROM item_feedback_events WHERE item_id = ? ORDER BY feedback_id",
                (item_id,),
            )]

    def get_active_dismissal(self, item_id: int | Item) -> dict | None:
        item_id = self._item_id(item_id)
        with self._connect() as connection:
            item = connection.execute(
                "SELECT status FROM items WHERE item_id = ?", (item_id,)
            ).fetchone()
            if item is None:
                raise ValueError(f"Unknown item_id: {item_id}")
            if item["status"] != "DISMISSED":
                return None
            row = connection.execute("""SELECT d.*
                FROM item_feedback_events d
                WHERE d.item_id = ? AND d.action = 'DISMISS'
                  AND NOT EXISTS (
                      SELECT 1 FROM item_feedback_events r
                      WHERE r.action = 'RESTORE' AND r.reverts_feedback_id = d.feedback_id
                  )
                ORDER BY d.feedback_id DESC LIMIT 1""", (item_id,)).fetchone()
            return dict(row) if row else None

    def get_active_dismissals(self, item_ids: list[int]) -> dict[int, dict]:
        """Fetch all active dismissals in one query (and one snapshot connection)."""
        if not item_ids:
            return {}
        placeholders = ",".join("?" for _ in item_ids)
        with self._connect() as connection:
            rows = connection.execute(f"""SELECT d.*
                FROM item_feedback_events d
                JOIN items i ON i.item_id = d.item_id AND i.status = 'DISMISSED'
                WHERE d.item_id IN ({placeholders}) AND d.action = 'DISMISS'
                  AND NOT EXISTS (
                      SELECT 1 FROM item_feedback_events r
                      WHERE r.action = 'RESTORE' AND r.reverts_feedback_id = d.feedback_id
                  )
                  AND d.feedback_id = (
                      SELECT MAX(latest.feedback_id) FROM item_feedback_events latest
                      WHERE latest.item_id = d.item_id AND latest.action = 'DISMISS'
                        AND NOT EXISTS (
                            SELECT 1 FROM item_feedback_events restored
                            WHERE restored.action = 'RESTORE'
                              AND restored.reverts_feedback_id = latest.feedback_id
                        )
                  )""", item_ids).fetchall()
            return {int(row["item_id"]): dict(row) for row in rows}

    def request_phase2(self, item_id: int | Item, title: str) -> int:
        """Immediately reserve a promoted lane while Telegram builds Phase 2 artifacts."""
        item_id = self._item_id(item_id)
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            item = connection.execute(
                "SELECT status FROM items WHERE item_id = ?", (item_id,)
            ).fetchone()
            if item is None:
                raise ValueError(f"Unknown item_id: {item_id}")
            if item["status"] == "DISMISSED":
                raise ValueError("DISMISSED items must be transitioned with restore_item before promotion")
            existing = connection.execute(
                "SELECT story_id, status FROM story_projects WHERE origin_item_id = ?", (item_id,)
            ).fetchone()
            if existing:
                story_id = int(existing["story_id"])
                if existing["status"] not in {"Phase 2 pending", "Phase 2", "Phase 3"}:
                    connection.execute(
                        "UPDATE story_projects SET status = 'Phase 2 pending', updated_at = ? WHERE story_id = ?",
                        (now, story_id),
                    )
            else:
                cursor = connection.execute(
                    """INSERT INTO story_projects
                       (origin_item_id, title, status, created_at, updated_at)
                       VALUES (?, ?, 'Phase 2 pending', ?, ?)""",
                    (item_id, title, now, now),
                )
                story_id = int(cursor.lastrowid or 0)
            connection.execute("UPDATE items SET status = 'PROMOTED' WHERE item_id = ?", (item_id,))
            return story_id

    def promote_item(self, item_id: int | Item, title: str, status: str = "ACTIVE") -> int:
        item_id = self._item_id(item_id)
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            item = connection.execute(
                "SELECT status FROM items WHERE item_id = ?", (item_id,)
            ).fetchone()
            if item is None:
                raise ValueError(f"Unknown item_id: {item_id}")
            if item["status"] == "DISMISSED":
                raise ValueError("DISMISSED items must be transitioned with restore_item before promotion")
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

    def issue_phase2_completion_token(self, story_id: int) -> str:
        """Issue an item-scoped, one-way capability for Telegram to finish Phase 2."""
        token = secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        with self._connect() as connection:
            cursor = connection.execute(
                """UPDATE story_projects SET phase2_completion_token_hash = ?, updated_at = ?
                   WHERE story_id = ? AND status = 'Phase 2 pending'""",
                (digest, utc_now(), story_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("Phase 2 completion token can only be issued for pending stories")
        return token

    def complete_phase2(self, story_id: int, token: str) -> dict:
        """Mark pending Phase 2 work complete; valid replays never downgrade Phase 3."""
        if not token or len(token) > 256:
            raise ValueError("Invalid Phase 2 completion token")
        supplied_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status, phase2_completion_token_hash FROM story_projects WHERE story_id = ?",
                (story_id,),
            ).fetchone()
            if row is None or not row["phase2_completion_token_hash"] or not hmac.compare_digest(
                supplied_hash, row["phase2_completion_token_hash"]
            ):
                raise ValueError("Invalid Phase 2 completion token")
            if row["status"] == "Phase 2 pending":
                connection.execute(
                    "UPDATE story_projects SET status = 'Phase 2', updated_at = ? WHERE story_id = ?",
                    (utc_now(), story_id),
                )
            elif row["status"] not in {"Phase 2", "Phase 3"}:
                raise ValueError("Story is not in a completable Phase 2 state")
        return self.get_story(story_id)

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
