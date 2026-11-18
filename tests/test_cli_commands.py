"""Unit tests for Extern CLI commands (auth, sync, doctor, status)."""

import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from todo.cli import cli
from todo.config import Config


@pytest.fixture
def cli_env():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        cfg_file = tmp_path / "config.toml"
        db_path = tmp_path / "todo.db"
        db_path.write_bytes(b"SQLITE_TEST_DB")

        env = {
            "TODO_CONFIG_PATH": str(cfg_file),
            "TODO_DB_PATH": str(db_path),
            "XDG_DATA_HOME": str(tmp_path),
        }
        with patch.dict(os.environ, env, clear=False):
            yield {
                "tmp": tmp_path,
                "cfg_file": cfg_file,
                "db_path": db_path,
            }


def test_doctor_command(cli_env):
    runner = CliRunner()
    res = runner.invoke(cli, ["doctor"])
    assert res.exit_code == 0
    assert "EXTERN SYSTEM DIAGNOSTICS" in res.output
    assert "Configuration" in res.output
    assert "SQLite Database" in res.output
    assert "Course Scanner" in res.output
    assert "Dropbox Sync" in res.output


def test_status_alias(cli_env):
    runner = CliRunner()
    res = runner.invoke(cli, ["status"])
    assert res.exit_code == 0
    assert "EXTERN SYSTEM DIAGNOSTICS" in res.output


def test_sync_status_command(cli_env):
    runner = CliRunner()
    res = runner.invoke(cli, ["sync", "status"])
    assert res.exit_code == 0
    assert "DROPBOX SYNC STATUS" in res.output
    assert "Configured" in res.output
    assert "Remote Path" in res.output


def test_sync_pull_and_push_not_configured(cli_env):
    runner = CliRunner()
    res_pull = runner.invoke(cli, ["sync", "pull"])
    assert res_pull.exit_code == 0
    assert "Dropbox sync is not configured" in res_pull.output

    res_push = runner.invoke(cli, ["sync", "push"])
    assert res_push.exit_code == 0
    assert "Dropbox sync is not configured" in res_push.output


def test_sync_pull_and_push_configured(cli_env):
    runner = CliRunner()
    with patch("todo.services.sync_service.SyncService.pull", return_value=(True, "Pulled rev 123")):
        with patch("todo.services.sync_service.SyncService.is_configured", True):
            res = runner.invoke(cli, ["sync", "pull"])
            assert res.exit_code == 0
            assert "Pulled rev 123" in res.output

    with patch("todo.services.sync_service.SyncService.push", return_value=(True, "Pushed rev 124")):
        with patch("todo.services.sync_service.SyncService.is_configured", True):
            res = runner.invoke(cli, ["sync", "push"])
            assert res.exit_code == 0
            assert "Pushed rev 124" in res.output


def test_auth_dropbox_logout(cli_env):
    cfg_file = cli_env["cfg_file"]
    Config.save_dropbox_credentials("tok_abc", app_key="key_xyz")
    assert Config.load().dropbox_refresh_token == "tok_abc"

    runner = CliRunner()
    res = runner.invoke(cli, ["auth", "dropbox", "--logout"])
    assert res.exit_code == 0
    assert "Unlinked Dropbox account" in res.output
    assert Config.load().dropbox_refresh_token is None


def test_auth_dropbox_login_flow(cli_env):
    runner = CliRunner()
    mock_flow = MagicMock()
    mock_flow.start.return_value = "https://www.dropbox.com/oauth2/authorize?code=1"

    with patch("todo.services.sync_service.SyncService.create_pkce_flow", return_value=("https://auth.url", mock_flow)):
        with patch("todo.services.sync_service.SyncService.finish_pkce_flow", return_value="ref_token_success"):
            with patch("todo.services.sync_service.SyncService.push", return_value=(True, "Sync ok")):
                res = runner.invoke(cli, ["auth", "dropbox"], input="auth_code_from_user\n")
                assert res.exit_code == 0
                assert "Dropbox successfully linked" in res.output
                assert Config.load().dropbox_refresh_token == "ref_token_success"


def test_auth_telegram_logout(cli_env):
    tmp_path = cli_env["tmp"]
    session_file = tmp_path / "telegram.session"
    session_file.write_bytes(b"session_bytes")

    Config.save_telegram_credentials(12345, "hash_abc", session_path=session_file)
    assert Config.load().telegram_api_id == 12345

    runner = CliRunner()
    res = runner.invoke(cli, ["auth", "telegram", "--logout"])
    assert res.exit_code == 0
    assert "Logged out of Telegram" in res.output
    assert not session_file.exists()
    assert Config.load().telegram_api_id is None


def test_auth_telegram_login_flow(cli_env):
    runner = CliRunner()
    mock_client = MagicMock()
    mock_client.__enter__.return_value = mock_client
    mock_client.is_user_authorized.return_value = True
    mock_me = MagicMock()
    mock_me.first_name = "Alice"
    mock_me.last_name = "Smith"
    mock_me.username = "alicesmith"
    mock_me.phone = "+1234567890"
    mock_client.get_me.return_value = mock_me

    with patch("telethon.sync.TelegramClient", return_value=mock_client):
        res = runner.invoke(cli, ["auth", "telegram"], input="123456\nmy_api_hash\n")
        assert res.exit_code == 0
        assert "Successfully authenticated as Alice Smith (@alicesmith)" in res.output
        cfg = Config.load()
        assert cfg.telegram_api_id == 123456
        assert cfg.telegram_api_hash == "my_api_hash"


def test_cli_no_sync_flag_and_startup(cli_env):
    """Verify that launching extern with --no-sync does not pull or enable sync."""
    runner = CliRunner()
    with patch("todo.cli.TodoApp") as mock_app_cls:
        mock_instance = MagicMock()
        mock_app_cls.return_value = mock_instance

        res = runner.invoke(cli, ["--no-sync"])
        assert res.exit_code == 0
        mock_app_cls.assert_called_once()
        _, kwargs = mock_app_cls.call_args
        assert kwargs.get("no_sync") is True
        assert kwargs.get("sync_service") is None
        mock_instance.run.assert_called_once()

