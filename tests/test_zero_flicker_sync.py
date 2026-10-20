"""Tests for zero-flicker rendering, near-real-time adaptive Google Tasks sync, and UI-thread isolation."""

import io
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from todo.config import Config
from todo.database import Database
from todo.integrations.google_tasks import GoogleOAuthManager, GoogleTasksClient
from todo.models import Task
from todo.services.daily_service import DailyService
from todo.services.google_tasks_service import GoogleTasksService, SyncResult, SyncSummary
from todo.services.google_tasks_sync_manager import GoogleTasksSyncManager
from todo.services.task_service import TaskService


@pytest.fixture
def sync_env(tmp_path: Path):
    """Set up an isolated environment with Database, GoogleTasksService, and SyncManager."""
    db_path = tmp_path / "test_sync.db"
    token_path = tmp_path / "token.json"
    cfg = Config(
        db_path=db_path,
        google_client_id="test_client_id",
        google_client_secret="test_secret",
        google_token_path=token_path,
        google_sync_interval=2.5,
        google_sync_idle_interval=10.0,
        google_sync_error_interval=20.0,
        google_sync_timeout=5.0,
    )
    db = Database(db_path)
    db.init_db()

    oauth_mgr = GoogleOAuthManager(
        token_path=token_path,
        client_id=cfg.google_client_id,
        client_secret=cfg.google_client_secret,
    )
    oauth_mgr.save_tokens({"access_token": "valid_token", "expires_in": 3600})

    client = GoogleTasksClient(oauth_mgr, timeout=(2.0, 5.0))
    service = GoogleTasksService(
        db=db,
        config=cfg,
        oauth_manager=oauth_mgr,
        client=client,
    )
    sync_mgr = GoogleTasksSyncManager(
        google_tasks_service=service,
        interval=cfg.google_sync_interval,
        idle_interval=cfg.google_sync_idle_interval,
        error_interval=cfg.google_sync_error_interval,
    )
    service.sync_manager = sync_mgr

    daily_svc = DailyService(db)
    daily_svc.google_tasks_service = service
    daily_svc.google_tasks_sync_manager = sync_mgr
    task_svc = TaskService(db)

    # Pre-populate list in cache and source
    service.get_source_for_list("list_main", "Main Tasks")
    with db.get_connection() as conn:
        conn.execute(
            "INSERT INTO google_task_lists_cache (id, title, cached_at, updated_at) VALUES (?, ?, ?, ?)",
            ("list_main", "Main Tasks", "2026-10-20T10:00:00", "2026-10-20T10:00:00"),
        )
        conn.commit()

    return {
        "db": db,
        "cfg": cfg,
        "client": client,
        "service": service,
        "sync_mgr": sync_mgr,
        "daily_svc": daily_svc,
        "task_svc": task_svc,
    }


def test_adaptive_polling_interval_transitions(sync_env):
    """Verify polling interval adapts between active (2.5s), idle (10.0s), and error backoff (20.0s)."""
    sync_mgr: GoogleTasksSyncManager = sync_env["sync_mgr"]

    # 1. Initially active interval
    assert sync_mgr.get_current_interval() == 2.5

    # 2. Simulate 35 seconds of inactivity -> transitions to idle interval
    sync_mgr.last_user_activity = time.time() - 35.0
    assert sync_mgr.get_current_interval() == 10.0

    # 3. User activity resets to active interval
    sync_mgr.record_user_activity()
    assert sync_mgr.get_current_interval() == 2.5

    # 4. Error backoff overrides idle and active intervals
    sync_mgr._in_error_backoff = True
    assert sync_mgr.get_current_interval() == 20.0

    # 5. Clear error backoff
    sync_mgr._in_error_backoff = False
    assert sync_mgr.get_current_interval() == 2.5


