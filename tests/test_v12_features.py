"""Comprehensive tests for Todo CLI V1.2 features:
- Global silence on all successful actions (no banners)
- Fuzzy search in Filemeta items with ranking & tolerance
- Next unfinished item ('n') with wrap-around and search support
- Multi-select batch operations ('Space' / 'Enter') with duplicate protection
"""

import io
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from todo.config import Config
from todo.database import Database
from todo.integrations.filemeta import FilemetaScanResult, ScannedFile
from todo.models import FileItem
from todo.search import fuzzy_score, fuzzy_search
from todo.services.daily_service import DailyService
from todo.services.source_service import SourceService
from todo.services.task_service import TaskService
from todo.ui.app import TodoApp
from todo.ui.console import console
from todo.ui.filemeta_view import FilemetaView
from todo.ui.key_reader import Key


# ============================================================================
# PART B: Fuzzy Search Tests
# ============================================================================


def test_fuzzy_search_ranking_and_tolerance():
    items = [
        FileItem(id=1, source_id=1, path="/p/1", title="01 - Introduction", completed=True),
        FileItem(id=2, source_id=1, path="/p/2", title="02 - Understanding Desire", completed=True),
        FileItem(id=3, source_id=1, path="/p/3", title="03 - The Big Idea", completed=False),
        FileItem(id=4, source_id=1, path="/p/4", title="04 - Writing Headlines", completed=False),
        FileItem(id=5, source_id=1, path="/p/5", title="05 - Psychology of Headlines", completed=False),
        FileItem(id=6, source_id=1, path="/p/6", title="06 - Creating Desire", completed=False),
        FileItem(id=7, source_id=1, path="/p/7", title="07 - Desire-Based Headlines", completed=False),
    ]

    # 1. Searching 'desire' finds all 3 desire items
    res_desire = fuzzy_search(items, "desire")
    titles_desire = [r.title for r in res_desire]
    assert len(titles_desire) == 3
    assert "02 - Understanding Desire" in titles_desire
    assert "06 - Creating Desire" in titles_desire
    assert "07 - Desire-Based Headlines" in titles_desire
    # Best prefix match ranked first
    assert titles_desire[0] == "07 - Desire-Based Headlines"

    # 2. Searching 'head psy' (multi-token, space-tolerant) finds 'Psychology of Headlines'
    res_head_psy = fuzzy_search(items, "head psy")
    assert len(res_head_psy) == 1
    assert res_head_psy[0].id == 5
    assert res_head_psy[0].title == "05 - Psychology of Headlines"

    # 3. Empty query returns all items in original order
    res_empty = fuzzy_search(items, "")
    assert len(res_empty) == len(items)
    assert [r.id for r in res_empty] == [r.id for r in items]

    res_spaces = fuzzy_search(items, "   ")
    assert len(res_spaces) == len(items)

    # 4. No matches returns empty list
    res_none = fuzzy_search(items, "xyznonexistentquery999")
    assert len(res_none) == 0

    # 5. Search preserves FileItem IDs and completion states
    for r in res_desire:
        orig = next(it for it in items if it.id == r.id)
        assert r.completed == orig.completed
        assert r.path == orig.path


# ============================================================================
# PART C: Next Unfinished Item ('n') Tests
# ============================================================================


