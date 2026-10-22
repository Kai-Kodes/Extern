"""Deterministic integration tests for unified Google Tasks and Google Calendar synchronization."""

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from todo.config import Config
from todo.database import Database
from todo.integrations.google_calendar import (
    GoogleCalendarClient,
    GoogleCalendarNetworkError,
)
from todo.integrations.google_tasks import GoogleOAuthManager, GoogleTasksClient
from todo.models import FileItem, Source, Task
from todo.services.daily_service import DailyService
from todo.services.google_calendar_service import GoogleCalendarService
from todo.services.google_tasks_service import GoogleTasksService
from todo.services.google_tasks_sync_manager import GoogleTasksSyncManager
from todo.services.task_service import TaskService


@pytest.fixture
def unified_env(tmp_path):
    """Set up an isolated database and wired service stack for unified testing."""
    db_path = tmp_path / "unified_test.db"
    db = Database(db_path)
    db.init_db()

    token_file = tmp_path / "unified_token.json"
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
        google_calendar_enabled=True,
        google_default_task_list="@default",
        google_default_calendar="primary",
        google_calendar_delete_policy="delete",
        google_calendar_unschedule_policy="delete",
        google_calendar_complete_policy="keep",
    )

    mock_tasks_client = MagicMock(spec=GoogleTasksClient)
    mock_cal_client = MagicMock(spec=GoogleCalendarClient)

    tasks_svc = GoogleTasksService(
        db=db,
        config=config,
        oauth_manager=oauth_mgr,
        client=mock_tasks_client,
    )
    cal_svc = GoogleCalendarService(
        db=db,
        config=config,
        oauth_manager=oauth_mgr,
        client=mock_cal_client,
    )
    sync_mgr = GoogleTasksSyncManager(
        google_tasks_service=tasks_svc,
        google_calendar_service=cal_svc,
    )
    tasks_svc.sync_manager = sync_mgr
    cal_svc.sync_manager = sync_mgr

    task_svc = TaskService(
        db=db,
        google_tasks_service=tasks_svc,
        google_calendar_service=cal_svc,
    )
    daily_svc = DailyService(
        db=db,
        google_tasks_service=tasks_svc,
        google_calendar_service=cal_svc,
    )
    daily_svc.google_tasks_sync_manager = sync_mgr

    return {
        "db": db,
        "config": config,
        "oauth_mgr": oauth_mgr,
        "mock_tasks_client": mock_tasks_client,
        "mock_cal_client": mock_cal_client,
        "tasks_svc": tasks_svc,
        "cal_svc": cal_svc,
        "sync_mgr": sync_mgr,
        "task_svc": task_svc,
        "daily_svc": daily_svc,
    }


# =========================================================================
# 1. Automatic Google Tasks Enqueueing & Source Policy
# =========================================================================


def test_manual_task_creation_enqueues_google_tasks(unified_env):
    """Verify manual task creation automatically enqueues Google Tasks create mutation."""
    task_svc: TaskService = unified_env["task_svc"]
    tasks_svc: GoogleTasksService = unified_env["tasks_svc"]

    task = task_svc.create_task(title="Review Q4 Plan", source_type="manual")

    # Should be in outbox
    pending = tasks_svc.get_pending_outbox()
    assert len(pending) == 1
    assert pending[0]["action"] == "create"
    assert pending[0]["task_id"] == task.id

    payload = json.loads(pending[0]["payload"])
    assert payload["title"] == "Review Q4 Plan"
    assert payload["list_id"] == "@default"

    # Should be recorded in task_google_tasks_map with local temporary ID
    mapping = tasks_svc.get_task_google_mapping(task.id)
    assert mapping is not None
    assert mapping["remote_id"].startswith(f"local_{task.id}_")


def test_universal_task_creation_enqueues_google_tasks(unified_env):
    """Verify universal task creation automatically enqueues Google Tasks create mutation."""
    task_svc: TaskService = unified_env["task_svc"]
    tasks_svc: GoogleTasksService = unified_env["tasks_svc"]

    task = task_svc.create_task(title="Someday idea", source_type="universal")

    pending = tasks_svc.get_pending_outbox()
    assert len(pending) == 1
    assert pending[0]["action"] == "create"
    assert pending[0]["task_id"] == task.id


