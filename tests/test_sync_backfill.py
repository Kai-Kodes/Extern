"""Deterministic tests for initial synchronization (backfill) of existing tasks to Google Tasks and Google Calendar."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from todo.cli import cli, sync_existing_cmd
from todo.config import Config
from todo.database import Database
from todo.integrations.google_tasks import GoogleOAuthManager
from todo.services.backfill_service import BackfillPlan, BackfillResult, SyncBackfillService, TaskBackfillItem
from todo.services.google_calendar_service import GoogleCalendarService
from todo.services.google_tasks_service import GoogleTasksService


@pytest.fixture
def backfill_env(tmp_path):
    """Setup isolated test environment with clean DB, OAuth tokens, and services."""
    db_path = tmp_path / "test_backfill.db"
    db = Database(db_path)
    db.init_db()

    token_file = tmp_path / "token.json"
    mgr = GoogleOAuthManager(token_path=token_file, client_id="cid", client_secret="sec")
    mgr.save_tokens({
        "access_token": "valid_access_token",
        "refresh_token": "valid_refresh_token",
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
        google_calendar_complete_policy="keep",
    )

    tasks_service = GoogleTasksService(db, config)
    calendar_service = GoogleCalendarService(db, config)

    # Mock remote API clients to prevent live network calls during tests
    tasks_client = MagicMock()
    tasks_client.create_task.return_value = {"id": "gtask_remote_123", "etag": "etag_1"}
    tasks_client.update_task.return_value = {"id": "gtask_remote_123", "etag": "etag_1"}
    tasks_service.client = tasks_client

    calendar_client = MagicMock()
    calendar_client.create_all_day_event.return_value = {"id": "gcal_event_456", "etag": "cal_etag_1"}
    calendar_client.find_events_by_task_id.return_value = []
    calendar_service.client = calendar_client

    service = SyncBackfillService(db, config, tasks_service=tasks_service, calendar_service=calendar_service)

    return {
        "db": db,
        "config": config,
        "service": service,
        "tasks_service": tasks_service,
        "calendar_service": calendar_service,
        "tasks_client": tasks_client,
        "calendar_client": calendar_client,
    }


def test_build_plan_mixed_task_types(backfill_env):
    """Verify build_plan discovers eligible tasks and applies sync policy correctly."""
    db = backfill_env["db"]
    service = backfill_env["service"]

    with db.get_connection() as conn:
        cursor = conn.cursor()
        # 1. Manual unscheduled task -> Tasks: Yes, Calendar: No (unscheduled)
        cursor.execute("INSERT INTO tasks (title, source_type, status, created_at, updated_at) VALUES ('Manual Unscheduled', 'manual', 'pending', '2026-10-22T10:00:00', '2026-10-22T10:00:00')")
        t1_id = cursor.lastrowid

        # 2. Manual scheduled task -> Tasks: Yes, Calendar: Yes
        cursor.execute("INSERT INTO tasks (title, source_type, status, created_at, updated_at) VALUES ('Manual Scheduled', 'manual', 'pending', '2026-10-22T10:00:00', '2026-10-22T10:00:00')")
        t2_id = cursor.lastrowid
        cursor.execute("INSERT INTO daily_tasks (task_id, date, position) VALUES (?, '2026-10-25', 0)", (t2_id,))

        # 3. Universal unscheduled task -> Tasks: Yes, Calendar: No
        cursor.execute("INSERT INTO tasks (title, source_type, status, created_at, updated_at) VALUES ('Universal Task', 'universal', 'pending', '2026-10-22T10:00:00', '2026-10-22T10:00:00')")
        t3_id = cursor.lastrowid

        # 4. Source-backed scheduled task (filemeta) -> Tasks: No (source-backed), Calendar: Yes (scheduled)
        cursor.execute("INSERT INTO tasks (title, source_type, status, created_at, updated_at) VALUES ('Filemeta Scheduled', 'filemeta', 'pending', '2026-10-22T10:00:00', '2026-10-22T10:00:00')")
        t4_id = cursor.lastrowid
        cursor.execute("INSERT INTO daily_tasks (task_id, date, position) VALUES (?, '2026-10-26', 0)", (t4_id,))

        # 5. Source-backed unscheduled task (telegram) -> Tasks: No, Calendar: No
        cursor.execute("INSERT INTO tasks (title, source_type, status, created_at, updated_at) VALUES ('Telegram Unscheduled', 'telegram', 'pending', '2026-10-22T10:00:00', '2026-10-22T10:00:00')")
        t5_id = cursor.lastrowid

        # 6. Google Tasks origin task (already mapped) -> Tasks: No (origin), Calendar: Yes (scheduled)
        cursor.execute("INSERT INTO sources (id, name, path, scan_status, created_at, updated_at) VALUES (1, 'Google Tasks', 'google_tasks://list/@default', 'completed', '2026-10-22', '2026-10-22')")
        cursor.execute("INSERT INTO file_items (id, source_id, path, title, type, metadata, created_at, updated_at) VALUES (1, 1, 'gtask_orig_1', 'Google Tasks Scheduled', 'google_tasks', '{}', '2026-10-22T10:00:00', '2026-10-22T10:00:00')")
        cursor.execute("INSERT INTO tasks (title, source_type, source_id, status, created_at, updated_at) VALUES ('Google Tasks Scheduled', 'google_tasks', 1, 'pending', '2026-10-22T10:00:00', '2026-10-22T10:00:00')")
        t6_id = cursor.lastrowid
        cursor.execute("INSERT INTO daily_tasks (task_id, date, position) VALUES (?, '2026-10-27', 0)", (t6_id,))

        conn.commit()

    plan = service.build_plan()
    assert len(plan.items) == 6

    # Verify task 1 (Manual Unscheduled)
    p1 = next(i for i in plan.items if i.task_id == t1_id)
    assert p1.sync_to_tasks is True
    assert p1.tasks_target_list == "@default"
    assert p1.sync_to_calendar is False
    assert p1.calendar_skip_reason == "not scheduled in daily_tasks"

    # Verify task 2 (Manual Scheduled)
    p2 = next(i for i in plan.items if i.task_id == t2_id)
    assert p2.sync_to_tasks is True
    assert p2.sync_to_calendar is True
    assert p2.calendar_target_id == "primary"

    # Verify task 3 (Universal)
    p3 = next(i for i in plan.items if i.task_id == t3_id)
    assert p3.sync_to_tasks is True
    assert p3.sync_to_calendar is False

    # Verify task 4 (Filemeta Scheduled)
    p4 = next(i for i in plan.items if i.task_id == t4_id)
    assert p4.sync_to_tasks is False
    assert "source-backed" in p4.tasks_skip_reason
    assert p4.sync_to_calendar is True

    # Verify task 5 (Telegram Unscheduled)
    p5 = next(i for i in plan.items if i.task_id == t5_id)
    assert p5.sync_to_tasks is False
    assert p5.sync_to_calendar is False

    # Verify task 6 (Google Tasks Origin)
    p6 = next(i for i in plan.items if i.task_id == t6_id)
    assert p6.sync_to_tasks is False
    assert p6.tasks_skip_reason in ("already google_tasks origin", "already mapped to Google Tasks")
    assert p6.sync_to_calendar is True

    assert plan.tasks_to_sync_count == 3  # t1, t2, t3
    assert plan.calendar_to_sync_count == 3  # t2, t4, t6


def test_completed_task_preservation_in_backfill(backfill_env):
    """Verify completed task state and completed_at timestamp are preserved during backfill."""
    db = backfill_env["db"]
    service = backfill_env["service"]

    with db.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO tasks (title, description, source_type, status, created_at, updated_at)
            VALUES ('Completed Task', 'Some note', 'manual', 'completed', '2026-10-20T10:00:00', '2026-10-21T12:00:00')
            """
        )
        t_id = cursor.lastrowid
        cursor.execute(
            """
            INSERT INTO daily_tasks (task_id, date, position, completed_at)
            VALUES (?, '2026-10-21', 0, '2026-10-21T12:00:00')
            """,
            (t_id,),
        )
        conn.commit()

    plan = service.build_plan()
    item = next(i for i in plan.items if i.task_id == t_id)
    assert item.completed is True
    assert item.completed_at == "2026-10-21T12:00:00"
    assert item.sync_to_tasks is True

    result = service.execute_backfill(plan=plan, drain=False)
    assert result.tasks_enqueued == 1

    # Check outbox payload
    with db.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT payload FROM google_tasks_outbox WHERE task_id = ? AND target = 'tasks'", (t_id,))
        row = cursor.fetchone()
        assert row is not None
        payload = json.loads(row["payload"])
        assert payload["status"] == "completed"
        assert payload["completed"] == "2026-10-21T12:00:00"
        assert payload["title"] == "Completed Task"
        assert payload["due_date"] == "2026-10-21"


