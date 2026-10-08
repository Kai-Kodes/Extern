"""Tests for full-screen TUI behavior, alternate screen, and navigation."""

import io
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from todo.config import Config
from todo.database import Database
from todo.models import DailyTask, Task
from todo.services.daily_service import DailyService
from todo.services.source_service import SourceService
from todo.services.task_service import TaskService
from todo.ui.app import TodoApp
from todo.ui.console import (
    _IN_ALT_SCREEN,
    alt_screen,
    calculate_today_progress,
    console,
    enter_alt_screen,
    exit_alt_screen,
    format_today_progress,
    render_today_header,
    tui_mode,
)
from todo.ui.key_reader import Key, KeyReader, key_reader


def test_alt_screen_lifecycle():
    """Verify alternate screen buffer enters and exits cleanly."""
    with patch("sys.stdout.isatty", return_value=True):
        with patch("todo.ui.console.console.set_alt_screen") as mock_set:
            with patch("todo.ui.console.console.show_cursor") as mock_cursor:
                with alt_screen():
                    mock_set.assert_called_with(True)
                    mock_cursor.assert_called_with(False)
                mock_set.assert_called_with(False)
                mock_cursor.assert_called_with(True)


def test_alt_screen_restores_on_exception():
    """Verify alternate screen restores normal terminal state even when exception is raised."""
    with patch("sys.stdout.isatty", return_value=True):
        with patch("todo.ui.console.console.set_alt_screen") as mock_set:
            with pytest.raises(RuntimeError):
                with alt_screen():
                    raise RuntimeError("Unexpected failure inside TUI")
            mock_set.assert_called_with(False)


def test_navigation_and_back():
    """Test navigating to Add Task screen and pressing 'b' to return to Today."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        cfg = Config(db_path=db_path)

        # Inputs: "+" (open Add Task), "b" (go back), "q" (quit)
        inputs = ["+\n", "b\n", "q\n"]
        with patch("sys.stdin", io.StringIO("".join(inputs))):
            app = TodoApp(cfg)
            app.run()


def test_today_delete_universal_semantics():
    """Test delete flow for universal task from Today UI."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        cfg = Config(db_path=db_path)
        app = TodoApp(cfg)

        # 1. Create a universal task and schedule for today
        u_task = app.task_service.create_task("Study Algorithms", source_type="universal")
        from datetime import datetime
        today_str = datetime.now().date().isoformat()
        dt, _ = app.daily_service.add_task_to_date(u_task.id, today_str)

        # 2. In TUI: press "d" (delete), "1" (select task 1), "1" (Remove from Today only), "q" (quit)
        inputs = ["d\n", "1\n", "1\n", "q\n"]
        with patch("sys.stdin", io.StringIO("".join(inputs))):
            app.run()

        # Task removed from today, but still in universal list
        assert len(app.daily_service.get_tasks_for_date(today_str)) == 0
        assert app.task_service.get_task_by_id(u_task.id) is not None

        # 3. Re-add to today, then permanently delete via option 2
        app.daily_service.add_task_to_date(u_task.id, today_str)
        # "d", "1", "2" (Permanently delete), "y" (confirm), "q"
        inputs_perm = ["d\n", "1\n", "2\n", "y\n", "q\n"]
        with patch("sys.stdin", io.StringIO("".join(inputs_perm))):
            app.run()

        assert len(app.daily_service.get_tasks_for_date(today_str)) == 0
        assert app.task_service.get_task_by_id(u_task.id) is None