def test_source_backed_task_does_not_flood_google_tasks(unified_env):
    """Verify imported Filemeta, Telegram, and YouTube items do not enqueue Google Tasks."""
    task_svc: TaskService = unified_env["task_svc"]
    tasks_svc: GoogleTasksService = unified_env["tasks_svc"]

    # Source-backed items
    fm_task = task_svc.create_task(title="notes.txt", source_type="filemeta", source_id=101)
    tg_task = task_svc.create_task(title="Lesson 01", source_type="telegram", source_id=102)
    yt_task = task_svc.create_task(title="Video #1", source_type="youtube", source_id=103)

    pending = tasks_svc.get_pending_outbox()
    assert len(pending) == 0
    assert tasks_svc.get_task_google_mapping(fm_task.id) is None
    assert tasks_svc.get_task_google_mapping(tg_task.id) is None
    assert tasks_svc.get_task_google_mapping(yt_task.id) is None


# =========================================================================
# 2. Dated Tasks Enqueue Both Google Tasks and Calendar Event
# =========================================================================


def test_dated_task_enqueues_both_google_task_and_calendar_event(unified_env):
    """Verify creating and scheduling a task enqueues both Google Tasks and Google Calendar mutations."""
    task_svc: TaskService = unified_env["task_svc"]
    daily_svc: DailyService = unified_env["daily_svc"]
    tasks_svc: GoogleTasksService = unified_env["tasks_svc"]
    cal_svc: GoogleCalendarService = unified_env["cal_svc"]

    task = task_svc.create_task(title="Deploy to Staging", source_type="manual")
    daily_svc.add_task_to_date(task.id, "2026-10-25")

    # Google Tasks outbox: create + due date update
    tasks_outbox = tasks_svc.get_pending_outbox()
    assert len(tasks_outbox) >= 1
    assert any(item["action"] == "create" for item in tasks_outbox)

    # Google Calendar outbox: calendar_create
    cal_outbox = cal_svc.get_pending_outbox()
    assert len(cal_outbox) == 1
    assert cal_outbox[0]["action"] == "calendar_create"
    assert cal_outbox[0]["task_id"] == task.id

    payload = json.loads(cal_outbox[0]["payload"])
    assert payload["date"] == "2026-10-25"
    assert payload["title"] == "Deploy to Staging"
    assert payload["calendar_id"] == "primary"


def test_drain_chained_outbox_operations(unified_env):
    """Verify draining outbox propagates remote IDs across Google Tasks and creates Calendar events."""
    task_svc: TaskService = unified_env["task_svc"]
    daily_svc: DailyService = unified_env["daily_svc"]
    tasks_svc: GoogleTasksService = unified_env["tasks_svc"]
    cal_svc: GoogleCalendarService = unified_env["cal_svc"]
    mock_tasks: MagicMock = unified_env["mock_tasks_client"]
    mock_cal: MagicMock = unified_env["mock_cal_client"]

    # Configure mock responses
    mock_tasks.create_task.return_value = {
        "id": "remote_gtask_888",
        "title": "Build Release",
        "etag": "etag_tasks_888",
    }
    mock_cal.find_events_by_task_id.return_value = []
    mock_cal.create_all_day_event.return_value = {
        "id": "remote_event_777",
        "summary": "Build Release",
        "start": {"date": "2026-10-25"},
        "end": {"date": "2026-10-26"},
    }

    task = task_svc.create_task(title="Build Release", source_type="manual")
    daily_svc.add_task_to_date(task.id, "2026-10-25")

    # Drain Google Tasks
    processed_tasks = tasks_svc.drain_outbox()
    assert processed_tasks >= 1
    mapping = tasks_svc.get_task_google_mapping(task.id)
    assert mapping["remote_id"] == "remote_gtask_888"

    # Drain Google Calendar
    processed_cal = cal_svc.drain_outbox()
    assert processed_cal == 1
    cal_mapping = cal_svc.get_event_mapping(task.id, "primary")
    assert cal_mapping["event_id"] == "remote_event_777"
    assert cal_mapping["start_date"] == "2026-10-25"
    assert cal_mapping["end_date"] == "2026-10-26"


