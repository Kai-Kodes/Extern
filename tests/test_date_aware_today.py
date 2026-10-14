"""Tests for date-aware Today view, completed task visibility until midnight, and carry-forward."""

import io
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from todo.config import Config
from todo.database import Database
from todo.integrations.filemeta import FilemetaScanResult, ScannedFile
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

    def set(self, dt: datetime):
        self.current = dt


@pytest.fixture
def sim_env():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        clock = SimulatedClock(datetime(2026, 10, 1, 9, 0, 0))
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


def test_completed_task_remains_visible_on_same_date_with_completed_styling(sim_env):
    """When a task is completed, it remains visible in Today for the remainder of that calendar day."""
    daily_svc = sim_env["daily_svc"]
    task_svc = sim_env["task_svc"]
    clock = sim_env["clock"]
    today_str = daily_svc.get_today_str()

    t1 = task_svc.create_task("Finish project report", source_type="manual")
    dt1, _ = daily_svc.add_task_to_date(t1.id, today_str)

    # Active before completion
    active_before = daily_svc.get_active_tasks_for_date(today_str)
    assert len(active_before) == 1
    assert active_before[0].id == dt1.id
    assert active_before[0].is_completed is False

    # Toggle completion
    completed_dt = daily_svc.toggle_task_completion(dt1.id)
    assert completed_dt is not None
    assert completed_dt.completed_at is not None

    # REMAINS VISIBLE in active Today on the same date!
    active_after = daily_svc.get_active_tasks_for_date(today_str)
    assert len(active_after) == 1
    assert active_after[0].id == dt1.id
    assert active_after[0].is_completed is True
    assert active_after[0].task.status == "completed"

    # Persists in SQLite and task history
    raw_task = task_svc.get_task_by_id(t1.id)
    assert raw_task is not None
    assert raw_task.status == "completed"

    # When date advances to tomorrow, the completed task disappears from Today
    clock.advance_days(1)
    tomorrow_str = daily_svc.get_today_str()
    active_tomorrow = daily_svc.get_active_tasks_for_date(tomorrow_str)
    assert len(active_tomorrow) == 0


def test_date_advancement_hides_past_completed_tasks_and_carries_unfinished_tasks_forward(sim_env):
    """Advancing calendar date hides past completed tasks; unfinished tasks carry forward cleanly without duplicates."""
    daily_svc = sim_env["daily_svc"]
    task_svc = sim_env["task_svc"]
    clock = sim_env["clock"]

    day1 = "2026-10-01"
    t1 = task_svc.create_task("Completed Task on Day 1", source_type="manual")
    t2 = task_svc.create_task("Unfinished Task on Day 1", source_type="manual")

    dt1, _ = daily_svc.add_task_to_date(t1.id, day1)
    dt2, _ = daily_svc.add_task_to_date(t2.id, day1)

    # Complete T1 on Day 1
    daily_svc.toggle_task_completion(dt1.id)

    # Day 1 active: BOTH T1 (completed today) and T2 (pending) are visible!
    day1_active = daily_svc.get_active_tasks_for_date(day1)
    assert len(day1_active) == 2
    assert day1_active[0].task_id == t1.id and day1_active[0].is_completed is True
    assert day1_active[1].task_id == t2.id and day1_active[1].is_completed is False

    # Advance clock to Day 2
    clock.advance_days(1)
    day2 = daily_svc.get_today_str()
    assert day2 == "2026-10-02"

    # Day 2 active: T1 is hidden (completed on Day 1), T2 carries forward!
    active_day2 = daily_svc.get_active_tasks_for_date(day2)
    assert len(active_day2) == 1
    assert active_day2[0].task_id == t2.id
    assert active_day2[0].is_completed is False

    # Advance clock to Day 3
    clock.advance_days(1)
    day3 = daily_svc.get_today_str()
    assert day3 == "2026-10-03"

    # Day 3 active: still carries T2 forward
    active_day3 = daily_svc.get_active_tasks_for_date(day3)
    assert len(active_day3) == 1
    assert active_day3[0].task_id == t2.id
    assert active_day3[0].is_completed is False

    # Verify NO duplicate daily_tasks rows were created
    with sim_env["db"].get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) AS cnt FROM daily_tasks WHERE task_id = ?", (t2.id,))
        count = cursor.fetchone()["cnt"]
        assert count == 1