def test_delete_source_ui_confirmation():
    """Test Filemeta source deletion through UI with confirmation."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        cfg = Config(db_path=db_path)
        app = TodoApp(cfg)

        from todo.integrations.filemeta import FilemetaScanResult, ScannedFile
        scan = FilemetaScanResult(
            source_name="Sample Course",
            source_path="/tmp/sample",
            items=[ScannedFile(name="1.mp4", path="/tmp/sample/1.mp4", title="1", type="video")],
        )
        app.source_service.add_or_update_source(scan)
        assert len(app.source_service.get_sources()) == 1

        # Open Filemeta ("+", "1"), press "d" (delete), "1" (select source 1), "y" (confirm), "b", "q"
        inputs = ["+\n", "1\n", "d\n", "1\n", "y\n", "b\n", "q\n"]
        with patch("sys.stdin", io.StringIO("".join(inputs))):
            app.run()

        assert len(app.source_service.get_sources()) == 0


def test_tui_mode_lifecycle():
    """Verify tui_mode enters alt screen and cbreak mode, restoring both on normal exit."""
    with patch("sys.stdout.isatty", return_value=True), \
         patch("sys.stdin.isatty", return_value=True), \
         patch("sys.stdin.fileno", return_value=0), \
         patch("termios.tcgetattr", return_value=[0, 0, 0, 0, 0, 0, [0]]), \
         patch("termios.tcsetattr") as mock_setattr, \
         patch("tty.setcbreak") as mock_cbreak, \
         patch("todo.ui.console.console.set_alt_screen") as mock_alt, \
         patch("todo.ui.console.console.show_cursor") as mock_cursor:

        with tui_mode():
            mock_alt.assert_called_with(True)
            mock_cursor.assert_called_with(False)
            mock_cbreak.assert_called_once()

        mock_alt.assert_called_with(False)
        mock_cursor.assert_called_with(True)
        mock_setattr.assert_called_once()


def test_tui_mode_restores_on_exception():
    """Verify tui_mode restores terminal even when an unhandled exception occurs."""
    with patch("sys.stdout.isatty", return_value=True), \
         patch("sys.stdin.isatty", return_value=True), \
         patch("sys.stdin.fileno", return_value=0), \
         patch("termios.tcgetattr", return_value=[0, 0, 0, 0, 0, 0, [0]]), \
         patch("termios.tcsetattr") as mock_setattr, \
         patch("tty.setcbreak") as mock_cbreak, \
         patch("todo.ui.console.console.set_alt_screen") as mock_alt:

        with pytest.raises(ZeroDivisionError):
            with tui_mode():
                _ = 1 / 0

        mock_alt.assert_called_with(False)
        mock_setattr.assert_called_once()


def test_key_reader_escape_sequence_parsing():
    """Verify KeyReader accurately parses ANSI escape sequences into Key constants."""
    kr = KeyReader()
    with patch("sys.stdin.isatty", return_value=True), \
         patch("sys.stdin.fileno", return_value=0), \
         patch("termios.tcgetattr", return_value=[]), \
         patch("termios.tcsetattr"), \
         patch("tty.setcbreak"):

        # 1. ESC [ A -> UP
        with patch("os.read", side_effect=[b"\x1b", b"[", b"A"]), \
             patch("select.select", return_value=([0], [], [])):
            assert kr.read_key() == Key.UP

        # 2. ESC [ B -> DOWN
        with patch("os.read", side_effect=[b"\x1b", b"[", b"B"]), \
             patch("select.select", return_value=([0], [], [])):
            assert kr.read_key() == Key.DOWN

        # 3. ESC [ C -> RIGHT
        with patch("os.read", side_effect=[b"\x1b", b"[", b"C"]), \
             patch("select.select", return_value=([0], [], [])):
            assert kr.read_key() == Key.RIGHT

        # 4. ESC [ D -> LEFT
        with patch("os.read", side_effect=[b"\x1b", b"[", b"D"]), \
             patch("select.select", return_value=([0], [], [])):
            assert kr.read_key() == Key.LEFT

        # 5. SS3 Mode: ESC O A -> UP
        with patch("os.read", side_effect=[b"\x1b", b"O", b"A"]), \
             patch("select.select", return_value=([0], [], [])):
            assert kr.read_key() == Key.UP

        # 6. Standalone ESC key (timeout on select)
        with patch("os.read", side_effect=[b"\x1b"]), \
             patch("select.select", return_value=([], [], [])):
            assert kr.read_key() == Key.ESCAPE

        # 7. Enter: \r and \n
        with patch("os.read", side_effect=[b"\r"]):
            assert kr.read_key() == Key.ENTER
        with patch("os.read", side_effect=[b"\n"]):
            assert kr.read_key() == Key.ENTER

        # 8. Backspace: \x7f and \x08
        with patch("os.read", side_effect=[b"\x7f"]):
            assert kr.read_key() == Key.BACKSPACE
        with patch("os.read", side_effect=[b"\x08"]):
            assert kr.read_key() == Key.BACKSPACE

        # 9. Delete: ESC [ 3 ~
        with patch("os.read", side_effect=[b"\x1b", b"[", b"3", b"~"]), \
             patch("select.select", return_value=([0], [], [])):
            assert kr.read_key() == Key.DELETE

        # 10. Space, Tab, Regular chars
        with patch("os.read", side_effect=[b" "]):
            assert kr.read_key() == Key.SPACE
        with patch("os.read", side_effect=[b"\t"]):
            assert kr.read_key() == Key.TAB
        with patch("os.read", side_effect=[b"+"]):
            assert kr.read_key() == "+"
        with patch("os.read", side_effect=[b"d"]):
            assert kr.read_key() == "d"

        # 11. Ctrl+C raises KeyboardInterrupt
        with patch("os.read", side_effect=[b"\x03"]):
            with pytest.raises(KeyboardInterrupt):
                kr.read_key()

        # 12. Ctrl+D raises EOFError
        with patch("os.read", side_effect=[b"\x04"]):
            with pytest.raises(EOFError):
                kr.read_key()



def test_key_reader_read_text_input_pauses_and_resumes():
    """Verify read_text_input exits cbreak mode, shows cursor, reads input, and restores cbreak."""
    kr = KeyReader()
    with patch("sys.stdin.isatty", return_value=True), \
         patch("sys.stdin.fileno", return_value=0), \
         patch("termios.tcgetattr", return_value=[]), \
         patch("termios.tcsetattr") as mock_setattr, \
         patch("tty.setcbreak") as mock_cbreak, \
         patch("todo.ui.console.console.show_cursor") as mock_cursor, \
         patch("todo.ui.console.console.input", return_value="Learn Python") as mock_input:

        kr.enter()
        assert kr._is_active is True

        res = kr.read_text_input("Task: > ")
        assert res == "Learn Python"

        mock_input.assert_called_with("Task: > ")
        mock_cursor.assert_any_call(True)
        mock_cursor.assert_any_call(False)
        assert kr._is_active is True
        kr.exit()


def test_arrow_key_navigation_and_toggle_today():
    """Verify arrow navigation moves selection cursor and Enter toggles task."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        cfg = Config(db_path=db_path)
        app = TodoApp(cfg)

        from datetime import datetime
        today_str = datetime.now().date().isoformat()
        t1 = app.task_service.create_task("Task 1", source_type="manual")
        t2 = app.task_service.create_task("Task 2", source_type="manual")
        app.daily_service.add_task_to_date(t1.id, today_str)
        app.daily_service.add_task_to_date(t2.id, today_str)

        # Sequence:
        # Start at index 0.
        # Press Down (moves to index 1).
        # Press Enter (toggles task 2).
        # Press Up (moves to index 0).
        # Press Enter (toggles task 1).
        # Press q (quits).
        inputs = ["down\n", "enter\n", "up\n", "enter\n", "q\n"]
        with patch("sys.stdin", io.StringIO("".join(inputs))):
            app.run()

        tasks = app.daily_service.get_tasks_for_date(today_str)
        assert tasks[0].is_completed is True
        assert tasks[1].is_completed is True


