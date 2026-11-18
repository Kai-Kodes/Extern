"""Unit tests for configuration management, TOML persistence, and credential storage."""

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from todo.config import Config, DEFAULT_DROPBOX_APP_KEY, DEFAULT_DROPBOX_PATH


def test_config_defaults():
    assert DEFAULT_DROPBOX_APP_KEY == "bcfyaam7zsvenih"
    assert DEFAULT_DROPBOX_PATH == "/todo.db"
    with tempfile.TemporaryDirectory() as tmpdir:
        config_file = Path(tmpdir) / "config.toml"
        with patch.dict(os.environ, {"TODO_CONFIG_PATH": str(config_file)}, clear=False):
            cfg = Config.load()
            assert cfg.dropbox_app_key == "bcfyaam7zsvenih"
            assert cfg.dropbox_path == "/todo.db"
            assert cfg.dropbox_refresh_token is None
            assert cfg.dropbox_sync_enabled is True
            assert str(cfg.telegram_session_path).endswith("telegram.session")


def test_config_env_overrides():
    with tempfile.TemporaryDirectory() as tmpdir:
        config_file = Path(tmpdir) / "config.toml"
        env = {
            "TODO_CONFIG_PATH": str(config_file),
            "DROPBOX_APP_KEY": "custom_app_key",
            "DROPBOX_REFRESH_TOKEN": "custom_refresh_token",
            "DROPBOX_PATH": "/Custom/todo.db",
            "DROPBOX_SYNC_ENABLED": "false",
            "TELEGRAM_API_ID": "98765",
            "TELEGRAM_API_HASH": "custom_hash",
            "TELEGRAM_SESSION_PATH": "/tmp/custom_tg.session",
        }
        with patch.dict(os.environ, env, clear=False):
            cfg = Config.load()
            assert cfg.dropbox_app_key == "custom_app_key"
            assert cfg.dropbox_refresh_token == "custom_refresh_token"
            assert cfg.dropbox_path == "/Custom/todo.db"
            assert cfg.dropbox_sync_enabled is False
            assert cfg.telegram_api_id == 98765
            assert cfg.telegram_api_hash == "custom_hash"
            assert cfg.telegram_session_path == Path("/tmp/custom_tg.session")


def test_config_save_and_clear_dropbox_credentials():
    with tempfile.TemporaryDirectory() as tmpdir:
        config_file = Path(tmpdir) / "config.toml"
        with patch.dict(os.environ, {"TODO_CONFIG_PATH": str(config_file)}, clear=False):
            # Save credentials
            Config.save_dropbox_credentials(
                refresh_token="test_refresh_123",
                app_key="my_app_key",
                path="/Extern/test.db",
            )
            assert config_file.exists()

            cfg = Config.load()
            assert cfg.dropbox_refresh_token == "test_refresh_123"
            assert cfg.dropbox_app_key == "my_app_key"
            assert cfg.dropbox_path == "/Extern/test.db"

            # Clear credentials
            Config.clear_dropbox_credentials()
            cfg_cleared = Config.load()
            assert cfg_cleared.dropbox_refresh_token is None
            assert cfg_cleared.dropbox_app_key == "my_app_key"  # Preserved app key


def test_config_save_and_clear_telegram_credentials():
    with tempfile.TemporaryDirectory() as tmpdir:
        config_file = Path(tmpdir) / "config.toml"
        with patch.dict(os.environ, {"TODO_CONFIG_PATH": str(config_file)}, clear=False):
            # Save credentials
            Config.save_telegram_credentials(
                api_id=123456,
                api_hash="secret_api_hash",
                session_path=Path("/tmp/my_session.session"),
            )
            cfg = Config.load()
            assert cfg.telegram_api_id == 123456
            assert cfg.telegram_api_hash == "secret_api_hash"
            assert cfg.telegram_session_path == Path("/tmp/my_session.session")

            # Clear credentials
            Config.clear_telegram_credentials()
            cfg_cleared = Config.load()
            assert cfg_cleared.telegram_api_id is None
            assert cfg_cleared.telegram_api_hash is None
            # Session path remains preserved
            assert cfg_cleared.telegram_session_path == Path("/tmp/my_session.session")
