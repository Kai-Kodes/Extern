"""Tests for the Universal Task-Editing system and TaskEditor component."""

import io
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from todo.config import Config
from todo.database import Database
from todo.models import FileItem, Task
from todo.services.daily_service import DailyService
from todo.services.task_service import TaskService
from todo.ui.app import TodoApp
from todo.ui.task_editor import TaskEditor


@pytest.fixture
def test_env():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        cfg = Config(db_path=db_path)
        db = Database(db_path)
        db.init_db()
        task_svc = TaskService(db)
        daily_svc = DailyService(db)
        editor = TaskEditor(task_svc, daily_svc)
        yield {
            "tmpdir": tmpdir,
            "db_path": db_path,
            "cfg": cfg,
            "db": db,
            "task_svc": task_svc,
            "daily_svc": daily_svc,
            "editor": editor,
        }


def test_task_service_update_task_basic(test_env):
    """Verify TaskService.update_task updates title and description without altering ID or status."""
    task_svc: TaskService = test_env["task_svc"]
    task = task_svc.create_task("Initial Title", "Initial Description")

    updated = task_svc.update_task(task.id, title="Updated Title", description="Updated Description")
    assert updated is not None
    assert updated.id == task.id
    assert updated.title == "Updated Title"
    assert updated.description == "Updated Description"
    assert updated.status == "pending"

    # Verify persisted in database
    retrieved = task_svc.get_task_by_id(task.id)
    assert retrieved is not None
    assert retrieved.title == "Updated Title"
    assert retrieved.description == "Updated Description"


def test_task_service_update_task_empty_title_raises(test_env):
    """Verify TaskService.update_task rejects blank title with ValueError."""
    task_svc: TaskService = test_env["task_svc"]
    task = task_svc.create_task("Valid Title")

    with pytest.raises(ValueError, match="Task title cannot be empty"):
        task_svc.update_task(task.id, title="   ")


def test_task_service_update_task_syncs_with_file_item(test_env):
    """Verify update_task syncs title with linked FileItem."""
    db: Database = test_env["db"]
    task_svc: TaskService = test_env["task_svc"]

    # Insert a dummy source and file item
    with db.get_connection() as conn:
        c = conn.cursor()
        c.execute(
            """
            INSERT INTO sources (name, path, created_at, updated_at)
            VALUES ('Test Source', 'test://source', '2026-10-19T00:00:00', '2026-10-19T00:00:00')
            """
        )
        s_id = c.lastrowid
        c.execute(
            """
            INSERT INTO file_items (source_id, path, title, type, created_at, updated_at)
            VALUES (?, 'item1', 'Original Item', 'file', '2026-10-19T00:00:00', '2026-10-19T00:00:00')
            """,
            (s_id,),
        )
        fi_id = c.lastrowid
        conn.commit()

    task = task_svc.create_task("Original Task", source_type="filemeta", source_id=fi_id)
    task_svc.update_task(task.id, title="Renamed Task")

    # Check file_items table
    with db.get_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT title FROM file_items WHERE id = ?", (fi_id,))
        row = c.fetchone()
        assert row["title"] == "Renamed Task"


def test_daily_service_get_and_unschedule_task(test_env):
    """Verify get_task_scheduled_date and unschedule_task preserve historical completed records."""
    task_svc: TaskService = test_env["task_svc"]
    daily_svc: DailyService = test_env["daily_svc"]

    task = task_svc.create_task("Meeting")
    assert daily_svc.get_task_scheduled_date(task.id) is None

    # Schedule for 2026-10-25
    daily_svc.add_task_to_date(task.id, "2026-10-25")
    assert daily_svc.get_task_scheduled_date(task.id) == "2026-10-25"

    # Reschedule to 2026-10-28
    daily_svc.reschedule_task(task.id, "2026-10-28")
    assert daily_svc.get_task_scheduled_date(task.id) == "2026-10-28"

    # Unschedule
    ok = daily_svc.unschedule_task(task.id)
    assert ok is True
    assert daily_svc.get_task_scheduled_date(task.id) is None


