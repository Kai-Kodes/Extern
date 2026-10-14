"""Tests for SQLite database initialization and constraints."""

import sqlite3
import tempfile
from pathlib import Path

import pytest

from todo.database import Database


@pytest.fixture
def temp_db():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        yield Database(db_path)


def test_database_initialization(temp_db):
    """Test that all required tables and indexes are created."""
    with temp_db.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {row["name"] for row in cursor.fetchall()}

        assert "sources" in tables
        assert "file_items" in tables
        assert "tasks" in tables
        assert "daily_tasks" in tables


def test_database_foreign_keys_and_cascades(temp_db):
    """Test foreign key enforcement and cascading deletes."""
    with temp_db.get_connection() as conn:
        cursor = conn.cursor()

        # Insert source
        cursor.execute(
            "INSERT INTO sources (name, path, created_at, updated_at) VALUES (?, ?, ?, ?)",
            ("Test Source", "/tmp/test", "2026-10-08T00:00:00", "2026-10-08T00:00:00"),
        )
        source_id = cursor.lastrowid

        # Insert file item
        cursor.execute(
            """
            INSERT INTO file_items (source_id, path, title, type, metadata, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (source_id, "/tmp/test/1.mp4", "1", "video", "{}", "2026-10-08T00:00:00", "2026-10-08T00:00:00"),
        )

        # Deleting source should cascade delete file_items
        cursor.execute("DELETE FROM sources WHERE id = ?", (source_id,))
        cursor.execute("SELECT COUNT(*) as cnt FROM file_items WHERE source_id = ?", (source_id,))
        assert cursor.fetchone()["cnt"] == 0


def test_fresh_database_initialization():
    """Verify that a brand new database initializes completely with all tables, columns, and indexes."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "fresh_test.db"
        db = Database(db_path)

        with db.get_connection() as conn:
            cursor = conn.cursor()

            # Verify tables
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = {row["name"] for row in cursor.fetchall()}
            expected_tables = {
                "sources",
                "source_folders",
                "file_items",
                "tasks",
                "daily_tasks",
                "telegram_channels_cache",
            }
            assert expected_tables.issubset(tables)

            # Verify sources columns
            cursor.execute("PRAGMA table_info(sources)")
            s_cols = {row["name"] for row in cursor.fetchall()}
            assert {"id", "name", "path", "canonical_uri", "scan_status", "scanned_at", "last_error"}.issubset(s_cols)

            # Verify file_items columns
            cursor.execute("PRAGMA table_info(file_items)")
            fi_cols = {row["name"] for row in cursor.fetchall()}
            assert {"id", "source_id", "folder_id", "path", "relative_path", "file_size", "extension", "title", "completed", "completed_at"}.issubset(fi_cols)

            # Verify indexes
            cursor.execute("SELECT name FROM sqlite_master WHERE type='index'")
            indexes = {row["name"] for row in cursor.fetchall()}
            assert "idx_file_items_folder_id" in indexes
            assert "idx_file_items_rel_path" in indexes
            assert "idx_source_folders_source_id" in indexes
            assert "idx_source_folders_parent_id" in indexes
            assert "idx_source_folders_rel_path" in indexes


def test_older_schema_migration_preserves_data_and_enables_hierarchy():
    """
    Regression test for startup crash:
    Create a database matching the older schema (missing folder_id, relative_path, source_folders, etc.),
    populate it with representative user data, and verify:
    1. Startup/initialization succeeds without crashing.
    2. All existing data (sources, items, completion statuses, tasks, daily tasks) survives 100%.
    3. The new folder hierarchy schema and indexes are created and operational.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "legacy_user.db"

        # Create older schema (v1.2) where folder_id and source_folders do not exist
        conn = sqlite3.connect(str(db_path))
        conn.executescript("""
            PRAGMA foreign_keys = ON;
            CREATE TABLE sources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                path TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE file_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
                path TEXT NOT NULL,
                title TEXT NOT NULL,
                type TEXT NOT NULL DEFAULT 'file',
                metadata TEXT NOT NULL DEFAULT '{}',
                completed INTEGER NOT NULL DEFAULT 0,
                completed_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(source_id, path)
            );
            CREATE INDEX idx_file_items_source_id ON file_items(source_id);
            CREATE TABLE tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                source_type TEXT NOT NULL DEFAULT 'manual',
                source_id INTEGER,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX idx_tasks_source ON tasks(source_type, source_id);
            CREATE TABLE daily_tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                date TEXT NOT NULL,
                position INTEGER NOT NULL DEFAULT 0,
                completed_at TEXT,
                UNIQUE(task_id, date)
            );
            CREATE INDEX idx_daily_tasks_date ON daily_tasks(date);
            CREATE TABLE telegram_channels_cache (
                id INTEGER PRIMARY KEY,
                title TEXT NOT NULL,
                username TEXT,
                is_channel INTEGER NOT NULL DEFAULT 1,
                is_group INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            );
        """)

        # Populate with representative user data
        conn.execute(
            "INSERT INTO sources (id, name, path, created_at, updated_at) VALUES (1, 'Copywriting Mastery', '/media/courses/copy', '2026-10-09T10:00:00', '2026-10-09T10:00:00')"
        )
        conn.execute(
            "INSERT INTO file_items (id, source_id, path, title, type, metadata, completed, completed_at, created_at, updated_at) VALUES (1, 1, '/media/courses/copy/01.mp4', '01 - Headlines', 'video', '{\"duration_formatted\":\"15:00\"}', 1, '2026-10-09T12:00:00', '2026-10-09T10:00:00', '2026-10-09T12:00:00')"
        )
        conn.execute(
            "INSERT INTO file_items (id, source_id, path, title, type, metadata, completed, completed_at, created_at, updated_at) VALUES (2, 1, '/media/courses/copy/02.mp4', '02 - Hooks', 'video', '{\"duration_formatted\":\"18:30\"}', 0, NULL, '2026-10-09T10:00:00', '2026-10-09T10:00:00')"
        )
        conn.execute(
            "INSERT INTO tasks (id, title, description, source_type, source_id, status, created_at, updated_at) VALUES (1, '01 - Headlines', 'Source: Copywriting Mastery', 'filemeta', 1, 'completed', '2026-10-09T10:05:00', '2026-10-09T12:00:00')"
        )
        conn.execute(
            "INSERT INTO daily_tasks (id, task_id, date, position, completed_at) VALUES (1, 1, '2026-10-09', 1, '2026-10-09T12:00:00')"
        )
        conn.commit()
        conn.close()

        # Initialize new application database abstraction (previously crashed with OperationalError: no such column: folder_id)
        db = Database(db_path)

        with db.get_connection() as verify_conn:
            c = verify_conn.cursor()

            # 1. Verify all existing data survived intact
            c.execute("SELECT id, name, path FROM sources WHERE id = 1")
            src = c.fetchone()
            assert src["name"] == "Copywriting Mastery"
            assert src["path"] == "/media/courses/copy"

            c.execute("SELECT id, title, completed, completed_at FROM file_items ORDER BY id")
            items = c.fetchall()
            assert len(items) == 2
            assert items[0]["title"] == "01 - Headlines"
            assert items[0]["completed"] == 1
            assert items[0]["completed_at"] == "2026-10-09T12:00:00"
            assert items[1]["title"] == "02 - Hooks"
            assert items[1]["completed"] == 0

            c.execute("SELECT id, title, status FROM tasks WHERE id = 1")
            task = c.fetchone()
            assert task["title"] == "01 - Headlines"
            assert task["status"] == "completed"

            c.execute("SELECT id, date, completed_at FROM daily_tasks WHERE id = 1")
            dt = c.fetchone()
            assert dt["date"] == "2026-10-09"
            assert dt["completed_at"] == "2026-10-09T12:00:00"

            # 2. Verify new columns exist and are functional
            c.execute("PRAGMA table_info(file_items)")
            cols = {r["name"] for r in c.fetchall()}
            assert "folder_id" in cols
            assert "relative_path" in cols
            assert "file_size" in cols
            assert "extension" in cols

            c.execute("PRAGMA table_info(sources)")
            s_cols = {r["name"] for r in c.fetchall()}
            assert "canonical_uri" in s_cols
            assert "scan_status" in s_cols

            # 3. Verify new source_folders table is present and operable
            c.execute(
                "INSERT INTO source_folders (source_id, parent_id, name, relative_path, created_at, updated_at) VALUES (1, NULL, 'Module 1', 'Module 1', '2026-10-13', '2026-10-13')"
            )
            folder_id = c.lastrowid
            c.execute("UPDATE file_items SET folder_id = ?, relative_path = 'Module 1/01.mp4' WHERE id = 1", (folder_id,))
            verify_conn.commit()

            c.execute("SELECT folder_id, relative_path FROM file_items WHERE id = 1")
            updated_item = c.fetchone()
            assert updated_item["folder_id"] == folder_id
            assert updated_item["relative_path"] == "Module 1/01.mp4"


def test_initialization_idempotency_running_twice_succeeds():
    """Verify that running init_db() multiple times is completely idempotent and produces no errors."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "idempotent.db"
        db = Database(db_path)

        # Rerun 1
        db.init_db()

        # Rerun 2
        db.init_db()

        with db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'")
            count = cursor.fetchone()[0]
            assert count >= 6

