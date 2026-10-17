import sqlite3
import threading
from pathlib import Path

from scripts import backup_db
from scripts.backup_db import backup


def create_database(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript("""
            PRAGMA foreign_keys = ON;
            CREATE TABLE sources(source_id INTEGER PRIMARY KEY, name TEXT NOT NULL);
            CREATE TABLE items(
                item_id INTEGER PRIMARY KEY,
                source_id INTEGER NOT NULL REFERENCES sources(source_id),
                status TEXT NOT NULL
            );
            INSERT INTO sources VALUES (1, 'Source');
            INSERT INTO items VALUES (1, 1, 'NEW');
            INSERT INTO items VALUES (2, 1, 'DISMISSED');
        """)


def test_backup_is_verified_and_preserves_rows_and_status_counts(tmp_path):
    database = tmp_path / "stories.sqlite3"
    backup_dir = tmp_path / "backups"
    create_database(database)

    destination = backup(database, backup_dir, keep_generations=2)

    assert destination.parent == backup_dir
    with sqlite3.connect(destination) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("SELECT count(*) FROM sources").fetchone() == (1,)
        assert dict(connection.execute(
            "SELECT status, count(*) FROM items GROUP BY status"
        )) == {"NEW": 1, "DISMISSED": 1}


def test_backup_prunes_only_after_a_verified_backup(tmp_path):
    database = tmp_path / "stories.sqlite3"
    backup_dir = tmp_path / "backups"
    create_database(database)
    backup_dir.mkdir()
    for name in ("stories-20000101T000000Z.sqlite3", "stories-20000102T000000Z.sqlite3"):
        (backup_dir / name).write_bytes(b"old")

    destination = backup(database, backup_dir, keep_generations=2)

    assert destination.exists()
    assert len(list(backup_dir.glob("stories-*.sqlite3"))) == 2


def test_backup_counts_and_copy_share_one_read_snapshot_during_concurrent_write(tmp_path, monkeypatch):
    database = tmp_path / "stories.sqlite3"
    backup_dir = tmp_path / "backups"
    create_database(database)
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA journal_mode=WAL")

    snapshot_read = threading.Event()
    writer_finished = threading.Event()
    original_snapshot = backup_db._snapshot
    snapshot_calls = 0

    def paused_snapshot(connection):
        nonlocal snapshot_calls, expected_counts, expected_statuses
        result = original_snapshot(connection)
        expected_counts, expected_statuses = result
        snapshot_calls += 1
        if snapshot_calls == 1:
            snapshot_read.set()
            assert writer_finished.wait(3)
        return result

    expected_counts = None
    expected_statuses = None

    def writer():
        assert snapshot_read.wait(3)
        with sqlite3.connect(database) as connection:
            connection.execute("INSERT INTO items VALUES (3, 1, 'NEW')")
        writer_finished.set()

    monkeypatch.setattr(backup_db, "_snapshot", paused_snapshot)
    thread = threading.Thread(target=writer)
    thread.start()
    destination = backup(database, backup_dir)
    thread.join(3)

    assert not thread.is_alive()
    with sqlite3.connect(destination) as connection:
        assert connection.execute("SELECT count(*) FROM items").fetchone() == (2,)
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT count(*) FROM items").fetchone() == (3,)
