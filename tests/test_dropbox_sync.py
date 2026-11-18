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


def test_sync_service_push_file(sync_env):
    """Verify push_file uploads a specific snapshot file and updates sync state."""
    cfg = sync_env["config"]
    tmp_path = sync_env["tmp"]
    snapshot = tmp_path / "snapshot.db"
    snapshot.write_bytes(b"SNAPSHOT_CONTENT")

    mock_client = MagicMock()
    mock_meta = MagicMock()
    mock_meta.rev = "rev_snap"
    mock_meta.content_hash = "hash_snap"
    mock_client.files_upload.return_value = mock_meta

    svc = SyncService(cfg, client=mock_client)
    ok, msg = svc.push_file(snapshot)

    assert ok is True
    assert "Successfully pushed" in msg
    mock_client.files_upload.assert_called_once()
    args, kwargs = mock_client.files_upload.call_args
    assert args[0] == b"SNAPSHOT_CONTENT"
    assert args[1] == "/todo.db"

    state = svc._read_sync_state()
    assert state.get("remote_rev") == "rev_snap"


def test_sync_service_debounced_push_batches_rapid_writes(sync_env):
    """Verify debouncing batches rapid edits into a single upload and clears dirty."""
    import time
    from todo.database import Database

    cfg = sync_env["config"]
    db_path = sync_env["db_path"]
    db = Database(db_path)

    mock_client = MagicMock()
    mock_meta = MagicMock()
    mock_meta.rev = "rev_debounced"
    mock_meta.content_hash = "hash_debounced"
    mock_client.files_upload.return_value = mock_meta

    svc = SyncService(cfg, client=mock_client)

    # Simulate write
    db.mark_dirty()
    assert db.dirty is True
    assert db.write_version == 1

    # Schedule debounce with 0.1s delay
    svc.schedule_debounced_push(db, delay=0.1)

    # Immediately schedule another write within debounce window
    db.mark_dirty()
    assert db.write_version == 2
    svc.schedule_debounced_push(db, delay=0.1)

    # Wait for timer to execute
    time.sleep(0.25)

    # Only 1 upload should have occurred
    assert mock_client.files_upload.call_count == 1
    # Dirty flag should now be cleared
    assert db.dirty is False


def test_sync_service_cancel_debounce_timer(sync_env):
    """Verify cancel_debounce_timer prevents queued upload."""
    import time
    from todo.database import Database

    cfg = sync_env["config"]
    db_path = sync_env["db_path"]
    db = Database(db_path)

    mock_client = MagicMock()
    svc = SyncService(cfg, client=mock_client)

    db.mark_dirty()
    svc.schedule_debounced_push(db, delay=0.1)
    svc.cancel_debounce_timer()

    time.sleep(0.2)
    mock_client.files_upload.assert_not_called()
    assert db.dirty is True


def test_sync_service_background_pull_skips_when_dirty(sync_env):
    """Verify background pull skips downloading when local DB has uncommitted/unsynced edits."""
    from todo.database import Database

    cfg = sync_env["config"]
    db_path = sync_env["db_path"]
    db = Database(db_path)
    db.mark_dirty()

    mock_client = MagicMock()
    mock_meta = MagicMock()
    mock_meta.rev = "rev_remote_newer"
    mock_meta.content_hash = "hash_remote_newer"
    mock_client.files_get_metadata.return_value = mock_meta

    svc = SyncService(cfg, client=mock_client)
    pulled_callback = MagicMock()

    t = svc.start_background_pull(db, on_pulled=pulled_callback)
    t.join(timeout=2.0)

    # Download should NOT happen because db is dirty
    mock_client.files_download.assert_not_called()
    pulled_callback.assert_not_called()


def test_sync_service_background_pull_updates_when_clean(sync_env):
    """Verify background pull downloads and replaces local DB when clean and newer rev exists."""
    from todo.database import Database

    cfg = sync_env["config"]
    db_path = sync_env["db_path"]
    db = Database(db_path)
    assert db.dirty is False

    mock_client = MagicMock()
    mock_meta = MagicMock()
    mock_meta.rev = "rev_newest"
    mock_meta.content_hash = "hash_newest"
    mock_client.files_get_metadata.return_value = mock_meta

    mock_res = MagicMock()
    # Create valid sqlite bytes for downloaded database
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
        tmp_db_for_bytes = Database(Path(tf.name))
        with tmp_db_for_bytes.get_connection() as conn:
            conn.execute(
                "INSERT INTO tasks (title, description, created_at, updated_at) VALUES (?, ?, ?, ?)",
                ("Remote Task", "From background pull", "2026-11-18T00:00:00", "2026-11-18T00:00:00"),
            )
            conn.commit()
        tmp_db_for_bytes.checkpoint()
        tmp_db_for_bytes.close()
        downloaded_bytes = Path(tf.name).read_bytes()
        Path(tf.name).unlink(missing_ok=True)

    mock_res.content = downloaded_bytes
    mock_client.files_download.return_value = (mock_meta, mock_res)

    svc = SyncService(cfg, client=mock_client)
    pulled_callback = MagicMock()

    t = svc.start_background_pull(db, on_pulled=pulled_callback)
    t.join(timeout=2.0)

    mock_client.files_download.assert_called_once()
    pulled_callback.assert_called_once()

    # Verify task from remote DB is now in local DB
    with db.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT title FROM tasks WHERE title = ?", ("Remote Task",))
        row = cursor.fetchone()
        assert row is not None
        assert row["title"] == "Remote Task"


def test_todoapp_exit_clean_does_not_spawn_subprocess(sync_env):
    """Verify clean exit does not spawn detached sync process."""
    from todo.ui.app import TodoApp

    cfg = sync_env["config"]
    mock_client = MagicMock()
    svc = SyncService(cfg, client=mock_client)

    app = TodoApp(config=cfg, sync_service=svc, no_sync=False)
    assert app.db.dirty is False

    with patch("subprocess.Popen") as mock_popen, patch.object(app, "_main_loop"):
        app.run()

    mock_popen.assert_not_called()


def test_todoapp_exit_dirty_spawns_detached_sync(sync_env):
    """Verify dirty exit spawns detached sync push process with start_new_session=True."""
    from todo.ui.app import TodoApp

    cfg = sync_env["config"]
    mock_client = MagicMock()
    svc = SyncService(cfg, client=mock_client)

    app = TodoApp(config=cfg, sync_service=svc, no_sync=False)

    def mark_dirty_during_loop():
        app.db.mark_dirty()

    with patch("subprocess.Popen") as mock_popen, patch.object(app, "_main_loop", side_effect=mark_dirty_during_loop):
        app.run()

    mock_popen.assert_called_once()
    args, kwargs = mock_popen.call_args
    cmd = args[0]
    assert cmd[1:] == ["-m", "todo.cli", "sync", "push"]
    assert kwargs.get("start_new_session") is True


def test_todoapp_exit_no_sync_flag_skips_spawn(sync_env):
    """Verify --no-sync flag prevents spawning detached push even when dirty."""
    from todo.ui.app import TodoApp

    cfg = sync_env["config"]
    mock_client = MagicMock()
    svc = SyncService(cfg, client=mock_client)

    app = TodoApp(config=cfg, sync_service=svc, no_sync=True)

    def mark_dirty_during_loop():
        app.db.mark_dirty()

    with patch("subprocess.Popen") as mock_popen, patch.object(app, "_main_loop", side_effect=mark_dirty_during_loop):
        app.run()

    mock_popen.assert_not_called()