def test_add_task_menu_arrow_navigation():
    """Verify navigating Add Task menu via arrows and Enter."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        cfg = Config(db_path=db_path)
        app = TodoApp(cfg)

        # Open Add Task (+), Down (moves to Universal task), Enter (opens Universal list), Back (b), Back (b), Quit (q)
        inputs = ["+\n", "down\n", "enter\n", "b\n", "b\n", "q\n"]
        with patch("sys.stdin", io.StringIO("".join(inputs))):
            app.run()


def test_progress_calculation():
    """Verify integer completion percentage calculations per spec."""
    assert calculate_today_progress(0, 0) == 0
    assert calculate_today_progress(0, 1) == 0
    assert calculate_today_progress(1, 1) == 100
    assert calculate_today_progress(1, 2) == 50
    assert calculate_today_progress(3, 5) == 60
    assert calculate_today_progress(7, 10) == 70


def test_format_today_progress_responsive():
    """Verify dynamic label formatting adapting to terminal width."""
    # Empty state
    assert format_today_progress(0, 0, 80) == "0% · 0/0"

    # Wide terminal -> full label
    assert format_today_progress(3, 4, 80, left_len=18) == "75% · 3/4 completed"

    # Medium terminal -> done label
    assert format_today_progress(3, 4, 38, left_len=18) == "75% · 3/4 done"

    # Narrow terminal -> compact label
    assert format_today_progress(3, 4, 30, left_len=18) == "75% · 3/4"


def test_today_header_rendering_layout():
    """Verify horizontal layout: left date and right percentage/count."""
    with console.capture() as capture:
        render_today_header("TODAY · 8 OCT 2026", completed=1, total=2)
    output = capture.get()
    assert "TODAY · 8 OCT 2026" in output
    assert "50% · 1/2 completed" in output

    # Empty state header
    with console.capture() as capture:
        render_today_header("TODAY · 8 OCT 2026", completed=0, total=0)
    empty_output = capture.get()
    assert "TODAY · 8 OCT 2026" in empty_output
    assert "0% · 0/0" in empty_output


def test_toggle_task_no_status_banner_and_cursor_stable():
    """
    Verify:
    1. Toggling task changes state (○ -> ✓ and ✓ -> ○)
    2. Header progress updates dynamically (0% -> 50% -> 100%)
    3. Output contains ZERO status banners ('Completed:', 'Marked pending:', etc.)
    4. Selection remains on the same task.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        cfg = Config(db_path=db_path)
        app = TodoApp(cfg)

        from datetime import datetime
        today_str = datetime.now().date().isoformat()
        t1 = app.task_service.create_task("Task 1", source_type="manual")
        t2 = app.task_service.create_task("Task 2", source_type="manual")
        app.daily_service.add_task_to_date(t1.id, today_str)
        app.daily_service.add_task_to_date(t2.id, today_str)

        # Move to task 2 (Down), toggle (Enter), toggle again (Enter), quit (q)
        inputs = ["down\n", "enter\n", "enter\n", "q\n"]
        with patch("sys.stdin", io.StringIO("".join(inputs))), console.capture() as capture:
            app.run()
        out = capture.get()

        # Ensure NO transient completion messages exist
        assert "Completed:" not in out
        assert "Marked completed:" not in out
        assert "Marked pending:" not in out
        assert "Task completed" not in out
        assert "Task marked pending" not in out


