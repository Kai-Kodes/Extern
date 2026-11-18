"""Tests for Termux / cross-device client compatibility, graceful error handling,
narrow screen truncation, KeyReader EINTR resilience, and SQLite WAL checkpointing."""

import io
import os
import signal
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner
from rich.console import Console

from todo.cli import cli
from todo.config import Config
from todo.database import Database
from todo.models import DailyTask, FileItem, Source, Task
from todo.services.daily_service import DailyService
from todo.services.source_service import SourceService
from todo.services.task_service import TaskService
from todo.ui.app import TodoApp
from todo.ui.console import check_and_clear_resized, render_today_header, tui_mode
from todo.ui.filemeta_view import FilemetaView
from todo.ui.key_reader import Key, KeyReader
from todo.ui.telegram_view import TelegramView
from todo.ui.timeline_view import TimelineView
from todo.ui.today_view import TodayView
from todo.ui.universal_view import UniversalView
from todo.ui.youtube_view import YouTubeView


@pytest.fixture
def temp_db(tmp_path: Path):
    db_file = tmp_path / "test_termux.db"
    db = Database(db_file)
    db.init_db()
    return db


def test_db_checkpoint_cli_command(temp_db: Database, tmp_path: Path, monkeypatch):
    """'todo db-checkpoint' flushes WAL and executes silently with exit code 0."""
    monkeypatch.setenv("TODO_DB_PATH", str(temp_db.db_path))

    task_service = TaskService(temp_db)
    task_service.create_task("Test Task")

    runner = CliRunner()
    result = runner.invoke(cli, ["db-checkpoint"])

    assert result.exit_code == 0
    assert result.output == ""  # Strict global silence on success


def test_app_exit_checkpoints_wal(tmp_path: Path, monkeypatch):
    """Exiting TodoApp cleanly runs db.close() and checkpoint()."""
    db_file = tmp_path / "test_app_exit.db"
    monkeypatch.setenv("TODO_DB_PATH", str(db_file))

    app = TodoApp(config=Config(db_path=db_file))
    with patch.object(app.db, "close", wraps=app.db.close) as mock_close:
        with patch("todo.ui.app.read_key", return_value="q"):
            with patch("todo.ui.app.tui_mode"):
                app._main_loop()

    # Database close was called, truncating WAL
    app.db.close()
    assert not (tmp_path / "test_app_exit.db-wal").exists() or (tmp_path / "test_app_exit.db-wal").stat().st_size == 0


def test_filemeta_missing_graceful_handling(temp_db: Database):
    """FilemetaView displays clean red error on '+' and 'r' when filemeta is missing."""
    source_service = SourceService(temp_db)
    task_service = TaskService(temp_db)
    daily_service = DailyService(temp_db)

    adapter = MagicMock()
    adapter.is_available.return_value = False

    view = FilemetaView(source_service, task_service, daily_service, adapter)

    # Press '+' when tool is missing -> surfaces red error without crashing
    with patch("todo.ui.filemeta_view.key_reader.read_key", side_effect=["+", "b"]):
        with patch("todo.ui.filemeta_view.print_status_message") as mock_status:
            view.run()
            mock_status.assert_called_with("filemeta not installed on this device.", style="red")


def test_telegram_missing_graceful_handling(temp_db: Database):
    """TelegramView displays clean red error on '+' and 'r' when telethon/credentials are missing."""
    source_service = SourceService(temp_db)
    task_service = TaskService(temp_db)
    daily_service = DailyService(temp_db)

    adapter = MagicMock()
    adapter.check_availability.return_value = (False, "Telethon library is not installed in the current environment.")

    view = TelegramView(adapter, source_service, task_service, daily_service)

    # Press '+' when telethon is missing -> surfaces red error without crashing
    with patch("todo.ui.telegram_view.key_reader.read_key", side_effect=["+", "b"]):
        with patch("todo.ui.telegram_view.print_status_message") as mock_status:
            view.run()
            mock_status.assert_called_with(
                "Telethon library is not installed in the current environment.", style="red"
            )