def test_idempotent_repeated_backfill_runs(backfill_env):
    """Verify executing backfill multiple times is strictly idempotent and does not duplicate entries."""
    db = backfill_env["db"]
    service = backfill_env["service"]
    tasks_client = backfill_env["tasks_client"]
    calendar_client = backfill_env["calendar_client"]

    with db.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("INSERT INTO tasks (title, source_type, status, created_at, updated_at) VALUES ('Idempotent Task', 'manual', 'pending', '2026-10-22T10:00:00', '2026-10-22T10:00:00')")
        t_id = cursor.lastrowid
        cursor.execute("INSERT INTO daily_tasks (task_id, date, position) VALUES (?, '2026-10-25', 0)", (t_id,))
        conn.commit()

    # Pass 1: Build plan and execute
    plan1 = service.build_plan()
    assert plan1.tasks_to_sync_count == 1
    assert plan1.calendar_to_sync_count == 1

    result1 = service.execute_backfill(plan=plan1, drain=True)
    assert result1.tasks_enqueued == 1
    assert result1.calendar_enqueued == 1
    assert result1.tasks_drained == 1
    assert result1.calendar_drained == 1
    assert tasks_client.create_task.call_count == 1
    assert calendar_client.create_all_day_event.call_count == 1

    # Pass 2: Re-build plan immediately after execution
    plan2 = service.build_plan()
    assert plan2.tasks_to_sync_count == 0
    assert plan2.calendar_to_sync_count == 0
    assert plan2.tasks_already_synced_count == 1
    assert plan2.calendar_already_synced_count == 1

    # Pass 3: Execute backfill again
    result2 = service.execute_backfill(plan=plan2, drain=True)
    assert result2.tasks_enqueued == 0
    assert result2.calendar_enqueued == 0
    # No additional remote calls should have been made
    assert tasks_client.create_task.call_count == 1
    assert calendar_client.create_all_day_event.call_count == 1


