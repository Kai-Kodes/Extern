"""Regression and unit tests for Chunky Tasks completion lifecycle semantics.

Verifies:
1. Completing one chunk task does not mark the parent FileItem or chunky resource completed.
2. 46-minute video with 20-minute chunk size progress progression:
   - Chunk 1: [0, 1200] -> coverage 1200/2760 (~43.5%), resource incomplete, FileItem incomplete.
   - Next proposal: [1200, 2400].
   - Chunk 2: [1200, 2400] -> coverage 2400/2760 (~87.0%), resource incomplete, FileItem incomplete.
   - Next proposal: [2400, 2760].
   - Chunk 3: [2400, 2760] -> coverage 2760/2760 (100%), resource complete, FileItem complete.
   - Next proposal: None.
3. Reopening a completed chunk removes its interval from current coverage, reopens parent FileItem,
   and preserves immutable completion history.
4. TaskService.set_task_status respects chunk task boundaries.
5. Explicitly completing the original resource via importer library still triggers full-completion override.
6. Telegram and Filemeta resources retain identical correct semantics.
7. Edge cases: empty resources, overlapping intervals, repeated completions.
"""

from datetime import datetime
from pathlib import Path
import pytest

from todo.database import Database
from todo.integrations.filemeta import FilemetaScanResult, ScannedFile
from todo.models import ChunkyResource, ChunkyTask, FileItem, Task
from todo.services.chunky_service import ChunkyService, calculate_coverage, propose_next_chunk
from todo.services.daily_service import DailyService
from todo.services.source_service import SourceService
from todo.services.task_service import TaskService


@pytest.fixture
def env(tmp_path: Path):
    db_file = tmp_path / "test_semantics.db"
    db = Database(db_file)
    db.init_db()

    now = datetime(2026, 10, 23, 10, 0, 0)
    date_provider = lambda: now

    daily_svc = DailyService(db, date_provider=date_provider)
    task_svc = TaskService(db, date_provider=date_provider)
    source_svc = SourceService(db, date_provider=date_provider)
    chunky_svc = ChunkyService(db, date_provider=date_provider)

    return {
        "db": db,
        "daily_svc": daily_svc,
        "task_svc": task_svc,
        "source_svc": source_svc,
        "chunky_svc": chunky_svc,
        "date_provider": date_provider,
    }