def test_completing_carried_forward_task_remains_visible_on_completion_day_and_hidden_next_day(sim_env):
    """Completing a carried-forward task keeps it visible on the completion date and hides it the next day."""
    daily_svc = sim_env["daily_svc"]
    task_svc = sim_env["task_svc"]
    clock = sim_env["clock"]

    # Day 1
    t1 = task_svc.create_task("Carried task", source_type="manual")
    dt1, _ = daily_svc.add_task_to_date(t1.id, "2026-10-01")

    # Advance to Day 3
    clock.advance_days(2)
    day3 = daily_svc.get_today_str()
    assert day3 == "2026-10-03"

    active_day3_before = daily_svc.get_active_tasks_for_date(day3)
    assert len(active_day3_before) == 1
    carried_dt = active_day3_before[0]
    assert carried_dt.is_completed is False

    # Complete it on Day 3
    completed_dt = daily_svc.toggle_task_completion(carried_dt.id)
    assert completed_dt.completed_at.startswith("2026-10-03")

    # Remains visible in active Today on Day 3 as completed!
    active_day3_after = daily_svc.get_active_tasks_for_date(day3)
    assert len(active_day3_after) == 1
    assert active_day3_after[0].is_completed is True

    # Day 3 progress reflects it
    comp_cnt, tot_cnt, pct = daily_svc.get_progress_for_date(day3)
    assert comp_cnt == 1
    assert tot_cnt == 1
    assert pct == 100

    # Advance to Day 4: task is now hidden, Day 4 progress is 0/0
    clock.advance_days(1)
    day4 = daily_svc.get_today_str()
    assert len(daily_svc.get_active_tasks_for_date(day4)) == 0
    comp_cnt4, tot_cnt4, pct4 = daily_svc.get_progress_for_date(day4)
    assert comp_cnt4 == 0
    assert tot_cnt4 == 0
    assert pct4 == 0


def test_progress_header_calculation_does_not_include_past_completed_tasks(sim_env):
    """Progress header counts only active tasks (pending + completed today). Excludes past completed tasks."""
    daily_svc = sim_env["daily_svc"]
    task_svc = sim_env["task_svc"]
    clock = sim_env["clock"]

    # Day 1: Create 2 tasks, complete 1
    t1 = task_svc.create_task("Task 1", source_type="manual")
    t2 = task_svc.create_task("Task 2", source_type="manual")
    dt1, _ = daily_svc.add_task_to_date(t1.id, "2026-10-01")
    dt2, _ = daily_svc.add_task_to_date(t2.id, "2026-10-01")
    daily_svc.toggle_task_completion(dt1.id)

    comp1, tot1, pct1 = daily_svc.get_progress_for_date("2026-10-01")
    assert comp1 == 1
    assert tot1 == 2
    assert pct1 == 50

    # Advance to Day 2:
    # T2 carries forward. Add new task T3 on Day 2.
    clock.advance_days(1)
    day2 = daily_svc.get_today_str()
    t3 = task_svc.create_task("Task 3", source_type="manual")
    daily_svc.add_task_to_date(t3.id, day2)

    # Prior to completing any tasks on Day 2:
    comp2, tot2, pct2 = daily_svc.get_progress_for_date(day2)
    assert comp2 == 0
    assert tot2 == 2  # T2 + T3
    assert pct2 == 0

    # Complete T3 on Day 2:
    active = daily_svc.get_active_tasks_for_date(day2)
    dt3 = next(t for t in active if t.task_id == t3.id)
    daily_svc.toggle_task_completion(dt3.id)

    comp2_after, tot2_after, pct2_after = daily_svc.get_progress_for_date(day2)
    assert comp2_after == 1
    assert tot2_after == 2
    assert pct2_after == 50


def test_no_duplicate_daily_task_when_explicitly_readding_carried_forward_task(sim_env):
    """Adding a task to today when it's already carried forward returns existing record without duplicates."""
    daily_svc = sim_env["daily_svc"]
    task_svc = sim_env["task_svc"]
    clock = sim_env["clock"]

    t1 = task_svc.create_task("Task 1", source_type="manual")
    dt1, is_new1 = daily_svc.add_task_to_date(t1.id, "2026-10-01")
    assert is_new1 is True

    # Advance to Day 2
    clock.advance_days(1)
    day2 = daily_svc.get_today_str()

    # Attempt to re-add T1 to Day 2
    dt_readd, is_new2 = daily_svc.add_task_to_date(t1.id, day2)
    assert is_new2 is False
    assert dt_readd.id == dt1.id

    with sim_env["db"].get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) AS cnt FROM daily_tasks WHERE task_id = ?", (t1.id,))
        assert cursor.fetchone()["cnt"] == 1