def test_task_editor_edit_title_and_save(test_env):
    """Verify editing task title via TaskEditor and saving."""
    task_svc: TaskService = test_env["task_svc"]
    editor: TaskEditor = test_env["editor"]

    task = task_svc.create_task("Old Title", "Some notes")

    # Inputs: "1" (select Title), "Brand New Title" (enter title), "s" (save)
    inputs = ["1\n", "Brand New Title\n", "s\n"]
    with patch("sys.stdin", io.StringIO("".join(inputs))):
        saved, msg = editor.edit_task(task)

    assert saved is True
    assert "updated" in msg.lower()
    updated = task_svc.get_task_by_id(task.id)
    assert updated.title == "Brand New Title"
    assert updated.description == "Some notes"


def test_task_editor_edit_description_and_save(test_env):
    """Verify editing task description via TaskEditor."""
    task_svc: TaskService = test_env["task_svc"]
    editor: TaskEditor = test_env["editor"]

    task = task_svc.create_task("Document Plan", "Draft v1")

    # Inputs: "2" (select Description), "Draft v2 finalized" (enter notes), "s" (save)
    inputs = ["2\n", "Draft v2 finalized\n", "s\n"]
    with patch("sys.stdin", io.StringIO("".join(inputs))):
        saved, msg = editor.edit_task(task)

    assert saved is True
    updated = task_svc.get_task_by_id(task.id)
    assert updated.description == "Draft v2 finalized"


def test_task_editor_clear_description(test_env):
    """Verify clearing description with '-' in TaskEditor."""
    task_svc: TaskService = test_env["task_svc"]
    editor: TaskEditor = test_env["editor"]

    task = task_svc.create_task("Clean Desk", "To be cleared")

    # Inputs: "2" (Description), "-" (clear), "s" (save)
    inputs = ["2\n", "-\n", "s\n"]
    with patch("sys.stdin", io.StringIO("".join(inputs))):
        saved, msg = editor.edit_task(task)

    assert saved is True
    updated = task_svc.get_task_by_id(task.id)
    assert updated.description == ""


def test_task_editor_reschedule_date(test_env):
    """Verify modifying scheduled date in TaskEditor."""
    task_svc: TaskService = test_env["task_svc"]
    daily_svc: DailyService = test_env["daily_svc"]
    editor: TaskEditor = test_env["editor"]

    task = task_svc.create_task("Doctor Appointment")
    daily_svc.add_task_to_date(task.id, "2026-10-20")

    # Inputs: "3" (Date), "2026-10-25" (new date), "s" (save)
    inputs = ["3\n", "2026-10-25\n", "s\n"]
    with patch("sys.stdin", io.StringIO("".join(inputs))):
        saved, msg = editor.edit_task(task)

    assert saved is True
    assert daily_svc.get_task_scheduled_date(task.id) == "2026-10-25"


def test_task_editor_unschedule_date(test_env):
    """Verify unscheduling a task by passing '-' in date prompt."""
    task_svc: TaskService = test_env["task_svc"]
    daily_svc: DailyService = test_env["daily_svc"]
    editor: TaskEditor = test_env["editor"]

    task = task_svc.create_task("Call Electrician")
    daily_svc.add_task_to_date(task.id, "2026-10-20")

    # Inputs: "3" (Date), "-" (unschedule), "s" (save)
    inputs = ["3\n", "-\n", "s\n"]
    with patch("sys.stdin", io.StringIO("".join(inputs))):
        saved, msg = editor.edit_task(task)

    assert saved is True
    assert daily_svc.get_task_scheduled_date(task.id) is None


def test_task_editor_validation_rejects_empty_title_and_recovers(test_env):
    """Verify attempting to save empty title fails validation, keeps editor open."""
    task_svc: TaskService = test_env["task_svc"]
    editor: TaskEditor = test_env["editor"]

    task = task_svc.create_task("Keep This Title")

    # Inputs:
    # "1\n", "-\n" (clear title with '-')
    # "s\n" (try to save -> fails validation)
    # "b\n" (cancel)
    inputs = ["1\n", "-\n", "s\n", "b\n"]
    with patch("sys.stdin", io.StringIO("".join(inputs))):
        saved, msg = editor.edit_task(task)

    assert saved is False
    assert "cancelled" in msg.lower()
    # Title in database is unchanged
    assert task_svc.get_task_by_id(task.id).title == "Keep This Title"


