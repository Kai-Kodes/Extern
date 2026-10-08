"""Tests for full-screen TUI behavior, alternate screen, and navigation."""

import io
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from todo.config import Config
from todo.database import Database
from todo.models import DailyTask, Task
from todo.services.daily_service import DailyService
from todo.services.source_service import SourceService
from todo.services.task_service import TaskService
from todo.ui.app import TodoApp
from todo.ui.console import _IN_ALT_SCREEN, alt_screen, enter_alt_screen, exit_alt_screen


def test_alt_screen_lifecycle():
    """Verify alternate screen buffer enters and exits cleanly."""
    with patch("sys.stdout.isatty", return_value=True):
        with patch("todo.ui.console.console.set_alt_screen") as mock_set:
            with patch("todo.ui.console.console.show_cursor") as mock_cursor:
                with alt_screen():
                    mock_set.assert_called_with(True)
                    mock_cursor.assert_called_with(False)
                mock_set.assert_called_with(False)
                mock_cursor.assert_called_with(True)


def test_alt_screen_restores_on_exception():
    """Verify alternate screen restores normal terminal state even when exception is raised."""
    with patch("sys.stdout.isatty", return_value=True):
        with patch("todo.ui.console.console.set_alt_screen") as mock_set:
            with pytest.raises(RuntimeError):
                with alt_screen():
                    raise RuntimeError("Unexpected failure inside TUI")
            mock_set.assert_called_with(False)


def test_navigation_and_back():
    """Test navigating to Add Task screen and pressing 'b' to return to Today."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        cfg = Config(db_path=db_path)

        # Inputs: "+" (open Add Task), "b" (go back), "q" (quit)
        inputs = ["+\n", "b\n", "q\n"]
        with patch("sys.stdin", io.StringIO("".join(inputs))):
            app = TodoApp(cfg)
            app.run()


def test_today_delete_universal_semantics():
    """Test delete flow for universal task from Today UI."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        cfg = Config(db_path=db_path)
        app = TodoApp(cfg)

        # 1. Create a universal task and schedule for today
        u_task = app.task_service.create_task("Study Algorithms", source_type="universal")
        from datetime import datetime
        today_str = datetime.now().date().isoformat()
        dt, _ = app.daily_service.add_task_to_date(u_task.id, today_str)

        # 2. In TUI: press "d" (delete), "1" (select task 1), "1" (Remove from Today only), "q" (quit)
        inputs = ["d\n", "1\n", "1\n", "q\n"]
        with patch("sys.stdin", io.StringIO("".join(inputs))):
            app.run()

        # Task removed from today, but still in universal list
        assert len(app.daily_service.get_tasks_for_date(today_str)) == 0
        assert app.task_service.get_task_by_id(u_task.id) is not None

        # 3. Re-add to today, then permanently delete via option 2
        app.daily_service.add_task_to_date(u_task.id, today_str)
        # "d", "1", "2" (Permanently delete), "y" (confirm), "q"
        inputs_perm = ["d\n", "1\n", "2\n", "y\n", "q\n"]
        with patch("sys.stdin", io.StringIO("".join(inputs_perm))):
            app.run()

        assert len(app.daily_service.get_tasks_for_date(today_str)) == 0
        assert app.task_service.get_task_by_id(u_task.id) is None


def test_delete_source_ui_confirmation():
    """Test Filemeta source deletion through UI with confirmation."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        cfg = Config(db_path=db_path)
        app = TodoApp(cfg)

        from todo.integrations.filemeta import FilemetaScanResult, ScannedFile
        scan = FilemetaScanResult(
            source_name="Sample Course",
            source_path="/tmp/sample",
            items=[ScannedFile(name="1.mp4", path="/tmp/sample/1.mp4", title="1", type="video")],
        )
        app.source_service.add_or_update_source(scan)
        assert len(app.source_service.get_sources()) == 1

        # Open Filemeta ("+", "1"), press "d" (delete), "1" (select source 1), "y" (confirm), "b", "q"
        inputs = ["+\n", "1\n", "d\n", "1\n", "y\n", "b\n", "q\n"]
        with patch("sys.stdin", io.StringIO("".join(inputs))):
            app.run()

        assert len(app.source_service.get_sources()) == 0
