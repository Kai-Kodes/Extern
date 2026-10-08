"""End-to-end simulation of the interactive application workflows."""

import io
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from todo.config import Config
from todo.ui.app import TodoApp


def test_e2e_full_workflow():
    """
    Test complete lifecycle:
    1. Open app -> see empty state -> quit
    2. Open app -> Add Task -> Manual -> Add to Universal list
    3. Open app -> Add Task -> Universal task -> select task -> adds to Today
    4. Open app -> Add Task -> Filemeta -> Add folder -> select course video -> adds to Today
    5. Open app -> Toggle tasks to complete -> Toggle back
    6. Reopen app in fresh instance -> verify all data persisted!
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "todo_e2e.db"
        course_dir = Path(tmpdir) / "Courses" / "Copywriting"
        course_dir.mkdir(parents=True)
        (course_dir / "01 - Understanding Desire.mp4").write_text("sample video 1")
        (course_dir / "02 - The Big Idea.mp4").write_text("sample video 2")

        cfg = Config(db_path=db_path, filemeta_command="filemeta")

        # Session 1: Launch and quit
        inputs_session1 = ["q\n"]
        with patch("sys.stdin", io.StringIO("".join(inputs_session1))):
            app = TodoApp(cfg)
            app.run()

        # Session 2: Create a universal task via Manual menu
        # + (Add task) -> 3 (Manual task) -> "Study DSA" -> 2 (Universal list) -> q (Quit)
        inputs_session2 = [
            "+\n",
            "3\n",
            "Study DSA\n",
            "2\n",
            "q\n",
        ]
        with patch("sys.stdin", io.StringIO("".join(inputs_session2))):
            app = TodoApp(cfg)
            app.run()

        # Verify universal task was created, but today is still empty
        from todo.services.task_service import TaskService
        from todo.services.daily_service import DailyService
        from datetime import datetime

        today_str = datetime.now().date().isoformat()
        db = app.db
        task_svc = TaskService(db)
        daily_svc = DailyService(db)

        univ_tasks = task_svc.get_universal_tasks()
        assert len(univ_tasks) == 1
        assert univ_tasks[0].title == "Study DSA"
        assert len(daily_svc.get_tasks_for_date(today_str)) == 0

        # Session 3: Add the universal task to today
        # + (Add task) -> 2 (Universal task) -> 1 (select "Study DSA") -> q (Quit)
        inputs_session3 = [
            "+\n",
            "2\n",
            "1\n",
            "q\n",
        ]
        with patch("sys.stdin", io.StringIO("".join(inputs_session3))):
            app = TodoApp(cfg)
            app.run()

        today_tasks = daily_svc.get_tasks_for_date(today_str)
        assert len(today_tasks) == 1
        assert today_tasks[0].task.title == "Study DSA"
        assert today_tasks[0].is_completed is False

        # Session 4: Add folder via Filemeta and select video
        # + (Add task) -> 1 (Filemeta) -> + (Add folder) -> path -> 1 (select 01 - Understanding Desire) -> q (Quit)
        inputs_session4 = [
            "+\n",
            "1\n",
            "+\n",
            f"{course_dir}\n",
            "1\n",
            "q\n",
        ]
        with patch("sys.stdin", io.StringIO("".join(inputs_session4))):
            app = TodoApp(cfg)
            app.run()

        today_tasks = daily_svc.get_tasks_for_date(today_str)
        assert len(today_tasks) == 2
        titles = [dt.task.title for dt in today_tasks]
        assert "Study DSA" in titles
        assert "01 - Understanding Desire" in titles

        # Session 5: Add manual task directly to today
        # + (Add task) -> 3 (Manual task) -> "Complete DBMS assignment" -> 1 (Today) -> q
        inputs_session5 = [
            "+\n",
            "3\n",
            "Complete DBMS assignment\n",
            "1\n",
            "q\n",
        ]
        with patch("sys.stdin", io.StringIO("".join(inputs_session5))):
            app = TodoApp(cfg)
            app.run()

        today_tasks = daily_svc.get_tasks_for_date(today_str)
        assert len(today_tasks) == 3

        # Session 6: Toggle tasks completion
        # Toggle task 1 (Study DSA) -> complete
        # Toggle task 2 -> complete
        # Toggle task 1 -> reopen
        # q
        inputs_session6 = [
            "1\n",
            "2\n",
            "1\n",
            "q\n",
        ]
        with patch("sys.stdin", io.StringIO("".join(inputs_session6))):
            app = TodoApp(cfg)
            app.run()

        today_tasks = daily_svc.get_tasks_for_date(today_str)
        assert today_tasks[0].is_completed is False
        assert today_tasks[1].is_completed is True

        # Session 7: Restart new instance and verify all data persists
        new_app = TodoApp(cfg)
        persisted_today = new_app.daily_service.get_tasks_for_date(today_str)
        assert len(persisted_today) == 3
        persisted_sources = new_app.source_service.get_sources()
        assert len(persisted_sources) == 1
        assert persisted_sources[0].name == "Copywriting"
        assert persisted_sources[0].item_count == 2
