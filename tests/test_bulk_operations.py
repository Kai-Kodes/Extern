"""Tests for consistent bulk selection, bulk deletion, and cancellation across all views."""

import io
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from todo.config import Config
from todo.database import Database
from todo.integrations.filemeta import FilemetaScanResult, ScannedFile
from todo.models import Task
from todo.services.daily_service import DailyService
from todo.services.source_service import SourceService
from todo.services.task_service import TaskService
from todo.ui.app import TodoApp
from todo.ui.console import console
from todo.ui.filemeta_view import FilemetaView
from todo.ui.key_reader import Key
from todo.ui.telegram_view import TelegramView
from todo.ui.universal_view import UniversalView
from todo.ui.youtube_view import YouTubeView


@pytest.fixture
def test_setup():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        cfg = Config(db_path=db_path)
        app = TodoApp(cfg)
        yield {
            "cfg": cfg,
            "app": app,
            "db": app.db,
            "daily_svc": app.daily_service,
            "task_svc": app.task_service,
            "source_svc": app.source_service,
        }


def test_today_bulk_select_and_delete_with_cancellation(test_setup):
    """Verify Space multi-selection, Esc cancellation, confirmation, and bulk delete in Today."""
    app = test_setup["app"]
    daily_svc = test_setup["daily_svc"]
    task_svc = test_setup["task_svc"]
    today_str = daily_svc.get_today_str()

    # Create 3 manual tasks and 1 universal task scheduled for today
    t1 = task_svc.create_task("Task 1", source_type="manual")
    t2 = task_svc.create_task("Task 2", source_type="manual")
    t3 = task_svc.create_task("Task 3", source_type="manual")
    u1 = task_svc.create_task("Universal Task", source_type="universal")

    daily_svc.add_task_to_date(t1.id, today_str)
    daily_svc.add_task_to_date(t2.id, today_str)
    daily_svc.add_task_to_date(t3.id, today_str)
    daily_svc.add_task_to_date(u1.id, today_str)

    assert len(daily_svc.get_active_tasks_for_date(today_str)) == 4

    # Session 1: Enter delete mode with d, select task 1, press Esc to cancel/clear, then quit
    # d (enter delete mode) -> space (select task 1) -> escape (cancel delete mode) -> q (quit)
    inputs1 = ["d\n", "space\n", "escape\n", "q\n"]
    with patch("sys.stdin", io.StringIO("".join(inputs1))):
        app.run()
    # All 4 still present
    assert len(daily_svc.get_active_tasks_for_date(today_str)) == 4

    # Session 2: Enter delete mode, select task 1 and task 2, press enter, cancel with n
    # d -> space (select 1) -> down -> space (select 2) -> enter -> n (cancel) -> escape -> q
    inputs2 = ["d\n", "space\n", "down\n", "space\n", "enter\n", "n\n", "escape\n", "q\n"]
    with patch("sys.stdin", io.StringIO("".join(inputs2))):
        app.run()
    assert len(daily_svc.get_active_tasks_for_date(today_str)) == 4

    # Session 3: Enter delete mode, select task 1 and task 2, enter, confirm with y
    # d -> space (select 1) -> down -> space (select 2) -> enter -> y (confirm) -> q
    inputs3 = ["d\n", "space\n", "down\n", "space\n", "enter\n", "y\n", "q\n"]
    with patch("sys.stdin", io.StringIO("".join(inputs3))):
        app.run()

    # Tasks 1 and 2 deleted!
    active_now = daily_svc.get_active_tasks_for_date(today_str)
    assert len(active_now) == 2
    titles = [dt.task.title for dt in active_now]
    assert "Task 1" not in titles
    assert "Task 2" not in titles
    assert "Task 3" in titles
    assert "Universal Task" in titles

    # Underlying manual tasks 1 and 2 permanently removed
    assert task_svc.get_task_by_id(t1.id) is None
    assert task_svc.get_task_by_id(t2.id) is None


