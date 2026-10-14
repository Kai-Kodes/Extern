"""Comprehensive tests for Timeline, Historical Completion, and Future Task Scheduling."""

import calendar
import io
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from todo.config import Config
from todo.database import Database
from todo.models import DailyTask
from todo.services.daily_service import DailyService
from todo.services.task_service import TaskService
from todo.ui.app import TodoApp
from todo.ui.key_reader import Key
from todo.ui.timeline_view import TimelineView


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
def timeline_env():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        clock = SimulatedClock(datetime(2026, 10, 10, 10, 0, 0))
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
            "timeline_view": app.timeline_view,
        }


# ==============================================================================
# 1. Historical Completions (Past Dates)
# ==============================================================================

def test_historical_completion_uses_completion_timestamp_not_scheduled_date(timeline_env):
    """
    CRITICAL REQUIREMENT:
    Tasks appear on the timeline date when they were ACTUALLY completed,
    NOT on the date they were originally scheduled.
    If scheduled on Oct 1 and completed on Oct 3, it must appear on Oct 3, NOT on Oct 1.
    """
    daily_svc = timeline_env["daily_svc"]
    task_svc = timeline_env["task_svc"]
    clock = timeline_env["clock"]

    # Start on Oct 1
    clock.set(datetime(2026, 10, 1, 9, 0, 0))
    t1 = task_svc.create_task("Finish Presentation Deck", source_type="manual")
    dt1, _ = daily_svc.add_task_to_date(t1.id, "2026-10-01")

    # Day 1: Task is pending on Oct 1
    oct1_data = daily_svc.get_timeline_data_for_date("2026-10-01")
    assert oct1_data["domain"] == "today"
    assert len(oct1_data["tasks"]) == 1
    assert oct1_data["tasks"][0].is_completed is False

    # Advance to Day 3 (Oct 3) without completing on Day 1 or Day 2
    clock.set(datetime(2026, 10, 3, 14, 30, 0))
    # It carries forward into Today on Oct 3
    active_oct3 = daily_svc.get_active_tasks_for_date("2026-10-03")
    assert len(active_oct3) == 1

    # Now complete the task on Oct 3 at 14:30:00
    completed_dt = daily_svc.toggle_task_completion(active_oct3[0].id)
    assert completed_dt.is_completed is True
    assert completed_dt.completed_at.startswith("2026-10-03T14:30:00")

    # Advance clock to Day 5 (Oct 5)
    clock.set(datetime(2026, 10, 5, 10, 0, 0))

    # Inspect Oct 1 on the timeline (a PAST date):
    # The task was NOT completed on Oct 1, so it MUST NOT appear as completed on Oct 1!
    past_oct1 = daily_svc.get_timeline_data_for_date("2026-10-01")
    assert past_oct1["domain"] == "past"
    assert len(past_oct1["tasks"]) == 0
    assert past_oct1["completed_count"] == 0

    # Inspect Oct 2 on the timeline (a PAST date):
    past_oct2 = daily_svc.get_timeline_data_for_date("2026-10-02")
    assert past_oct2["domain"] == "past"
    assert len(past_oct2["tasks"]) == 0

    # Inspect Oct 3 on the timeline (a PAST date):
    # The task WAS completed on Oct 3, so it MUST appear on Oct 3!
    past_oct3 = daily_svc.get_timeline_data_for_date("2026-10-03")
    assert past_oct3["domain"] == "past"
    assert len(past_oct3["tasks"]) == 1
    assert past_oct3["tasks"][0].task_id == t1.id
    assert past_oct3["tasks"][0].is_completed is True
    assert past_oct3["completed_count"] == 1


