"""Tests for SQLite database initialization and constraints."""

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
