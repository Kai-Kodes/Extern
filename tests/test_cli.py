"""Tests for CLI commands and UI view rendering."""

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner
import pytest

from todo.cli import cli
from todo.database import Database
from todo.models import DailyTask, Task
from todo.services.daily_service import DailyService
from todo.services.source_service import SourceService
from todo.services.task_service import TaskService
from todo.ui.console import console
from todo.ui.today_view import TodayView


@pytest.fixture
def isolated_env():
    runner = CliRunner()
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "cli_test.db"
        with patch.dict(os.environ, {"TODO_DB_PATH": str(db_path)}):
            yield runner, db_path


def test_cli_add_and_today(isolated_env):
    runner, _ = isolated_env

    # 1. Initially today is empty
    result = runner.invoke(cli, ["today"])
    assert result.exit_code == 0
    assert "No tasks for today." in result.output

    # 2. Add task to today
    result = runner.invoke(cli, ["add", "Finish Copywriting Lesson 03"])
    assert result.exit_code == 0
    assert "Added \"Finish Copywriting Lesson 03\" to today's tasks." in result.output

    # 3. View today
    result = runner.invoke(cli, ["today"])
    assert result.exit_code == 0
    assert "Finish Copywriting Lesson 03" in result.output
    assert "○" in result.output


def test_cli_universal_add_and_list(isolated_env):
    runner, _ = isolated_env

    # 1. Add universal task
    result = runner.invoke(cli, ["add", "Learn React", "--universal"])
    assert result.exit_code == 0
    assert "Added \"Learn React\" to universal task list." in result.output

    # 2. List universal tasks
    result = runner.invoke(cli, ["list"])
    assert result.exit_code == 0
    assert "Learn React" in result.output


def test_today_view_empty_state_rendering():
    """Verify empty-state view does not crash and renders clean text."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Database(Path(tmpdir) / "test.db")
        daily_svc = DailyService(db)
        view = TodayView(daily_svc)

        # Capturing output
        with console.capture() as capture:
            view.render([])
        output = capture.get()

        assert "TODAY" in output
        assert "No tasks for today." in output
        assert "[+] Add task" in output
        assert "[q] Quit" in output


def test_today_view_with_tasks_rendering():
    """Verify task list view renders completed and pending icons."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Database(Path(tmpdir) / "test.db")
        daily_svc = DailyService(db)
        view = TodayView(daily_svc)

        t1 = Task(id=1, title="Lesson 1", status="completed")
        t2 = Task(id=2, title="Lesson 2", status="pending")

        tasks = [
            DailyTask(id=1, task_id=1, date="2026-10-08", completed_at="2026-10-08T10:00:00", task=t1),
            DailyTask(id=2, task_id=2, date="2026-10-08", completed_at=None, task=t2),
        ]

        with console.capture() as capture:
            view.render(tasks)
        output = capture.get()

        assert "Lesson 1" in output
        assert "Lesson 2" in output
        assert "✓" in output
        assert "○" in output
