"""Tests for SourceService, TaskService, and DailyService."""

import tempfile
from datetime import datetime
from pathlib import Path

import pytest

from todo.database import Database
from todo.integrations.filemeta import FilemetaScanResult, ScannedFile
from todo.services.daily_service import DailyService
from todo.services.source_service import SourceService
from todo.services.task_service import TaskService


@pytest.fixture
def test_env():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "todo_test.db"
        db = Database(db_path)
        source_svc = SourceService(db)
        task_svc = TaskService(db)
        daily_svc = DailyService(db)
        yield {
            "db_path": db_path,
            "db": db,
            "source_svc": source_svc,
            "task_svc": task_svc,
            "daily_svc": daily_svc,
        }


def test_source_creation_and_duplicate_detection(test_env):
    source_svc = test_env["source_svc"]

    scan_result = FilemetaScanResult(
        source_name="Copywriting Course",
        source_path="/home/user/Courses/Copywriting",
        items=[
            ScannedFile(
                name="01-Intro.mp4",
                path="/home/user/Courses/Copywriting/01-Intro.mp4",
                title="01-Intro",
                type="video",
                metadata={"duration": 300},
            ),
            ScannedFile(
                name="02-Desire.mp4",
                path="/home/user/Courses/Copywriting/02-Desire.mp4",
                title="02-Desire",
                type="video",
                metadata={"duration": 450},
            ),
        ],
    )

    # First import
    source1, added1, updated1 = source_svc.add_or_update_source(scan_result)
    assert source1.id is not None
    assert source1.name == "Copywriting Course"
    assert source1.item_count == 2
    assert added1 == 2
    assert updated1 == 0

    # Duplicate import: same path, updated items
    scan_result_rescan = FilemetaScanResult(
        source_name="Copywriting Course (Renamed)",
        source_path="/home/user/Courses/Copywriting",
        items=[
            ScannedFile(
                name="01-Intro.mp4",
                path="/home/user/Courses/Copywriting/01-Intro.mp4",
                title="01-Intro",
                type="video",
                metadata={"duration": 305},
            ),
            ScannedFile(
                name="03-Headlines.mp4",
                path="/home/user/Courses/Copywriting/03-Headlines.mp4",
                title="03-Headlines",
                type="video",
                metadata={"duration": 600},
            ),
        ],
    )
    source2, added2, updated2 = source_svc.add_or_update_source(scan_result_rescan)
    assert source2.id == source1.id  # Must NOT duplicate source ID
    assert source2.name == "Copywriting Course (Renamed)"
    assert added2 == 1  # 03-Headlines added
    assert updated2 == 1  # 01-Intro updated

    sources = source_svc.get_sources()
    assert len(sources) == 1
    assert sources[0].item_count == 3  # 01-Intro, 02-Desire, 03-Headlines


def test_task_creation_and_universal_task(test_env):
    task_svc = test_env["task_svc"]
    daily_svc = test_env["daily_svc"]
    today_str = datetime.now().date().isoformat()

    # Create universal tasks
    t1 = task_svc.create_task("Learn React", source_type="universal")
    t2 = task_svc.create_task("Finish DBMS project", source_type="universal")

    assert t1.id is not None
    assert t1.source_type == "universal"

    universal_tasks = task_svc.get_universal_tasks()
    assert len(universal_tasks) == 2
    assert universal_tasks[0].title == "Learn React"
    assert universal_tasks[1].title == "Finish DBMS project"

    # Initially, today has no tasks
    today_tasks = daily_svc.get_tasks_for_date(today_str)
    assert len(today_tasks) == 0

    # Add universal task to today
    dt1, is_new1 = daily_svc.add_task_to_date(t1.id, today_str)
    assert is_new1 is True
    assert dt1.task_id == t1.id
    assert dt1.is_completed is False

    # Adding again should NOT duplicate
    dt1_repeat, is_new_repeat = daily_svc.add_task_to_date(t1.id, today_str)
    assert is_new_repeat is False
    assert dt1_repeat.id == dt1.id

    # Master universal task list still has exactly 2 tasks
    assert len(task_svc.get_universal_tasks()) == 2

    # Today now has 1 task
    today_tasks = daily_svc.get_tasks_for_date(today_str)
    assert len(today_tasks) == 1
    assert today_tasks[0].task.title == "Learn React"


def test_add_filemeta_item_to_today(test_env):
    source_svc = test_env["source_svc"]
    task_svc = test_env["task_svc"]
    daily_svc = test_env["daily_svc"]
    today_str = datetime.now().date().isoformat()

    # Import source
    scan_result = FilemetaScanResult(
        source_name="Copywriting Course",
        source_path="/tmp/course",
        items=[
            ScannedFile(
                name="Understanding Desire.mp4",
                path="/tmp/course/Understanding Desire.mp4",
                title="Understanding Desire",
                type="video",
                metadata={"duration": 1455, "duration_formatted": "24:15"},
            )
        ],
    )
    source, _, _ = source_svc.add_or_update_source(scan_result)
    items = source_svc.get_items_by_source(source.id)
    assert len(items) == 1
    file_item = items[0]

    # Convert filemeta item to task
    task = task_svc.get_or_create_filemeta_task(file_item, source.name)
    assert task.title == "Understanding Desire"
    assert task.source_type == "filemeta"
    assert task.source_id == file_item.id

    # Repeating get_or_create returns identical task entity
    task_repeat = task_svc.get_or_create_filemeta_task(file_item, source.name)
    assert task_repeat.id == task.id

    # Add to today
    dt, is_new = daily_svc.add_task_to_date(task.id, today_str)
    assert is_new is True

    today_tasks = daily_svc.get_tasks_for_date(today_str)
    assert len(today_tasks) == 1
    assert today_tasks[0].task.title == "Understanding Desire"


def test_completing_and_reopening_today_task(test_env):
    task_svc = test_env["task_svc"]
    daily_svc = test_env["daily_svc"]
    today_str = datetime.now().date().isoformat()

    task = task_svc.create_task("Finish MariaDB assignment", source_type="manual")
    dt, _ = daily_svc.add_task_to_date(task.id, today_str)

    assert dt.is_completed is False
    assert dt.completed_at is None

    # Complete task
    updated_dt = daily_svc.toggle_task_completion(dt.id)
    assert updated_dt.is_completed is True
    assert updated_dt.completed_at is not None
    assert updated_dt.task.status == "completed"

    # Reopen task
    reopened_dt = daily_svc.toggle_task_completion(dt.id)
    assert reopened_dt.is_completed is False
    assert reopened_dt.completed_at is None
    assert reopened_dt.task.status == "pending"


def test_data_persistence_across_restarts(test_env):
    db_path = test_env["db_path"]
    today_str = datetime.now().date().isoformat()

    # Step 1: Write data
    task = test_env["task_svc"].create_task("Study DSA", source_type="universal")
    test_env["daily_svc"].add_task_to_date(task.id, today_str)

    # Step 2: "Close" and create new database connection to same file
    new_db = Database(db_path)
    new_task_svc = TaskService(new_db)
    new_daily_svc = DailyService(new_db)

    # Step 3: Verify all data persisted
    universal = new_task_svc.get_universal_tasks()
    assert len(universal) == 1
    assert universal[0].title == "Study DSA"

    today_tasks = new_daily_svc.get_tasks_for_date(today_str)
    assert len(today_tasks) == 1
    assert today_tasks[0].task.title == "Study DSA"