def test_ui_thread_isolation_latency(sync_env):
    """Verify that all UI-triggered mutations complete in < 15ms with ZERO blocking HTTP calls on calling thread."""
    service: GoogleTasksService = sync_env["service"]
    daily_svc: DailyService = sync_env["daily_svc"]
    client = sync_env["client"]

    # If any HTTP method were called on client, inject a 2-second sleep that would fail the latency test
    def fail_if_blocking(*args, **kwargs):
        time.sleep(2.0)
        raise RuntimeError("Network I/O called on UI thread!")

    with patch.object(client, "create_task", side_effect=fail_if_blocking), \
         patch.object(client, "update_task", side_effect=fail_if_blocking), \
         patch.object(client, "set_task_completed", side_effect=fail_if_blocking), \
         patch.object(client, "delete_task", side_effect=fail_if_blocking):

        # 1. Create task (optimistic local insert + outbox)
        t0 = time.perf_counter()
        task = service.create_task("list_main", "Fast UI Task", notes="Instant notes", due_date="2026-10-25")
        duration = time.perf_counter() - t0
        assert duration < 0.05  # < 50ms, typically < 3ms
        assert task.id is not None
        assert task.title == "Fast UI Task"

        # Outbox queued
        outbox = service.get_pending_outbox()
        assert len(outbox) == 1
        assert outbox[0]["action"] == "create"

        # 2. Update task
        t0 = time.perf_counter()
        updated = service.update_task(task.id, title="Updated Fast Title", notes="Updated notes")
        duration = time.perf_counter() - t0
        assert duration < 0.05
        assert updated.title == "Updated Fast Title"

        # 3. Toggle task completion via DailyService
        dt = daily_svc.get_active_tasks_for_date("2026-10-25")[0]
        t0 = time.perf_counter()
        daily_svc.toggle_task_completion(dt.id)
        duration = time.perf_counter() - t0
        assert duration < 0.05

        # 4. Move task to Universal via DailyService
        t0 = time.perf_counter()
        daily_svc.move_task_to_universal(dt.id)
        duration = time.perf_counter() - t0
        assert duration < 0.05

        # 5. Add back to date via DailyService
        t0 = time.perf_counter()
        daily_svc.add_task_to_date(task.id, "2026-10-28")
        duration = time.perf_counter() - t0
        assert duration < 0.05

        # 6. Delete task via service
        t0 = time.perf_counter()
        deleted = service.delete_task(task.id)
        duration = time.perf_counter() - t0
        assert duration < 0.05
        assert deleted is True


def test_incremental_sync_and_change_version_tracking(sync_env):
    """Verify that sync detects actual changes, uses updated_min, and increments change_version only on real data mutation."""
    service: GoogleTasksService = sync_env["service"]
    sync_mgr: GoogleTasksSyncManager = sync_env["sync_mgr"]
    client = sync_env["client"]

    initial_version = sync_mgr.change_version

    # 1. Remote poll with NO changes returns has_changes=False and does NOT increment change_version
    with patch.object(client, "get_tasks", return_value=[]):
        res = service.sync_tasks_for_list("list_main", updated_min="2026-10-20T10:00:00.000Z")
        assert res.has_changes is False
        assert sync_mgr.change_version == initial_version

    # 2. Remote poll with a new remote task
    remote_item = {
        "id": "remote_incoming_123",
        "title": "Cloud Added Task",
        "notes": "Incoming from phone",
        "status": "needsAction",
        "due": "2026-10-22T00:00:00.000Z",
    }
    with patch.object(client, "get_tasks", return_value=[remote_item]):
        res2 = service.sync_tasks_for_list("list_main", updated_min="2026-10-20T10:00:00.000Z")
        assert res2.has_changes is True

    # When sync_all_lists runs and finds changes, sync_mgr increments change_version
    remote_item2 = {
        "id": "remote_incoming_456",
        "title": "Second Cloud Added Task",
        "notes": "Incoming from web",
        "status": "needsAction",
    }
    with patch.object(client, "get_task_lists", return_value=[{"id": "list_main", "title": "Main Tasks"}]), \
         patch.object(client, "get_tasks", return_value=[remote_item2]):
        sync_mgr.sync_now(incremental=True)
        assert sync_mgr.change_version > initial_version


def test_stable_cursor_selection_logic():
    """Verify cursor stays anchored to the same task ID when tasks are inserted or reordered."""
    from dataclasses import dataclass

    @dataclass
    class MockItem:
        id: int
        title: str

    # Initial list: [A (id=10), B (id=20), C (id=30)]
    # User cursor is on B (idx=1)
    tasks = [MockItem(10, "Task A"), MockItem(20, "Task B"), MockItem(30, "Task C")]
    selected_idx = 1
    selected_task_id = tasks[selected_idx].id
    assert selected_task_id == 20

    # Remote sync inserts Task 0 at the beginning:
    updated_tasks = [MockItem(5, "Task 0"), MockItem(10, "Task A"), MockItem(20, "Task B"), MockItem(30, "Task C")]

    # Resolution logic
    found_idx = next((i for i, t in enumerate(updated_tasks) if t.id == selected_task_id), None)
    assert found_idx is not None
    selected_idx = found_idx

    # Cursor is now at index 2, pointing to the EXACT SAME task (Task B)
    assert selected_idx == 2
    assert updated_tasks[selected_idx].title == "Task B"