def test_multiple_historical_completions_ordered_by_completion_time(timeline_env):
    """Multiple tasks completed on the same date appear in chronological order of completion."""
    daily_svc = timeline_env["daily_svc"]
    task_svc = timeline_env["task_svc"]
    clock = timeline_env["clock"]

    clock.set(datetime(2026, 10, 10, 9, 0, 0))
    t1 = task_svc.create_task("Morning Task", source_type="manual")
    t2 = task_svc.create_task("Noon Task", source_type="manual")
    t3 = task_svc.create_task("Evening Task", source_type="manual")

    dt1, _ = daily_svc.add_task_to_date(t1.id, "2026-10-10")
    dt2, _ = daily_svc.add_task_to_date(t2.id, "2026-10-10")
    dt3, _ = daily_svc.add_task_to_date(t3.id, "2026-10-10")

    # Complete t2 first at 10:00
    clock.set(datetime(2026, 10, 10, 10, 0, 0))
    daily_svc.toggle_task_completion(dt2.id)

    # Complete t1 second at 12:00
    clock.set(datetime(2026, 10, 10, 12, 0, 0))
    daily_svc.toggle_task_completion(dt1.id)

    # Complete t3 third at 18:00
    clock.set(datetime(2026, 10, 10, 18, 0, 0))
    daily_svc.toggle_task_completion(dt3.id)

    # Advance to tomorrow (Oct 11) so Oct 10 is in the past
    clock.set(datetime(2026, 10, 11, 8, 0, 0))

    hist_data = daily_svc.get_timeline_data_for_date("2026-10-10")
    assert hist_data["domain"] == "past"
    assert hist_data["completed_count"] == 3
    tasks = hist_data["tasks"]
    assert [t.task.title for t in tasks] == ["Noon Task", "Morning Task", "Evening Task"]


def test_past_date_with_zero_completions_returns_empty_list(timeline_env):
    """Browsing to a past date with no completions returns empty tasks list cleanly."""
    daily_svc = timeline_env["daily_svc"]
    data = daily_svc.get_timeline_data_for_date("2025-01-01")
    assert data["domain"] == "past"
    assert data["tasks"] == []
    assert data["completed_count"] == 0
    assert data["progress_pct"] == 0


def test_inspect_past_task_details_does_not_mutate_state(timeline_env):
    """Inspecting a historical task via details modal preserves its completed status and timestamp."""
    daily_svc = timeline_env["daily_svc"]
    task_svc = timeline_env["task_svc"]
    clock = timeline_env["clock"]
    timeline_view = timeline_env["timeline_view"]

    clock.set(datetime(2026, 10, 10, 9, 0, 0))
    t = task_svc.create_task("Review Q3 Earnings", description="Compare forecast with actuals")
    dt, _ = daily_svc.add_task_to_date(t.id, "2026-10-10")
    daily_svc.toggle_task_completion(dt.id)

    # Move to next day
    clock.set(datetime(2026, 10, 11, 9, 0, 0))
    data = daily_svc.get_timeline_data_for_date("2026-10-10")
    hist_dt = data["tasks"][0]
    orig_completed_at = hist_dt.completed_at

    # Simulate opening task details modal and dismissing with Enter
    with patch("sys.stdin", io.StringIO("enter\n")):
        timeline_view._show_task_details(hist_dt)

    # Verify no state was modified
    refreshed_data = daily_svc.get_timeline_data_for_date("2026-10-10")
    assert refreshed_data["tasks"][0].is_completed is True
    assert refreshed_data["tasks"][0].completed_at == orig_completed_at
    assert task_svc.get_task_by_id(t.id).status == "completed"


# ==============================================================================
# 2. Today View Parity
# ==============================================================================

def test_today_timeline_matches_active_today_view(timeline_env):
    """Timeline for today's date matches Today view active tasks, counts, and progress percentage."""
    daily_svc = timeline_env["daily_svc"]
    task_svc = timeline_env["task_svc"]
    today_str = daily_svc.get_today_str()

    t1 = task_svc.create_task("Task 1")
    t2 = task_svc.create_task("Task 2")
    t3 = task_svc.create_task("Task 3")

    daily_svc.add_task_to_date(t1.id, today_str)
    dt2, _ = daily_svc.add_task_to_date(t2.id, today_str)
    daily_svc.add_task_to_date(t3.id, today_str)

    daily_svc.toggle_task_completion(dt2.id)

    # Compare get_active_tasks_for_date with get_timeline_data_for_date
    active_today = daily_svc.get_active_tasks_for_date(today_str)
    timeline_today = daily_svc.get_timeline_data_for_date(today_str)

    assert timeline_today["domain"] == "today"
    assert len(timeline_today["tasks"]) == len(active_today)
    assert timeline_today["completed_count"] == 1
    assert timeline_today["total_count"] == 3
    assert timeline_today["progress_pct"] == 33


