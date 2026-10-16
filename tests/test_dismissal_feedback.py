import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace

import pytest

from scripts import story_cli
from story_store import StoryStore


def make_item(tmp_path):
    db = tmp_path / "stories.sqlite3"
    store = StoryStore(db)
    source_id = store.add_source("Example", "A", "US", "https://example.test")
    item = store.add_item(source_id, "A candidate", "https://example.test/item")
    return db, store, item.item_id


def dismiss(store, item_id, request_id="dismiss-1", **overrides):
    values = {
        "reason_code": "not_relevant",
        "note": "Outside the beat",
        "actor_sub": "google-sub-1",
        "actor_email": "reviewer@example.test",
        "request_id": request_id,
    }
    values.update(overrides)
    return store.dismiss_item(item_id, **values)


def restore(store, item_id, feedback_id, request_id="restore-1", **overrides):
    values = {
        "dismissal_feedback_id": feedback_id,
        "actor_sub": "google-sub-1",
        "actor_email": "reviewer@example.test",
        "request_id": request_id,
    }
    values.update(overrides)
    return store.restore_item(item_id, **values)


def test_dismiss_appends_event_and_projects_item_state(tmp_path):
    _, store, item_id = make_item(tmp_path)

    event = dismiss(store, item_id)

    assert event["feedback_id"] > 0
    assert event["item_id"] == item_id
    assert event["action"] == "DISMISS"
    assert event["reason_code"] == "not_relevant"
    assert event["note"] == "Outside the beat"
    assert event["prior_status"] == "NEW"
    assert event["resulting_status"] == "DISMISSED"
    assert event["reverts_feedback_id"] is None
    assert event["actor_sub"] == "google-sub-1"
    assert event["actor_email"] == "reviewer@example.test"
    assert event["request_id"] == "dismiss-1"
    assert event["created_at"].endswith("Z")
    assert store.get_item(item_id)["status"] == "DISMISSED"
    assert store.list_item_feedback(item_id) == [event]
    assert store.get_active_dismissal(item_id) == event


def test_restore_appends_without_mutating_dismissal_and_returns_prior_status(tmp_path):
    _, store, item_id = make_item(tmp_path)
    dismissal = dismiss(store, item_id)

    restored = restore(store, item_id, dismissal["feedback_id"])

    assert restored["action"] == "RESTORE"
    assert restored["reason_code"] == ""
    assert restored["note"] == ""
    assert restored["prior_status"] == "DISMISSED"
    assert restored["resulting_status"] == "NEW"
    assert restored["reverts_feedback_id"] == dismissal["feedback_id"]
    assert store.get_item(item_id)["status"] == "NEW"
    assert store.get_active_dismissal(item_id) is None
    assert store.list_item_feedback(item_id) == [dismissal, restored]


def test_exact_request_retries_return_original_events(tmp_path):
    _, store, item_id = make_item(tmp_path)
    first = dismiss(store, item_id)
    assert dismiss(store, item_id) == first

    restored = restore(store, item_id, first["feedback_id"])
    assert restore(store, item_id, first["feedback_id"]) == restored
    assert len(store.list_item_feedback(item_id)) == 2


@pytest.mark.parametrize(
    "changed",
    [
        {"reason_code": "duplicate"},
        {"note": "different"},
        {"actor_sub": "different-sub"},
        {"actor_email": "different@example.test"},
    ],
)
def test_dismiss_request_id_reuse_with_changed_payload_conflicts(tmp_path, changed):
    _, store, item_id = make_item(tmp_path)
    dismiss(store, item_id)
    with pytest.raises(ValueError, match="request_id"):
        dismiss(store, item_id, **changed)


def test_restore_request_id_reuse_with_changed_payload_conflicts(tmp_path):
    _, store, item_id = make_item(tmp_path)
    first = dismiss(store, item_id)
    restore(store, item_id, first["feedback_id"])
    with pytest.raises(ValueError, match="request_id"):
        restore(store, item_id, first["feedback_id"], actor_sub="different-sub")