# =========================================================================
# 3. Partial Failure & Retry Without Duplication
# =========================================================================


def test_partial_failure_tasks_success_calendar_failure(unified_env):
    """Verify if Tasks succeeds but Calendar fails, only Calendar is retried without duplicate tasks."""
    task_svc: TaskService = unified_env["task_svc"]
    daily_svc: DailyService = unified_env["daily_svc"]
    tasks_svc: GoogleTasksService = unified_env["tasks_svc"]
    cal_svc: GoogleCalendarService = unified_env["cal_svc"]
    mock_tasks: MagicMock = unified_env["mock_tasks_client"]
    mock_cal: MagicMock = unified_env["mock_cal_client"]

    mock_tasks.create_task.return_value = {"id": "remote_gtask_success"}
    # Simulate temporary network error on Calendar
    mock_cal.find_events_by_task_id.return_value = []
    mock_cal.create_all_day_event.side_effect = GoogleCalendarNetworkError("Temporary network outage")

    task = task_svc.create_task(title="Quarterly Review", source_type="manual")
    daily_svc.add_task_to_date(task.id, "2026-10-30")

    # Drain tasks succeeds
    assert tasks_svc.drain_outbox() >= 1
    assert len(tasks_svc.get_pending_outbox()) == 0
    assert tasks_svc.get_task_google_mapping(task.id)["remote_id"] == "remote_gtask_success"

    # Drain calendar fails safely
    assert cal_svc.drain_outbox() == 0
    cal_pending = cal_svc.get_pending_outbox()
    assert len(cal_pending) == 1
    assert cal_pending[0]["retry_count"] == 1

    # Now calendar network recovers
    mock_cal.create_all_day_event.side_effect = None
    mock_cal.create_all_day_event.return_value = {
        "id": "remote_event_recovered",
        "summary": "Quarterly Review",
        "start": {"date": "2026-10-30"},
        "end": {"date": "2026-10-31"},
    }

    # Second pass: calendar drains successfully
    assert cal_svc.drain_outbox() == 1
    assert len(cal_svc.get_pending_outbox()) == 0
    assert cal_svc.get_event_mapping(task.id, "primary")["event_id"] == "remote_event_recovered"

    # Tasks client was called once (no duplicate remote tasks created!)
    assert mock_tasks.create_task.call_count == 1


# =========================================================================
# 4. Reschedule, Unschedule, Edit, and Delete Lifecycle
# =========================================================================


def test_reschedule_task_updates_both_remotes(unified_env):
    """Verify rescheduling enqueues Google Tasks due date update AND Calendar event update."""
    task_svc: TaskService = unified_env["task_svc"]
    daily_svc: DailyService = unified_env["daily_svc"]
    tasks_svc: GoogleTasksService = unified_env["tasks_svc"]
    cal_svc: GoogleCalendarService = unified_env["cal_svc"]

    task = task_svc.create_task(title="Launch Site", source_type="manual")
    daily_svc.add_task_to_date(task.id, "2026-10-25")

    # Clear initial create outbox items
    with unified_env["db"].get_connection() as conn:
        conn.execute("DELETE FROM google_tasks_outbox")
        conn.commit()

    # Reschedule to new date
    daily_svc.reschedule_task(task.id, "2026-11-05")

    # Check tasks outbox
    tasks_outbox = tasks_svc.get_pending_outbox()
    assert len(tasks_outbox) == 1
    assert tasks_outbox[0]["action"] == "update"
    assert json.loads(tasks_outbox[0]["payload"])["due_date"] == "2026-11-05"

    # Check calendar outbox
    cal_outbox = cal_svc.get_pending_outbox()
    assert len(cal_outbox) == 1
    assert cal_outbox[0]["action"] == "calendar_update"
    assert json.loads(cal_outbox[0]["payload"])["date"] == "2026-11-05"


