"""Deterministic regression tests for persistent input prompt across all task-creation and editing flows."""

import io
import os
import pty
import select
import sys
import time
from unittest.mock import patch

import pytest

from todo.config import Config
from todo.database import Database
from todo.models import Task
from todo.services.daily_service import DailyService
from todo.services.task_service import TaskService
from todo.ui.console import console, render_readline_prompt
from todo.ui.key_reader import KeyReader, key_reader
from todo.ui.manual_view import ManualView
from todo.ui.task_editor import TaskEditor
from todo.ui.universal_view import UniversalView


def _run_in_pty(func, inputs):
    """
    Execute a callable in a real Linux pseudo-terminal (PTY) and capture
    all raw terminal output bytes across a sequence of inputs.
    """
    master, slave = pty.openpty()
    pid = os.fork()
    if pid == 0:
        os.close(master)
        os.setsid()
        os.dup2(slave, 0)
        os.dup2(slave, 1)
        os.dup2(slave, 2)
        os.close(slave)

        # Reset standard streams from duplicated fds to bypass pytest stream capture in child
        sys.stdin = open(0, "r", encoding="utf-8", closefd=False)
        sys.stdout = open(1, "w", encoding="utf-8", closefd=False)
        sys.stderr = open(2, "w", encoding="utf-8", closefd=False)
        sys.__stdin__ = sys.stdin
        sys.__stdout__ = sys.stdout
        sys.__stderr__ = sys.stderr

        from todo.ui.console import console
        console.file = sys.stdout

        try:
            func()
        finally:
            os._exit(0)
    else:
        os.close(slave)
        output = bytearray()
        try:
            import time
            for inp in inputs:
                time.sleep(0.05)
                while True:
                    r, _, _ = select.select([master], [], [], 0.05)
                    if not r:
                        break
                    try:
                        chunk = os.read(master, 2048)
                        if not chunk:
                            break
                        output.extend(chunk)
                    except OSError:
                        break
                os.write(master, inp)

            while True:
                r, _, _ = select.select([master], [], [], 0.3)
                if not r:
                    break
                try:
                    chunk = os.read(master, 2048)
                    if not chunk:
                        break
                    output.extend(chunk)
                except OSError:
                    break
        finally:
            os.close(master)
            os.waitpid(pid, 0)
        return bytes(output)


# =========================================================================
# 1. PTY-level Terminal Display & Cursor Tests
# =========================================================================


def test_prompt_visible_before_first_keystroke():
    """Verify prompt label is rendered immediately and cursor is positioned after it."""

    def child():
        res = key_reader.read_text_input("Task: > ")
        print(f"RESULT:{res}")

    raw = _run_in_pty(child, [b"\n"])
    assert b"Task: > " in raw


def test_prompt_persistent_after_character_insertion():
    """Verify typing characters does not overwrite or shift the prompt."""

    def child():
        res = key_reader.read_text_input("Task: > ")
        print(f"RESULT:{res}")

    raw = _run_in_pty(child, [b"Buy coffee\n"])
    assert b"Task: > Buy coffee" in raw
    assert b"RESULT:Buy coffee" in raw


def test_prompt_persistent_after_backspace():
    """Verify Backspace uses standard erase (\\x08 \\x08) and never erases the prompt label."""

    def child():
        res = key_reader.read_text_input("Task: > ")
        print(f"RESULT:{res}")

    # Type 'abc', backspace 1 char ('c'), type 'd', press Enter
    raw = _run_in_pty(child, [b"abc", b"\x7f", b"d\n"])
    # Should contain prompt and final value
    assert b"Task: > " in raw
    assert b"RESULT:abd" in raw
    # Backspace must NOT move to column 0 (\r \r was the bug signature)
    assert b"\r \r" not in raw


def test_prompt_persistent_on_empty_input_excessive_backspace():
    """Verify that pressing Backspace when input is empty rings bell (\\x07) and never moves into or erases prompt."""

    def child():
        res = key_reader.read_text_input("Task: > ")
        print(f"RESULT:{res}")

    # Send 25 backspaces immediately on empty buffer, then type 'Fresh'\n
    raw = _run_in_pty(child, [b"\x7f" * 25, b"Fresh\n"])
    assert b"Task: > " in raw
    assert b"RESULT:Fresh" in raw
    # Ensure \r \r never occurred
    assert b"\r \r" not in raw


def test_repeated_insertion_and_deletion_no_vertical_drift():
    """Verify repeated insertion and deletion on the same line preserves prompt and does not drift vertically."""

    def child():
        res = key_reader.read_text_input("Task: > ")
        print(f"RESULT:{res}")

    # 5 cycles of typing 4 chars and deleting 4 chars
    inputs = []
    for _ in range(5):
        inputs.append(b"abcd")
        inputs.append(b"\x7f\x7f\x7f\x7f")
    inputs.append(b"Permanent Task\n")

    raw = _run_in_pty(child, inputs)
    assert b"Task: > " in raw
    assert b"RESULT:Permanent Task" in raw
    assert b"\r \r" not in raw