def test_today_bulk_delete_universal_task_preserves_universal_entity(test_setup):
    """Removing universal tasks via bulk delete in Today only removes daily schedule, not universal task."""
    app = test_setup["app"]
    daily_svc = test_setup["daily_svc"]
    task_svc = test_setup["task_svc"]
    today_str = daily_svc.get_today_str()

    u1 = task_svc.create_task("Universal 1", source_type="universal")
    u2 = task_svc.create_task("Universal 2", source_type="universal")
    daily_svc.add_task_to_date(u1.id, today_str)
    daily_svc.add_task_to_date(u2.id, today_str)

    # Bulk delete both in Today: d -> space -> down -> space -> enter -> y -> q
    inputs = ["d\n", "space\n", "down\n", "space\n", "enter\n", "y\n", "q\n"]
    with patch("sys.stdin", io.StringIO("".join(inputs))):
        app.run()

    # Removed from Today
    assert len(daily_svc.get_active_tasks_for_date(today_str)) == 0

    # Still exist in Universal Tasks master list!
    u_tasks = task_svc.get_universal_tasks()
    assert len(u_tasks) == 2
    assert u_tasks[0].id == u1.id
    assert u_tasks[1].id == u2.id


def test_universal_view_bulk_select_and_delete(test_setup):
    """Verify multi-selection and bulk delete in Universal Tasks view."""
    task_svc = test_setup["task_svc"]
    daily_svc = test_setup["daily_svc"]
    view = UniversalView(task_svc, daily_svc)

    u1 = task_svc.create_task("U-Task 1", source_type="universal")
    u2 = task_svc.create_task("U-Task 2", source_type="universal")
    u3 = task_svc.create_task("U-Task 3", source_type="universal")

    # d -> space (select 1) -> down -> space (select 2) -> enter -> y (confirm) -> b (exit)
    inputs = ["d\n", "space\n", "down\n", "space\n", "enter\n", "y\n", "b\n"]
    with patch("sys.stdin", io.StringIO("".join(inputs))):
        view.run()

    remaining = task_svc.get_universal_tasks()
    assert len(remaining) == 1
    assert remaining[0].id == u3.id
    assert remaining[0].title == "U-Task 3"


def test_universal_view_bulk_schedule_to_today(test_setup):
    """Verify pressing Enter with multi-selected universal tasks schedules all into Today."""
    task_svc = test_setup["task_svc"]
    daily_svc = test_setup["daily_svc"]
    view = UniversalView(task_svc, daily_svc)
    today_str = daily_svc.get_today_str()

    u1 = task_svc.create_task("U-Task 1", source_type="universal")
    u2 = task_svc.create_task("U-Task 2", source_type="universal")
    u3 = task_svc.create_task("U-Task 3", source_type="universal")

    # Select 1 and 3, then press Enter
    # Space (select 1) -> down -> down -> Space (select 3) -> Enter
    inputs = ["space\n", "down\n", "down\n", "space\n", "enter\n"]
    with patch("sys.stdin", io.StringIO("".join(inputs))):
        view.run()

    scheduled = daily_svc.get_active_tasks_for_date(today_str)
    assert len(scheduled) == 2
    scheduled_ids = {dt.task_id for dt in scheduled}
    assert u1.id in scheduled_ids
    assert u3.id in scheduled_ids


def test_bulk_delete_sources_preserves_standalone_tasks(test_setup):
    """Bulk deleting sources unlinks tasks and cascades file items without touching filesystem."""
    source_svc = test_setup["source_svc"]
    task_svc = test_setup["task_svc"]
    daily_svc = test_setup["daily_svc"]
    today_str = daily_svc.get_today_str()

    # Create two sources
    scan1 = FilemetaScanResult(
        source_name="Source 1",
        source_path="/courses/s1",
        items=[
            ScannedFile(name="v1.mp4", path="/courses/s1/v1.mp4", title="V1", type="video"),
        ],
    )
    scan2 = FilemetaScanResult(
        source_name="Source 2",
        source_path="/courses/s2",
        items=[
            ScannedFile(name="v2.mp4", path="/courses/s2/v2.mp4", title="V2", type="video"),
        ],
    )
    s1, _, _ = source_svc.add_or_update_source(scan1)
    s2, _, _ = source_svc.add_or_update_source(scan2)

    item1 = source_svc.get_items_by_source(s1.id)[0]
    task1 = task_svc.get_or_create_filemeta_task(item1, source_name="Source 1")
    daily_svc.add_task_to_date(task1.id, today_str)

    assert task1.source_id == item1.id

    # Bulk delete sources
    deleted_count = source_svc.delete_sources_bulk([s1.id, s2.id])
    assert deleted_count == 2
    assert len(source_svc.get_sources()) == 0

    # User task remains scheduled, but source_id is unlinked to NULL
    reloaded_task = task_svc.get_task_by_id(task1.id)
    assert reloaded_task is not None
    assert reloaded_task.source_id is None
    assert len(daily_svc.get_active_tasks_for_date(today_str)) == 1