def test_46_min_video_exact_scenario_via_today(env):
    """
    Reproduces the exact reported scenario:
    - 46-minute video (2760.0 seconds) imported as a YouTube FileItem.
    - User creates 20-minute chunk (1200s).
    - User completes chunk in Today view.
    - Resource MUST remain partially completed (1200/2760s, ~43%).
    - FileItem MUST remain incomplete.
    - Next chunk MUST be proposed at [1200, 2400].
    - Second chunk completes -> [1200, 2400] -> 2400/2760s (~87%).
    - Final chunk completes -> [2400, 2760] -> 2760/2760s (100%).
    - Parent FileItem completes only upon 100% coverage.
    """
    daily_svc: DailyService = env["daily_svc"]
    task_svc: TaskService = env["task_svc"]
    source_svc: SourceService = env["source_svc"]
    chunky_svc: ChunkyService = env["chunky_svc"]

    # 1. Create YouTube source and 46-minute video (2760s)
    scan_res = FilemetaScanResult(
        source_name="Single Videos",
        source_path="youtube://videos",
        items=[
            ScannedFile(
                name="vid_46m",
                path="youtube://video/vid_46m",
                title="Deep Work Lecture (46 mins)",
                type="video",
                metadata={"video_id": "vid_46m", "duration": 2760, "source_type": "youtube"},
            )
        ],
    )
    source, _, _ = source_svc.add_or_update_source(scan_res)
    file_item = source_svc.get_items_by_source(source.id)[0]
    assert file_item.completed is False

    # 2. Register chunky resource
    resource = chunky_svc.get_or_create_resource(
        source_type="youtube",
        canonical_uri="youtube://video/vid_46m",
        title=file_item.title,
        total_range=2760.0,
        range_unit="seconds",
        file_item_id=file_item.id,
    )
    assert resource.is_fully_completed is False
    assert resource.completed_range == 0.0

    # -------------------------------------------------------------------------
    # CHUNK 1: [0, 1200]
    # -------------------------------------------------------------------------
    next_1 = propose_next_chunk(
        chunky_svc.get_completed_ranges(resource.id),
        resource.total_range,
        chunk_size=1200.0,
    )
    assert next_1 == (0.0, 1200.0)

    task_1, chunk_1 = chunky_svc.create_chunk_task(
        resource=resource,
        start_pos=next_1[0],
        end_pos=next_1[1],
        task_service=task_svc,
    )
    daily_svc.add_task_to_date(task_1.id, "2026-10-23")
    today_items = daily_svc.get_tasks_for_date("2026-10-23")
    assert len(today_items) == 1
    daily_1_id = today_items[0].id

    # Complete Chunk 1 via Today view
    daily_svc.toggle_task_completion(daily_1_id)

    # ASSERTIONS AFTER CHUNK 1
    # a) Parent file item MUST NOT be completed!
    refreshed_fi = source_svc.get_item_by_id(file_item.id)
    assert refreshed_fi.completed is False

    # b) Chunky resource must be partially completed (1200/2760 = 43.48% -> 43%)
    res_1 = chunky_svc.get_resource_by_id(resource.id)
    assert res_1.is_fully_completed is False
    assert res_1.fully_completed_at is None
    assert res_1.completed_range == 1200.0
    assert res_1.progress_pct == 43

    # c) Chunky task 1 is completed
    chunks_1 = chunky_svc.get_chunks_for_resource(resource.id)
    assert chunks_1[0].is_completed is True

    # -------------------------------------------------------------------------
    # CHUNK 2: [1200, 2400]
    # -------------------------------------------------------------------------
    next_2 = propose_next_chunk(
        chunky_svc.get_completed_ranges(resource.id),
        resource.total_range,
        chunk_size=1200.0,
    )
    assert next_2 == (1200.0, 2400.0)

    task_2, chunk_2 = chunky_svc.create_chunk_task(
        resource=resource,
        start_pos=next_2[0],
        end_pos=next_2[1],
        task_service=task_svc,
    )
    daily_svc.add_task_to_date(task_2.id, "2026-10-23")
    daily_2_id = [t.id for t in daily_svc.get_tasks_for_date("2026-10-23") if t.task_id == task_2.id][0]

    # Complete Chunk 2 via Today view
    daily_svc.toggle_task_completion(daily_2_id)

    # ASSERTIONS AFTER CHUNK 2
    refreshed_fi = source_svc.get_item_by_id(file_item.id)
    assert refreshed_fi.completed is False

    res_2 = chunky_svc.get_resource_by_id(resource.id)
    assert res_2.is_fully_completed is False
    assert res_2.completed_range == 2400.0
    assert res_2.progress_pct == 86  # 2400 / 2760 = 86.95% -> 86%

    # -------------------------------------------------------------------------
    # CHUNK 3: [2400, 2760] (Final remaining 6 mins)
    # -------------------------------------------------------------------------
    next_3 = propose_next_chunk(
        chunky_svc.get_completed_ranges(resource.id),
        resource.total_range,
        chunk_size=1200.0,
    )
    assert next_3 == (2400.0, 2760.0)

    task_3, chunk_3 = chunky_svc.create_chunk_task(
        resource=resource,
        start_pos=next_3[0],
        end_pos=next_3[1],
        task_service=task_svc,
    )
    daily_svc.add_task_to_date(task_3.id, "2026-10-23")
    daily_3_id = [t.id for t in daily_svc.get_tasks_for_date("2026-10-23") if t.task_id == task_3.id][0]

    # Complete Chunk 3 via Today view
    daily_svc.toggle_task_completion(daily_3_id)

    # ASSERTIONS AFTER FINAL CHUNK (100% COVERAGE)
    res_3 = chunky_svc.get_resource_by_id(resource.id)
    assert res_3.is_fully_completed is True
    assert res_3.fully_completed_at is not None
    assert res_3.completed_range == 2760.0
    assert res_3.progress_pct == 100

    # Parent file item IS now marked complete because all 2760s are consumed
    refreshed_fi = source_svc.get_item_by_id(file_item.id)
    assert refreshed_fi.completed is True

    # No more chunks proposed
    next_none = propose_next_chunk(
        chunky_svc.get_completed_ranges(resource.id),
        resource.total_range,
        chunk_size=1200.0,
        is_fully_completed=res_3.is_fully_completed,
    )
    assert next_none is None