# ==============================================================================
# 3. Future Task Scheduling & Emergence
# ==============================================================================

def test_future_scheduled_task_does_not_appear_in_today_until_date_arrives(timeline_env):
    """
    CRITICAL REQUIREMENT:
    Tasks scheduled for a future date do not appear in Today until that date arrives.
    When the date arrives, they appear automatically without background jobs or duplication.
    """
    daily_svc = timeline_env["daily_svc"]
    task_svc = timeline_env["task_svc"]
    clock = timeline_env["clock"]

    # Start on Oct 10
    clock.set(datetime(2026, 10, 10, 10, 0, 0))
    today_str = daily_svc.get_today_str()
    assert today_str == "2026-10-10"

    # Schedule a future task for Oct 15
    future_date = "2026-10-15"
    future_dt = daily_svc.create_task_for_date("Ship v2.0 Release", future_date)
    assert future_dt.date == future_date
    assert future_dt.is_completed is False

    # 1. On Oct 10: Task MUST NOT appear in active Today tasks
    active_today = daily_svc.get_active_tasks_for_date("2026-10-10")
    assert not any(dt.task_id == future_dt.task_id for dt in active_today)

    # 2. On Oct 10: Task MUST appear in Oct 15 Timeline
    oct15_timeline = daily_svc.get_timeline_data_for_date(future_date)
    assert oct15_timeline["domain"] == "future"
    assert len(oct15_timeline["tasks"]) == 1
    assert oct15_timeline["tasks"][0].task_id == future_dt.task_id

    # 3. Advance to Oct 12: Still must not appear in Today
    clock.set(datetime(2026, 10, 12, 10, 0, 0))
    active_oct12 = daily_svc.get_active_tasks_for_date("2026-10-12")
    assert not any(dt.task_id == future_dt.task_id for dt in active_oct12)

    # 4. Advance to Oct 15: Task AUTOMATICALLY appears in Today!
    clock.set(datetime(2026, 10, 15, 8, 0, 0))
    active_oct15 = daily_svc.get_active_tasks_for_date("2026-10-15")
    assert any(dt.task_id == future_dt.task_id for dt in active_oct15)
    matched_dt = next(dt for dt in active_oct15 if dt.task_id == future_dt.task_id)
    assert matched_dt.task.title == "Ship v2.0 Release"
    assert matched_dt.is_completed is False


def test_future_task_uncompleted_carries_forward_after_arrival(timeline_env):
    """Once a future task's date arrives, if not completed, it carries forward to subsequent days."""
    daily_svc = timeline_env["daily_svc"]
    clock = timeline_env["clock"]

    clock.set(datetime(2026, 10, 10, 10, 0, 0))
    future_dt = daily_svc.create_task_for_date("File Taxes", "2026-10-12")

    # Oct 12: Arrived in Today
    clock.set(datetime(2026, 10, 12, 10, 0, 0))
    assert len(daily_svc.get_active_tasks_for_date("2026-10-12")) == 1

    # Oct 13: Not finished, carries forward
    clock.set(datetime(2026, 10, 13, 10, 0, 0))
    active_oct13 = daily_svc.get_active_tasks_for_date("2026-10-13")
    assert len(active_oct13) == 1
    assert active_oct13[0].task_id == future_dt.task_id


