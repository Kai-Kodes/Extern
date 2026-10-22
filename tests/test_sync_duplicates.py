"""Comprehensive regression tests for Google Tasks sync deduplication, outbox idempotency, and echo reconciliation."""

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from todo.cli import cli
from todo.config import Config
from todo.database import Database
from todo.integrations.google_calendar import GoogleCalendarClient
from todo.integrations.google_tasks import GoogleOAuthManager, GoogleTasksClient
from todo.models import FileItem, Source, Task
from todo.services.daily_service import DailyService
from todo.services.google_calendar_service import GoogleCalendarService
from todo.services.google_tasks_service import GoogleTasksService
from todo.services.google_tasks_sync_manager import GoogleTasksSyncManager
from todo.services.task_service import TaskService


@pytest.fixture
def dedup_env(tmp_path):
    """Set up isolated DB and wired services for deduplication testing."""
    db_path = tmp_path / "dedup_test.db"
    db = Database(db_path)
    db.init_db()

    token_file = tmp_path / "dedup_token.json"
    oauth_mgr = GoogleOAuthManager(
        token_path=token_file,
        client_id="test_client_id",
        client_secret="test_client_secret",
    )
    oauth_mgr.save_tokens({
        "access_token": "valid_token",
        "refresh_token": "valid_refresh",
        "expires_in": 3600,
        "scope": "https://www.googleapis.com/auth/tasks https://www.googleapis.com/auth/calendar.events",
    })

    config = Config(
        db_path=db_path,
        google_token_path=token_file,
        google_tasks_enabled=True,
        google_calendar_enabled=False,
        google_default_task_list="@default",
    )

    mock_tasks_client = MagicMock(spec=GoogleTasksClient)
    tasks_svc = GoogleTasksService(
        db=db,
        config=config,
        oauth_manager=oauth_mgr,
        client=mock_tasks_client,
    )
    daily_svc = DailyService(db)
    task_svc = TaskService(db)
    task_svc.google_tasks_service = tasks_svc
    daily_svc.google_tasks_service = tasks_svc
    tasks_svc.daily_service = daily_svc
    tasks_svc.task_service = task_svc

    return {
        "db": db,
        "config": config,
        "tasks_svc": tasks_svc,
        "task_svc": task_svc,
        "daily_svc": daily_svc,
        "client": mock_tasks_client,
    }


def test_manual_task_sync_does_not_create_duplicate(dedup_env):
    """
    Creating a manual task, syncing it to Google Tasks, and subsequently
    pulling remote tasks must update the existing task without creating a duplicate.
    """
    task_svc = dedup_env["task_svc"]
    daily_svc = dedup_env["daily_svc"]
    tasks_svc = dedup_env["tasks_svc"]
    mock_client = dedup_env["client"]
    db = dedup_env["db"]

    # 1. Create a manual task and schedule for today
    manual_task = task_svc.create_task("Test Manual Task", source_type="manual")
    daily_svc.add_task_to_date(manual_task.id, "2026-10-22")

    # 2. Outbox drain simulates creating it on Google Tasks
    mock_client.create_task.return_value = {
        "id": "remote_task_xyz_123",
        "title": "Test Manual Task",
        "status": "needsAction",
        "etag": '"etag_abc"',
        "updated": "2026-10-22T10:00:00.000Z",
    }
    drained = tasks_svc.drain_outbox()
    assert drained > 0

    # Verify task_google_tasks_map has the real remote_id
    mapping = tasks_svc.get_task_google_mapping(manual_task.id)
    assert mapping is not None
    assert mapping["remote_id"] == "remote_task_xyz_123"

    # 3. Background sync calls sync_tasks_for_list with the remote task returned from Google Tasks
    mock_client.get_tasks.return_value = [
        {
            "id": "remote_task_xyz_123",
            "title": "Test Manual Task",
            "notes": "",
            "status": "needsAction",
            "due": "2026-10-22T00:00:00.000Z",
            "etag": '"etag_abc_updated"',
            "updated": "2026-10-22T10:05:00.000Z",
        }
    ]

    res = tasks_svc.sync_tasks_for_list("@default")

    # 4. Assert: Exactly 1 task in tasks table, 1 daily_task, 0 duplicate file_items
    with db.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, title, source_type FROM tasks")
        all_tasks = cursor.fetchall()
        assert len(all_tasks) == 1
        assert all_tasks[0]["id"] == manual_task.id
        assert all_tasks[0]["source_type"] == "manual"

        cursor.execute("SELECT id, task_id, date FROM daily_tasks")
        all_daily = cursor.fetchall()
        assert len(all_daily) == 1
        assert all_daily[0]["task_id"] == manual_task.id
        assert all_daily[0]["date"] == "2026-10-22"

        cursor.execute("SELECT id FROM file_items")
        assert len(cursor.fetchall()) == 0

    # 5. Subsequent sync cycles remain strictly idempotent
    tasks_svc.sync_tasks_for_list("@default")
    with db.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) as cnt FROM tasks")
        assert cursor.fetchone()["cnt"] == 1
        cursor.execute("SELECT COUNT(*) as cnt FROM daily_tasks")
        assert cursor.fetchone()["cnt"] == 1


