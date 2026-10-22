"""Tests verifying that connection and sync-status text is completely absent from Extern's normal TUI footer,
while preserving all synchronization functionality, change detection, and keyboard navigation.
"""

from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

from todo.config import Config
from todo.database import Database
from todo.models import DailyTask, Task
from todo.services.daily_service import DailyService
from todo.services.google_tasks_service import GoogleTasksService
from todo.services.google_tasks_sync_manager import GoogleTasksSyncManager
from todo.services.task_service import TaskService
from todo.ui.console import render_footer
from todo.ui.timeline_view import TimelineView
from todo.ui.today_view import TodayView
from todo.ui.universal_view import UniversalView


@pytest.fixture
def tui_footer_env(tmp_path: Path):
    db_path = tmp_path / "test_footer.db"
    token_path = tmp_path / "token.json"
    cfg = Config(
        db_path=db_path,
        google_client_id="test_id",
        google_client_secret="test_secret",
        google_token_path=token_path,
    )
    db = Database(db_path)
    db.init_db()

    task_svc = TaskService(db)
    daily_svc = DailyService(db)

    oauth_mgr = MagicMock()
    oauth_mgr.is_authenticated.return_value = True
    oauth_mgr.has_tasks_scope.return_value = True
    oauth_mgr.has_calendar_scope.return_value = True
    oauth_mgr.needs_reauthorization_for_calendar.return_value = False

    gtasks_svc = MagicMock()
    gtasks_svc.oauth_manager = oauth_mgr
    gtasks_svc.is_authenticated.return_value = True

    sync_mgr = GoogleTasksSyncManager(
        google_tasks_service=gtasks_svc,
        interval=60.0,
    )
    sync_mgr.last_sync_time = datetime(2026, 10, 22, 22, 54, 0)
    sync_mgr.last_sync_status = "Tasks & Calendar Synced 22:54"

    return {
        "db": db,
        "cfg": cfg,
        "task_svc": task_svc,
        "daily_svc": daily_svc,
        "sync_mgr": sync_mgr,
        "gtasks_svc": gtasks_svc,
    }


def test_render_footer_omits_status_line_and_preserves_clean_layout():
    """Verify render_footer produces exactly divider, hints, and trailing newline, ignoring status."""
    hints = [("+", "Add task"), ("q", "Quit")]

    # Call with a typical status string
    status_str = "● Connected · Tasks & Calendar Synced 22:54"
    with patch("todo.ui.console.console.print") as mock_print:
        render_footer(hints, status=status_str)

        # Must have exactly 3 print calls: divider, shortcuts, and trailing newline
        assert mock_print.call_count == 3
        calls = [c[0] for c in mock_print.call_args_list]

        # 1. Divider line
        assert any("─" in str(arg) for arg in calls[0])
        # 2. Hints line
        assert any("Add task" in str(arg) for arg in calls[1])
        assert any("Quit" in str(arg) for arg in calls[1])
        # 3. Trailing newline (empty call)
        assert calls[2] == ()

        # Ensure no status text or status-specific styles leaked
        combined_output = " ".join(str(c) for c in mock_print.call_args_list)
        assert "Connected" not in combined_output
        assert "Synced" not in combined_output
        assert "22:54" not in combined_output
        assert "dim green" not in combined_output
        assert "bold yellow" not in combined_output


def test_today_view_footer_clean_without_sync_text(tui_footer_env):
    """Verify TodayView does not render any connection or sync status text in empty or populated states."""
    daily_svc = tui_footer_env["daily_svc"]
    task_svc = tui_footer_env["task_svc"]
    view = TodayView(daily_svc, task_svc)

    status_str = "● Connected · Tasks & Calendar Synced 22:54"

    # 1. Empty state
    with patch("todo.ui.console.console.print") as mock_print:
        view.render(tasks=[], sync_status=status_str, clear=False)
        output = " ".join(str(c) for c in mock_print.call_args_list)

        assert "Connected" not in output
        assert "Synced" not in output
        assert "22:54" not in output
        assert "Syncing" not in output
        # Shortcuts must be intact
        assert "Add task" in output
        assert "Timeline" in output
        assert "Universal" in output
        assert "Pomodoro" in output
        assert "Google" in output
        assert "Quit" in output

    # 2. Populated state with a task
    t = task_svc.create_task("Test task")
    daily_svc.add_task_to_date(t.id, "2026-10-22")
    active_tasks = daily_svc.get_active_tasks_for_date("2026-10-22")

    with patch("todo.ui.console.console.print") as mock_print:
        view.render(tasks=active_tasks, sync_status=status_str, clear=False)
        output = " ".join(str(c) for c in mock_print.call_args_list)

        assert "Connected" not in output
        assert "Synced" not in output
        assert "22:54" not in output
        # Shortcuts must be intact
        assert "Navigate" in output
        assert "Toggle" in output
        assert "Edit" in output
        assert "Delete" in output


def test_today_view_calendar_reauth_hint_preserved_without_status_line(tui_footer_env):
    """Verify that when calendar auth is needed, shortcut hint shows 'Google (Auth)' without status line."""
    daily_svc = tui_footer_env["daily_svc"]
    task_svc = tui_footer_env["task_svc"]
    view = TodayView(daily_svc, task_svc)

    auth_status = "● Tasks Connected · Calendar Auth Needed"
    with patch("todo.ui.console.console.print") as mock_print:
        view.render(tasks=[], sync_status=auth_status, clear=False)
        output = " ".join(str(c) for c in mock_print.call_args_list)

        # Status line is absent
        assert "● Tasks Connected" not in output
        assert "● Connected" not in output
        # Shortcut hint is present
        assert "Google (Auth)" in output


def test_timeline_view_footer_clean_without_sync_text(tui_footer_env):
    """Verify TimelineView footer contains no sync status text."""
    daily_svc = tui_footer_env["daily_svc"]
    task_svc = tui_footer_env["task_svc"]
    sync_mgr = tui_footer_env["sync_mgr"]

    view = TimelineView(daily_svc, task_svc)
    view.sync_manager = sync_mgr

    with patch("todo.ui.timeline_view.key_reader.read_key", return_value="b"), \
         patch("todo.ui.console.console.print") as mock_print:
        view.run(initial_date="2026-10-22")
        output = " ".join(str(c) for c in mock_print.call_args_list)

        assert "Connected" not in output
        assert "Synced" not in output
        assert "22:54" not in output
        assert "Day" in output
        assert "Week" in output


def test_universal_view_footer_clean_without_sync_text(tui_footer_env):
    """Verify UniversalView footer contains no sync status text."""
    daily_svc = tui_footer_env["daily_svc"]
    task_svc = tui_footer_env["task_svc"]
    sync_mgr = tui_footer_env["sync_mgr"]

    view = UniversalView(task_svc, daily_svc)
    view.sync_manager = sync_mgr

    with patch("todo.ui.universal_view.key_reader.read_key", return_value="b"), \
         patch("todo.ui.console.console.print") as mock_print:
        view.run()
        output = " ".join(str(c) for c in mock_print.call_args_list)

        assert "Connected" not in output
        assert "Synced" not in output
        assert "22:54" not in output
        assert "Add universal task" in output or "Add task" in output


def test_sync_manager_status_reporting_preserved(tui_footer_env):
    """Verify that sync manager's underlying status tracking and reporting remain 100% functional."""
    sync_mgr: GoogleTasksSyncManager = tui_footer_env["sync_mgr"]
    status_text = sync_mgr.get_status_text()

    assert "Connected" in status_text
    assert "Synced" in status_text
    assert "22:54" in status_text