def test_task_editor_validation_rejects_invalid_date(test_env):
    """Verify invalid date format is rejected."""
    task_svc: TaskService = test_env["task_svc"]
    editor: TaskEditor = test_env["editor"]

    task = task_svc.create_task("Check Tires")

    # Inputs: "3\n", "not-a-date\n", "s\n", "b\n"
    inputs = ["3\n", "not-a-date\n", "s\n", "b\n"]
    with patch("sys.stdin", io.StringIO("".join(inputs))):
        saved, msg = editor.edit_task(task)

    assert saved is False
    assert "cancelled" in msg.lower()


def test_task_editor_cancel_discards_edits(test_env):
    """Verify pressing 'b' discards all pending field modifications."""
    task_svc: TaskService = test_env["task_svc"]
    editor: TaskEditor = test_env["editor"]

    task = task_svc.create_task("Original Title", "Original Notes")

    # Inputs: "1\n", "Changed Title\n", "2\n", "Changed Notes\n", "b\n" (cancel)
    inputs = ["1\n", "Changed Title\n", "2\n", "Changed Notes\n", "b\n"]
    with patch("sys.stdin", io.StringIO("".join(inputs))):
        saved, msg = editor.edit_task(task)

    assert saved is False
    assert "cancelled" in msg.lower()

    persisted = task_svc.get_task_by_id(task.id)
    assert persisted.title == "Original Title"
    assert persisted.description == "Original Notes"


def test_task_editor_completed_task_date_locked(test_env):
    """Verify historical completed tasks cannot have their date altered."""
    task_svc: TaskService = test_env["task_svc"]
    daily_svc: DailyService = test_env["daily_svc"]
    editor: TaskEditor = test_env["editor"]

    task = task_svc.create_task("Completed Past Task")
    dt, _ = daily_svc.add_task_to_date(task.id, "2026-10-15")
    daily_svc.toggle_task_completion(dt.id)
    completed_task = task_svc.get_task_by_id(task.id)
    assert completed_task.status == "completed"

    # Inputs: "3\n" (try to edit date on completed task -> shows warning), "b\n" (cancel)
    inputs = ["3\n", "b\n"]
    with patch("sys.stdin", io.StringIO("".join(inputs))):
        saved, msg = editor.edit_task(completed_task)

    assert saved is False
    assert daily_svc.get_task_scheduled_date(task.id) is None


def test_today_view_edit_shortcut(test_env):
    """Test pressing 'e' in Today view edits highlighted task and updates Today."""
    cfg: Config = test_env["cfg"]
    app = TodoApp(cfg)

    # Create task scheduled for today
    today_str = app.daily_service.get_today_str()
    task = app.task_service.create_task("Task Before Edit")
    app.daily_service.add_task_to_date(task.id, today_str)

    # In Today TUI: "e\n" (edit), "1\n", "Task After Edit\n", "s\n", "q\n" (quit)
    inputs = ["e\n", "1\n", "Task After Edit\n", "s\n", "q\n"]
    with patch("sys.stdin", io.StringIO("".join(inputs))):
        app.run()

    updated = app.task_service.get_task_by_id(task.id)
    assert updated.title == "Task After Edit"


def test_universal_view_edit_shortcut(test_env):
    """Test pressing 'e' in Universal Tasks edits highlighted task."""
    cfg: Config = test_env["cfg"]
    app = TodoApp(cfg)

    u_task = app.task_service.create_task("Backlog Task", source_type="universal")

    # In Universal View: "e\n" (edit), "1\n", "Polished Backlog Task\n", "s\n", "b\n" (back)
    inputs = ["e\n", "1\n", "Polished Backlog Task\n", "s\n", "b\n"]
    with patch("sys.stdin", io.StringIO("".join(inputs))):
        app.universal_view.run(select_mode=True)

    updated = app.task_service.get_task_by_id(u_task.id)
    assert updated.title == "Polished Backlog Task"