def test_invalid_transitions_and_unknown_items_are_rejected(tmp_path):
    _, store, item_id = make_item(tmp_path)
    store.update_item_status(item_id, "IGNORED")
    with pytest.raises(ValueError, match="NEW"):
        dismiss(store, item_id)
    with pytest.raises(ValueError, match="Unknown item_id"):
        dismiss(store, 999)

    store.update_item_status(item_id, "NEW")
    first = dismiss(store, item_id)
    with pytest.raises(ValueError, match="NEW"):
        dismiss(store, item_id, request_id="dismiss-2")
    with pytest.raises(ValueError, match="active dismissal"):
        restore(store, item_id, first["feedback_id"] + 100)
    with pytest.raises(ValueError, match="Unknown item_id"):
        restore(store, 999, first["feedback_id"], request_id="restore-unknown")
    restore(store, item_id, first["feedback_id"])
    with pytest.raises(ValueError, match="DISMISSED"):
        restore(store, item_id, first["feedback_id"], request_id="restore-2")


def test_restore_rejects_another_items_valid_dismissal(tmp_path):
    _, store, item_id = make_item(tmp_path)
    source_id = store.get_item(item_id)["source_id"]
    other_id = store.add_item(
        source_id, "Another candidate", "https://example.test/other"
    ).item_id
    current = dismiss(store, item_id)
    other = dismiss(store, other_id, request_id="dismiss-other")

    with pytest.raises(ValueError, match="active dismissal"):
        restore(store, item_id, other["feedback_id"])

    assert store.get_item(item_id)["status"] == "DISMISSED"
    assert store.get_active_dismissal(item_id) == current


def test_update_item_status_cannot_create_unaudited_dismissal(tmp_path):
    _, store, item_id = make_item(tmp_path)

    with pytest.raises(ValueError, match="dismiss_item"):
        store.update_item_status(item_id, "DISMISSED")

    assert store.get_item(item_id)["status"] == "NEW"
    assert store.list_item_feedback(item_id) == []


@pytest.mark.parametrize("status", ["NEW", "IGNORED", "PROMOTED"])
def test_update_item_status_cannot_move_a_dismissed_item(tmp_path, status):
    _, store, item_id = make_item(tmp_path)
    dismissal = dismiss(store, item_id)

    with pytest.raises(ValueError, match="restore_item"):
        store.update_item_status(item_id, status)

    assert store.get_item(item_id)["status"] == "DISMISSED"
    assert store.get_active_dismissal(item_id) == dismissal


def test_promote_rejects_dismissed_item_until_restored(tmp_path):
    _, store, item_id = make_item(tmp_path)
    dismissal = dismiss(store, item_id)

    with pytest.raises(ValueError, match="restore_item"):
        store.promote_item(item_id, "Should not exist")

    restore(store, item_id, dismissal["feedback_id"])
    story_id = store.promote_item(item_id, "Restored story")
    assert store.get_story(story_id)["origin_item_id"] == item_id


def test_quarantine_does_not_move_dismissed_items(tmp_path, monkeypatch):
    _, store, item_id = make_item(tmp_path)
    dismissal = dismiss(store, item_id)
    monkeypatch.setattr(
        "collectors.validate_candidate",
        lambda *_args, **_kwargs: SimpleNamespace(decision="old", reason="too old"),
    )

    store.quarantine_invalid_items()

    assert store.get_item(item_id)["status"] == "DISMISSED"
    assert store.get_active_dismissal(item_id) == dismissal


def test_cli_update_status_dismissed_creates_manual_feedback_event(tmp_path, monkeypatch):
    db, store, item_id = make_item(tmp_path)
    monkeypatch.setattr(
        sys,
        "argv",
        ["story_cli.py", "--db", str(db), "update-status", str(item_id), "DISMISSED"],
    )

    story_cli.main()

    event = store.get_active_dismissal(item_id)
    assert event is not None
    assert event["reason_code"] == "manual_cli"
    assert event["note"] == ""
    assert event["actor_sub"] == "cli"
    assert event["actor_email"] == "local-cli"
    assert event["request_id"].startswith("cli-dismiss-")


def test_reopening_store_never_drops_feedback_immutability_triggers(tmp_path):
    db, store, item_id = make_item(tmp_path)
    event = dismiss(store, item_id)

    class DropProtectedStore(StoryStore):
        def _connect(self):
            connection = super()._connect()

            def deny_trigger_drop(action, _arg1, _arg2, _db_name, _source):
                if action == sqlite3.SQLITE_DROP_TRIGGER:
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK

            connection.set_authorizer(deny_trigger_drop)
            return connection

    reopened = DropProtectedStore(db)
    assert reopened.list_item_feedback(item_id) == [event]
    with sqlite3.connect(db) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE item_feedback_events SET note='tampered' WHERE feedback_id=?",
                (event["feedback_id"],),
            )