def test_filemeta_next_unfinished_navigation():
    """Verify 'n' moves between unfinished items, wraps around, and preserves state."""
    items = [
        FileItem(id=1, source_id=1, path="/1", title="Lesson 1", completed=True),
        FileItem(id=2, source_id=1, path="/2", title="Lesson 2", completed=True),
        FileItem(id=3, source_id=1, path="/3", title="Lesson 3", completed=False),
        FileItem(id=4, source_id=1, path="/4", title="Lesson 4", completed=False),
        FileItem(id=5, source_id=1, path="/5", title="Lesson 5", completed=False),
    ]

    # Active list has unfinished items at index 2, 3, 4
    unfinished_indices = [i for i, it in enumerate(items) if not it.completed]
    assert unfinished_indices == [2, 3, 4]

    # Starting at index 0 (Lesson 1) -> next unfinished is index 2 (Lesson 3)
    curr = 0
    next_idx = next((i for i in unfinished_indices if i > curr), unfinished_indices[0])
    assert next_idx == 2

    # From index 2 -> next is index 3 (Lesson 4)
    curr = 2
    next_idx = next((i for i in unfinished_indices if i > curr), unfinished_indices[0])
    assert next_idx == 3

    # From index 3 -> next is index 4 (Lesson 5)
    curr = 3
    next_idx = next((i for i in unfinished_indices if i > curr), unfinished_indices[0])
    assert next_idx == 4

    # From index 4 (last) -> wraps around to index 2 (Lesson 3)
    curr = 4
    next_idx = next((i for i in unfinished_indices if i > curr), unfinished_indices[0])
    assert next_idx == 2


def test_filemeta_next_unfinished_all_complete_silent():
    """When all items are completed, pressing 'n' does nothing and produces no banner."""
    items = [
        FileItem(id=1, source_id=1, path="/1", title="Lesson 1", completed=True),
        FileItem(id=2, source_id=1, path="/2", title="Lesson 2", completed=True),
    ]
    unfinished_indices = [i for i, it in enumerate(items) if not it.completed]
    assert len(unfinished_indices) == 0


# ============================================================================
# PART D: Multi-Select Tests
# ============================================================================


def test_multi_select_batch_add_to_today_and_duplicate_protection():
    """Verify selecting multiple items with Space and pressing Enter adds them to Today."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        cfg = Config(db_path=db_path)
        app = TodoApp(cfg)

        scan = FilemetaScanResult(
            source_name="Copywriting Course",
            source_path="/tmp/courses/copywriting",
            items=[
                ScannedFile(name="1.mp4", path="/p/1", title="01 - Intro", type="video"),
                ScannedFile(name="2.mp4", path="/p/2", title="02 - Desire", type="video"),
                ScannedFile(name="3.mp4", path="/p/3", title="03 - Offer", type="video"),
            ],
        )
        src, _, _ = app.source_service.add_or_update_source(scan)

        today_str = datetime.now().date().isoformat()
        # Item 1 is already in today before the batch operation
        t1 = app.task_service.get_or_create_filemeta_task(
            app.source_service.get_items_by_source(src.id)[0], source_name=src.name
        )
        app.daily_service.add_task_to_date(t1.id, today_str)
        assert len(app.daily_service.get_tasks_for_date(today_str)) == 1

        # Now launch source view:
        # Navigate: "+" (Add task) -> "1" (Filemeta) -> "1" (Course 1)
        # Select items: "space" (select 1), "down", "space" (select 2), "down", "space" (select 3)
        # Enter: batch add
        # q: quit
        inputs = [
            "+\n", "1\n", "1\n",
            "space\n", "down\n", "space\n", "down\n", "space\n",
            "enter\n",
            "q\n",
        ]

        with patch("sys.stdin", io.StringIO("".join(inputs))), console.capture() as capture:
            app.run()
        out = capture.get()

        today_tasks = app.daily_service.get_tasks_for_date(today_str)
        # All 3 items are present in today
        assert len(today_tasks) == 3
        titles = [dt.task.title for dt in today_tasks]
        assert "01 - Intro" in titles
        assert "02 - Desire" in titles
        assert "03 - Offer" in titles

        # Duplicate protection verified: 01 - Intro was NOT duplicated!
        assert titles.count("01 - Intro") == 1

        # GLOBAL SILENCE: zero success banners
        assert "Added 3 tasks" not in out
        assert "Added to today" not in out
        assert "Successfully" not in out


def test_multi_select_and_search_integration():
    """Verify multi-select works while searching and preserves stable item IDs."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        cfg = Config(db_path=db_path)
        app = TodoApp(cfg)

        scan = FilemetaScanResult(
            source_name="Linux Course",
            source_path="/tmp/courses/linux",
            items=[
                ScannedFile(name="1.mp4", path="/p/1", title="01 - Intro", type="video"),
                ScannedFile(name="2.mp4", path="/p/2", title="02 - Networking", type="video"),
                ScannedFile(name="3.mp4", path="/p/3", title="03 - Advanced Networking", type="video"),
            ],
        )
        src, _, _ = app.source_service.add_or_update_source(scan)
        today_str = datetime.now().date().isoformat()

        # Open course, search "network", select result with Space, press Enter
        inputs = [
            "+\n", "1\n", "1\n",
            "/\n", "network\n",
            "space\n",
            "enter\n",
            "q\n",
        ]

        with patch("sys.stdin", io.StringIO("".join(inputs))), console.capture() as capture:
            app.run()
        out = capture.get()

        today_tasks = app.daily_service.get_tasks_for_date(today_str)
        assert len(today_tasks) == 1
        assert "Networking" in today_tasks[0].task.title
        assert "Added" not in out


