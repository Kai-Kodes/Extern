"""Regression tests for Chunky Tasks 'Entire remaining resource' functionality."""

import pytest
from pathlib import Path

from todo.database import Database
from todo.integrations.filemeta import FilemetaScanResult, ScannedFile
from todo.models import ChunkyResource, FileItem, Source
from todo.services.chunky_service import (
    ChunkyService,
    get_remaining_intervals,
    propose_entire_remaining_chunk,
    propose_next_chunk,
    calculate_coverage,
)
from todo.services.daily_service import DailyService
from todo.services.source_service import SourceService
from todo.services.task_service import TaskService


@pytest.fixture
def temp_db(tmp_path: Path) -> Database:
    db_file = tmp_path / "test_todo.db"
    return Database(db_file)


@pytest.fixture
def chunky_service(temp_db: Database) -> ChunkyService:
    return ChunkyService(temp_db)


@pytest.fixture
def services(temp_db: Database, chunky_service: ChunkyService):
    task_svc = TaskService(temp_db)
    daily_svc = DailyService(temp_db)
    source_svc = SourceService(temp_db)
    return task_svc, daily_svc, source_svc, chunky_service


def test_entire_remaining_no_previous_chunks():
    """1. A resource with no previous chunks proposes the entire total range in one chunk."""
    total_range = 2800.0  # 46m 40s
    completed_intervals = []

    gaps = get_remaining_intervals(completed_intervals, total_range)
    assert gaps == [(0.0, 2800.0)]

    proposed = propose_entire_remaining_chunk(completed_intervals, total_range)
    assert proposed == (0.0, 2800.0)
    start_pos, end_pos = proposed
    assert (end_pos - start_pos) == 2800.0


def test_entire_remaining_partially_completed_resource():
    """2. A partially completed resource (e.g. 20m of 46m 40s) proposes exactly the remaining interval."""
    total_range = 2800.0  # 46m 40s
    completed_intervals = [(0.0, 1200.0)]  # 00:00:00–00:20:00

    gaps = get_remaining_intervals(completed_intervals, total_range)
    assert gaps == [(1200.0, 2800.0)]

    proposed = propose_entire_remaining_chunk(completed_intervals, total_range)
    assert proposed == (1200.0, 2800.0)
    start_pos, end_pos = proposed
    assert (end_pos - start_pos) == 1600.0  # 26m 40s


def test_entire_remaining_final_interval_shorter_than_preset():
    """3. A resource whose final interval is shorter than a normal preset covers the exact remaining tail."""
    total_range = 2800.0  # 46m 40s
    # Completed chunk 1 (0 to 20m) and chunk 2 (20m to 40m)
    completed_intervals = [(0.0, 1200.0), (1200.0, 2400.0)]

    gaps = get_remaining_intervals(completed_intervals, total_range)
    assert gaps == [(2400.0, 2800.0)]

    proposed = propose_entire_remaining_chunk(completed_intervals, total_range)
    assert proposed == (2400.0, 2800.0)
    start_pos, end_pos = proposed
    assert (end_pos - start_pos) == 400.0  # 6m 40s


def test_entire_remaining_multiple_gaps_does_not_bridge_completed_intervals():
    """4. A resource with multiple gaps safely proposes the earliest gap without bridging across completed intervals."""
    total_range = 2800.0
    # Middle section completed: 1000s to 1500s
    completed_intervals = [(1000.0, 1500.0)]

    gaps = get_remaining_intervals(completed_intervals, total_range)
    assert gaps == [(0.0, 1000.0), (1500.0, 2800.0)]

    # Proposing entire remaining must propose the earliest gap (0 to 1000), NOT bridge to 2800!
    proposed = propose_entire_remaining_chunk(completed_intervals, total_range)
    assert proposed == (0.0, 1000.0)

    # After completing the first gap, the next entire remaining proposal covers the second gap
    completed_intervals.append((0.0, 1000.0))
    gaps_after = get_remaining_intervals(completed_intervals, total_range)
    assert gaps_after == [(1500.0, 2800.0)]

    proposed_next = propose_entire_remaining_chunk(completed_intervals, total_range)
    assert proposed_next == (1500.0, 2800.0)


def test_entire_remaining_multiple_gaps_label_and_communication():
    """Verify that multiple gaps explicitly indicate they select the earliest contiguous gap."""
    total_range = 2800.0
    completed_intervals = [(1000.0, 1500.0)]
    gaps = get_remaining_intervals(completed_intervals, total_range)
    assert len(gaps) == 2
    first_gap_len = gaps[0][1] - gaps[0][0]
    assert first_gap_len == 1000.0
    # Earliest gap proposal
    prop = propose_entire_remaining_chunk(completed_intervals, total_range)
    assert prop == (0.0, 1000.0)
    # Coverage calculation from first gap + completed intervals does not bridge or overlap
    cov, pct = calculate_coverage(completed_intervals + [prop], total_range)
    assert cov == 1500.0  # 1000s (first gap) + 500s (middle)