def test_reason_and_note_are_validated_and_bounded(tmp_path):
    _, store, item_id = make_item(tmp_path)
    for reason in ("", "has spaces", "x" * 65):
        with pytest.raises(ValueError, match="reason_code"):
            dismiss(store, item_id, request_id=f"bad-{len(reason)}", reason_code=reason)
    with pytest.raises(ValueError, match="note"):
        dismiss(store, item_id, request_id="long-note", note="x" * 1001)


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("actor_sub", "   "),
        ("actor_sub", "x" * 256),
        ("actor_email", "\t"),
        ("actor_email", "x" * 321),
        ("request_id", ""),
        ("request_id", "x" * 256),
    ],
)
def test_database_rejects_blank_or_oversized_feedback_identity(tmp_path, column, value):
    db, _, item_id = make_item(tmp_path)
    fields = {
        "actor_sub": "subject",
        "actor_email": "reviewer@example.test",
        "request_id": "direct-request",
    }
    fields[column] = value
    with sqlite3.connect(db) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """INSERT INTO item_feedback_events
                   (item_id, action, reason_code, note, prior_status, resulting_status,
                    actor_sub, actor_email, request_id, created_at)
                   VALUES (?, 'DISMISS', 'other', '', 'NEW', 'DISMISSED', ?, ?, ?, 'now')""",
                (item_id, fields["actor_sub"], fields["actor_email"], fields["request_id"]),
            )


def test_database_restore_reference_must_be_dismissal_for_same_item(tmp_path):
    db, store, item_id = make_item(tmp_path)
    source_id = store.get_item(item_id)["source_id"]
    other_id = store.add_item(source_id, "Other", "https://example.test/other").item_id
    other_dismissal = dismiss(store, other_id)

    with sqlite3.connect(db) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        with pytest.raises(sqlite3.IntegrityError, match="same item"):
            connection.execute(
                """INSERT INTO item_feedback_events
                   (item_id, action, reason_code, note, prior_status, resulting_status,
                    reverts_feedback_id, actor_sub, actor_email, request_id, created_at)
                   VALUES (?, 'RESTORE', '', '', 'DISMISSED', 'NEW', ?,
                           'subject', 'reviewer@example.test', 'bad-restore', 'now')""",
                (item_id, other_dismissal["feedback_id"]),
            )


def test_database_triggers_reject_feedback_update_and_delete(tmp_path):
    db, store, item_id = make_item(tmp_path)
    event = dismiss(store, item_id)
    with sqlite3.connect(db) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE item_feedback_events SET note='tampered' WHERE feedback_id=?",
                (event["feedback_id"],),
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "DELETE FROM item_feedback_events WHERE feedback_id=?",
                (event["feedback_id"],),
            )


def create_legacy_database(db):
    with sqlite3.connect(db) as connection:
        connection.executescript(
            """
            PRAGMA foreign_keys=ON;
            CREATE TABLE sources (
                source_id INTEGER PRIMARY KEY, name TEXT NOT NULL, lane TEXT NOT NULL,
                jurisdiction TEXT NOT NULL, source_url TEXT NOT NULL,
                source_type TEXT NOT NULL DEFAULT 'web', active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL, notes TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE items (
                item_id INTEGER PRIMARY KEY, source_id INTEGER NOT NULL REFERENCES sources(source_id),
                headline TEXT NOT NULL, raw_url TEXT NOT NULL, canonical_url TEXT NOT NULL UNIQUE,
                url_hash TEXT NOT NULL UNIQUE, published_at TEXT, first_seen_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'NEW' CHECK(status IN ('NEW','PROMOTED','IGNORED','DISMISSED')),
                validation_reason TEXT NOT NULL DEFAULT ''
            );
            INSERT INTO sources(source_id,name,lane,jurisdiction,source_url,created_at)
                VALUES (41,'Legacy','A','US','https://legacy.test','2025-01-01T00:00:00Z');
            INSERT INTO items(item_id,source_id,headline,raw_url,canonical_url,url_hash,first_seen_at,status)
                VALUES (73,41,'Dismissed','https://legacy.test/73','https://legacy.test/73','h73','2025-01-02T00:00:00Z','DISMISSED');
            INSERT INTO items(item_id,source_id,headline,raw_url,canonical_url,url_hash,first_seen_at,status)
                VALUES (88,41,'Still new','https://legacy.test/88','https://legacy.test/88','h88','2025-01-03T00:00:00Z','NEW');
            """
        )