def test_unschedule_task_clears_due_date_and_applies_calendar_policy(unified_env):
    """Verify unscheduling clears due date in Google Tasks and enqueues Calendar delete mutation."""
    task_svc: TaskService = unified_env["task_svc"]
    daily_svc: DailyService = unified_env["daily_svc"]
    tasks_svc: GoogleTasksService = unified_env["tasks_svc"]
    cal_svc: GoogleCalendarService = unified_env["cal_svc"]

    task = task_svc.create_task(title="Flexible Goal", source_type="manual")
    daily_svc.add_task_to_date(task.id, "2026-10-25")

    with unified_env["db"].get_connection() as conn:
        conn.execute("DELETE FROM google_tasks_outbox")
        conn.commit()

    daily_svc.unschedule_task(task.id)

    # Google Tasks outbox should have clear_due=True
    tasks_outbox = tasks_svc.get_pending_outbox()
    assert len(tasks_outbox) == 1
    assert json.loads(tasks_outbox[0]["payload"])["clear_due"] is True

    # Calendar outbox should have calendar_delete
    cal_outbox = cal_svc.get_pending_outbox()
    assert len(cal_outbox) == 1
    assert cal_outbox[0]["action"] == "calendar_delete"


def test_task_title_and_description_edits(unified_env):
    """Verify editing task title and description enqueues updates for both Google Tasks and Calendar."""
    task_svc: TaskService = unified_env["task_svc"]
    daily_svc: DailyService = unified_env["daily_svc"]
    tasks_svc: GoogleTasksService = unified_env["tasks_svc"]
    cal_svc: GoogleCalendarService = unified_env["cal_svc"]

    task = task_svc.create_task(title="Old Title", description="Old Desc", source_type="manual")
    daily_svc.add_task_to_date(task.id, "2026-10-25")

    # Seed local calendar event mapping so on_task_edited recognizes the event
    cal_svc.save_event_mapping(
        task_id=task.id,
        calendar_id="primary",
        event_id="evt_mapped_123",
        summary="Old Title",
        start_date="2026-10-25",
        end_date="2026-10-26",
    )

    with unified_env["db"].get_connection() as conn:
        conn.execute("DELETE FROM google_tasks_outbox")
        conn.commit()

    task_svc.update_task(task.id, title="Brand New Title", description="Brand New Notes")

    tasks_outbox = tasks_svc.get_pending_outbox()
    assert len(tasks_outbox) == 1
    t_payload = json.loads(tasks_outbox[0]["payload"])
    assert t_payload["title"] == "Brand New Title"
    assert t_payload["notes"] == "Brand New Notes"

    cal_outbox = cal_svc.get_pending_outbox()
    assert len(cal_outbox) == 1
    c_payload = json.loads(cal_outbox[0]["payload"])
    assert c_payload["title"] == "Brand New Title"
    assert c_payload["description"] == "Brand New Notes"


def test_task_completion_preserves_calendar_history(unified_env):
    """Verify toggling task completion marks Google Task completed but preserves Calendar event."""
    task_svc: TaskService = unified_env["task_svc"]
    daily_svc: DailyService = unified_env["daily_svc"]
    tasks_svc: GoogleTasksService = unified_env["tasks_svc"]
    cal_svc: GoogleCalendarService = unified_env["cal_svc"]

    task = task_svc.create_task(title="Complete Documentation", source_type="manual")
    dt, _ = daily_svc.add_task_to_date(task.id, "2026-10-25")

    with unified_env["db"].get_connection() as conn:
        conn.execute("DELETE FROM google_tasks_outbox")
        conn.commit()

    daily_svc.toggle_task_completion(dt.id)

    # Google Tasks outbox should have action="complete"
    tasks_outbox = tasks_svc.get_pending_outbox()
    assert len(tasks_outbox) == 1
    assert tasks_outbox[0]["action"] == "complete"

    # Calendar outbox should NOT have deleted the event (history preserved)
    cal_outbox = cal_svc.get_pending_outbox()
    assert len(cal_outbox) == 0