def test_reopening_chunk_recalculates_coverage_and_reopens_parent(env):
    """
    Reopening a chunk in Today:
    - Removes that chunk's interval from resource coverage.
    - Reopens the parent FileItem if it was completed.
    - Sets is_fully_completed = False on chunky_resources.
    - Proposes the reopened interval as the next gap.
    - Appends 'reopened' action to immutable history.
    """
    daily_svc: DailyService = env["daily_svc"]
    task_svc: TaskService = env["task_svc"]
    source_svc: SourceService = env["source_svc"]
    chunky_svc: ChunkyService = env["chunky_svc"]
    db: Database = env["db"]

    # 1. Setup 1800s video with 2 chunks of 900s
    scan_res = FilemetaScanResult(
        source_name="Single Videos",
        source_path="youtube://videos",
        items=[
            ScannedFile(
                name="vid_1800",
                path="youtube://video/vid_1800",
                title="Short Lecture",
                type="video",
                metadata={"video_id": "vid_1800", "duration": 1800, "source_type": "youtube"},
            )
        ],
    )
    source, _, _ = source_svc.add_or_update_source(scan_res)
    file_item = source_svc.get_items_by_source(source.id)[0]

    resource = chunky_svc.get_or_create_resource(
        source_type="youtube",
        canonical_uri="youtube://video/vid_1800",
        title=file_item.title,
        total_range=1800.0,
        file_item_id=file_item.id,
    )

    t1, c1 = chunky_svc.create_chunk_task(resource, 0.0, 900.0, task_service=task_svc)
    t2, c2 = chunky_svc.create_chunk_task(resource, 900.0, 1800.0, task_service=task_svc)
    daily_svc.add_task_to_date(t1.id, "2026-10-23")
    daily_svc.add_task_to_date(t2.id, "2026-10-23")
    d1_id = [t.id for t in daily_svc.get_tasks_for_date("2026-10-23") if t.task_id == t1.id][0]
    d2_id = [t.id for t in daily_svc.get_tasks_for_date("2026-10-23") if t.task_id == t2.id][0]

    # Complete both -> 100% complete
    daily_svc.toggle_task_completion(d1_id)
    daily_svc.toggle_task_completion(d2_id)

    res_full = chunky_svc.get_resource_by_id(resource.id)
    assert res_full.is_fully_completed is True
    assert res_full.completed_range == 1800.0
    assert source_svc.get_item_by_id(file_item.id).completed is True

    # 2. Reopen Chunk 2 via Today toggle
    daily_svc.toggle_task_completion(d2_id)

    res_reopened = chunky_svc.get_resource_by_id(resource.id)
    assert res_reopened.is_fully_completed is False
    assert res_reopened.fully_completed_at is None
    assert res_reopened.completed_range == 900.0
    assert res_reopened.progress_pct == 50

    # Parent FileItem must be reopened
    assert source_svc.get_item_by_id(file_item.id).completed is False

    # Propose next gap: starts at [900, 1800]
    next_gap = propose_next_chunk(
        chunky_svc.get_completed_ranges(resource.id),
        resource.total_range,
        chunk_size=900.0,
    )
    assert next_gap == (900.0, 1800.0)

    # Verify history records
    with db.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT action, task_id FROM chunky_completion_history WHERE resource_id = ? ORDER BY id ASC",
            (resource.id,),
        )
        actions = [(r["action"], r["task_id"]) for r in cursor.fetchall()]

    assert actions == [
        ("completed", t1.id),
        ("completed", t2.id),
        ("reopened", t2.id),
    ]