# ============================================================================
# PART A: Global Silence Tests
# ============================================================================


def test_global_silence_on_source_deletion():
    """Deleting a Filemeta source must produce ZERO success message."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        cfg = Config(db_path=db_path)
        app = TodoApp(cfg)

        scan = FilemetaScanResult(
            source_name="Source to Delete",
            source_path="/tmp/courses/del",
            items=[ScannedFile(name="1.mp4", path="/p/1", title="1", type="video")],
        )
        app.source_service.add_or_update_source(scan)
        assert len(app.source_service.get_sources()) == 1

        # Delete source: "+" -> "1" (Filemeta) -> "d" -> "1" -> "y" (confirm) -> "b" -> "q"
        inputs = ["+\n", "1\n", "d\n", "1\n", "y\n", "b\n", "q\n"]
        with patch("sys.stdin", io.StringIO("".join(inputs))), console.capture() as capture:
            app.run()
        out = capture.get()

        assert len(app.source_service.get_sources()) == 0
        assert "Deleted source" not in out
        assert "Real files remain untouched" not in out
        assert "Deleted" not in out
        assert "Successfully" not in out


def test_global_silence_on_universal_task_operations():
    """Creating and deleting universal tasks must produce ZERO success messages."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        cfg = Config(db_path=db_path)
        app = TodoApp(cfg)

        # 1. Create universal task: "u" -> "+" -> "Learn Rust" -> "b" -> "q"
        inputs_create = ["u\n", "+\n", "Learn Rust\n", "b\n", "q\n"]
        with patch("sys.stdin", io.StringIO("".join(inputs_create))), console.capture() as capture:
            app.run()
        out_create = capture.get()

        assert "Created universal task" not in out_create
        assert "Created" not in out_create
        assert len(app.task_service.get_universal_tasks()) == 1

        # 2. Delete universal task: "u" -> "d" -> "1" -> "y" -> "b" -> "q"
        inputs_delete = ["u\n", "d\n", "1\n", "y\n", "b\n", "q\n"]
        with patch("sys.stdin", io.StringIO("".join(inputs_delete))), console.capture() as capture:
            app.run()
        out_delete = capture.get()

        assert "Deleted universal task" not in out_delete
        assert "Deleted" not in out_delete
        assert len(app.task_service.get_universal_tasks()) == 0


