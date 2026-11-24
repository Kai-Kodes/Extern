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


def test_first_run_auto_config_creation(cli_env):
    """Verify that on a brand-new install, default config.toml is created automatically."""
    tmp_path = cli_env["tmp"]
    new_cfg_file = tmp_path / "subdir" / "new_config.toml"
    with patch.dict(os.environ, {"TODO_CONFIG_PATH": str(new_cfg_file)}, clear=False):
        assert not new_cfg_file.exists()
        cfg = Config.load()
        assert new_cfg_file.exists()
        assert cfg.is_first_run is True
        assert cfg.dropbox_app_key == "bcfyaam7zsvenih"
        assert cfg.dropbox_path == "/todo.db"

        # Subsequent load is no longer first run
        cfg2 = Config.load()
        assert cfg2.is_first_run is False


def test_first_run_hint_in_cli_today(cli_env):
    """Verify that running 'extern today' displays a welcoming first-run hint on brand new install."""
    tmp_path = cli_env["tmp"]
    new_cfg_file = tmp_path / "fresh" / "config.toml"
    new_db_file = tmp_path / "fresh" / "fresh_todo.db"
    with patch.dict(os.environ, {"TODO_CONFIG_PATH": str(new_cfg_file), "TODO_DB_PATH": str(new_db_file)}, clear=False):
        runner = CliRunner()
        res = runner.invoke(cli, ["today"])
        assert res.exit_code == 0
        assert "Welcome to Extern!" in res.output


def test_uninstall_command_interactive_abort(cli_env):
    """Verify that interactive prompt allows aborting uninstallation without touching files."""
    cfg_file = cli_env["cfg_file"]
    db_path = cli_env["db_path"]
    cfg_file.write_text("dummy = true\n")
    assert cfg_file.exists()
    assert db_path.exists()

    runner = CliRunner()
    res = runner.invoke(cli, ["uninstall"], input="n\n")
    assert res.exit_code == 0
    assert "Uninstallation aborted" in res.output
    assert cfg_file.exists()
    assert db_path.exists()


def test_uninstall_command_yes_removes_data(cli_env):
    """Verify that 'extern uninstall --yes' cleanly removes config, DB, session, and state."""
    tmp_path = cli_env["tmp"]
    cfg_file = cli_env["cfg_file"]
    db_path = cli_env["db_path"]
    cfg_file.write_text("dummy = true\n")

    cfg = Config.load(auto_create=False)
    data_dir = cfg.get_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)

    session_file = data_dir / "telegram.session"
    session_file.write_bytes(b"tg_session_data")
    sync_state = data_dir / "sync_state.json"
    sync_state.write_text('{"rev": "123"}\n')

    assert cfg_file.exists()
    assert db_path.exists()
    assert session_file.exists()
    assert sync_state.exists()

    runner = CliRunner()
    res = runner.invoke(cli, ["uninstall", "--yes", "--skip-pkg"])
    assert res.exit_code == 0
    assert "EXTERN UNINSTALLATION" in res.output
    assert "Removed configuration" in res.output
    assert "Removed database files" in res.output
    assert "Extern has been cleanly uninstalled" in res.output

    assert not cfg_file.exists()
    assert not db_path.exists()
    assert not session_file.exists()
    assert not sync_state.exists()


def test_uninstall_command_keep_data(cli_env):
    """Verify that 'extern uninstall --yes --keep-data' preserves DB while removing config."""
    cfg_file = cli_env["cfg_file"]
    db_path = cli_env["db_path"]
    cfg_file.write_text("dummy = true\n")
    assert cfg_file.exists()
    assert db_path.exists()

    runner = CliRunner()
    res = runner.invoke(cli, ["uninstall", "--yes", "--keep-data", "--skip-pkg"])
    assert res.exit_code == 0
    assert "Preserved database and task history" in res.output
    assert not cfg_file.exists()
    assert db_path.exists()  # Kept!


def test_telethon_pure_python_compatibility():
    """Verify that telethon works in pure-Python mode even if native cryptg is unavailable."""
    import telethon
    # cryptg is optional for telethon; confirm telethon imports and initializes cleanly
    from telethon.crypto import AES
    assert AES is not None


def test_detect_and_uninstall_package_pipx():
    """Verify pipx detection and uninstall invocation."""
    from todo.cli import _detect_and_uninstall_package
    import sys

    with patch.object(sys, "prefix", "/home/user/.local/share/pipx/venvs/extern"):
        with patch("shutil.which", return_value="/usr/bin/pipx"):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0)
                method, ok, msg = _detect_and_uninstall_package()
                assert method == "pipx"
                assert ok is True
                assert "pipx" in msg
                mock_run.assert_called_with(
                    ["/usr/bin/pipx", "uninstall", "extern"],
                    capture_output=True,
                    text=True,
                    timeout=15,
                )


def test_detect_and_uninstall_package_uv():
    """Verify uv tool detection and uninstall invocation."""
    from todo.cli import _detect_and_uninstall_package
    import sys

    with patch.object(sys, "prefix", "/home/user/.local/share/uv/tools/extern"):
        with patch("shutil.which", return_value="/usr/bin/uv"):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0)
                method, ok, msg = _detect_and_uninstall_package()
                assert method == "uv tool"
                assert ok is True
                assert "uv tool" in msg
                mock_run.assert_called_with(
                    ["/usr/bin/uv", "tool", "uninstall", "extern"],
                    capture_output=True,
                    text=True,
                    timeout=15,
                )


def test_detect_and_uninstall_package_pip():
    """Verify standard pip fallback when neither pipx nor uv is detected."""
    from todo.cli import _detect_and_uninstall_package
    import sys

    with patch.object(sys, "prefix", "/home/user/.venv"):
        with patch.object(sys, "executable", "/home/user/.venv/bin/python"):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0)
                method, ok, msg = _detect_and_uninstall_package()
                assert method == "pip"
                assert ok is True
                mock_run.assert_called_with(
                    ["/home/user/.venv/bin/python", "-m", "pip", "uninstall", "-y", "extern", "todo-cli"],
                    capture_output=True,
                    text=True,
                    timeout=20,
                )