def test_entire_remaining_fully_completed_resource():
    """5. A fully completed resource returns None."""
    total_range = 2800.0
    completed_intervals = [(0.0, 2800.0)]

    gaps = get_remaining_intervals(completed_intervals, total_range)
    assert gaps == []

    proposed = propose_entire_remaining_chunk(completed_intervals, total_range)
    assert proposed is None

    # Flagged as fully completed
    proposed_flagged = propose_entire_remaining_chunk([], total_range, is_fully_completed=True)
    assert proposed_flagged is None


def test_entire_remaining_reopened_chunk_restores_gap(services):
    """6. When a chunk task is reopened, its gap is restored and can be scheduled via Entire remaining resource."""
    task_svc, daily_svc, source_svc, chunky_svc = services

    # Set up source and file item
    scan_res = FilemetaScanResult(
        source_name="Single Videos",
        source_path="youtube://videos",
        items=[
            ScannedFile(
                name="test_vid",
                path="https://youtube.com/watch?v=test_vid",
                title="46m 40s Tutorial",
                type="video",
                metadata={"video_id": "test_vid", "duration": 2800, "source_type": "youtube"},
            )
        ],
    )
    source, _, _ = source_svc.add_or_update_source(scan_res)
    file_item = source_svc.get_items_by_source(source.id)[0]

    resource = chunky_svc.get_or_create_resource(
        source_type="youtube",
        canonical_uri="https://youtube.com/watch?v=test_vid",
        title="46m 40s Tutorial",
        total_range=2800.0,
        range_unit="seconds",
        file_item_id=file_item.id,
    )

    # Chunk 1: 0 to 1200
    task1, ctask1 = chunky_svc.create_chunk_task(
        resource=resource,
        start_pos=0.0,
        end_pos=1200.0,
        task_service=task_svc,
        task_title="Chunk 1",
    )
    dt1, _ = daily_svc.add_task_to_date(task1.id, "2026-10-24")
    daily_svc.toggle_task_completion(dt1.id)

    # Chunk 2 via Entire Remaining: 1200 to 2800
    proposed = chunky_svc.propose_entire_remaining_chunk(
        chunky_svc.get_completed_ranges(resource.id),
        resource.total_range,
        resource.is_fully_completed,
    )
    assert proposed == (1200.0, 2800.0)

    task2, ctask2 = chunky_svc.create_chunk_task(
        resource=resource,
        start_pos=proposed[0],
        end_pos=proposed[1],
        task_service=task_svc,
        task_title="Chunk 2 (Entire Remaining)",
    )
    dt2, _ = daily_svc.add_task_to_date(task2.id, "2026-10-24")
    daily_svc.toggle_task_completion(dt2.id)

    # Verify resource is now fully completed
    res_after = chunky_svc.get_resource_by_id(resource.id)
    assert res_after.is_fully_completed is True
    assert res_after.completed_range == 2800.0

    # No chunk available when fully completed
    assert chunky_svc.propose_entire_remaining_chunk(
        chunky_svc.get_completed_ranges(resource.id),
        res_after.total_range,
        res_after.is_fully_completed,
    ) is None

    # Now REOPEN Chunk 2
    daily_svc.toggle_task_completion(dt2.id)

    # Verify resource is no longer completed
    res_reopened = chunky_svc.get_resource_by_id(resource.id)
    assert res_reopened.is_fully_completed is False
    assert res_reopened.completed_range == 1200.0

    # Entire remaining resource is now available again for 1200 to 2800!
    proposed_again = chunky_svc.propose_entire_remaining_chunk(
        chunky_svc.get_completed_ranges(resource.id),
        res_reopened.total_range,
        res_reopened.is_fully_completed,
    )
    assert proposed_again == (1200.0, 2800.0)


def test_entire_remaining_paginated_document():
    """Document with pages proposes entire remaining pages."""
    total_pages = 100.0
    completed_intervals = [(0.0, 30.0)]  # pages 1-30 completed

    gaps = get_remaining_intervals(completed_intervals, total_pages)
    assert gaps == [(30.0, 100.0)]

    proposed = propose_entire_remaining_chunk(completed_intervals, total_pages)
    assert proposed == (30.0, 100.0)
    assert (proposed[1] - proposed[0]) == 70.0
