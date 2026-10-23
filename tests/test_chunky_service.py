"""Tests for ChunkyService: resource management, chunk lifecycle, completion sync, and full-resource completion."""

from datetime import datetime
from pathlib import Path
import pytest

from todo.database import Database
from todo.models import FileItem
from todo.services.chunky_service import ChunkyService, propose_next_chunk
from todo.services.source_service import SourceService
from todo.services.task_service import TaskService


@pytest.fixture
def chunky_env(tmp_path: Path):
    db_path = tmp_path / "test_chunky.db"
    db = Database(db_path)
    db.init_db()

    task_svc = TaskService(db)
    source_svc = SourceService(db)
    chunky_svc = ChunkyService(db)

    # Create dummy source and file items
    with db.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO sources (name, path, canonical_uri, created_at, updated_at)
            VALUES ('YouTube Course', 'youtube://playlist/PL123', 'https://youtube.com/playlist?list=PL123', '2026-10-23T10:00:00', '2026-10-23T10:00:00')
            """
        )
        src_id = cursor.lastrowid
        cursor.execute(
            """
            INSERT INTO file_items (source_id, path, relative_path, title, type, metadata, completed, created_at, updated_at)
            VALUES (?, 'youtube://video/vid_abc', 'vid_abc', 'Lesson 1: Deep Dive', 'video', '{"duration": 5400}', 0, '2026-10-23T10:00:00', '2026-10-23T10:00:00')
            """,
            (src_id,),
        )
        fi_id = cursor.lastrowid
        conn.commit()

    return {
        "db": db,
        "task_svc": task_svc,
        "source_svc": source_svc,
        "chunky_svc": chunky_svc,
        "fi_id": fi_id,
        "src_id": src_id,
    }


def test_get_or_create_resource_and_idempotency(chunky_env):
    chunky_svc: ChunkyService = chunky_env["chunky_svc"]
    fi_id: int = chunky_env["fi_id"]

    res1 = chunky_svc.get_or_create_resource(
        source_type="youtube",
        canonical_uri="youtube://video/vid_abc",
        title="Lesson 1: Deep Dive",
        total_range=5400.0,
        range_unit="seconds",
        file_item_id=fi_id,
    )
    assert res1.id is not None
    assert res1.total_range == 5400.0
    assert res1.completed_range == 0.0
    assert res1.is_fully_completed is False

    # Calling again returns same resource
    res2 = chunky_svc.get_or_create_resource(
        source_type="youtube",
        canonical_uri="youtube://video/vid_abc",
        title="Lesson 1: Deep Dive (Updated Title)",
        total_range=5400.0,
        range_unit="seconds",
        file_item_id=fi_id,
    )
    assert res2.id == res1.id
    assert res2.title == "Lesson 1: Deep Dive (Updated Title)"


def test_create_chunk_task_and_propose_next(chunky_env):
    chunky_svc: ChunkyService = chunky_env["chunky_svc"]
    task_svc: TaskService = chunky_env["task_svc"]
    fi_id: int = chunky_env["fi_id"]

    res = chunky_svc.get_or_create_resource(
        source_type="youtube",
        canonical_uri="youtube://video/vid_abc",
        title="Lesson 1",
        total_range=5400.0,
        range_unit="seconds",
        file_item_id=fi_id,
    )

    # 1. Propose first chunk of 20 min (1200s)
    next_range = propose_next_chunk(
        chunky_svc.get_completed_ranges(res.id), res.total_range, chunk_size=1200.0
    )
    assert next_range == (0.0, 1200.0)

    # Create chunk 1
    task1, chunk1 = chunky_svc.create_chunk_task(
        res, next_range[0], next_range[1], task_service=task_svc
    )
    assert task1.id is not None
    assert task1.title == "Lesson 1 [00:00:00–00:20:00]"
    assert "t=0s" in task1.description
    assert chunk1.chunk_index == 1
    assert chunk1.start_pos == 0.0
    assert chunk1.end_pos == 1200.0
    assert chunk1.is_completed is False

    # 2. Before chunk 1 is completed, propose_next_chunk still sees [0, 1200] as uncompleted gap
    # unless completed or filtered
    # When chunk 1 is completed:
    chunky_svc.on_task_completion_changed(task1.id, is_completed=True)

    # Resource completed_range is now 1200
    res_after = chunky_svc.get_resource_by_id(res.id)
    assert res_after.completed_range == 1200.0
    assert res_after.progress_pct == 22

    # Next chunk proposal starts from 1200
    comp_ranges = chunky_svc.get_completed_ranges(res.id)
    assert comp_ranges == [(0.0, 1200.0)]
    next_range_2 = propose_next_chunk(comp_ranges, res.total_range, chunk_size=1200.0)
    assert next_range_2 == (1200.0, 2400.0)

    # Create chunk 2
    task2, chunk2 = chunky_svc.create_chunk_task(
        res, next_range_2[0], next_range_2[1], task_service=task_svc
    )
    assert task2.title == "Lesson 1 [00:20:00–00:40:00]"
    assert "t=1200s" in task2.description
    assert chunk2.chunk_index == 2


def test_reopening_chunk_recalculates_coverage_and_preserves_history(chunky_env):
    chunky_svc: ChunkyService = chunky_env["chunky_svc"]
    task_svc: TaskService = chunky_env["task_svc"]
    fi_id: int = chunky_env["fi_id"]
    db: Database = chunky_env["db"]

    res = chunky_svc.get_or_create_resource(
        source_type="youtube",
        canonical_uri="youtube://video/vid_abc",
        title="Lesson 1",
        total_range=3600.0,
        file_item_id=fi_id,
    )

    t1, c1 = chunky_svc.create_chunk_task(res, 0.0, 1200.0, task_service=task_svc)
    t2, c2 = chunky_svc.create_chunk_task(res, 1200.0, 2400.0, task_service=task_svc)

    # Complete both
    chunky_svc.on_task_completion_changed(t1.id, is_completed=True)
    chunky_svc.on_task_completion_changed(t2.id, is_completed=True)

    res_updated = chunky_svc.get_resource_by_id(res.id)
    assert res_updated.completed_range == 2400.0

    # Reopen chunk 1
    chunky_svc.on_task_completion_changed(t1.id, is_completed=False)

    res_reopened = chunky_svc.get_resource_by_id(res.id)
    # Remaining completed chunk is 2 (1200 to 2400), length 1200
    assert res_reopened.completed_range == 1200.0

    # Verify history table contains records for completed and reopened
    with db.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT action, task_id FROM chunky_completion_history WHERE resource_id = ? ORDER BY id ASC",
            (res.id,),
        )
        history = [dict(r) for r in cursor.fetchall()]

    assert len(history) == 3
    assert history[0]["action"] == "completed" and history[0]["task_id"] == t1.id
    assert history[1]["action"] == "completed" and history[1]["task_id"] == t2.id
    assert history[2]["action"] == "reopened" and history[2]["task_id"] == t1.id


def test_full_resource_completion_override(chunky_env):
    """
    If a user completes the original imported resource through its normal workflow:
    - Resource progress is set to 100%.
    - Fully completed timestamp is recorded.
    - Outstanding chunk tasks transition to completed terminal state.
    - Historical completion records are preserved.
    - No new chunks can be proposed.
    """
    chunky_svc: ChunkyService = chunky_env["chunky_svc"]
    task_svc: TaskService = chunky_env["task_svc"]
    fi_id: int = chunky_env["fi_id"]
    db: Database = chunky_env["db"]

    res = chunky_svc.get_or_create_resource(
        source_type="youtube",
        canonical_uri="youtube://video/vid_abc",
        title="Lesson 1",
        total_range=3600.0,
        file_item_id=fi_id,
    )

    # Create chunk 1 [0, 1200] and chunk 2 [1200, 2400]
    t1, c1 = chunky_svc.create_chunk_task(res, 0.0, 1200.0, task_service=task_svc)
    t2, c2 = chunky_svc.create_chunk_task(res, 1200.0, 2400.0, task_service=task_svc)

    # Complete chunk 1, leave chunk 2 pending
    chunky_svc.on_task_completion_changed(t1.id, is_completed=True)

    # Now, complete the original resource normally via on_file_item_completion_changed
    chunky_svc.on_file_item_completion_changed(fi_id, is_completed=True)

    res_done = chunky_svc.get_resource_by_id(res.id)
    assert res_done.is_fully_completed is True
    assert res_done.fully_completed_at is not None
    assert res_done.completed_range == 3600.0
    assert res_done.progress_pct == 100

    # Outstanding chunk 2 should be in completed state
    t2_after = task_svc.get_task_by_id(t2.id)
    assert t2_after.status == "completed"

    chunks = chunky_svc.get_chunks_for_resource(res.id)
    for ch in chunks:
        assert ch.is_completed is True

    # No new chunk can be proposed
    assert (
        propose_next_chunk(
            chunky_svc.get_completed_ranges(res.id),
            res_done.total_range,
            chunk_size=1200.0,
            is_fully_completed=True,
        )
        is None
    )

    # Historical records preserved
    with db.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT action, task_id FROM chunky_completion_history WHERE resource_id = ? ORDER BY id ASC",
            (res.id,),
        )
        actions = [dict(r) for r in cursor.fetchall()]

    assert any(a["action"] == "full_override" and a["task_id"] == t2.id for a in actions)


def test_reopening_original_resource(chunky_env):
    """
    If a fully completed resource is reopened:
    - is_fully_completed is set to False.
    - Progress is recalculated from actually completed chunks.
    """
    chunky_svc: ChunkyService = chunky_env["chunky_svc"]
    task_svc: TaskService = chunky_env["task_svc"]
    fi_id: int = chunky_env["fi_id"]

    res = chunky_svc.get_or_create_resource(
        source_type="youtube",
        canonical_uri="youtube://video/vid_abc",
        title="Lesson 1",
        total_range=3600.0,
        file_item_id=fi_id,
    )

    t1, c1 = chunky_svc.create_chunk_task(res, 0.0, 1200.0, task_service=task_svc)
    chunky_svc.on_task_completion_changed(t1.id, is_completed=True)

    # Full completion
    chunky_svc.on_file_item_completion_changed(fi_id, is_completed=True)
    assert chunky_svc.get_resource_by_id(res.id).is_fully_completed is True

    # Explicit reopening of original resource
    chunky_svc.on_file_item_completion_changed(fi_id, is_completed=False)

    res_after = chunky_svc.get_resource_by_id(res.id)
    assert res_after.is_fully_completed is False
    assert res_after.fully_completed_at is None
    # Prior completed chunks still count
    assert res_after.completed_range == 1200.0
    assert res_after.progress_pct == 33