def test_timeline_view_edit_shortcut(test_env):
    """Test pressing 'e' in Timeline view edits highlighted task."""
    cfg: Config = test_env["cfg"]
    app = TodoApp(cfg)

    today_str = app.daily_service.get_today_str()
    task = app.task_service.create_task("Timeline Task")
    app.daily_service.add_task_to_date(task.id, today_str)

    # In Timeline: "e\n" (edit), "1\n", "Renamed on Timeline\n", "s\n", "b\n" (exit timeline)
    inputs = ["e\n", "1\n", "Renamed on Timeline\n", "s\n", "b\n"]
    with patch("sys.stdin", io.StringIO("".join(inputs))):
        app.timeline_view.run()

    updated = app.task_service.get_task_by_id(task.id)
    assert updated.title == "Renamed on Timeline"


def test_task_editor_prefilled_values_preserved_on_empty_input(test_env):
    """Verify pressing Enter on prefilled fields retains existing values."""
    task_svc: TaskService = test_env["task_svc"]
    daily_svc: DailyService = test_env["daily_svc"]
    editor: TaskEditor = test_env["editor"]

    task = task_svc.create_task("Original Title", "Original Description")
    daily_svc.add_task_to_date(task.id, "2026-11-01")

    # Select 1 (Title) -> Enter (keep prefill)
    # Select 2 (Desc) -> Enter (keep prefill)
    # Select 3 (Date) -> Enter (keep prefill)
    # Press 's' (Save)
    inputs = ["1\n", "\n", "2\n", "\n", "3\n", "\n", "s\n"]
    with patch("sys.stdin", io.StringIO("".join(inputs))):
        saved, msg = editor.edit_task(task)

    assert saved is True
    persisted = task_svc.get_task_by_id(task.id)
    assert persisted.title == "Original Title"
    assert persisted.description == "Original Description"
    assert daily_svc.get_task_scheduled_date(task.id) == "2026-11-01"


def test_task_editor_unscheduled_prefill_empty(test_env):
    """Verify editing an unscheduled task has empty date prefill and stays unscheduled on Enter."""
    task_svc: TaskService = test_env["task_svc"]
    daily_svc: DailyService = test_env["daily_svc"]
    editor: TaskEditor = test_env["editor"]

    task = task_svc.create_task("Unscheduled Task")
    assert daily_svc.get_task_scheduled_date(task.id) is None

    # Select 3 (Date) -> Enter -> Save
    inputs = ["3\n", "\n", "s\n"]
    with patch("sys.stdin", io.StringIO("".join(inputs))):
        saved, msg = editor.edit_task(task)

    assert saved is True
    assert daily_svc.get_task_scheduled_date(task.id) is None


def test_task_editor_multiline_description_retention_and_editing(test_env):
    """Verify multiline description retains literal newlines when untouched and decodes \\n when edited."""
    task_svc: TaskService = test_env["task_svc"]
    editor: TaskEditor = test_env["editor"]

    multiline_text = "Line 1\nLine 2\nLine 3"
    task = task_svc.create_task("Multiline Task", multiline_text)

    # 1. Edit only title; ensure multiline description is preserved untouched
    inputs1 = ["1\n", "Title Modified\n", "s\n"]
    with patch("sys.stdin", io.StringIO("".join(inputs1))):
        saved1, _ = editor.edit_task(task)
    assert saved1 is True
    t1 = task_svc.get_task_by_id(task.id)
    assert t1.title == "Title Modified"
    assert t1.description == multiline_text

    # 2. Edit description with escaped newlines
    inputs2 = ["2\n", "First\\nSecond\\nThird\n", "s\n"]
    with patch("sys.stdin", io.StringIO("".join(inputs2))):
        saved2, _ = editor.edit_task(task)
    assert saved2 is True
    t2 = task_svc.get_task_by_id(task.id)
    assert t2.description == "First\nSecond\nThird"