def test_task_service_set_task_status_chunk_completion(env):
    """
    Calling task_service.set_task_status directly respects chunk task boundaries:
    does not complete the parent FileItem or full chunky resource.
    """
    task_svc: TaskService = env["task_svc"]
    source_svc: SourceService = env["source_svc"]
    chunky_svc: ChunkyService = env["chunky_svc"]

    scan_res = FilemetaScanResult(
        source_name="Single Videos",
        source_path="youtube://videos",
        items=[
            ScannedFile(
                name="vid_direct",
                path="youtube://video/vid_direct",
                title="Direct Task Test",
                type="video",
                metadata={"video_id": "vid_direct", "duration": 3600, "source_type": "youtube"},
            )
        ],
    )
    source, _, _ = source_svc.add_or_update_source(scan_res)
    file_item = source_svc.get_items_by_source(source.id)[0]

    resource = chunky_svc.get_or_create_resource(
        source_type="youtube",
        canonical_uri="youtube://video/vid_direct",
        title=file_item.title,
        total_range=3600.0,
        file_item_id=file_item.id,
    )

    task, chunk = chunky_svc.create_chunk_task(resource, 0.0, 1200.0, task_service=task_svc)

    # Set chunk task completed via TaskService
    task_svc.update_task_status(task.id, "completed")

    assert source_svc.get_item_by_id(file_item.id).completed is False
    res = chunky_svc.get_resource_by_id(resource.id)
    assert res.is_fully_completed is False
    assert res.completed_range == 1200.0


def test_explicit_full_completion_override_from_library_still_works(env):
    """
    Explicitly completing the original resource in library view triggers full override:
    - Resource progress becomes 100%.
    - Outstanding chunk tasks transition to completed.
    - History records 'full_override'.
    """
    task_svc: TaskService = env["task_svc"]
    source_svc: SourceService = env["source_svc"]
    chunky_svc: ChunkyService = env["chunky_svc"]
    db: Database = env["db"]

    scan_res = FilemetaScanResult(
        source_name="Single Videos",
        source_path="youtube://videos",
        items=[
            ScannedFile(
                name="vid_override",
                path="youtube://video/vid_override",
                title="Override Test",
                type="video",
                metadata={"video_id": "vid_override", "duration": 3600, "source_type": "youtube"},
            )
        ],
    )
    source, _, _ = source_svc.add_or_update_source(scan_res)
    file_item = source_svc.get_items_by_source(source.id)[0]

    resource = chunky_svc.get_or_create_resource(
        source_type="youtube",
        canonical_uri="youtube://video/vid_override",
        title=file_item.title,
        total_range=3600.0,
        file_item_id=file_item.id,
    )

    t1, c1 = chunky_svc.create_chunk_task(resource, 0.0, 1200.0, task_service=task_svc)
    t2, c2 = chunky_svc.create_chunk_task(resource, 1200.0, 2400.0, task_service=task_svc)

    # User explicitly marks the FileItem complete in library view
    source_svc.toggle_item_completion(file_item.id)

    assert source_svc.get_item_by_id(file_item.id).completed is True
    res = chunky_svc.get_resource_by_id(resource.id)
    assert res.is_fully_completed is True
    assert res.completed_range == 3600.0
    assert res.progress_pct == 100

    # Outstanding chunk 2 transitioned to completed
    assert task_svc.get_task_by_id(t2.id).status == "completed"
    assert chunky_svc.get_chunks_for_resource(resource.id)[1].is_completed is True

    with db.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT action FROM chunky_completion_history WHERE resource_id = ? ORDER BY id ASC",
            (resource.id,),
        )
        actions = [r["action"] for r in cursor.fetchall()]
    assert "full_override" in actions


