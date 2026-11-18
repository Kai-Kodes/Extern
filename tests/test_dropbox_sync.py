"""Unit and integration tests for the native Dropbox SyncService."""

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from todo.config import Config
from todo.services.sync_service import SyncService


@pytest.fixture
def sync_env():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        db_path = tmp_path / "todo.db"
        config_path = tmp_path / "config.toml"
        cfg = Config(
            db_path=db_path,
            dropbox_refresh_token="mock_refresh_token",
            dropbox_app_key="mock_app_key",
            dropbox_path="/todo.db",
            dropbox_sync_enabled=True,
        )
        # Point data dir to temp
        with patch.object(Config, "get_data_dir", return_value=tmp_path):
            yield {
                "tmp": tmp_path,
                "db_path": db_path,
                "config": cfg,
            }


def test_sync_service_configured(sync_env):
    cfg = sync_env["config"]
    svc = SyncService(cfg)
    assert svc.is_configured is True

    cfg.dropbox_refresh_token = None
    assert svc.is_configured is False

    cfg.dropbox_refresh_token = "mock_tok"
    cfg.dropbox_sync_enabled = False
    assert svc.is_configured is False


def test_sync_service_pull_download(sync_env):
    cfg = sync_env["config"]
    db_path = sync_env["db_path"]

    mock_client = MagicMock()
    mock_meta = MagicMock()
    mock_meta.rev = "rev_1001"
    mock_meta.content_hash = "hash_1001"
    mock_client.files_get_metadata.return_value = mock_meta

    mock_res = MagicMock()
    mock_res.content = b"SQLITE_DATABASE_CONTENT"
    mock_client.files_download.return_value = (mock_meta, mock_res)

    svc = SyncService(cfg, client=mock_client)
    ok, msg = svc.pull()

    assert ok is True
    assert "Successfully pulled" in msg
    assert db_path.read_bytes() == b"SQLITE_DATABASE_CONTENT"

    state = svc._read_sync_state()
    assert state.get("remote_rev") == "rev_1001"
    assert state.get("content_hash") == "hash_1001"


def test_sync_service_pull_cached_rev_skips(sync_env):
    cfg = sync_env["config"]
    db_path = sync_env["db_path"]
    db_path.write_bytes(b"EXISTING_CONTENT")

    mock_client = MagicMock()
    mock_meta = MagicMock()
    mock_meta.rev = "rev_1001"
    mock_meta.content_hash = "hash_1001"
    mock_client.files_get_metadata.return_value = mock_meta

    svc = SyncService(cfg, client=mock_client)
    svc._write_sync_state({"remote_rev": "rev_1001"})

    ok, msg = svc.pull(force=False)
    assert ok is True
    assert "already up to date" in msg
    # files_download should NOT be called when rev matches
    mock_client.files_download.assert_not_called()


def test_sync_service_pull_offline_fallback(sync_env):
    cfg = sync_env["config"]
    mock_client = MagicMock()
    mock_client.files_get_metadata.side_effect = Exception("Connection timed out")

    svc = SyncService(cfg, client=mock_client)
    ok, msg = svc.pull()

    # Never raises exception; gracefully reports non-blocking notice
    assert ok is False
    assert "Offline or Dropbox unreachable" in msg


def test_sync_service_push(sync_env):
    cfg = sync_env["config"]
    db_path = sync_env["db_path"]
    db_path.write_bytes(b"LOCAL_SQLITE_CONTENT")

    mock_client = MagicMock()
    mock_meta = MagicMock()
    mock_meta.rev = "rev_2002"
    mock_meta.content_hash = "hash_2002"
    mock_client.files_upload.return_value = mock_meta

    svc = SyncService(cfg, client=mock_client)
    ok, msg = svc.push()

    assert ok is True
    assert "Successfully pushed" in msg
    mock_client.files_upload.assert_called_once()

    state = svc._read_sync_state()
    assert state.get("remote_rev") == "rev_2002"


def test_sync_service_push_offline_fallback(sync_env):
    cfg = sync_env["config"]
    db_path = sync_env["db_path"]
    db_path.write_bytes(b"LOCAL_SQLITE_CONTENT")

    mock_client = MagicMock()
    mock_client.files_upload.side_effect = Exception("Network unreachable")

    svc = SyncService(cfg, client=mock_client)
    ok, msg = svc.push()

    # Never crashes
    assert ok is False
    assert "Offline or Dropbox unreachable" in msg


def test_sync_service_pkce_flow(sync_env):
    cfg = sync_env["config"]
    svc = SyncService(cfg)

    with patch("dropbox.DropboxOAuth2FlowNoRedirect") as mock_flow_cls:
        mock_flow = MagicMock()
        mock_flow.start.return_value = "https://www.dropbox.com/oauth2/authorize?test=1"
        mock_flow_cls.return_value = mock_flow

        url, flow = svc.create_pkce_flow("custom_app_key")
        assert url.startswith("https://www.dropbox.com")
        assert flow == mock_flow

        # Finish flow
        mock_result = MagicMock()
        mock_result.refresh_token = "new_refresh_token_xyz"
        mock_flow.finish.return_value = mock_result

        token = svc.finish_pkce_flow(mock_flow, "auth_code_123")
        assert token == "new_refresh_token_xyz"


def test_sync_service_status(sync_env):
    cfg = sync_env["config"]
    db_path = sync_env["db_path"]
    db_path.write_bytes(b"12345")

    mock_client = MagicMock()
    mock_meta = MagicMock()
    mock_meta.rev = "rev_3003"
    mock_meta.size = 5
    mock_client.files_get_metadata.return_value = mock_meta

    svc = SyncService(cfg, client=mock_client)
    svc._write_sync_state({"remote_rev": "rev_3003"})

    status = svc.status()
    assert status["configured"] is True
    assert status["local_db_exists"] is True
    assert status["local_db_size_bytes"] == 5
    assert status["cached_rev"] == "rev_3003"
    assert status["connection"] == "online"