def test_overdue_tasks_do_not_appear_on_future_dates(timeline_env):
    """Overdue tasks carry forward into Today, but do NOT populate future date views."""
    daily_svc = timeline_env["daily_svc"]
    task_svc = timeline_env["task_svc"]
    clock = timeline_env["clock"]

    # Start on Oct 10 and schedule an overdue task from Oct 08
    clock.set(datetime(2026, 10, 10, 10, 0, 0))
    t = task_svc.create_task("Overdue Maintenance")
    daily_svc.add_task_to_date(t.id, "2026-10-08")

    # Overdue task appears in Today on Oct 10
    active_today = daily_svc.get_active_tasks_for_date("2026-10-10")
    assert len(active_today) == 1

    # But inspecting Oct 20 (future date) must show 0 tasks
    future_data = daily_svc.get_timeline_data_for_date("2026-10-20")
    assert future_data["domain"] == "future"
    assert len(future_data["tasks"]) == 0


# ==============================================================================
# 4. Rescheduling Tasks
# ==============================================================================

def test_reschedule_task_maintains_exactly_one_active_pending_entry(timeline_env):
    """Rescheduling a pending task changes its scheduled date without creating duplicate active entries."""
    daily_svc = timeline_env["daily_svc"]
    task_svc = timeline_env["task_svc"]

    t = task_svc.create_task("Database Backup")
    dt1, _ = daily_svc.add_task_to_date(t.id, "2026-10-15")

    # Reschedule to Oct 20
    dt2 = daily_svc.reschedule_task(t.id, "2026-10-20")
    assert dt2.date == "2026-10-20"
    assert dt2.task_id == t.id

    # Verify Oct 15 has 0 tasks and Oct 20 has 1 task
    oct15 = daily_svc.get_timeline_data_for_date("2026-10-15")
    assert len(oct15["tasks"]) == 0

    oct20 = daily_svc.get_timeline_data_for_date("2026-10-20")
    assert len(oct20["tasks"]) == 1
    assert oct20["tasks"][0].task_id == t.id


def test_reschedule_daily_task_by_id(timeline_env):
    """reschedule_daily_task correctly resolves task_id and moves the task."""
    daily_svc = timeline_env["daily_svc"]
    dt = daily_svc.create_task_for_date("Deploy Security Patch", "2026-10-18")

    rescheduled = daily_svc.reschedule_daily_task(dt.id, "2026-10-25")
    assert rescheduled.date == "2026-10-25"
    assert rescheduled.task_id == dt.task_id

    oct18 = daily_svc.get_timeline_data_for_date("2026-10-18")
    assert len(oct18["tasks"]) == 0

    oct25 = daily_svc.get_timeline_data_for_date("2026-10-25")
    assert len(oct25["tasks"]) == 1


def test_cannot_reschedule_completed_task(timeline_env):
    """Attempting to reschedule an already-completed task raises ValueError."""
    daily_svc = timeline_env["daily_svc"]
    clock = timeline_env["clock"]

    clock.set(datetime(2026, 10, 10, 10, 0, 0))
    dt = daily_svc.create_task_for_date("Done Task", "2026-10-10")
    daily_svc.toggle_task_completion(dt.id)

    with pytest.raises(ValueError, match="Cannot reschedule completed task"):
        daily_svc.reschedule_task(dt.task_id, "2026-10-20")

    with pytest.raises(ValueError, match="Cannot reschedule a completed historical task record"):
        daily_svc.reschedule_daily_task(dt.id, "2026-10-20")


def test_move_future_task_to_universal(timeline_env):
    """Moving a future scheduled task to Universal Tasks removes it from the future date."""
    daily_svc = timeline_env["daily_svc"]
    task_svc = timeline_env["task_svc"]

    dt = daily_svc.create_task_for_date("Backlog Idea", "2026-10-25")
    assert len(daily_svc.get_timeline_data_for_date("2026-10-25")["tasks"]) == 1

    daily_svc.move_task_to_universal(dt.id)

    # Removed from future schedule
    assert len(daily_svc.get_timeline_data_for_date("2026-10-25")["tasks"]) == 0
    # Available in Universal
    univ_tasks = task_svc.get_universal_tasks()
    assert any(t.id == dt.task_id for t in univ_tasks)