def test_bulk_delete_file_items_within_source(test_setup):
    """Verify deleting selected items from a source removes items and updates progress."""
    source_svc = test_setup["source_svc"]
    scan = FilemetaScanResult(
        source_name="Course Alpha",
        source_path="/courses/alpha",
        items=[
            ScannedFile(name="01.mp4", path="/courses/alpha/01.mp4", title="Lesson 1", type="video"),
            ScannedFile(name="02.mp4", path="/courses/alpha/02.mp4", title="Lesson 2", type="video"),
            ScannedFile(name="03.mp4", path="/courses/alpha/03.mp4", title="Lesson 3", type="video"),
            ScannedFile(name="04.mp4", path="/courses/alpha/04.mp4", title="Lesson 4", type="video"),
        ],
    )
    src, _, _ = source_svc.add_or_update_source(scan)
    items = source_svc.get_items_by_source(src.id)
    assert len(items) == 4

    # Delete items 2 and 3 in bulk
    deleted = source_svc.delete_file_items_bulk([items[1].id, items[2].id])
    assert deleted == 2

    remaining = source_svc.get_items_by_source(src.id)
    assert len(remaining) == 2
    assert [it.title for it in remaining] == ["Lesson 1", "Lesson 4"]

    # Delete single item
    assert source_svc.delete_file_item(remaining[0].id) is True
    assert len(source_svc.get_items_by_source(src.id)) == 1


def test_today_delete_mode_zero_selection_enter_does_nothing(test_setup):
    """Pressing Enter in Today delete mode with 0 items selected does nothing and does not delete."""
    app = test_setup["app"]
    daily_svc = test_setup["daily_svc"]
    task_svc = test_setup["task_svc"]
    today_str = daily_svc.get_today_str()

    t1 = task_svc.create_task("Protected Task", source_type="manual")
    daily_svc.add_task_to_date(t1.id, today_str)
    assert len(daily_svc.get_active_tasks_for_date(today_str)) == 1

    # d (enter delete mode) -> enter (0 selected: does nothing) -> escape (exit delete mode) -> q (quit)
    inputs = ["d\n", "enter\n", "escape\n", "q\n"]
    with patch("sys.stdin", io.StringIO("".join(inputs))):
        app.run()

    # Task is NOT deleted!
    assert len(daily_svc.get_active_tasks_for_date(today_str)) == 1


def test_today_delete_mode_blocks_unrelated_shortcuts(test_setup):
    """In Today delete mode, unrelated shortcuts like +, m, u, and digits are blocked."""
    app = test_setup["app"]
    daily_svc = test_setup["daily_svc"]
    task_svc = test_setup["task_svc"]
    today_str = daily_svc.get_today_str()

    t1 = task_svc.create_task("Task 1", source_type="manual")
    daily_svc.add_task_to_date(t1.id, today_str)

    # d (delete mode) -> + -> m -> u -> 1 -> escape (exit delete mode) -> q (quit)
    inputs = ["d\n", "+\n", "m\n", "u\n", "1\n", "escape\n", "q\n"]
    with patch("sys.stdin", io.StringIO("".join(inputs))):
        app.run()

    # Still in Today, no task added or moved or completion toggled
    active = daily_svc.get_active_tasks_for_date(today_str)
    assert len(active) == 1
    assert active[0].is_completed is False


def test_today_move_mode_workflow_and_cancellation(test_setup):
    """Test dedicated move mode in Today: m -> Space -> Enter, and cancellation via Esc."""
    app = test_setup["app"]
    daily_svc = test_setup["daily_svc"]
    task_svc = test_setup["task_svc"]
    today_str = daily_svc.get_today_str()

    t1 = task_svc.create_task("Task to Move", source_type="manual")
    t2 = task_svc.create_task("Task to Keep", source_type="manual")
    daily_svc.add_task_to_date(t1.id, today_str)
    daily_svc.add_task_to_date(t2.id, today_str)

    # Session 1: Enter move mode with m, select 0 items, enter does nothing, esc cancels
    inputs1 = ["m\n", "enter\n", "escape\n", "q\n"]
    with patch("sys.stdin", io.StringIO("".join(inputs1))):
        app.run()
    assert len(daily_svc.get_active_tasks_for_date(today_str)) == 2
    assert len(task_svc.get_universal_tasks()) == 0

    # Session 2: Enter move mode with m, select Task 1 with Space, press enter to move
    inputs2 = ["m\n", "space\n", "enter\n", "q\n"]
    with patch("sys.stdin", io.StringIO("".join(inputs2))):
        app.run()

    # Task 1 moved to universal, Task 2 remains in Today
    active = daily_svc.get_active_tasks_for_date(today_str)
    assert len(active) == 1
    assert active[0].task.title == "Task to Keep"
    u_tasks = task_svc.get_universal_tasks()
    assert len(u_tasks) == 1
    assert u_tasks[0].title == "Task to Move"