def test_dry_run_leaves_database_untouched(backfill_env):
    """Verify dry-run mode / build_plan makes zero modifications to outbox or mappings."""
    db = backfill_env["db"]
    service = backfill_env["service"]

    with db.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("INSERT INTO tasks (title, source_type, status, created_at, updated_at) VALUES ('Dry Run Task', 'manual', 'pending', '2026-10-22T10:00:00', '2026-10-22T10:00:00')")
        conn.commit()

    plan = service.build_plan()
    assert plan.tasks_to_sync_count == 1

    with db.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT count(*) as count FROM google_tasks_outbox")
        assert cursor.fetchone()["count"] == 0
        cursor.execute("SELECT count(*) as count FROM task_google_tasks_map")
        assert cursor.fetchone()["count"] == 0
        cursor.execute("SELECT count(*) as count FROM google_calendar_events")
        assert cursor.fetchone()["count"] == 0


def test_missing_auth_handling(backfill_env):
    """Verify execute_backfill returns descriptive failure if auth is missing."""
    db = backfill_env["db"]
    service = backfill_env["service"]

    with db.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("INSERT INTO tasks (title, source_type, status, created_at, updated_at) VALUES ('Auth Task', 'manual', 'pending', '2026-10-22T10:00:00', '2026-10-22T10:00:00')")
        conn.commit()

    # Mock unauthenticated
    with patch.object(service.tasks_service, "is_authenticated", return_value=False):
        plan = service.build_plan()
        assert plan.has_tasks_auth is False

        result = service.execute_backfill(plan=plan)
        assert result.success is False
        assert any("Google Tasks authentication required" in err for err in result.errors)


def test_calendar_disabled_skips_calendar(backfill_env):
    """Verify that when google_calendar_enabled=False, calendar synchronization is skipped."""
    db = backfill_env["db"]
    service = backfill_env["service"]
    service.config.google_calendar_enabled = False
    service.calendar_service.config.google_calendar_enabled = False

    with db.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("INSERT INTO tasks (title, source_type, status, created_at, updated_at) VALUES ('Cal Disabled Task', 'manual', 'pending', '2026-10-22T10:00:00', '2026-10-22T10:00:00')")
        t_id = cursor.lastrowid
        cursor.execute("INSERT INTO daily_tasks (task_id, date, position) VALUES (?, '2026-10-25', 0)", (t_id,))
        conn.commit()

    plan = service.build_plan()
    assert plan.tasks_to_sync_count == 1
    assert plan.calendar_to_sync_count == 0
    p = next(i for i in plan.items if i.task_id == t_id)
    assert p.calendar_skip_reason == "Google Calendar disabled in settings"


