"""Tests for Chunky Tasks sync isolation, carry forward, timeline history, and lifecycle hooks."""

import pytest
from datetime import datetime
from unittest.mock import MagicMock

from todo.database import Database
from todo.integrations.filemeta import FilemetaScanResult, ScannedFile
from todo.models import ChunkyResource, ChunkyTask, FileItem, Source, Task
from todo.services.chunky_service import ChunkyService
from todo.services.daily_service import DailyService
from todo.services.google_tasks_service import GoogleTasksService
from todo.services.source_service import SourceService
from todo.services.task_service import TaskService


@pytest.fixture
def temp_db(tmp_path):
    db_file = tmp_path / "test_chunky_sync.db"
    db = Database(db_file)
    db.init_db()
    return db


def test_google_tasks_sync_isolation(temp_db):
    """Source-backed chunky tasks must never be synchronized to Google Tasks."""
    task_service = TaskService(temp_db)
    gtasks_service = GoogleTasksService(temp_db, config=MagicMock())

    # Create tasks of each source type
    yt_task = task_service.create_task(
        title="YT Chunk [00:00:00–00:20:00]",
        source_type="youtube",
        source_id=1,
    )
    tg_task = task_service.create_task(
        title="TG Chunk [00:00:00–00:15:00]",
        source_type="telegram",
        source_id=2,
    )
    fm_task = task_service.create_task(
        title="PDF Chunk [p. 1–10]",
        source_type="filemeta",
        source_id=3,
    )

    # Calling sync_task_created on source-backed tasks must be a no-op
    gtasks_service.sync_task_created(yt_task)
    gtasks_service.sync_task_created(tg_task)
    gtasks_service.sync_task_created(fm_task)

    # Check outbox is empty
    with temp_db.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) as cnt FROM google_tasks_outbox")
        assert cursor.fetchone()["cnt"] == 0


def test_today_scheduling_and_carry_forward(temp_db):
    """Chunky tasks schedule into Today and carry forward across dates."""
    date_val = datetime(2026, 10, 23, 10, 0, 0)
    date_provider = lambda: date_val

    daily_service = DailyService(temp_db, date_provider=date_provider)
    task_service = TaskService(temp_db, date_provider=date_provider)
    chunky_service = ChunkyService(temp_db, date_provider=date_provider)

    # Create a resource & chunky task
    resource = chunky_service.get_or_create_resource(
        source_type="youtube",
        canonical_uri="https://youtube.com/watch?v=abc",
        title="Advanced Rust",
        total_range=3600.0,
        range_unit="seconds",
    )
    task, chunky_task = chunky_service.create_chunk_task(
        resource=resource,
        start_pos=0.0,
        end_pos=1200.0,
        task_service=task_service,
    )

    # Schedule into Today (2026-10-23)
    daily_service.add_task_to_date(task.id, "2026-10-23")
    today_tasks = daily_service.get_tasks_for_date("2026-10-23")
    assert len(today_tasks) == 1
    assert today_tasks[0].task_id == task.id
    assert not today_tasks[0].is_completed

    # Move date forward to 2026-10-24: active tasks carries forward pending task automatically
    date_val = datetime(2026, 10, 24, 10, 0, 0)
    next_day_active = daily_service.get_active_tasks_for_date("2026-10-24")
    assert len(next_day_active) == 1
    assert next_day_active[0].task_id == task.id
    assert not next_day_active[0].is_completed