def test_remove_future_daily_task(timeline_env):
    """Removing a future task from schedule leaves the underlying task intact but unscheduled."""
    daily_svc = timeline_env["daily_svc"]
    task_svc = timeline_env["task_svc"]

    dt = daily_svc.create_task_for_date("Optional Meeting", "2026-10-30")
    daily_svc.remove_daily_task(dt.id)

    assert len(daily_svc.get_timeline_data_for_date("2026-10-30")["tasks"]) == 0
    assert task_svc.get_task_by_id(dt.task_id) is not None


# ==============================================================================
# 5. Calendar Navigation & Month-End Clamping
# ==============================================================================

def test_month_navigation_boundary_clamping_non_leap_year(timeline_env):
    """
    Month navigation clamping for non-leap years:
    Jan 31 -> Feb 28
    March 31 -> Feb 28
    """
    view = timeline_env["timeline_view"]

    # Jan 31, 2026 + 1 month -> Feb 28, 2026
    d1 = date(2026, 1, 31)
    shifted_next = view._shift_month(d1, 1)
    assert shifted_next == date(2026, 2, 28)

    # March 31, 2026 - 1 month -> Feb 28, 2026
    d2 = date(2026, 3, 31)
    shifted_prev = view._shift_month(d2, -1)
    assert shifted_prev == date(2026, 2, 28)


def test_month_navigation_boundary_clamping_leap_year(timeline_env):
    """
    Month navigation clamping for leap years (e.g. 2024):
    Jan 31, 2024 + 1 month -> Feb 29, 2024
    """
    view = timeline_env["timeline_view"]

    d = date(2024, 1, 31)
    shifted = view._shift_month(d, 1)
    assert shifted == date(2024, 2, 29)


def test_month_navigation_30_day_clamping(timeline_env):
    """Aug 31 + 1 month -> Sept 30."""
    view = timeline_env["timeline_view"]
    d = date(2026, 8, 31)
    assert view._shift_month(d, 1) == date(2026, 9, 30)


def test_month_navigation_across_year_boundaries(timeline_env):
    """Month shifting across Dec/Jan year boundaries."""
    view = timeline_env["timeline_view"]

    # Dec 31, 2026 + 1 month -> Jan 31, 2027
    dec = date(2026, 12, 31)
    assert view._shift_month(dec, 1) == date(2027, 1, 31)

    # Jan 15, 2026 - 1 month -> Dec 15, 2025
    jan = date(2026, 1, 15)
    assert view._shift_month(jan, -1) == date(2025, 12, 15)


def test_direct_date_parsing_and_jump(timeline_env):
    """Valid and invalid date parsing for 'g' (go to date)."""
    view = timeline_env["timeline_view"]

    assert view._parse_date("2026-10-14") == date(2026, 10, 14)
    assert view._parse_date("2024-02-29") == date(2024, 2, 29)

    # Invalid dates
    assert view._parse_date("not-a-date") is None
    assert view._parse_date("2026-02-30") is None  # Feb 30 does not exist
    assert view._parse_date("2026-13-01") is None  # Month 13 does not exist


# ==============================================================================
# 6. Live Search in Timeline
# ==============================================================================

def test_live_search_filters_tasks_on_active_date(timeline_env):
    """Live search filters the tasks displayed on the current timeline date."""
    daily_svc = timeline_env["daily_svc"]
    clock = timeline_env["clock"]

    clock.set(datetime(2026, 10, 10, 10, 0, 0))
    daily_svc.create_task_for_date("Refactor TUI loop", "2026-10-10")
    daily_svc.create_task_for_date("Write Documentation", "2026-10-10")
    daily_svc.create_task_for_date("Refactor Database indexes", "2026-10-10")

    data = daily_svc.get_timeline_data_for_date("2026-10-10")
    tasks = data["tasks"]

    from todo.search import LiveSearchController
    search = LiveSearchController[DailyTask](
        key_fn=lambda dt: f"{dt.task.title} {dt.task.description or ''}"
    )

    search.query = "Refactor"
    filtered = search.filter(tasks)
    assert len(filtered) == 2
    assert all("Refactor" in dt.task.title for dt in filtered)

    search.query = "Doc"
    filtered = search.filter(tasks)
    assert len(filtered) == 1
    assert filtered[0].task.title == "Write Documentation"


