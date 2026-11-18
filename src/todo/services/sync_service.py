"""Native Dropbox synchronization service for Extern."""

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

from todo.config import Config
from todo.database import Database


class SyncService:
    """
    Manages Dropbox synchronization using the official dropbox Python SDK.
    Supports PKCE OAuth2 authentication, state caching, atomic database updates,
    and graceful offline operation.
    """

    def __init__(
        self,
        config: Optional[Config] = None,
        client: Optional[Any] = None,
    ):
        self.config = config or Config.load()
        self._client = client
        self.state_file = self.config.get_data_dir() / "sync_state.json"
        self._debounce_timer: Optional[threading.Timer] = None
        self._debounce_lock = threading.Lock()

    @property
    def is_configured(self) -> bool:
        """Check if Dropbox synchronization has credentials configured."""
        has_token = bool(self.config.dropbox_refresh_token or self.config.dropbox_access_token)
        return bool(has_token and self.config.dropbox_sync_enabled)

    def get_client(self) -> Optional[Any]:
        """Obtain an authenticated Dropbox client instance, refreshing tokens automatically."""
        if self._client is not None:
            return self._client

        if not self.is_configured:
            return None

        try:
            import dropbox

            if self.config.dropbox_refresh_token:
                self._client = dropbox.Dropbox(
                    oauth2_refresh_token=self.config.dropbox_refresh_token,
                    app_key=self.config.dropbox_app_key,
                    timeout=15,
                )
            elif self.config.dropbox_access_token:
                self._client = dropbox.Dropbox(
                    oauth2_access_token=self.config.dropbox_access_token,
                    timeout=15,
                )
            return self._client
        except Exception:
            return None

    def _read_sync_state(self) -> Dict[str, Any]:
        """Read local sync state cache."""
        if self.state_file.exists():
            try:
                return json.loads(self.state_file.read_text(encoding="utf-8"))
            except Exception:
                return {}
        return {}

    def _write_sync_state(self, updates: Dict[str, Any]) -> None:
        """Update and persist local sync state cache."""
        try:
            state = self._read_sync_state()
            state.update(updates)
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            self.state_file.write_text(json.dumps(state, indent=2), encoding="utf-8")
        except Exception:
            pass

    def create_pkce_flow(self, app_key: Optional[str] = None) -> Tuple[str, Any]:
        """
        Create a DropboxOAuth2FlowNoRedirect instance with PKCE enabled.
        Returns: (authorization_url, flow_instance)
        """
        import dropbox

        key = app_key or self.config.dropbox_app_key
        flow = dropbox.DropboxOAuth2FlowNoRedirect(
            consumer_key=key,
            token_access_type="offline",
            use_pkce=True,
        )
        auth_url = flow.start()
        return auth_url, flow

    def finish_pkce_flow(self, flow: Any, auth_code: str) -> str:
        """
        Exchange user authorization code for long-lived offline refresh token.
        Returns: refresh_token string
        """
        result = flow.finish(auth_code.strip())
        refresh_token = getattr(result, "refresh_token", None)
        if not refresh_token:
            raise ValueError(
                "Dropbox did not return a refresh token. "
                "Please verify that token_access_type='offline' was requested."
            )
        return refresh_token

    def pull(self, force: bool = False) -> Tuple[bool, str]:
        """
        Pull the remote database from Dropbox into the local database path.
        Compares remote revision/content_hash with cached state to prevent unnecessary downloads.
        Returns: (success: bool, message: str)
        """
        if not self.is_configured:
            return False, "Dropbox sync is not configured or disabled."

        client = self.get_client()
        if client is None:
            return False, "Failed to initialize Dropbox client."

        remote_path = self.config.dropbox_path
        local_path = self.config.db_path

        try:
            import dropbox
            import dropbox.exceptions

            # Check remote file metadata
            try:
                remote_meta = client.files_get_metadata(remote_path)
            except dropbox.exceptions.ApiError as e:
                # If file doesn't exist remotely yet, this is not a failure for pull
                error_summary = str(e.error if hasattr(e, "error") else e)
                if "not_found" in error_summary.lower():
                    return True, "Remote database does not exist yet; will push on exit."
                raise

            remote_rev = getattr(remote_meta, "rev", None)
            remote_hash = getattr(remote_meta, "content_hash", None)

            # Check if local file exists and matches remote rev
            state = self._read_sync_state()
            cached_rev = state.get("remote_rev")

            if not force and local_path.exists() and cached_rev and cached_rev == remote_rev:
                return True, "Local database is already up to date with remote."

            # Download remote database
            _, res = client.files_download(remote_path)
            content = res.content

            # Write atomically to local database
            local_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_db = local_path.with_suffix(".pull.tmp")
            tmp_db.write_bytes(content)

            # Flush WAL and remove any stale WAL/SHM sidecars
            wal_file = Path(f"{local_path}-wal")
            shm_file = Path(f"{local_path}-shm")
            if wal_file.exists():
                try:
                    wal_file.unlink()
                except OSError:
                    pass
            if shm_file.exists():
                try:
                    shm_file.unlink()
                except OSError:
                    pass

            tmp_db.replace(local_path)

            now_iso = datetime.now(timezone.utc).isoformat()
            self._write_sync_state(
                {
                    "remote_rev": remote_rev,
                    "content_hash": remote_hash,
                    "last_pull_at": now_iso,
                    "last_sync_status": "ok",
                }
            )

            return True, f"Successfully pulled database from Dropbox (rev {remote_rev})."

        except Exception as e:
            return False, f"Notice: Offline or Dropbox unreachable ({e}). Using local database."

    def push_file(self, file_path: Path) -> Tuple[bool, str]:
        """
        Upload a specific database snapshot file to Dropbox.
        Returns: (success: bool, message: str)
        """
        if not self.is_configured:
            return False, "Dropbox sync is not configured or disabled."

        client = self.get_client()
        if client is None:
            return False, "Failed to initialize Dropbox client."

        if not file_path.exists():
            return False, f"Snapshot file not found at '{file_path}'."

        remote_path = self.config.dropbox_path

        try:
            from dropbox.files import WriteMode

            data = file_path.read_bytes()

            # Upload to Dropbox
            meta = client.files_upload(
                data,
                remote_path,
                mode=WriteMode.overwrite,
                mute=True,
            )

            remote_rev = getattr(meta, "rev", None)
            remote_hash = getattr(meta, "content_hash", None)
            now_iso = datetime.now(timezone.utc).isoformat()

            self._write_sync_state(
                {
                    "remote_rev": remote_rev,
                    "content_hash": remote_hash,
                    "last_push_at": now_iso,
                    "last_sync_status": "ok",
                }
            )

            return True, f"Successfully pushed database to Dropbox (rev {remote_rev})."

        except Exception as e:
            return False, f"Notice: Offline or Dropbox unreachable ({e}). Changes preserved locally."

    def push(self, force: bool = False) -> Tuple[bool, str]:
        """
        Flush SQLite WAL state and upload local database to Dropbox.
        Returns: (success: bool, message: str)
        """
        if not self.is_configured:
            return False, "Dropbox sync is not configured or disabled."

        local_path = self.config.db_path
        if not local_path.exists():
            return False, f"Local database not found at '{local_path}'."

        # Flush SQLite WAL to ensure everything is consolidated into todo.db
        try:
            db = Database(local_path)
            db.checkpoint()
            db.close()
        except Exception:
            pass

        return self.push_file(local_path)

    def _debounced_push_worker(self, db: Database) -> None:
        """Worker executed when the debounce timer fires to snapshot and upload."""
        import tempfile

        if not self.is_configured:
            return

        target_version = db.write_version

        # Create temporary snapshot file using online backup API
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
            snapshot_path = Path(tf.name)

        try:
            db.backup_to_file(snapshot_path)
            success, _ = self.push_file(snapshot_path)
            if success:
                db.clear_dirty(version=target_version)
        except Exception:
            pass
        finally:
            try:
                snapshot_path.unlink(missing_ok=True)
            except OSError:
                pass

    def schedule_debounced_push(self, db: Database, delay: float = 3.0) -> None:
        """Schedule a background push after delay seconds, debouncing subsequent requests."""
        if not self.is_configured:
            return

        with self._debounce_lock:
            if self._debounce_timer is not None:
                self._debounce_timer.cancel()
            self._debounce_timer = threading.Timer(delay, self._debounced_push_worker, args=(db,))
            self._debounce_timer.daemon = True
            self._debounce_timer.start()

    def cancel_debounce_timer(self) -> None:
        """Cancel any active debounce push timer."""
        with self._debounce_lock:
            if self._debounce_timer is not None:
                self._debounce_timer.cancel()
                self._debounce_timer = None

    def _background_pull_worker(
        self, db: Database, on_pulled: Optional[Callable[[], None]] = None
    ) -> None:
        """Background worker that queries Dropbox metadata and pulls newer database if clean."""
        if not self.is_configured:
            return

        client = self.get_client()
        if client is None:
            return

        remote_path = self.config.dropbox_path
        local_path = self.config.db_path

        try:
            # Check remote file metadata
            try:
                remote_meta = client.files_get_metadata(remote_path)
            except Exception:
                return

            remote_rev = getattr(remote_meta, "rev", None)
            remote_hash = getattr(remote_meta, "content_hash", None)

            # Check if local file exists and matches remote rev
            state = self._read_sync_state()
            cached_rev = state.get("remote_rev")

            if local_path.exists() and cached_rev and cached_rev == remote_rev:
                return

            # If local uncommitted/unsynced edits exist, skip pull to protect local edits
            if db.dirty:
                return

            # Download remote database
            _, res = client.files_download(remote_path)
            content = res.content

            # Write atomically to local database
            local_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_db = local_path.with_suffix(".pull.tmp")
            tmp_db.write_bytes(content)

            # Double check dirty flag before swapping
            if db.dirty:
                tmp_db.unlink(missing_ok=True)
                return

            # Safely checkpoint and close database before file replacement
            db.close()

            # Flush WAL and remove any stale WAL/SHM sidecars
            wal_file = Path(f"{local_path}-wal")
            shm_file = Path(f"{local_path}-shm")
            if wal_file.exists():
                try:
                    wal_file.unlink()
                except OSError:
                    pass
            if shm_file.exists():
                try:
                    shm_file.unlink()
                except OSError:
                    pass

            tmp_db.replace(local_path)

            now_iso = datetime.now(timezone.utc).isoformat()
            self._write_sync_state(
                {
                    "remote_rev": remote_rev,
                    "content_hash": remote_hash,
                    "last_pull_at": now_iso,
                    "last_sync_status": "ok",
                }
            )

            # Re-initialize DB connection / schema state
            db.init_db()

            # Notify caller (e.g. redraw Today view)
            if on_pulled is not None:
                try:
                    on_pulled()
                except Exception:
                    pass

        except Exception:
            pass

    def start_background_pull(
        self, db: Database, on_pulled: Optional[Callable[[], None]] = None
    ) -> threading.Thread:
        """Spawn a background daemon thread to check remote metadata and pull if newer."""
        thread = threading.Thread(
            target=self._background_pull_worker,
            args=(db, on_pulled),
            daemon=True,
            name="DropboxBackgroundPull",
        )
        thread.start()
        return thread

    def status(self) -> Dict[str, Any]:
        """Get summary of local and remote sync status."""
        state = self._read_sync_state()
        local_exists = self.config.db_path.exists()
        local_size = self.config.db_path.stat().st_size if local_exists else 0

        info = {
            "configured": self.is_configured,
            "app_key": self.config.dropbox_app_key,
            "remote_path": self.config.dropbox_path,
            "local_db_path": str(self.config.db_path),
            "local_db_exists": local_exists,
            "local_db_size_bytes": local_size,
            "cached_rev": state.get("remote_rev"),
            "cached_hash": state.get("content_hash"),
            "last_pull_at": state.get("last_pull_at"),
            "last_push_at": state.get("last_push_at"),
            "last_sync_status": state.get("last_sync_status", "not_run"),
        }

        # Check connectivity if configured
        if self.is_configured:
            client = self.get_client()
            if client is not None:
                try:
                    import dropbox.exceptions

                    meta = client.files_get_metadata(self.config.dropbox_path)
                    info["remote_exists"] = True
                    info["remote_rev"] = getattr(meta, "rev", None)
                    info["remote_size_bytes"] = getattr(meta, "size", 0)
                    info["connection"] = "online"
                except Exception as e:
                    error_summary = str(getattr(e, "error", e))
                    if "not_found" in error_summary.lower():
                        info["remote_exists"] = False
                        info["connection"] = "online"
                    else:
                        info["connection"] = f"unreachable ({e})"
            else:
                info["connection"] = "auth_error"
        else:
            info["connection"] = "not_configured"

        return info