def test_task_deletion_cleans_up_remotes(unified_env):
    """Verify deleting a task permanently enqueues deletion on both Google Tasks and Calendar."""
    task_svc: TaskService = unified_env["task_svc"]
    daily_svc: DailyService = unified_env["daily_svc"]
    tasks_svc: GoogleTasksService = unified_env["tasks_svc"]
    cal_svc: GoogleCalendarService = unified_env["cal_svc"]

    task = task_svc.create_task(title="Temp Task", source_type="manual")
    daily_svc.add_task_to_date(task.id, "2026-10-25")

    # Set real remote id so delete is enqueued
    with unified_env["db"].get_connection() as conn:
        conn.execute(
            "UPDATE task_google_tasks_map SET remote_id = 'remote_gtask_real' WHERE task_id = ?",
            (task.id,),
        )
        conn.execute("DELETE FROM google_tasks_outbox")
        conn.commit()

    task_svc.delete_task(task.id)

    tasks_outbox = tasks_svc.get_pending_outbox()
    assert len(tasks_outbox) == 1
    assert tasks_outbox[0]["action"] == "delete"

    cal_outbox = cal_svc.get_pending_outbox()
    assert len(cal_outbox) == 1
    assert cal_outbox[0]["action"] == "calendar_delete"


# =========================================================================
# 5. Database Migration Tests
# =========================================================================


def test_database_migration_from_v1_schema(tmp_path):
    """Verify database migration initializes new tables and backfills task_google_tasks_map."""
    db_path = tmp_path / "v1_migration.db"
    conn = sqlite3.connect(str(db_path))

    # Create older v1 schema without calendar tables or target column in outbox
    conn.executescript(
        """
        CREATE TABLE sources (id INTEGER PRIMARY KEY, name TEXT, path TEXT UNIQUE, created_at TEXT, updated_at TEXT);
        CREATE TABLE file_items (id INTEGER PRIMARY KEY, source_id INTEGER, path TEXT, title TEXT, metadata TEXT DEFAULT '{}', completed INTEGER DEFAULT 0, created_at TEXT, updated_at TEXT);
        CREATE TABLE tasks (id INTEGER PRIMARY KEY, title TEXT, description TEXT, source_type TEXT DEFAULT 'manual', source_id INTEGER, status TEXT DEFAULT 'pending', created_at TEXT, updated_at TEXT);
        CREATE TABLE daily_tasks (id INTEGER PRIMARY KEY, task_id INTEGER, date TEXT, position INTEGER, completed_at TEXT);
        CREATE TABLE google_task_lists_cache (id TEXT PRIMARY KEY, title TEXT, updated TEXT, etag TEXT, cached_at TEXT, updated_at TEXT);
        CREATE TABLE google_tasks_outbox (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id INTEGER, action TEXT, payload TEXT, created_at TEXT);
        """
    )

    # Insert existing Google Task and file_item
    conn.execute(
        "INSERT INTO file_items (id, source_id, path, title, metadata, created_at, updated_at) VALUES (50, 1, 'gtask_remote_existing', 'Existing Google Task', '{\"list_id\": \"list_main\", \"etag\": \"etag_1\"}', 'now', 'now')"
    )
    conn.execute(
        "INSERT INTO tasks (id, title, source_type, source_id, status, created_at, updated_at) VALUES (100, 'Existing Google Task', 'google_tasks', 50, 'pending', 'now', 'now')"
    )
    conn.commit()
    conn.close()

    # Now open with Database class and run init_db()
    db = Database(db_path)
    db.init_db()

    with db.get_connection() as check_conn:
        cursor = check_conn.cursor()

        # 1. Verify google_tasks_outbox has new columns
        cursor.execute("PRAGMA table_info(google_tasks_outbox)")
        columns = {row["name"] for row in cursor.fetchall()}
        assert "target" in columns
        assert "retry_count" in columns
        assert "last_error" in columns
        assert "locked_until" in columns

        # 2. Verify google_calendar_events and google_calendars_cache exist
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='google_calendar_events'")
        assert cursor.fetchone() is not None

        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='google_calendars_cache'")
        assert cursor.fetchone() is not None

        # 3. Verify task_google_tasks_map exists and was backfilled with existing Google Task
        cursor.execute("SELECT task_id, list_id, remote_id, etag FROM task_google_tasks_map WHERE task_id = 100")
        row = cursor.fetchone()
        assert row is not None
        assert row["task_id"] == 100
        assert row["list_id"] == "list_main"
        assert row["remote_id"] == "gtask_remote_existing"
        assert row["etag"] == "etag_1"
