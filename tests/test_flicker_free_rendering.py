"""Tests for zero-flicker rendering pipeline, atomic frame buffering, and synchronized terminal output."""

import io
from unittest.mock import MagicMock, PropertyMock, patch

import pytest
from rich.console import Console, ConsoleDimensions

from todo.ui.console import (
    ViewportManager,
    clear_viewport,
    commit_viewport,
    console,
    exit_alt_screen,
    read_key,
    viewport_manager,
)
from todo.ui.key_reader import KeyReader


def test_viewport_manager_atomic_frame_buffering_and_synchronized_output():
    """Verify that ViewportManager buffers frames and outputs synchronized in-place overwrites."""
    buffer = io.StringIO()
    test_console = Console(file=buffer, force_terminal=True, color_system="standard", width=80)

    mgr = ViewportManager(test_console)
    mgr._force_active = True

    # Begin frame capture
    mgr.begin_frame()
    assert mgr._capturing is True

    # Print frame contents
    test_console.print("HEADER ROW", highlight=False)
    test_console.print("CONTENT ROW 1", highlight=False)
    test_console.print("FOOTER ROW", highlight=False)

    # Nothing should be written to the underlying file yet (buffered in memory)
    assert buffer.getvalue() == ""

    # Commit frame
    mgr.commit_frame()
    assert mgr._capturing is False

    output = buffer.getvalue()
    # 1. Must use DEC Mode 2026 Synchronized Output
    assert output.startswith("\x1b[?2026h")
    assert output.endswith("\x1b[?2026l")

    # 2. Must reposition cursor to home (\x1b[H) rather than erasing screen (\x1b[2J)
    assert "\x1b[H" in output
    assert "\x1b[2J" not in output

    # 3. Must clear to end of line (\x1b[K) and clear trailing rows below view (\x1b[J)
    assert "\x1b[K" in output
    assert "\x1b[J" in output
    assert "HEADER ROW" in output
    assert "CONTENT ROW 1" in output
    assert "FOOTER ROW" in output


def test_clear_viewport_eliminates_destructive_screen_wipe():
    """Verify that clear_viewport does not emit \x1b[2J when atomic viewport management is active."""
    buffer = io.StringIO()
    with patch("sys.stdout", buffer), patch.object(viewport_manager, "_force_active", True):
        clear_viewport()
        console.print("Today View Header", highlight=False)
        commit_viewport()

        output = buffer.getvalue()
        assert "\x1b[2J" not in output
        assert "\x1b[?2026h\x1b[H" in output
        assert "Today View Header" in output


def test_hook_triggers_commit_before_read_key():
    """Verify that read_key automatically commits any pending buffered frame before reading input."""
    buffer = io.StringIO()
    with patch("sys.stdout", buffer), patch.object(viewport_manager, "_force_active", True):
        clear_viewport()
        console.print("Waiting for keypress...", highlight=False)
        assert viewport_manager._capturing is True

        # When read_key is called, before_read hook commits the frame
        with patch("sys.stdin", io.StringIO("q\n")):
            key = read_key()
            assert key == "q"

        assert viewport_manager._capturing is False
        output = buffer.getvalue()
        assert "Waiting for keypress..." in output
        assert "\x1b[?2026h" in output
        assert "\x1b[?2026l" in output


def test_hook_triggers_commit_before_read_text_input():
    """Verify that read_text_input automatically commits any pending buffered frame before reading line input."""
    buffer = io.StringIO()
    with patch("sys.stdout", buffer), patch.object(viewport_manager, "_force_active", True):
        clear_viewport()
        console.print("New Task Form Screen", highlight=False)
        assert viewport_manager._capturing is True

        from todo.ui.key_reader import key_reader

        with patch("sys.stdin", io.StringIO("Buy milk\n")):
            text = key_reader.read_text_input("Title: ")
            assert text == "Buy milk"

        assert viewport_manager._capturing is False
        output = buffer.getvalue()
        assert "New Task Form Screen" in output


def test_outer_console_capture_is_not_disturbed():
    """Verify that existing unit tests using with console.capture(): are completely unaffected."""
    with console.capture() as cap:
        clear_viewport()
        console.print("Sample Captured Output", highlight=False)
        commit_viewport()

    assert "Sample Captured Output" in cap.get()


def test_consecutive_clear_viewport_discards_stale_frame():
    """Verify that rapid consecutive clear_viewport calls discard stale frames without corrupting state."""
    buffer = io.StringIO()
    with patch("sys.stdout", buffer), patch.object(viewport_manager, "_force_active", True):
        clear_viewport()
        console.print("Stale Initial Frame", highlight=False)

        # Second clear without commit
        clear_viewport()
        console.print("Fresh Active Frame", highlight=False)

        commit_viewport()
        output = buffer.getvalue()

        assert "Fresh Active Frame" in output
        assert "Stale Initial Frame" not in output


def test_empty_frame_clears_screen_without_2J():
    """Verify that committing an empty frame outputs in-place clear without emitting \x1b[2J."""
    buffer = io.StringIO()
    with patch("sys.stdout", buffer), patch.object(viewport_manager, "_force_active", True):
        clear_viewport()
        commit_viewport()

        output = buffer.getvalue()
        assert output == "\x1b[?2026h\x1b[H\x1b[J\x1b[?2026l"
        assert "\x1b[2J" not in output


def test_exit_alt_screen_discards_pending_capture():
    """Verify that exit_alt_screen safely discards any in-flight frame capture."""
    with patch.object(viewport_manager, "_force_active", True):
        clear_viewport()
        assert viewport_manager._capturing is True

        exit_alt_screen()
        assert viewport_manager._capturing is False


def test_resize_detection_in_main_loop(tmp_path):
    """Verify that TodoApp._main_loop detects terminal resize and triggers a redraw."""
    from todo.config import Config
    from todo.database import Database
    from todo.ui.app import TodoApp

    db_path = tmp_path / "test_app.db"
    db = Database(db_path)
    db.init_db()
    cfg = Config(db_path=db_path)

    app = TodoApp(cfg)

    call_count = 0
    current_size = ConsoleDimensions(80, 24)

    def mock_read_key(timeout=None):
        nonlocal call_count, current_size
        call_count += 1
        if call_count == 1:
            # Simulate resize event on first idle poll
            current_size = ConsoleDimensions(120, 40)
            return None
        return "q"

    def dynamic_size():
        return current_size

    with patch("todo.ui.app.read_key", side_effect=mock_read_key), \
         patch.object(Console, "size", new_callable=PropertyMock, side_effect=dynamic_size), \
         patch.object(app.today_view, "render") as mock_render:

        app._main_loop()

        # Should render at least twice: initial startup + resize trigger
        assert mock_render.call_count >= 2