# ==============================================================================
# 7. Interactive TUI Navigation & App Integration
# ==============================================================================

def test_interactive_tui_enter_timeline_and_navigate(timeline_env):
    """Pressing 'l' in Today view opens TimelineView; navigation and 'b' return to Today."""
    app = timeline_env["app"]
    daily_svc = timeline_env["daily_svc"]
    clock = timeline_env["clock"]

    clock.set(datetime(2026, 10, 10, 10, 0, 0))
    daily_svc.create_task_for_date("Today Task", "2026-10-10")

    # Keystrokes:
    # 'l' (open Timeline) -> 'left' (go to Oct 9) -> 'right' (back to Oct 10) -> 'b' (return to Today) -> 'q' (quit)
    inputs = ["l\n", "left\n", "right\n", "b\n", "q\n"]
    with patch("sys.stdin", io.StringIO("".join(inputs))):
        app.run()


def test_interactive_timeline_jump_to_today_with_t(timeline_env):
    """Pressing 't' in Timeline jumps immediately back to today's date."""
    app = timeline_env["app"]
    clock = timeline_env["clock"]
    view = timeline_env["timeline_view"]

    clock.set(datetime(2026, 10, 10, 10, 0, 0))

    # Open timeline on a distant future date, press 't' to jump to today, then 'b' to exit
    inputs = ["t\n", "b\n"]
    with patch("sys.stdin", io.StringIO("".join(inputs))):
        view.run(initial_date="2027-05-20")


def test_interactive_timeline_go_to_date_modal(timeline_env):
    """Pressing 'g' in Timeline opens date prompt and jumps to entered date."""
    view = timeline_env["timeline_view"]

    # 'g' -> type '2026-12-25' -> enter -> 'b'
    inputs = ["g\n", "2026-12-25\n", "b\n"]
    with patch("sys.stdin", io.StringIO("".join(inputs))):
        view.run(initial_date="2026-10-10")


def test_interactive_future_task_manage_menu_reschedule(timeline_env):
    """Selecting a future task and pressing Enter opens manage menu to reschedule."""
    daily_svc = timeline_env["daily_svc"]
    clock = timeline_env["clock"]
    view = timeline_env["timeline_view"]

    clock.set(datetime(2026, 10, 10, 10, 0, 0))
    dt = daily_svc.create_task_for_date("Annual Review", "2026-11-01")

    # Enter -> select 1 (Reschedule) -> enter '2026-11-15' -> 'b' (exit timeline)
    inputs = ["enter\n", "1\n", "2026-11-15\n", "b\n"]
    with patch("sys.stdin", io.StringIO("".join(inputs))):
        view.run(initial_date="2026-11-01")

    # Verify task moved to Nov 15
    assert len(daily_svc.get_timeline_data_for_date("2026-11-01")["tasks"]) == 0
    nov15_data = daily_svc.get_timeline_data_for_date("2026-11-15")
    assert len(nov15_data["tasks"]) == 1
    assert nov15_data["tasks"][0].task_id == dt.task_id


# ==============================================================================
# 8. Database Migrations & Index Validation
# ==============================================================================

def test_database_index_on_completed_at_exists(timeline_env):
    """Verify that idx_daily_tasks_completed_at exists and speeds up timeline queries."""
    db = timeline_env["db"]
    with db.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND name = 'idx_daily_tasks_completed_at'"
        )
        row = cursor.fetchone()
        assert row is not None
        assert row["name"] == "idx_daily_tasks_completed_at"


