"""Unit test verifying friendly TUI behavior when Telegram is not authenticated."""

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from todo.config import Config
from todo.ui.app import TodoApp


def test_telegram_unconfigured_tui_guard():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "todo.db"
        cfg = Config(
            db_path=db_path,
            telegram_api_id=None,
            telegram_api_hash=None,
            telegram_session_path=None,
        )
        app = TodoApp(config=cfg)

        # Check adapter availability
        ok, err = app.telegram_adapter.check_availability()
        assert ok is False
        assert "not configured" in err

        # Verify telegram_view handle_add_channel shows friendly screen instead of crashing
        with patch("todo.ui.telegram_view.key_reader.read_key", return_value="b"):
            res = app.telegram_view.handle_add_channel()
            assert res is None