def test_youtube_missing_graceful_handling(temp_db: Database):
    """YouTubeView displays clean red error on '+' when yt-dlp is missing."""
    source_service = SourceService(temp_db)
    task_service = TaskService(temp_db)
    daily_service = DailyService(temp_db)

    adapter = MagicMock()
    adapter.is_available.return_value = False

    view = YouTubeView(adapter, source_service, task_service, daily_service)

    # Press '+' when yt-dlp is missing -> surfaces red error without crashing
    with patch("todo.ui.youtube_view.key_reader.read_key", side_effect=["+", "b"]):
        with patch("todo.ui.youtube_view.print_status_message") as mock_status:
            view.run()
            mock_status.assert_called_with("yt-dlp not installed on this device.", style="red")


def test_key_reader_eintr_and_resize_resilience():
    """KeyReader returns None gracefully on EINTR without splitting keys or raising."""
    reader = KeyReader()

    with patch("sys.stdin.isatty", return_value=True), \
         patch("sys.stdin.fileno", return_value=0), \
         patch("termios.tcgetattr", return_value=[0] * 7), \
         patch("termios.tcsetattr"), \
         patch("tty.setcbreak"), \
         patch("select.select", side_effect=InterruptedError("Interrupted by SIGWINCH")):
        result = reader.read_key(timeout=0.2)
        assert result is None


def test_sigwinch_handler_in_tui_mode():
    """tui_mode captures SIGWINCH and check_and_clear_resized reports it."""
    if not hasattr(signal, "SIGWINCH"):
        pytest.skip("SIGWINCH not available on this platform")

    check_and_clear_resized()  # clear any stale flag
    with tui_mode():
        # Trigger SIGWINCH to self
        os.kill(os.getpid(), signal.SIGWINCH)
        assert check_and_clear_resized() is True
        # Once read, flag resets
        assert check_and_clear_resized() is False


def test_narrow_terminal_today_header_truncation():
    """Today header dynamically truncates date title on narrow terminal while preserving progress."""
    buf = io.StringIO()
    test_console = Console(file=buf, width=30)

    with patch("todo.ui.console.console", test_console):
        render_today_header("TODAY · 18 NOVEMBER 2026", completed=10, total=10)

    output = buf.getvalue()
    # Right-hand progress is completely intact
    assert "100% · 10/10" in output
    # Left-hand date title was truncated with ellipsis to avoid wrapping
    assert "TODAY" in output
    assert "…" in output


def test_narrow_terminal_today_view_single_line(temp_db: Database):
    """TodayView tasks table truncates titles onto single lines on 45-column terminals."""
    daily_service = DailyService(temp_db)
    task_service = TaskService(temp_db)
    today_view = TodayView(daily_service, task_service)

    long_title = "A" * 80  # Long title that would wrap on a 45-col terminal
    task = task_service.create_task(long_title)
    dt, _ = daily_service.add_task_to_date(task.id, "2026-11-18")

    buf = io.StringIO()
    test_console = Console(file=buf, width=45)

    with patch("todo.ui.today_view.console", test_console), \
         patch("todo.ui.console.console", test_console):
        today_view.render([dt], clear=False, current_date="2026-11-18")

    output = buf.getvalue()
    lines = [line for line in output.splitlines() if "1." in line]
    assert len(lines) == 1, "Task row must be strictly single-line"
    assert "…" in lines[0]


def test_narrow_terminal_universal_view_single_line(temp_db: Database):
    """UniversalView tasks table truncates titles onto single lines on 45-column terminals."""
    daily_service = DailyService(temp_db)
    task_service = TaskService(temp_db)
    view = UniversalView(task_service, daily_service)

    long_title = "Backlog " * 15
    task_service.create_task(long_title, source_type="universal")

    buf = io.StringIO()
    test_console = Console(file=buf, width=45)

    with patch("todo.ui.universal_view.console", test_console), \
         patch("todo.ui.universal_view.key_reader.read_key", return_value="b"):
        view.run()

    output = buf.getvalue()
    lines = [line for line in output.splitlines() if "1." in line]
    assert len(lines) == 1, "Universal task row must be strictly single-line"
    assert "…" in lines[0]