def test_database_idempotent_init_and_migration(timeline_env):
    """Running Database.init_db multiple times is completely idempotent."""
    db = timeline_env["db"]
    # Re-run init_db twice
    db.init_db()
    db.init_db()

    with db.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("PRAGMA table_info(daily_tasks)")
        columns = {r["name"] for r in cursor.fetchall()}
        assert "completed_at" in columns


def test_past_date_actions_are_blocked_with_status_warnings(timeline_env):
    """Attempting to schedule (+), delete (d), or move (m) on past dates is safely blocked."""
    view = timeline_env["timeline_view"]
    daily_svc = timeline_env["daily_svc"]
    clock = timeline_env["clock"]

    clock.set(datetime(2026, 10, 10, 10, 0, 0))
    # Create task completed on Oct 05
    dt = daily_svc.create_task_for_date("Old Task", "2026-10-05")
    clock.set(datetime(2026, 10, 5, 12, 0, 0))
    daily_svc.toggle_task_completion(dt.id)
    clock.set(datetime(2026, 10, 10, 10, 0, 0))

    # Send '+', 'd', 'm', then 'b' to exit
    inputs = ["+\n", "d\n", "m\n", "b\n"]
    with patch("sys.stdin", io.StringIO("".join(inputs))):
        view.run(initial_date="2026-10-05")

    # Task is still there and completed
    data = daily_svc.get_timeline_data_for_date("2026-10-05")
    assert len(data["tasks"]) == 1
    assert data["tasks"][0].is_completed is True


def test_week_and_month_navigation_interactive(timeline_env):
    """Verify week ([ / ]) and month (PageUp / PageDown) interactive keystrokes."""
    view = timeline_env["timeline_view"]

    # '[' (-7 days) -> ']' (+7 days) -> 'pageup' (-1 month) -> 'pagedown' (+1 month) -> 'b'
    inputs = ["[\n", "]\n", "pageup\n", "pagedown\n", "b\n"]
    with patch("sys.stdin", io.StringIO("".join(inputs))):
        view.run(initial_date="2026-10-10")


def test_invalid_date_interactive_prompt_shows_error(timeline_env):
    """Entering an invalid date string in 'g' modal does not crash and shows status message."""
    view = timeline_env["timeline_view"]

    # 'g' -> invalid date -> enter -> 'b'
    inputs = ["g\n", "invalid-date-string\n", "b\n"]
    with patch("sys.stdin", io.StringIO("".join(inputs))):
        view.run(initial_date="2026-10-10")


def test_future_task_manage_menu_move_to_today(timeline_env):
    """Managing a future task with option 2 moves it directly to Today."""
    daily_svc = timeline_env["daily_svc"]
    clock = timeline_env["clock"]
    view = timeline_env["timeline_view"]

    clock.set(datetime(2026, 10, 10, 10, 0, 0))
    today_str = "2026-10-10"
    dt = daily_svc.create_task_for_date("Deliver Proposal", "2026-10-22")

    # Enter -> select 2 (Move to Today) -> 'b'
    inputs = ["enter\n", "2\n", "b\n"]
    with patch("sys.stdin", io.StringIO("".join(inputs))):
        view.run(initial_date="2026-10-22")

    # Oct 22 now has 0 tasks
    assert len(daily_svc.get_timeline_data_for_date("2026-10-22")["tasks"]) == 0
    # Today has the task
    active_today = daily_svc.get_active_tasks_for_date(today_str)
    assert any(t.task_id == dt.task_id for t in active_today)


def test_future_task_manage_menu_move_to_universal(timeline_env):
    """Managing a future task with option 3 moves it to Universal Tasks."""
    daily_svc = timeline_env["daily_svc"]
    task_svc = timeline_env["task_svc"]
    view = timeline_env["timeline_view"]

    dt = daily_svc.create_task_for_date("Postpone this idea", "2026-10-28")

    # Enter -> select 3 (Move to Universal) -> 'b'
    inputs = ["enter\n", "3\n", "b\n"]
    with patch("sys.stdin", io.StringIO("".join(inputs))):
        view.run(initial_date="2026-10-28")

    assert len(daily_svc.get_timeline_data_for_date("2026-10-28")["tasks"]) == 0
    assert any(t.id == dt.task_id for t in task_svc.get_universal_tasks())