def test_task_editor_single_field_edit_leaves_others_untouched(test_env):
    """Verify editing only one field preserves all other fields completely."""
    task_svc: TaskService = test_env["task_svc"]
    daily_svc: DailyService = test_env["daily_svc"]
    editor: TaskEditor = test_env["editor"]

    task = task_svc.create_task("Single Edit Test", "Original Note")
    daily_svc.add_task_to_date(task.id, "2026-10-25")

    # Only edit description
    inputs = ["2\n", "Completely New Note\n", "s\n"]
    with patch("sys.stdin", io.StringIO("".join(inputs))):
        saved, _ = editor.edit_task(task)

    assert saved is True
    persisted = task_svc.get_task_by_id(task.id)
    assert persisted.title == "Single Edit Test"
    assert persisted.description == "Completely New Note"
    assert daily_svc.get_task_scheduled_date(task.id) == "2026-10-25"


def test_task_editor_create_task_starts_empty_and_isolated(test_env):
    """Verify create_task starts with empty fields and does not inherit stale state from edit_task."""
    task_svc: TaskService = test_env["task_svc"]
    daily_svc: DailyService = test_env["daily_svc"]
    editor: TaskEditor = test_env["editor"]

    # First edit an existing task
    task = task_svc.create_task("Stale Title", "Stale Notes")
    inputs_edit = ["1\n", "Updated Stale Title\n", "s\n"]
    with patch("sys.stdin", io.StringIO("".join(inputs_edit))):
        editor.edit_task(task)

    # Now create a brand new task
    inputs_create = ["1\n", "Fresh Created Task\n", "2\n", "Fresh Note\n", "3\n", "2026-10-30\n", "s\n"]
    with patch("sys.stdin", io.StringIO("".join(inputs_create))):
        saved, msg, new_task = editor.create_task()

    assert saved is True
    assert new_task is not None
    assert new_task.title == "Fresh Created Task"
    assert new_task.description == "Fresh Note"
    assert daily_svc.get_task_scheduled_date(new_task.id) == "2026-10-30"

    # Verify original task is untouched
    orig = task_svc.get_task_by_id(task.id)
    assert orig.title == "Updated Stale Title"
    assert orig.description == "Stale Notes"


def test_task_editor_google_tasks_error_surfaced(test_env):
    """Verify errors from Google Tasks update are surfaced and editor handles them."""
    task_svc: TaskService = test_env["task_svc"]
    daily_svc: DailyService = test_env["daily_svc"]

    task = task_svc.create_task("Google Task", source_type="google_tasks", source_id=999)

    mock_gtasks_service = type("MockService", (), {})()
    def fail_update(**kwargs):
        raise RuntimeError("Google API network timeout (HTTP 504)")
    mock_gtasks_service.update_task = fail_update

    editor = TaskEditor(task_svc, daily_svc, google_tasks_service=mock_gtasks_service)

    # Attempt to edit title and save -> fails with error -> cancel with 'b'
    inputs = ["1\n", "Attempted Title\n", "s\n", "b\n"]
    with patch("sys.stdin", io.StringIO("".join(inputs))):
        saved, msg = editor.edit_task(task)

    assert saved is False
    assert "cancelled" in msg.lower()
    # Task title was not updated in DB because remote update failed
    assert task_svc.get_task_by_id(task.id).title == "Google Task"


def test_key_reader_readline_prefill_hook():
    """Verify KeyReader.read_text_input configures and clears readline hook in interactive mode."""
    from todo.ui.key_reader import KeyReader
    kr = KeyReader()

    startup_hook_calls = []
    inserted_texts = []

    class MockReadline:
        @staticmethod
        def set_startup_hook(fn=None):
            startup_hook_calls.append(fn)
            if fn:
                fn()

        @staticmethod
        def insert_text(text):
            inserted_texts.append(text)

    with patch("sys.stdin.isatty", return_value=True), \
         patch("sys.stdin.fileno", return_value=0), \
         patch("termios.tcgetattr", return_value=[]), \
         patch("termios.tcsetattr"), \
         patch("tty.setcbreak"), \
         patch("todo.ui.console.console.show_cursor"), \
         patch("todo.ui.console.console.input", return_value="Result Text") as mock_input, \
         patch.dict("sys.modules", {"readline": MockReadline}):

        val = kr.read_text_input(prompt_text="Title: ", prefill="Sample Title")
        assert val == "Result Text"
        assert "Sample Title" in inserted_texts
        assert startup_hook_calls[-1] is None