def test_reopen_migrates_in_place_backfills_once_and_preserves_ids_statuses(tmp_path):
    db = tmp_path / "legacy.sqlite3"
    create_legacy_database(db)

    store = StoryStore(db)
    assert [(row["item_id"], row["status"]) for row in store.list_items()] == [
        (88, "NEW"),
        (73, "DISMISSED"),
    ]
    events = store.list_item_feedback(73)
    assert len(events) == 1
    event = events[0]
    assert event["action"] == "DISMISS"
    assert event["reason_code"] == "legacy_backfill"
    assert event["note"] == ""
    assert event["prior_status"] == "NEW"
    assert event["resulting_status"] == "DISMISSED"
    assert event["actor_sub"] == "migration"
    assert event["actor_email"] == "migration"
    assert event["request_id"] == "migration-dismissed-73"
    assert store.get_active_dismissal(73) == event

    reopened = StoryStore(db)
    assert reopened.list_item_feedback(73) == [event]
    assert reopened.list_item_feedback(88) == []
    assert reopened.get_item(73)["status"] == "DISMISSED"
    assert reopened.get_item(88)["status"] == "NEW"

    class MustNotRerunMigrationStore(StoryStore):
        def _migrate_feedback_v1(self, connection):
            raise AssertionError("versioned feedback migration ran twice")

    MustNotRerunMigrationStore(db)


def test_feedback_migration_failure_rolls_back_table_backfill_and_triggers(tmp_path):
    db = tmp_path / "legacy-failure.sqlite3"
    create_legacy_database(db)

    class FailingMigrationStore(StoryStore):
        def _install_feedback_triggers(self, connection):
            raise RuntimeError("injected trigger installation failure")

    with pytest.raises(RuntimeError, match="injected"):
        FailingMigrationStore(db)

    with sqlite3.connect(db) as connection:
        objects = connection.execute(
            """SELECT type, name FROM sqlite_master
               WHERE name LIKE 'item_feedback%' ORDER BY type, name"""
        ).fetchall()
        assert objects == []
        assert connection.execute(
            "SELECT status FROM items WHERE item_id=73"
        ).fetchone()[0] == "DISMISSED"
        assert connection.execute(
            "SELECT 1 FROM schema_migrations WHERE version='feedback_v1'"
        ).fetchone() is None

    migrated = StoryStore(db)
    assert len(migrated.list_item_feedback(73)) == 1


def _simultaneous_calls(*calls):
    barrier = Barrier(len(calls))

    def invoke(call):
        barrier.wait()
        try:
            return "ok", call()
        except ValueError as error:
            return "error", str(error)

    with ThreadPoolExecutor(max_workers=len(calls)) as executor:
        return list(executor.map(invoke, calls))


def test_simultaneous_different_dismiss_requests_append_one_event(tmp_path):
    db, store, item_id = make_item(tmp_path)
    first_store, second_store = StoryStore(db), StoryStore(db)

    outcomes = _simultaneous_calls(
        lambda: dismiss(first_store, item_id, request_id="dismiss-a"),
        lambda: dismiss(second_store, item_id, request_id="dismiss-b"),
    )

    assert sorted(kind for kind, _ in outcomes) == ["error", "ok"]
    assert len(store.list_item_feedback(item_id)) == 1
    assert store.get_item(item_id)["status"] == "DISMISSED"


def test_simultaneous_exact_dismiss_retry_returns_same_event(tmp_path):
    db, store, item_id = make_item(tmp_path)
    first_store, second_store = StoryStore(db), StoryStore(db)

    outcomes = _simultaneous_calls(
        lambda: dismiss(first_store, item_id, request_id="same-dismiss"),
        lambda: dismiss(second_store, item_id, request_id="same-dismiss"),
    )

    assert [kind for kind, _ in outcomes] == ["ok", "ok"]
    assert outcomes[0][1] == outcomes[1][1]
    assert store.list_item_feedback(item_id) == [outcomes[0][1]]


def test_simultaneous_restore_requests_append_one_restore(tmp_path):
    db, store, item_id = make_item(tmp_path)
    dismissal = dismiss(store, item_id)
    first_store, second_store = StoryStore(db), StoryStore(db)

    outcomes = _simultaneous_calls(
        lambda: restore(first_store, item_id, dismissal["feedback_id"], "restore-a"),
        lambda: restore(second_store, item_id, dismissal["feedback_id"], "restore-b"),
    )

    assert sorted(kind for kind, _ in outcomes) == ["error", "ok"]
    assert [event["action"] for event in store.list_item_feedback(item_id)] == [
        "DISMISS",
        "RESTORE",
    ]
    assert store.get_item(item_id)["status"] == "NEW"