def test_filemeta_items_delete_mode_workflow(test_setup):
    """Test dedicated delete selection mode in Filemeta item browser."""
    source_svc = test_setup["source_svc"]
    task_svc = test_setup["task_svc"]
    daily_svc = test_setup["daily_svc"]
    adapter = MagicMock()
    view = FilemetaView(source_svc, task_svc, daily_svc, adapter)

    scan = FilemetaScanResult(
        source_name="Course F",
        source_path="/courses/f",
        items=[
            ScannedFile(name="01.mp4", path="/courses/f/01.mp4", title="Lesson 1", type="video"),
            ScannedFile(name="02.mp4", path="/courses/f/02.mp4", title="Lesson 2", type="video"),
        ],
    )
    src, _, _ = source_svc.add_or_update_source(scan)

    # In item browser: press d (delete mode), Space (select 1), Enter (confirm selection), y (confirm delete), b (exit)
    with patch("todo.ui.filemeta_view.key_reader.read_key", side_effect=["d", Key.SPACE, Key.ENTER, "y", "b"]):
        view.handle_source_items(src)

    remaining = source_svc.get_items_by_source(src.id)
    assert len(remaining) == 1
    assert remaining[0].title == "Lesson 2"


def test_youtube_videos_delete_mode_workflow(test_setup):
    """Test dedicated delete selection mode in YouTube video browser."""
    source_svc = test_setup["source_svc"]
    task_svc = test_setup["task_svc"]
    daily_svc = test_setup["daily_svc"]
    adapter = MagicMock()
    view = YouTubeView(adapter, source_svc, task_svc, daily_svc)

    scan = FilemetaScanResult(
        source_name="YT Playlist",
        source_path="youtube://playlist/PLtest",
        items=[
            ScannedFile(name="vid1", path="youtube://playlist/PLtest/vid1", title="Video 1", type="video"),
            ScannedFile(name="vid2", path="youtube://playlist/PLtest/vid2", title="Video 2", type="video"),
        ],
    )
    src, _, _ = source_svc.add_or_update_source(scan)

    # In video browser: press d (delete mode), Space (select 1), Enter (confirm selection), y (confirm delete), b (exit)
    with patch("todo.ui.youtube_view.key_reader.read_key", side_effect=["d", Key.SPACE, Key.ENTER, "y", "b"]):
        view.handle_playlist_videos(src)

    remaining = source_svc.get_items_by_source(src.id)
    assert len(remaining) == 1
    assert remaining[0].title == "Video 2"


def test_telegram_lessons_delete_mode_workflow(test_setup):
    """Test dedicated delete selection mode in Telegram lesson browser."""
    source_svc = test_setup["source_svc"]
    task_svc = test_setup["task_svc"]
    daily_svc = test_setup["daily_svc"]
    adapter = MagicMock()
    view = TelegramView(adapter, source_svc, task_svc, daily_svc)

    scan = FilemetaScanResult(
        source_name="TG Channel",
        source_path="telegram://-100222",
        items=[
            ScannedFile(name="tg1", path="telegram://-100222/1", title="Lesson 1", type="video"),
            ScannedFile(name="tg2", path="telegram://-100222/2", title="Lesson 2", type="video"),
        ],
    )
    src, _, _ = source_svc.add_or_update_source(scan)

    # In lesson browser: press d (delete mode), Space (select 1), Enter (confirm selection), y (confirm delete), b (exit)
    with patch("todo.ui.telegram_view.key_reader.read_key", side_effect=["d", Key.SPACE, Key.ENTER, "y", "b"]):
        view.handle_channel_lessons(src)

    remaining = source_svc.get_items_by_source(src.id)
    assert len(remaining) == 1
    assert remaining[0].title == "Lesson 2"