def test_prefilled_text_editing_preserves_prompt():
    """Verify editing a prefilled value retains the prompt and allows modifying existing text."""

    def child():
        res = key_reader.read_text_input("Title: ", prefill="Existing Item")
        print(f"RESULT:{res}")

    # Prefill is 'Existing Item'. Backspace 4 times (removes 'Item'), type 'Task\n'
    raw = _run_in_pty(child, [b"\x7f\x7f\x7f\x7f", b"Task\n"])
    assert b"Title: Existing Item" in raw
    assert b"RESULT:Existing Task" in raw


def test_cursor_navigation_keys_stay_within_buffer():
    """Verify navigation keys (Ctrl+A Home, Left Arrow) stop at the prompt boundary."""

    def child():
        res = key_reader.read_text_input("Task: > ")
        print(f"RESULT:{res}")

    # Type 'world', Ctrl+A (Home), type 'hello ', Enter -> 'hello world'
    raw = _run_in_pty(child, [b"world", b"\x01", b"hello ", b"\n"])
    assert b"Task: > " in raw
    assert b"RESULT:hello world" in raw


# =========================================================================
# 2. Shared Prompt Rendering & Ignore Markers
# =========================================================================


def test_render_readline_prompt_plain_string():
    """Verify plain string prompts are returned cleanly without escape sequences."""
    res = render_readline_prompt(console, "Task: > ")
    assert res == "Task: > "


def test_render_readline_prompt_rich_markup():
    """Verify Rich markup prompts wrap ANSI escape codes with \\001 and \\002 readline ignore markers."""
    res = render_readline_prompt(console, "[bold cyan]Task: > [/bold cyan]")
    assert "\001\x1b[" in res
    assert "\002" in res
    assert "Task: > " in res


def test_render_readline_prompt_empty_and_none():
    """Verify empty or None prompts return empty string."""
    assert render_readline_prompt(console, "") == ""
    assert render_readline_prompt(console, None) == ""


# =========================================================================
# 3. View-Level Flow Audits (Manual, Universal, Editor, Cancellation)
# =========================================================================


@pytest.fixture
def app_env(tmp_path):
    db_path = tmp_path / "test_views.db"
    db = Database(db_path)
    db.init_db()
    task_svc = TaskService(db)
    daily_svc = DailyService(db)
    editor = TaskEditor(task_svc, daily_svc)
    return {
        "db": db,
        "task_svc": task_svc,
        "daily_svc": daily_svc,
        "editor": editor,
    }


def test_manual_view_cancellation_preserves_clean_state(app_env):
    """Verify entering 'b' or empty in ManualView cancels without creating a task."""
    task_svc: TaskService = app_env["task_svc"]
    daily_svc: DailyService = app_env["daily_svc"]
    view = ManualView(task_svc, daily_svc)

    with patch("sys.stdin", io.StringIO("b\n")):
        res = view.run()
        assert res is None

    with patch("sys.stdin", io.StringIO("\n")):
        res = view.run()
        assert res is None

    assert len(task_svc.get_universal_tasks()) == 0
    assert len(daily_svc.get_tasks_for_date(daily_svc.get_today_str())) == 0


def test_universal_view_task_creation_prompt_flow(app_env):
    """Verify UniversalView uses the shared prompt and persists the task on submission."""
    task_svc: TaskService = app_env["task_svc"]
    daily_svc: DailyService = app_env["daily_svc"]
    view = UniversalView(task_svc, daily_svc, task_editor=app_env["editor"])

    # User inputs: "+" (add task), "My New Universal Task\n", "q" (quit)
    inputs = ["+\n", "My New Universal Task\n", "q\n"]
    with patch("sys.stdin", io.StringIO("".join(inputs))):
        view.run(select_mode=False)

    tasks = task_svc.get_universal_tasks()
    assert len(tasks) == 1
    assert tasks[0].title == "My New Universal Task"


def test_task_editor_multiline_description_and_date(app_env):
    """Verify TaskEditor preserves multiline newlines (\\n) and scheduled dates across editing."""
    task_svc: TaskService = app_env["task_svc"]
    editor: TaskEditor = app_env["editor"]

    task = task_svc.create_task(title="Original", description="Line 1\nLine 2", source_type="manual")

    # Inputs: "1\n" (edit title), "New Title\n", "2\n" (edit desc), "Line A\\nLine B\n", "3\n" (edit date), "2026-10-31\n", "s\n"
    inputs = [
        "1\n",
        "New Title\n",
        "2\n",
        "Line A\\nLine B\n",
        "3\n",
        "2026-10-31\n",
        "s\n",
    ]
    with patch("sys.stdin", io.StringIO("".join(inputs))):
        saved, msg = editor.edit_task(task)

    assert saved is True
    updated = task_svc.get_task_by_id(task.id)
    assert updated.title == "New Title"
    assert updated.description == "Line A\nLine B"
    assert app_env["daily_svc"].get_task_scheduled_date(task.id) == "2026-10-31"


def test_non_tty_input_and_eof():
    """Verify non-TTY stdin (pipelines, test mocks) continues to read lines and raises EOFError when closed."""
    kr = KeyReader()

    with patch("sys.stdin", io.StringIO("Piped Task\n")):
        val = kr.read_text_input("Task: > ")
        assert val == "Piped Task"

    with patch("sys.stdin", io.StringIO("")):
        with pytest.raises(EOFError):
            kr.read_text_input("Task: > ")