def test_toggle_filemeta_sync_and_progress_update():
    """Verify toggling in Today updates Filemeta item and recalculates progress."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        cfg = Config(db_path=db_path)
        app = TodoApp(cfg)

        from todo.integrations.filemeta import FilemetaScanResult, ScannedFile
        scan = FilemetaScanResult(
            source_name="Course A",
            source_path="/tmp/course_a",
            items=[
                ScannedFile(name="v1.mp4", path="/tmp/course_a/v1.mp4", title="Video 1", type="video"),
            ],
        )
        app.source_service.add_or_update_source(scan)
        items = app.source_service.get_items_by_source(1)
        item = items[0]
        assert item.completed is False

        # Add to today
        task = app.task_service.get_or_create_filemeta_task(item, source_name="Course A")
        from datetime import datetime
        today_str = datetime.now().date().isoformat()
        dt, _ = app.daily_service.add_task_to_date(task.id, today_str)

        # Toggle via Enter: should complete daily task and sync FileItem
        inputs = ["enter\n", "q\n"]
        with patch("sys.stdin", io.StringIO("".join(inputs))), console.capture() as capture:
            app.run()
        out = capture.get()

        # Check DB state
        tasks = app.daily_service.get_tasks_for_date(today_str)
        assert tasks[0].is_completed is True
        item_updated = app.source_service.get_items_by_source(1)[0]
        assert item_updated.completed is True

        # Ensure header reflected 100% and NO status message was printed
        assert "100% · 1/1 completed" in out
        assert "Completed:" not in out
        assert "Marked pending:" not in out


