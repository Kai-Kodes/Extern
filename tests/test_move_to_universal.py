"""Tests for moving Today tasks to Universal Tasks (single and bulk)."""

import io
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from todo.config import Config
from todo.database import Database
from todo.services.daily_service import DailyService
from todo.services.source_service import SourceService
from todo.services.task_service import TaskService
from todo.ui.app import TodoApp


class SimulatedClock:
    def __init__(self, start_dt: datetime):
        self.current = start_dt

    def __call__(self) -> datetime:
        return self.current

    def advance_days(self, days: int = 1):
        self.current += timedelta(days=days)


@pytest.fixture
def test_env():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        clock = SimulatedClock(datetime(2026, 10, 1, 10, 0, 0))
        cfg = Config(db_path=db_path)
        app = TodoApp(config=cfg, date_provider=clock)
        yield {
            "tmpdir": tmpdir,
            "db_path": db_path,
            "clock": clock,
            "cfg": cfg,
            "app": app,
            "db": app.db,
            "daily_svc": app.daily_service,
            "task_svc": app.task_service,
            "source_svc": app.source_service,
        }


def test_move_single_task_to_universal(test_env):
    """Moving a highlighted task to Universal Tasks preserves metadata and removes it from Today."""
    daily_svc = test_env["daily_svc"]
    task_svc = test_env["task_svc"]
    today_str = daily_svc.get_today_str()

    t = task_svc.create_task(
        title="Analyze Q3 Performance",
        description="Check metric dashboards",
        source_type="manual",
    )
    dt, _ = daily_svc.add_task_to_date(t.id, today_str)

    assert len(daily_svc.get_active_tasks_for_date(today_str)) == 1
    assert len(task_svc.get_universal_tasks()) == 0

    # Move to Universal
    success = daily_svc.move_task_to_universal(dt.id)
    assert success is True

    # Immediately removed from Today
    assert len(daily_svc.get_active_tasks_for_date(today_str)) == 0

    # Appears in Universal Tasks
    u_tasks = task_svc.get_universal_tasks()
    assert len(u_tasks) == 1
    u = u_tasks[0]
    assert u.id == t.id
    assert u.title == "Analyze Q3 Performance"
    assert u.description == "Check metric dashboards"
    assert u.source_type == "universal"
    assert u.status == "pending"


def test_move_bulk_tasks_to_universal(test_env):
    """Moving multiple selected tasks to Universal Tasks transfers all selected and leaves unselected tasks."""
    daily_svc = test_env["daily_svc"]
    task_svc = test_env["task_svc"]
    today_str = daily_svc.get_today_str()

    t1 = task_svc.create_task("Task 1", source_type="manual")
    t2 = task_svc.create_task("Task 2", source_type="manual")
    t3 = task_svc.create_task("Task 3", source_type="manual")

    dt1, _ = daily_svc.add_task_to_date(t1.id, today_str)
    dt2, _ = daily_svc.add_task_to_date(t2.id, today_str)
    dt3, _ = daily_svc.add_task_to_date(t3.id, today_str)

    assert len(daily_svc.get_active_tasks_for_date(today_str)) == 3

    # Move dt1 and dt3
    moved_count = daily_svc.move_tasks_to_universal_bulk([dt1.id, dt3.id])
    assert moved_count == 2

    # Today now only has dt2
    remaining_today = daily_svc.get_active_tasks_for_date(today_str)
    assert len(remaining_today) == 1
    assert remaining_today[0].task_id == t2.id

    # Universal has t1 and t3
    u_tasks = task_svc.get_universal_tasks()
    assert len(u_tasks) == 2
    u_ids = {u.id for u in u_tasks}
    assert u_ids == {t1.id, t3.id}


def test_moved_task_does_not_carry_forward_to_next_day(test_env):
    """A task moved to Universal is deactivated from daily scheduling and does not reappear tomorrow."""
    daily_svc = test_env["daily_svc"]
    task_svc = test_env["task_svc"]
    clock = test_env["clock"]

    t = task_svc.create_task("Review Architecture", source_type="manual")
    dt, _ = daily_svc.add_task_to_date(t.id, "2026-10-01")

    # Move to Universal
    daily_svc.move_task_to_universal(dt.id)

    # Advance clock to Day 2 and Day 3
    clock.advance_days(1)
    day2 = daily_svc.get_today_str()
    assert len(daily_svc.get_active_tasks_for_date(day2)) == 0

    clock.advance_days(1)
    day3 = daily_svc.get_today_str()
    assert len(daily_svc.get_active_tasks_for_date(day3)) == 0

    # Remains accessible in Universal Tasks
    assert len(task_svc.get_universal_tasks()) == 1


def test_explicitly_rescheduling_moved_task_back_to_today(test_env):
    """A moved task can be rescheduled back into Today from Universal Tasks."""
    daily_svc = test_env["daily_svc"]
    task_svc = test_env["task_svc"]
    clock = test_env["clock"]

    t = task_svc.create_task("Draft Roadmap", source_type="manual")
    dt, _ = daily_svc.add_task_to_date(t.id, "2026-10-01")

    daily_svc.move_task_to_universal(dt.id)

    # Advance to Day 2
    clock.advance_days(1)
    day2 = daily_svc.get_today_str()

    # Reschedule from Universal back into Today on Day 2
    new_dt, is_new = daily_svc.add_task_to_date(t.id, day2)
    assert is_new is True

    active_day2 = daily_svc.get_active_tasks_for_date(day2)
    assert len(active_day2) == 1
    assert active_day2[0].task_id == t.id


def test_interactive_tui_move_shortcut_single_and_bulk(test_env):
    """Test pressing 'm' in interactive Today view moves single and bulk-selected tasks."""
    app = test_env["app"]
    daily_svc = test_env["daily_svc"]
    task_svc = test_env["task_svc"]
    today_str = daily_svc.get_today_str()

    t1 = task_svc.create_task("Interactive Task 1", source_type="manual")
    t2 = task_svc.create_task("Interactive Task 2", source_type="manual")
    t3 = task_svc.create_task("Interactive Task 3", source_type="manual")

    daily_svc.add_task_to_date(t1.id, today_str)
    daily_svc.add_task_to_date(t2.id, today_str)
    daily_svc.add_task_to_date(t3.id, today_str)

    # Enter move mode with 'm', select Task 1 with Space, confirm move with Enter, then quit
    inputs1 = ["m\n", "space\n", "enter\n", "q\n"]
    with patch("sys.stdin", io.StringIO("".join(inputs1))):
        app.run()

    active_after1 = daily_svc.get_active_tasks_for_date(today_str)
    assert len(active_after1) == 2
    assert "Interactive Task 1" not in [dt.task.title for dt in active_after1]
    assert task_svc.get_task_by_id(t1.id).source_type == "universal"

    # Now enter move mode with 'm', bulk select remaining 2 tasks and press Enter
    # m -> Space (select Task 2) -> down -> Space (select Task 3) -> enter -> q
    inputs2 = ["m\n", "space\n", "down\n", "space\n", "enter\n", "q\n"]
    with patch("sys.stdin", io.StringIO("".join(inputs2))):
        app.run()

    active_after2 = daily_svc.get_active_tasks_for_date(today_str)
    assert len(active_after2) == 0
    assert len(task_svc.get_universal_tasks()) == 3