def test_remote_completion_and_notes_update_mapped_manual_task(dedup_env):
    """Remote updates to status and notes on Google Tasks cleanly reconcile onto the local manual task."""
    task_svc = dedup_env["task_svc"]
    daily_svc = dedup_env["daily_svc"]
    tasks_svc = dedup_env["tasks_svc"]
    mock_client = dedup_env["client"]
    db = dedup_env["db"]

    task = task_svc.create_task("Finish Review", source_type="manual")
    daily_svc.add_task_to_date(task.id, "2026-10-22")

    mock_client.create_task.return_value = {
        "id": "remote_finish_review",
        "title": "Finish Review",
        "status": "needsAction",
        "etag": '"e1"',
    }
    tasks_svc.drain_outbox()

    # Remote changes: marked completed with notes added
    mock_client.get_tasks.return_value = [
        {
            "id": "remote_finish_review",
            "title": "Finish Review",
            "notes": "Reviewed and approved by team",
            "status": "completed",
            "completed": "2026-10-22T10:15:00.000Z",
            "due": "2026-10-22T00:00:00.000Z",
            "etag": '"e2"',
        }
    ]

    tasks_svc.sync_tasks_for_list("@default")

    updated_task = task_svc.get_task_by_id(task.id)
    assert updated_task.status == "completed"
    assert updated_task.description == "Reviewed and approved by team"

    # Daily task completion is reconciled
    with db.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT completed_at FROM daily_tasks WHERE task_id = ?", (task.id,))
        dt = cursor.fetchone()
        assert dt["completed_at"] == "2026-10-22T10:15:00.000Z"
        cursor.execute("SELECT COUNT(*) as cnt FROM tasks")
        assert cursor.fetchone()["cnt"] == 1