def test_completion_history_and_reopen_lifecycle(temp_db):
    """Completing a chunky task tracks progress; reopening recalculates without deleting history."""
    now = datetime(2026, 10, 23, 12, 0, 0)
    chunky_service = ChunkyService(temp_db, date_provider=lambda: now)
    task_service = TaskService(temp_db, date_provider=lambda: now)
    daily_service = DailyService(temp_db, date_provider=lambda: now)

    resource = chunky_service.get_or_create_resource(
        source_type="filemeta",
        canonical_uri="/books/clean_code.pdf",
        title="Clean Code",
        total_range=400.0,
        range_unit="pages",
    )

    task, chunky_task = chunky_service.create_chunk_task(
        resource=resource,
        start_pos=0.0,
        end_pos=50.0,
        task_service=task_service,
    )
    daily_service.add_task_to_date(task.id, "2026-10-23")
    daily_tasks = daily_service.get_tasks_for_date("2026-10-23")
    daily_task_id = daily_tasks[0].id

    # Complete chunk via daily_service toggle
    daily_service.toggle_task_completion(daily_task_id)

    # Verify resource progress updated
    updated_res = chunky_service.get_resource_by_id(resource.id)
    assert updated_res.completed_range == 50.0
    assert updated_res.progress_pct == 12  # (50/400)*100 = 12.5 -> 12%

    # Verify history recorded
    with temp_db.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT action, start_pos, end_pos FROM chunky_completion_history WHERE resource_id = ?", (resource.id,))
        rows = cursor.fetchall()
        assert len(rows) == 1
        assert rows[0]["action"] == "completed"
        assert rows[0]["start_pos"] == 0.0
        assert rows[0]["end_pos"] == 50.0

    # Reopen chunk
    daily_service.toggle_task_completion(daily_task_id)

    updated_res = chunky_service.get_resource_by_id(resource.id)
    assert updated_res.completed_range == 0.0
    assert updated_res.progress_pct == 0

    # History preserved and appended
    with temp_db.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT action FROM chunky_completion_history WHERE resource_id = ? ORDER BY id ASC", (resource.id,))
        history = [r["action"] for r in cursor.fetchall()]
        assert history == ["completed", "reopened"]


def test_full_resource_override(temp_db):
    """Marking the entire original resource complete sets progress to 100% and closes outstanding chunks."""
    source_service = SourceService(temp_db)
    task_service = TaskService(temp_db)
    daily_service = DailyService(temp_db)
    chunky_service = ChunkyService(temp_db)

    # 1. Add source with 1 video item
    scanned_file = ScannedFile(
        name="vid_100",
        path="https://youtube.com/watch?v=100",
        title="Full Machine Learning Course",
        type="video",
        metadata={"duration": 7200, "canonical_url": "https://youtube.com/watch?v=100"},
    )
    scan_res = FilemetaScanResult(
        source_name="ML Playlist",
        source_path="youtube://playlist/PL_ML",
        items=[scanned_file],
    )
    source, _, _ = source_service.add_or_update_source(scan_res)
    items = source_service.get_items_by_source(source.id)
    file_item = items[0]

    # 2. Create resource & chunk task
    resource = chunky_service.get_or_create_resource(
        source_type="youtube",
        canonical_uri="https://youtube.com/watch?v=100",
        title=file_item.title,
        total_range=7200.0,
        range_unit="seconds",
        file_item_id=file_item.id,
    )
    task, chunky_task = chunky_service.create_chunk_task(
        resource=resource,
        start_pos=0.0,
        end_pos=1800.0,
        task_service=task_service,
    )
    daily_service.add_task_to_date(task.id, "2026-10-23")

    # Verify initial task state is pending
    assert task_service.get_task_by_id(task.id).status == "pending"

    # 3. User marks the whole course item complete in library view
    source_service.set_item_completion(file_item.id, True)

    # Verify resource is 100% complete
    updated_res = chunky_service.get_resource_by_id(resource.id)
    assert updated_res.is_fully_completed
    assert updated_res.completed_range == 7200.0
    assert updated_res.progress_pct == 100

    # Verify outstanding chunk task was overridden to completed
    updated_task = task_service.get_task_by_id(task.id)
    assert updated_task.status == "completed"

    # Verify history recorded full_override
    with temp_db.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT action FROM chunky_completion_history WHERE resource_id = ?", (resource.id,))
        actions = [r["action"] for r in cursor.fetchall()]
        assert "full_override" in actions