def test_calendar_complete_policy_delete_skips_completed_task(backfill_env):
    """Verify that when calendar delete policy is active, completed tasks are not synced to calendar."""
    db = backfill_env["db"]
    service = backfill_env["service"]
    service.config.google_calendar_complete_policy = "delete"

    with db.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("INSERT INTO tasks (title, source_type, status, created_at, updated_at) VALUES ('Complete Policy Task', 'manual', 'completed', '2026-10-22T10:00:00', '2026-10-22T10:00:00')")
        t_id = cursor.lastrowid
        cursor.execute("INSERT INTO daily_tasks (task_id, date, position, completed_at) VALUES (?, '2026-10-25', 0, '2026-10-25T15:00:00')", (t_id,))
        conn.commit()

    plan = service.build_plan()
    p = next(i for i in plan.items if i.task_id == t_id)
    assert p.sync_to_calendar is False
    assert p.calendar_skip_reason == "completed (calendar delete policy)"


def test_cli_sync_existing_dry_run(backfill_env, monkeypatch):
    """Verify CLI sync-existing with --dry-run option displays plan and makes no changes."""
    config = backfill_env["config"]
    db = backfill_env["db"]

    with db.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("INSERT INTO tasks (title, source_type, status, created_at, updated_at) VALUES ('DryRunTask', 'manual', 'pending', '2026-10-22T10:00:00', '2026-10-22T10:00:00')")
        conn.commit()

    monkeypatch.setattr(Config, "load", lambda: config)

    runner = CliRunner()
    result = runner.invoke(cli, ["sync-existing", "--dry-run"])
    assert result.exit_code == 0
    assert "TASK SYNCHRONIZATION BACKFILL" in result.output
    assert "Dry run completed" in result.output
    assert "DryRunTask" in result.output


def test_cli_sync_existing_conflict_options():
    """Verify specifying both --tasks-only and --calendar-only fails gracefully."""
    runner = CliRunner()
    result = runner.invoke(cli, ["sync-existing", "--tasks-only", "--calendar-only"])
    assert result.exit_code != 0
    assert "Cannot specify both --tasks-only and --calendar-only" in result.output


def test_cli_backfill_alias_dry_run(backfill_env, monkeypatch):
    """Verify 'todo backfill' command alias works identically to 'todo sync-existing'."""
    config = backfill_env["config"]
    monkeypatch.setattr(Config, "load", lambda: config)

    runner = CliRunner()
    result = runner.invoke(cli, ["backfill", "--dry-run"])
    assert result.exit_code == 0
    assert "TASK SYNCHRONIZATION BACKFILL" in result.output
    assert "Dry run completed" in result.output


def test_cli_tasks_only_and_calendar_only_flags(backfill_env, monkeypatch):
    """Verify CLI --tasks-only and --calendar-only execute without conflict."""
    config = backfill_env["config"]
    monkeypatch.setattr(Config, "load", lambda: config)

    runner = CliRunner()
    res1 = runner.invoke(cli, ["sync-existing", "--tasks-only", "--dry-run"])
    assert res1.exit_code == 0

    res2 = runner.invoke(cli, ["sync-existing", "--calendar-only", "--dry-run"])
    assert res2.exit_code == 0


def test_execute_backfill_records_drain_errors(backfill_env):
    """Verify execute_backfill reflects network/API drain errors recorded in outbox."""
    db = backfill_env["db"]
    service = backfill_env["service"]

    with db.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("INSERT INTO tasks (title, source_type, status, created_at, updated_at) VALUES ('Error Task', 'manual', 'pending', '2026-10-22T10:00:00', '2026-10-22T10:00:00')")
        conn.commit()

    # Simulate drain raising an error that is recorded in outbox
    def failing_drain():
        with db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE google_tasks_outbox SET retry_count = 1, last_error = 'HTTP 500 Internal Error'")
            conn.commit()
        return 0

    service.tasks_service.drain_outbox = failing_drain

    plan = service.build_plan()
    result = service.execute_backfill(plan=plan, drain=True)
    assert result.success is False
    assert any("HTTP 500 Internal Error" in e for e in result.errors)