def test_outbox_create_retry_idempotency(dedup_env):
    """If an outbox create task is retried after a remote ID has already been assigned, it updates rather than creates."""
    tasks_svc = dedup_env["tasks_svc"]
    task_svc = dedup_env["task_svc"]
    mock_client = dedup_env["client"]
    db = dedup_env["db"]

    task = task_svc.create_task("Retryable Task", source_type="manual")

    # Simulate already having received remote ID from first attempt
    with db.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT OR REPLACE INTO task_google_tasks_map (task_id, list_id, remote_id, etag, updated_at)
            VALUES (?, '@default', 'already_created_rem_id', '"etag_1"', CURRENT_TIMESTAMP)
            """,
            (task.id,),
        )
        conn.commit()

    # Outbox item still present
    tasks_svc.enqueue_outbox(
        action="create",
        task_id=task.id,
        payload={
            "list_id": "@default",
            "title": "Retryable Task",
            "notes": "Updated notes",
            "local_task_id": task.id,
        },
    )

    mock_client.update_task.return_value = {"id": "already_created_rem_id"}
    drained = tasks_svc.drain_outbox()
    assert drained > 0

    # Ensure update_task was called, NOT create_task
    assert mock_client.update_task.called
    assert not mock_client.create_task.called


def test_reconcile_duplicate_echoes(dedup_env):
    """
    Safe remediation cleanly removes spurious duplicate echo tasks, transfers completion/notes,
    cleans up duplicate daily_tasks, and preserves original task and history.
    """
    db = dedup_env["db"]

    # Seed exact real-world duplicate condition
    with db.get_connection() as conn:
        cursor = conn.cursor()
        # Original manual task (ID 57)
        cursor.execute(
            """
            INSERT INTO tasks (id, title, description, source_type, source_id, status, created_at, updated_at)
            VALUES (57, 'test tasking', '', 'manual', NULL, 'pending', '2026-10-22T10:19:11.316224', '2026-10-22T10:19:11.328577')
            """
        )
        cursor.execute(
            """
            INSERT INTO daily_tasks (id, task_id, date, position, completed_at)
            VALUES (47, 57, '2026-10-22', 8, NULL)
            """
        )
        cursor.execute(
            """
            INSERT INTO task_google_tasks_map (task_id, list_id, remote_id, etag, updated_at)
            VALUES (57, '@default', 'WVllZDBGOVdqbk9XYWZOTQ', '"vdisOMXpaH0"', '2026-10-22T10:19:11.830200')
            """
        )

        # Spurious echo google_tasks (ID 58)
        cursor.execute(
            """
            INSERT INTO sources (id, name, path, scan_status, created_at, updated_at)
            VALUES (1, 'Default', 'google_tasks://list/@default', 'completed', '2026-10-22T10:00:00', '2026-10-22T10:00:00')
            """
        )
        cursor.execute(
            """
            INSERT INTO file_items (id, source_id, path, title, type, metadata, completed, created_at, updated_at)
            VALUES (1178, 1, 'WVllZDBGOVdqbk9XYWZOTQ', 'test tasking', 'task', '{}', 0, '2026-10-22T10:19:12.555221', '2026-10-22T10:19:12.555221')
            """
        )
        cursor.execute(
            """
            INSERT INTO tasks (id, title, description, source_type, source_id, status, created_at, updated_at)
            VALUES (58, 'test tasking', '', 'google_tasks', 1178, 'pending', '2026-10-22T10:19:12.555221', '2026-10-22T10:19:18.327448')
            """
        )
        cursor.execute(
            """
            INSERT INTO daily_tasks (id, task_id, date, position, completed_at)
            VALUES (48, 58, '2026-10-22', 9, NULL)
            """
        )
        conn.commit()

    # Run reconciliation
    reconciled = db.reconcile_duplicate_echoes()
    assert len(reconciled) == 1
    assert reconciled[0]["original_id"] == 57
    assert reconciled[0]["echo_id"] == 58

    # Verify state after reconciliation
    with db.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, title, source_type FROM tasks")
        tasks = cursor.fetchall()
        assert len(tasks) == 1
        assert tasks[0]["id"] == 57
        assert tasks[0]["source_type"] == "manual"

        cursor.execute("SELECT id, task_id, date, position FROM daily_tasks")
        daily = cursor.fetchall()
        assert len(daily) == 1
        assert daily[0]["id"] == 47
        assert daily[0]["task_id"] == 57
        assert daily[0]["position"] == 8

        cursor.execute("SELECT id FROM file_items WHERE id = 1178")
        assert cursor.fetchone() is None

        cursor.execute("SELECT task_id, remote_id FROM task_google_tasks_map WHERE task_id = 57")
        mapping = cursor.fetchone()
        assert mapping is not None
        assert mapping["remote_id"] == "WVllZDBGOVdqbk9XYWZOTQ"

    # Re-running is idempotent
    second_run = db.reconcile_duplicate_echoes()
    assert len(second_run) == 0


def test_partial_unique_index_blocks_duplicate_remote_ids(dedup_env):
    """The database partial unique index prevents two tasks from ever mapping to the same remote ID."""
    db = dedup_env["db"]

    with db.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("INSERT INTO tasks (id, title, created_at, updated_at) VALUES (1, 'Task 1', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP), (2, 'Task 2', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)")
        cursor.execute(
            "INSERT INTO task_google_tasks_map (task_id, list_id, remote_id, updated_at) VALUES (1, '@default', 'remote_same', CURRENT_TIMESTAMP)"
        )
        conn.commit()

        # Attempting to map Task 2 to the same remote_id must raise IntegrityError
        with pytest.raises(sqlite3.IntegrityError):
            cursor.execute(
                "INSERT INTO task_google_tasks_map (task_id, list_id, remote_id, updated_at) VALUES (2, '@default', 'remote_same', CURRENT_TIMESTAMP)"
            )

        # Temporary local_ IDs are permitted to coexist without conflict
        cursor.execute(
            "INSERT INTO task_google_tasks_map (task_id, list_id, remote_id, updated_at) VALUES (2, '@default', 'local_temp_2', CURRENT_TIMESTAMP)"
        )
        conn.commit()


def test_cli_audit_duplicates_command(dedup_env, monkeypatch):
    """CLI audit-duplicates command displays diagnostic table with redacted remote IDs and supports --fix."""
    db = dedup_env["db"]
    config = dedup_env["config"]
    monkeypatch.setattr(Config, "load", lambda: config)

    # Seed duplicate pair
    with db.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("INSERT INTO tasks (id, title, source_type, created_at, updated_at) VALUES (10, 'Audit Item', 'manual', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)")
        cursor.execute("INSERT INTO daily_tasks (task_id, date, position) VALUES (10, '2026-10-22', 1)")
        cursor.execute(
            "INSERT INTO task_google_tasks_map (task_id, list_id, remote_id, updated_at) VALUES (10, '@default', 'SECRET_REMOTE_ID_12345', CURRENT_TIMESTAMP)"
        )
        cursor.execute(
            "INSERT INTO sources (id, name, path, created_at, updated_at) VALUES (1, 'Default', 'google_tasks://list/@default', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        )
        cursor.execute(
            "INSERT INTO file_items (id, source_id, path, title, type, created_at, updated_at) VALUES (50, 1, 'SECRET_REMOTE_ID_12345', 'Audit Item', 'task', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        )
        cursor.execute(
            "INSERT INTO tasks (id, title, source_type, source_id, created_at, updated_at) VALUES (11, 'Audit Item', 'google_tasks', 50, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        )
        cursor.execute("INSERT INTO daily_tasks (task_id, date, position) VALUES (11, '2026-10-22', 2)")
        conn.commit()

    runner = CliRunner()

    # 1. Read-only diagnostic: checks table output and redacted secret
    result = runner.invoke(cli, ["audit-duplicates"])
    assert result.exit_code == 0
    assert "Original" in result.output
    assert "Spurious" in result.output
    assert "Audit" in result.output
    assert "Found 1 duplicate echo pair(s)" in result.output
    # Full secret remote ID must NOT be leaked
    assert "SECRET_REMOTE_ID_12345" not in result.output
    assert "SECR" in result.output

    # 2. Reconcile with --fix
    fix_result = runner.invoke(cli, ["audit-duplicates", "--fix"])
    assert fix_result.exit_code == 0
    assert "Successfully reconciled 1 duplicate echo pair" in fix_result.output
    assert "Preserved original Task #10" in fix_result.output
    assert "removed echo Task #11" in fix_result.output

    # 3. Subsequent check reports clean
    clean_result = runner.invoke(cli, ["audit-duplicates"])
    assert clean_result.exit_code == 0
    assert "No duplicate tasks or spurious echoes detected" in clean_result.output