def test_future_task_manage_menu_remove_from_schedule(timeline_env):
    """Managing a future task with option 4 removes it from schedule."""
    daily_svc = timeline_env["daily_svc"]
    view = timeline_env["timeline_view"]

    dt = daily_svc.create_task_for_date("Cancel meeting schedule", "2026-10-29")

    # Enter -> select 4 (Remove from schedule) -> 'b'
    inputs = ["enter\n", "4\n", "b\n"]
    with patch("sys.stdin", io.StringIO("".join(inputs))):
        view.run(initial_date="2026-10-29")

    assert len(daily_svc.get_timeline_data_for_date("2026-10-29")["tasks"]) == 0


def test_universal_view_schedules_to_target_date(timeline_env):
    """UniversalView.run(target_date=...) schedules the selected universal task to that date."""
    app = timeline_env["app"]
    task_svc = timeline_env["task_svc"]
    daily_svc = timeline_env["daily_svc"]

    u_task = task_svc.create_task("Sprint Planning", source_type="universal")

    # Press Enter on task 1 to schedule it, then it returns None
    inputs = ["enter\n"]
    with patch("sys.stdin", io.StringIO("".join(inputs))):
        app.universal_view.run(select_mode=True, target_date="2026-10-31")

    # Verify scheduled on Oct 31
    oct31_data = daily_svc.get_timeline_data_for_date("2026-10-31")
    assert any(t.task_id == u_task.id for t in oct31_data["tasks"])


def test_manual_view_schedules_to_target_date(timeline_env):
    """ManualView.run(target_date=...) schedules the manual task to target_date when option 1 is chosen."""
    app = timeline_env["app"]
    daily_svc = timeline_env["daily_svc"]

    # Enter task title "Future Client Sync" -> select destination 1 (Scheduled date)
    inputs = ["Future Client Sync\n", "1\n"]
    with patch("sys.stdin", io.StringIO("".join(inputs))):
        app.manual_view.run(target_date="2026-11-05")

    nov05_data = daily_svc.get_timeline_data_for_date("2026-11-05")
    assert any(t.task.title == "Future Client Sync" for t in nov05_data["tasks"])


def test_legacy_database_migration_without_completed_at(tmp_path):
    """Verify an older SQLite database lacking completed_at in daily_tasks migrates safely."""
    import sqlite3
    db_file = tmp_path / "legacy.db"

    # Create legacy table schema without completed_at
    conn = sqlite3.connect(str(db_file))
    conn.executescript(
        """
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
        CREATE TABLE daily_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            date TEXT NOT NULL,
            position INTEGER NOT NULL DEFAULT 0,
            UNIQUE(task_id, date)
        );
        INSERT INTO tasks (title, created_at, updated_at) VALUES ('Old Task', '2025-01-01', '2025-01-01');
        INSERT INTO daily_tasks (task_id, date, position) VALUES (1, '2025-01-01', 1);
        """
    )
    conn.close()

    # Now open with Database class, which runs init_db and migrations
    db = Database(db_file)

    with db.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("PRAGMA table_info(daily_tasks)")
        columns = {r["name"] for r in cursor.fetchall()}
        assert "completed_at" in columns

        # Existing row preserved
        cursor.execute("SELECT id, task_id, date, position, completed_at FROM daily_tasks WHERE id = 1")
        row = cursor.fetchone()
        assert row["task_id"] == 1
        assert row["completed_at"] is None

        # Index exists
        cursor.execute("SELECT name FROM sqlite_master WHERE type = 'index' AND name = 'idx_daily_tasks_completed_at'")
        assert cursor.fetchone() is not None