def test_two_way_completion_sync_with_imported_sources_across_dates(sim_env):
    """Imported item completion state syncs two-way with Today tasks across date boundaries."""
    daily_svc = sim_env["daily_svc"]
    source_svc = sim_env["source_svc"]
    task_svc = sim_env["task_svc"]
    clock = sim_env["clock"]

    scan = FilemetaScanResult(
        source_name="Tutorials",
        source_path="/tutorials",
        items=[
            ScannedFile(name="lesson1.mp4", path="/tutorials/lesson1.mp4", title="Lesson 1", type="video"),
        ],
    )
    src, _, _ = source_svc.add_or_update_source(scan)
    item = source_svc.get_items_by_source(src.id)[0]

    # Create task and schedule for Day 1
    task = task_svc.get_or_create_filemeta_task(item, source_name="Tutorials")
    dt, _ = daily_svc.add_task_to_date(task.id, "2026-10-01")

    # Advance to Day 2
    clock.advance_days(1)
    day2 = daily_svc.get_today_str()

    # Complete in Today on Day 2
    daily_svc.toggle_task_completion(dt.id)

    # Item in source_service is marked completed
    reloaded_item = source_svc.get_item_by_id(item.id)
    assert reloaded_item.completed == 1

    # Task remains visible in Today on Day 2 as completed
    active_day2 = daily_svc.get_active_tasks_for_date(day2)
    assert len(active_day2) == 1
    assert active_day2[0].is_completed is True

    # Toggle uncompleted via SourceService
    source_svc.toggle_item_completion(item.id)
    reloaded_task = task_svc.get_task_by_id(task.id)
    assert reloaded_task.status == "pending"

    # Reappears as pending in active Today on Day 2
    active_after_uncomp = daily_svc.get_active_tasks_for_date(day2)
    assert len(active_after_uncomp) == 1
    assert active_after_uncomp[0].is_completed is False


def test_tasks_moved_to_universal_remain_absent_from_today(sim_env):
    """Tasks moved to Universal Tasks remain absent from Today, both today and tomorrow."""
    daily_svc = sim_env["daily_svc"]
    task_svc = sim_env["task_svc"]
    clock = sim_env["clock"]
    today_str = daily_svc.get_today_str()

    t = task_svc.create_task("Roadmap item", source_type="manual")
    dt, _ = daily_svc.add_task_to_date(t.id, today_str)

    # Move to Universal
    daily_svc.move_task_to_universal(dt.id)

    # Absent from Today today
    assert len(daily_svc.get_active_tasks_for_date(today_str)) == 0

    # Advance clock to tomorrow
    clock.advance_days(1)
    tomorrow_str = daily_svc.get_today_str()
    assert len(daily_svc.get_active_tasks_for_date(tomorrow_str)) == 0

    # Still accessible in Universal Tasks
    u_tasks = task_svc.get_universal_tasks()
    assert len(u_tasks) == 1
    assert u_tasks[0].id == t.id


def test_restarting_app_preserves_state_and_date_behavior(sim_env):
    """Closing and reopening TodoApp preserves tasks, date awareness, and carry-forward state."""
    db_path = sim_env["db_path"]
    clock = sim_env["clock"]
    daily_svc = sim_env["daily_svc"]
    task_svc = sim_env["task_svc"]

    # Session 1: Day 1
    t1 = task_svc.create_task("T1 Completed", source_type="manual")
    t2 = task_svc.create_task("T2 Pending", source_type="manual")
    dt1, _ = daily_svc.add_task_to_date(t1.id, "2026-10-01")
    dt2, _ = daily_svc.add_task_to_date(t2.id, "2026-10-01")
    daily_svc.toggle_task_completion(dt1.id)

    # Advance clock to Day 2
    clock.advance_days(1)

    # Session 2: Create new TodoApp instance simulating restart
    new_cfg = Config(db_path=db_path)
    new_app = TodoApp(config=new_cfg, date_provider=clock)

    today_str = new_app.daily_service.get_today_str()
    assert today_str == "2026-10-02"

    active_tasks = new_app.daily_service.get_active_tasks_for_date(today_str)
    assert len(active_tasks) == 1
    assert active_tasks[0].task_id == t2.id
    assert active_tasks[0].task.title == "T2 Pending"
    assert active_tasks[0].is_completed is False

    # Progress on Day 2 after restart
    comp, tot, pct = new_app.daily_service.get_progress_for_date(today_str)
    assert comp == 0
    assert tot == 1
    assert pct == 0