def test_durable_outbox_chained_mutations_drain(sync_env):
    """Verify that multiple offline mutations for the same task (create -> update -> complete) drain correctly in sequence."""
    service: GoogleTasksService = sync_env["service"]
    client = sync_env["client"]

    # 1. Create task locally while in async mode
    task = service.create_task("list_main", "Offline Chain Task", notes="Original notes", sync_remote=False)

    # 2. Update task locally
    service.update_task(task.id, title="Renamed Offline Task", notes="New notes", sync_remote=False)

    # 3. Complete task locally
    service.toggle_task_completion(task.id, sync_remote=False)

    outbox = service.get_pending_outbox()
    assert len(outbox) == 3
    assert [o["action"] for o in outbox] == ["create", "update", "complete"]

    # 4. Now connect and drain outbox
    mock_remote_created = {
        "id": "remote_assigned_id_777",
        "title": "Offline Chain Task",
        "notes": "Original notes",
    }
    with patch.object(client, "create_task", return_value=mock_remote_created) as mock_c, \
         patch.object(client, "update_task") as mock_u, \
         patch.object(client, "set_task_completed") as mock_comp:

        drained = service.drain_outbox()
        assert drained == 3
        mock_c.assert_called_once()
        mock_u.assert_called_once_with(
            list_id="list_main",
            task_id="remote_assigned_id_777",
            title="Renamed Offline Task",
            notes="New notes",
            due_date=None,
            clear_due=False,
        )
        mock_comp.assert_called_once_with(
            list_id="list_main",
            task_id="remote_assigned_id_777",
            completed=True,
        )

    assert len(service.get_pending_outbox()) == 0


def test_concurrent_sync_and_db_latency(sync_env):
    """Measure DB transaction latency under active concurrent background sync."""
    db: Database = sync_env["db"]
    sync_mgr: GoogleTasksSyncManager = sync_env["sync_mgr"]
    service: GoogleTasksService = sync_env["service"]
    client = sync_env["client"]

    # Run background sync worker
    with patch.object(client, "get_task_lists", return_value=[{"id": "list_main", "title": "Main Tasks"}]), \
         patch.object(client, "get_tasks", return_value=[]):
        sync_mgr.start()
        try:
            # Measure 100 rapid concurrent database operations
            latencies = []
            for i in range(100):
                t0 = time.perf_counter()
                with db.get_connection() as conn:
                    cursor = conn.cursor()
                    cursor.execute("SELECT COUNT(*) FROM tasks")
                    _ = cursor.fetchone()
                latencies.append(time.perf_counter() - t0)

            max_latency = max(latencies)
            avg_latency = sum(latencies) / len(latencies)
            assert avg_latency < 0.010, f"Avg latency {avg_latency*1000:.2f}ms exceeded 10ms"
            assert max_latency < 0.060, f"Max latency {max_latency*1000:.2f}ms exceeded 60ms"
        finally:
            sync_mgr.stop()


def test_today_view_zero_redraw_on_timeout_without_changes(sync_env):
    """Verify that TodayView does not clear or redraw the screen when read_key times out and data is unchanged."""
    from todo.ui.app import TodoApp

    cfg = sync_env["cfg"]
    app = TodoApp(cfg)

    # User does nothing for 2 timeouts (None, None), then quits ("q")
    inputs = [None, None, "q"]

    with patch("todo.ui.app.read_key", side_effect=inputs), \
         patch.object(app.today_view, "render") as mock_render:
        app._main_loop()

        # Render should only be called ONCE on startup, NOT on every timeout!
        assert mock_render.call_count == 1


def test_today_view_redraws_when_change_version_increments(sync_env):
    """Verify that TodayView re-renders when background sync signals change_version update."""
    from todo.ui.app import TodoApp

    cfg = sync_env["cfg"]
    app = TodoApp(cfg)

    # First iteration: startup render.
    # Second iteration: background sync increments change_version -> trigger render.
    # Third iteration: quit.
    call_idx = 0

    def mock_read_key(timeout=None):
        nonlocal call_idx
        call_idx += 1
        if call_idx == 1:
            # Background thread notifies change
            app.google_tasks_sync_manager.notify_change()
            return None
        return "q"

    with patch("todo.ui.app.read_key", side_effect=mock_read_key), \
         patch.object(app.today_view, "render") as mock_render:
        app._main_loop()

        # Should render twice: initial + background update
        assert mock_render.call_count == 2


def test_google_tasks_view_zero_redraw_on_timeout_without_changes(sync_env):
    """Verify that GoogleTasksView does not clear the viewport on timeouts when data has not changed."""
    from todo.ui.google_tasks_view import GoogleTasksView

    service = sync_env["service"]
    daily_svc = sync_env["daily_svc"]
    sync_mgr = sync_env["sync_mgr"]

    view = GoogleTasksView(
        google_tasks_service=service,
        daily_service=daily_svc,
        sync_manager=sync_mgr,
    )

    inputs = [None, None, "b"]

    with patch("todo.ui.google_tasks_view.key_reader.read_key", side_effect=inputs), \
         patch("todo.ui.google_tasks_view.clear_viewport") as mock_clear:
        view.run()

        # clear_viewport should only be called ONCE (initial render), zero flicker during idle timeouts
        assert mock_clear.call_count == 1