def test_telegram_and_filemeta_chunk_completion_semantics(env):
    """
    Ensures the strict chunk vs parent completion distinction holds
    for both Telegram channels and Filemeta PDFs.
    """
    daily_svc: DailyService = env["daily_svc"]
    task_svc: TaskService = env["task_svc"]
    source_svc: SourceService = env["source_svc"]
    chunky_svc: ChunkyService = env["chunky_svc"]

    # 1. Telegram video
    tg_scan = FilemetaScanResult(
        source_name="Math Channel",
        source_path="telegram://-100123",
        items=[
            ScannedFile(
                name="msg_42",
                path="telegram://-100123/42",
                title="Lesson 42: Differential Equations",
                type="video",
                metadata={"duration": 1800, "source_type": "telegram"},
            )
        ],
    )
    tg_source, _, _ = source_svc.add_or_update_source(tg_scan)
    tg_fi = source_svc.get_items_by_source(tg_source.id)[0]

    tg_res = chunky_svc.get_or_create_resource(
        source_type="telegram",
        canonical_uri="telegram://-100123/42",
        title=tg_fi.title,
        total_range=1800.0,
        range_unit="seconds",
        file_item_id=tg_fi.id,
    )
    tg_task, _ = chunky_svc.create_chunk_task(tg_res, 0.0, 600.0, task_service=task_svc)
    daily_svc.add_task_to_date(tg_task.id, "2026-10-23")
    tg_dt_id = daily_svc.get_tasks_for_date("2026-10-23")[0].id

    daily_svc.toggle_task_completion(tg_dt_id)

    assert source_svc.get_item_by_id(tg_fi.id).completed is False
    assert chunky_svc.get_resource_by_id(tg_res.id).is_fully_completed is False
    assert chunky_svc.get_resource_by_id(tg_res.id).completed_range == 600.0

    # 2. Filemeta PDF
    fm_scan = FilemetaScanResult(
        source_name="Books",
        source_path="/path/to/books",
        items=[
            ScannedFile(
                name="book.pdf",
                path="/path/to/books/book.pdf",
                title="Operating Systems Concepts",
                type="document",
                metadata={"page_count": 800, "source_type": "filemeta"},
            )
        ],
    )
    fm_source, _, _ = source_svc.add_or_update_source(fm_scan)
    fm_fi = source_svc.get_items_by_source(fm_source.id)[0]

    fm_res = chunky_svc.get_or_create_resource(
        source_type="filemeta",
        canonical_uri="/path/to/books/book.pdf",
        title=fm_fi.title,
        total_range=800.0,
        range_unit="pages",
        file_item_id=fm_fi.id,
    )
    fm_task, _ = chunky_svc.create_chunk_task(fm_res, 0.0, 50.0, task_service=task_svc)
    daily_svc.add_task_to_date(fm_task.id, "2026-10-23")
    fm_dt_id = [t.id for t in daily_svc.get_tasks_for_date("2026-10-23") if t.task_id == fm_task.id][0]

    daily_svc.toggle_task_completion(fm_dt_id)

    assert source_svc.get_item_by_id(fm_fi.id).completed is False
    assert chunky_svc.get_resource_by_id(fm_res.id).is_fully_completed is False
    assert chunky_svc.get_resource_by_id(fm_res.id).completed_range == 50.0


def test_empty_resources_overlapping_intervals_and_repeated_completion(env):
    """
    Verifies:
    - Empty resources (total_range <= 0) don't crash.
    - Overlapping intervals do not double-count coverage.
    - Repeated completion toggles on the same state are idempotent.
    """
    chunky_svc: ChunkyService = env["chunky_svc"]
    task_svc: TaskService = env["task_svc"]
    db: Database = env["db"]

    # 1. Zero total range
    covered_0, pct_0 = calculate_coverage([(0.0, 10.0)], total_range=0.0)
    assert covered_0 == 0.0
    assert pct_0 == 0
    assert propose_next_chunk([(0.0, 10.0)], total_range=0.0, chunk_size=10.0) is None

    # 2. Overlapping intervals
    res = chunky_svc.get_or_create_resource(
        source_type="filemeta",
        canonical_uri="/tmp/test_overlap",
        title="Overlap Test",
        total_range=1000.0,
    )
    t1, _ = chunky_svc.create_chunk_task(res, 0.0, 400.0, task_service=task_svc)
    t2, _ = chunky_svc.create_chunk_task(res, 200.0, 600.0, task_service=task_svc)

    chunky_svc.on_task_completion_changed(t1.id, is_completed=True)
    chunky_svc.on_task_completion_changed(t2.id, is_completed=True)

    # Range [0, 400] and [200, 600] merged is [0, 600] = 600.0 (NOT 800.0)
    res_merged = chunky_svc.get_resource_by_id(res.id)
    assert res_merged.completed_range == 600.0
    assert res_merged.progress_pct == 60

    # 3. Repeated completion (idempotence)
    chunky_svc.on_task_completion_changed(t1.id, is_completed=True)
    with db.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT count(*) as cnt FROM chunky_completion_history WHERE task_id = ?",
            (t1.id,),
        )
        assert cursor.fetchone()["cnt"] == 1  # No duplicate history entry