def test_global_silence_on_manual_task_creation():
    """Creating manual tasks for Today or Universal list must produce ZERO success messages."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        cfg = Config(db_path=db_path)
        app = TodoApp(cfg)

        # "+" -> "3" (Manual) -> "Buy groceries" -> "1" (Today) -> "q"
        inputs = ["+\n", "3\n", "Buy groceries\n", "1\n", "q\n"]
        with patch("sys.stdin", io.StringIO("".join(inputs))), console.capture() as capture:
            app.run()
        out = capture.get()

        today_str = datetime.now().date().isoformat()
        assert len(app.daily_service.get_tasks_for_date(today_str)) == 1
        assert "Added" not in out
        assert "Successfully" not in out


def test_filemeta_toggle_and_rescan_silent():
    """Toggling item completion and rescanning must produce ZERO success banners."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        cfg = Config(db_path=db_path)
        app = TodoApp(cfg)

        scan = FilemetaScanResult(
            source_name="Course A",
            source_path="/tmp/courses/a",
            items=[
                ScannedFile(name="1.mp4", path="/p/1", title="01 - Intro", type="video"),
            ],
        )
        src, _, _ = app.source_service.add_or_update_source(scan)

        # 1. Toggle completion: "+" -> "1" -> "1" -> "t" -> "b" -> "b" -> "q"
        inputs = ["+\n", "1\n", "1\n", "t\n", "b\n", "b\n", "q\n"]
        with patch("sys.stdin", io.StringIO("".join(inputs))), console.capture() as capture:
            app.run()
        out = capture.get()

        item = app.source_service.get_items_by_source(src.id)[0]
        assert item.completed is True
        assert "Completed" not in out
        assert "Marked pending" not in out

        # 2. Rescan: "+" -> "1" -> "1" -> "r" -> "b" -> "b" -> "q"
        inputs_rescan = ["+\n", "1\n", "1\n", "r\n", "b\n", "b\n", "q\n"]
        with patch("sys.stdin", io.StringIO("".join(inputs_rescan))), \
             patch.object(app.filemeta_adapter, "scan_folder", return_value=scan), \
             console.capture() as capture:
            app.run()
        out_rescan = capture.get()

        assert "Rescanned" not in out_rescan
        assert "updated" not in out_rescan
        assert "added" not in out_rescan


def test_multi_select_space_deselect():
    """Pressing Space twice on an item toggles selection on and off."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        cfg = Config(db_path=db_path)
        app = TodoApp(cfg)

        scan = FilemetaScanResult(
            source_name="Course B",
            source_path="/tmp/courses/b",
            items=[
                ScannedFile(name="1.mp4", path="/p/1", title="01 - Intro", type="video"),
                ScannedFile(name="2.mp4", path="/p/2", title="02 - Next", type="video"),
            ],
        )
        src, _, _ = app.source_service.add_or_update_source(scan)
        today_str = datetime.now().date().isoformat()

        # Item 1: space (selected), space (deselected) -> down -> Item 2: space (selected) -> enter (add) -> q
        inputs = [
            "+\n", "1\n", "1\n",
            "space\n", "space\n",
            "down\n", "space\n",
            "enter\n",
            "q\n",
        ]
        with patch("sys.stdin", io.StringIO("".join(inputs))):
            app.run()

        today_tasks = app.daily_service.get_tasks_for_date(today_str)
        # Only item 2 should be in today!
        assert len(today_tasks) == 1
        assert today_tasks[0].task.title == "02 - Next"


def test_filemeta_header_progress_independent_of_search():
    """Course header progress reflects all course items, unaffected by search filtering."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        cfg = Config(db_path=db_path)
        app = TodoApp(cfg)

        scan = FilemetaScanResult(
            source_name="Python Mastery",
            source_path="/tmp/courses/python",
            items=[
                ScannedFile(name="1.mp4", path="/p/1", title="01 - Basics", type="video"),
                ScannedFile(name="2.mp4", path="/p/2", title="02 - Advanced", type="video"),
            ],
        )
        src, _, _ = app.source_service.add_or_update_source(scan)
        # Mark 1 completed
        app.source_service.toggle_item_completion(app.source_service.get_items_by_source(src.id)[0].id)

        # Open course, search "Advanced" (only 1 item in search results)
        inputs = [
            "+\n", "1\n", "1\n",
            "/\n", "Advanced\n",
            "escape\n",
            "b\n", "b\n", "q\n",
        ]
        with patch("sys.stdin", io.StringIO("".join(inputs))), console.capture() as capture:
            app.run()
        out = capture.get()

        # Header progress must be 50% · 1/2 (not 0% · 0/1 from search filter)
        assert "50% · 1/2" in out
        assert "completed" not in out
        assert "done" not in out

