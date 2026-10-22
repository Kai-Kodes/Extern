"""Service for managing Google Tasks synchronization, local persistence, and offline caching."""

import json
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from todo.config import Config
from todo.database import Database
from todo.integrations.google_tasks import (
    GoogleOAuthManager,
    GoogleTasksAPIError,
    GoogleTasksAuthError,
    GoogleTasksClient,
    GoogleTasksNetworkError,
    GoogleTasksNotFoundError,
    format_rfc3339_due_date,
    parse_rfc3339_due_date,
)
from todo.models import FileItem, Source, Task
from todo.services.daily_service import DailyService
from todo.services.source_service import SourceService
from todo.services.task_service import TaskService


class SyncResult(tuple):
    """Tuple (tasks, is_offline) with an added has_changes boolean attribute."""

    def __new__(cls, tasks: List[Task], is_offline: bool, has_changes: bool = False):
        instance = super().__new__(cls, (tasks, is_offline))
        instance.has_changes = has_changes
        return instance

    @property
    def tasks(self) -> List[Task]:
        return self[0]

    @property
    def is_offline(self) -> bool:
        return self[1]


class SyncSummary(tuple):
    """Tuple (success, message) with an added has_changes boolean attribute."""

    def __new__(cls, success: bool, message: str, has_changes: bool = False):
        instance = super().__new__(cls, (success, message))
        instance.has_changes = has_changes
        return instance

    @property
    def success(self) -> bool:
        return self[0]

    @property
    def message(self) -> str:
        return self[1]


class GoogleTasksService:
    """Coordinates Google Tasks cloud sync, SQLite caching, task entities, and scheduling."""

    def __init__(
        self,
        db: Database,
        config: Config,
        date_provider: Optional[Callable[[], datetime]] = None,
        oauth_manager: Optional[GoogleOAuthManager] = None,
        client: Optional[GoogleTasksClient] = None,
    ):
        self.db = db
        self.config = config
        self.date_provider = date_provider
        self.sync_manager: Optional[Any] = None
        self._list_last_synced: Dict[str, str] = {}

        if oauth_manager:
            self.oauth_manager = oauth_manager
        else:
            self.oauth_manager = GoogleOAuthManager(
                token_path=config.google_token_path or (config.db_path.parent / "google_token.json"),
                client_id=config.google_client_id,
                client_secret=config.google_client_secret,
                credentials_file=config.google_credentials_file,
            )

        self.client = client or GoogleTasksClient(
            self.oauth_manager,
            timeout=(3.0, getattr(config, "google_sync_timeout", 7.0)),
        )
        self.task_service = TaskService(db, date_provider=date_provider)
        self.source_service = SourceService(db, date_provider=date_provider)
        self.daily_service = DailyService(db, date_provider=date_provider)

    def get_now(self) -> datetime:
        return self.date_provider() if self.date_provider else datetime.now()

    def is_authenticated(self) -> bool:
        return self.oauth_manager.is_authenticated()

    def has_credentials(self) -> bool:
        return self.oauth_manager.has_configured_credentials()

    def disconnect(self) -> None:
        """Disconnect and revoke Google Tasks access."""
        self.oauth_manager.disconnect()

    def get_cached_task_lists(self) -> List[Dict[str, Any]]:
        """Retrieve task lists stored in the local cache."""
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, title, updated, etag, cached_at
                FROM google_task_lists_cache
                ORDER BY title ASC
                """
            )
            rows = cursor.fetchall()
        return [dict(r) for r in rows]

    def prune_cached_task_list(self, list_id: str) -> None:
        """Remove a single task list from the local cache if deleted remotely."""
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM google_task_lists_cache WHERE id = ?", (list_id,))
            conn.commit()

    def enqueue_outbox(self, action: str, task_id: Optional[int], payload: Dict[str, Any]) -> int:
        """Enqueue an offline/pending mutation to be synchronized with Google Tasks."""
        now_iso = self.get_now().isoformat()
        payload_json = json.dumps(payload, ensure_ascii=False)
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO google_tasks_outbox (task_id, action, payload, created_at, target)
                VALUES (?, ?, ?, ?, 'tasks')
                """,
                (task_id, action, payload_json, now_iso),
            )
            outbox_id = cursor.lastrowid
            conn.commit()
        return outbox_id

    def get_pending_outbox(self) -> List[Dict[str, Any]]:
        """Retrieve all queued outbox records for Google Tasks in FIFO order."""
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, task_id, action, payload, created_at
                FROM google_tasks_outbox
                WHERE target = 'tasks' OR target IS NULL
                ORDER BY id ASC
                """
            )
            rows = cursor.fetchall()
        return [dict(r) for r in rows]

    def remove_outbox_item(self, outbox_id: int) -> None:
        """Remove a successfully processed item from the outbox."""
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM google_tasks_outbox WHERE id = ?", (outbox_id,))
            conn.commit()

    def get_task_google_mapping(self, task_id: int) -> Optional[Dict[str, Any]]:
        """Resolve Google Tasks remote mapping for any task."""
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT task_id, list_id, remote_id, etag, updated_at FROM task_google_tasks_map WHERE task_id = ?",
                (task_id,),
            )
            row = cursor.fetchone()
            if row:
                return dict(row)

            # Fallback to file_items if task.source_type == 'google_tasks'
            cursor.execute("SELECT source_type, source_id FROM tasks WHERE id = ?", (task_id,))
            t_row = cursor.fetchone()
            if t_row and t_row["source_type"] == "google_tasks" and t_row["source_id"]:
                cursor.execute("SELECT path, metadata FROM file_items WHERE id = ?", (t_row["source_id"],))
                fi_row = cursor.fetchone()
                if fi_row:
                    meta = json.loads(fi_row["metadata"] or "{}")
                    return {
                        "task_id": task_id,
                        "list_id": meta.get("list_id", "@default"),
                        "remote_id": fi_row["path"],
                        "etag": meta.get("etag", ""),
                        "updated_at": fi_row["updated_at"] if "updated_at" in fi_row.keys() else "",
                    }
        return None

    def drain_outbox(self) -> int:
        """
        Process queued outbox mutations against Google Tasks API.
        Returns the number of processed mutations.
        """
        if not self.is_authenticated():
            return 0

        items = self.get_pending_outbox()
        if not items:
            return 0

        processed = 0
        for item in items:
            action = item["action"]
            payload = json.loads(item["payload"] or "{}")

            # If payload task_id starts with local_, resolve to remote_id if task was already assigned one
            if payload.get("task_id", "").startswith("local_") and item.get("task_id"):
                with self.db.get_connection() as conn:
                    cursor = conn.cursor()
                    cursor.execute("SELECT remote_id FROM task_google_tasks_map WHERE task_id = ?", (item["task_id"],))
                    gtm_row = cursor.fetchone()
                    if gtm_row and not gtm_row["remote_id"].startswith("local_"):
                        payload["task_id"] = gtm_row["remote_id"]
                    else:
                        cursor.execute(
                            """
                            SELECT fi.path FROM file_items fi
                            JOIN tasks t ON t.source_id = fi.id
                            WHERE t.id = ?
                            """,
                            (item["task_id"],),
                        )
                        fi_row = cursor.fetchone()
                        if fi_row and not fi_row["path"].startswith("local_"):
                            payload["task_id"] = fi_row["path"]

            try:
                if action == "complete":
                    self.client.set_task_completed(
                        list_id=payload["list_id"],
                        task_id=payload["task_id"],
                        completed=True,
                    )
                elif action == "reopen":
                    self.client.set_task_completed(
                        list_id=payload["list_id"],
                        task_id=payload["task_id"],
                        completed=False,
                    )
                elif action == "update":
                    self.client.update_task(
                        list_id=payload["list_id"],
                        task_id=payload["task_id"],
                        title=payload.get("title"),
                        notes=payload.get("notes"),
                        due_date=payload.get("due_date"),
                        clear_due=payload.get("clear_due", False),
                    )
                elif action == "create":
                    local_task_id = payload.get("local_task_id") or item.get("task_id")
                    existing_remote_id = None
                    if local_task_id:
                        with self.db.get_connection() as conn:
                            cursor = conn.cursor()
                            cursor.execute(
                                "SELECT remote_id FROM task_google_tasks_map WHERE task_id = ?",
                                (local_task_id,),
                            )
                            gtm_row = cursor.fetchone()
                            if gtm_row and not gtm_row["remote_id"].startswith("local_"):
                                existing_remote_id = gtm_row["remote_id"]

                    if existing_remote_id:
                        # Task was already created remotely on a previous attempt; update rather than duplicate
                        created = self.client.update_task(
                            list_id=payload["list_id"],
                            task_id=existing_remote_id,
                            title=payload.get("title"),
                            notes=payload.get("notes"),
                            due_date=payload.get("due_date"),
                            status=payload.get("status"),
                            completed=payload.get("completed"),
                        ) or {"id": existing_remote_id}
                    else:
                        created = self.client.create_task(
                            list_id=payload["list_id"],
                            title=payload["title"],
                            notes=payload.get("notes", ""),
                            due_date=payload.get("due_date"),
                            status=payload.get("status"),
                            completed=payload.get("completed"),
                        )
                    local_task_id = payload.get("local_task_id")
                    if local_task_id and created.get("id"):
                        remote_id = created["id"]
                        now_iso = self.get_now().isoformat()
                        with self.db.get_connection() as conn:
                            cursor = conn.cursor()
                            cursor.execute(
                                """
                                INSERT INTO task_google_tasks_map (task_id, list_id, remote_id, etag, updated_at)
                                VALUES (?, ?, ?, ?, ?)
                                ON CONFLICT(task_id) DO UPDATE SET
                                    list_id=excluded.list_id,
                                    remote_id=excluded.remote_id,
                                    etag=excluded.etag,
                                    updated_at=excluded.updated_at
                                """,
                                (local_task_id, payload["list_id"], remote_id, created.get("etag", ""), now_iso),
                            )
                            cursor.execute("SELECT source_id FROM tasks WHERE id = ?", (local_task_id,))
                            t_row = cursor.fetchone()
                            if t_row and t_row["source_id"]:
                                fi_id = t_row["source_id"]
                                cursor.execute("SELECT metadata FROM file_items WHERE id = ?", (fi_id,))
                                fi_row = cursor.fetchone()
                                meta = json.loads(fi_row["metadata"] if fi_row else "{}")
                                meta["id"] = remote_id
                                meta["etag"] = created.get("etag", "")
                                meta["updated"] = created.get("updated", now_iso)
                                cursor.execute(
                                    "UPDATE file_items SET path = ?, metadata = ?, updated_at = ? WHERE id = ?",
                                    (remote_id, json.dumps(meta, ensure_ascii=False), now_iso, fi_id),
                                )
                                # Propagate remote_id to any subsequent queued outbox items for this task
                                cursor.execute(
                                    "SELECT id, payload FROM google_tasks_outbox WHERE task_id = ? AND id > ?",
                                    (local_task_id, item["id"]),
                                )
                                for f_row in cursor.fetchall():
                                    f_payload = json.loads(f_row["payload"] or "{}")
                                    if f_payload.get("task_id", "").startswith("local_"):
                                        f_payload["task_id"] = remote_id
                                        cursor.execute(
                                            "UPDATE google_tasks_outbox SET payload = ? WHERE id = ?",
                                            (json.dumps(f_payload, ensure_ascii=False), f_row["id"]),
                                        )
                                conn.commit()
                elif action == "delete":
                    self.client.delete_task(
                        list_id=payload["list_id"],
                        task_id=payload["task_id"],
                    )
                self.remove_outbox_item(item["id"])
                processed += 1
            except GoogleTasksNotFoundError:
                # Target resource no longer exists remotely, drop mutation safely
                self.remove_outbox_item(item["id"])
                processed += 1
            except (GoogleTasksNetworkError, requests_exception()) as e:
                # Network failed, record error and pause draining until next sync pass
                with self.db.get_connection() as conn:
                    cursor = conn.cursor()
                    cursor.execute(
                        "UPDATE google_tasks_outbox SET retry_count = retry_count + 1, last_error = ? WHERE id = ?",
                        (str(e), item["id"]),
                    )
                    conn.commit()
                break
            except Exception as e:
                # Malformed or unexpected error, record error and pause
                with self.db.get_connection() as conn:
                    cursor = conn.cursor()
                    cursor.execute(
                        "UPDATE google_tasks_outbox SET retry_count = retry_count + 1, last_error = ? WHERE id = ?",
                        (str(e), item["id"]),
                    )
                    conn.commit()
                break

        return processed

    def sync_all_lists(self, incremental: bool = True) -> SyncSummary:
        """
        Perform a full or incremental synchronization cycle across all Google Tasks lists:
        1. Drain pending outbox changes to Google Tasks.
        2. Sync all task lists.
        3. Sync tasks for each task list (auto-scheduling & completion reconciliation).
        Returns: SyncSummary(success, status_description, has_changes=...)
        """
        if not self.is_authenticated():
            return SyncSummary(False, "Not authenticated", has_changes=False)

        has_any_changes = False

        # 1. Drain outbox
        try:
            drained = self.drain_outbox()
            if drained > 0:
                has_any_changes = True
        except Exception:
            pass

        # 2. Sync task lists
        try:
            lists, is_offline = self.sync_task_lists()
            if is_offline:
                return SyncSummary(False, "Offline", has_changes=has_any_changes)
        except Exception as e:
            return SyncSummary(False, f"Failed syncing lists: {e}", has_changes=has_any_changes)

        # 3. Sync each list
        synced_count = 0
        now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        for l in lists:
            list_id = l.get("id")
            if not list_id:
                continue
            updated_min = self._list_last_synced.get(list_id) if incremental else None
            try:
                res = self.sync_tasks_for_list(list_id, updated_min=updated_min)
                self._list_last_synced[list_id] = now_utc
                if getattr(res, "has_changes", False):
                    has_any_changes = True
                synced_count += 1
            except GoogleTasksNotFoundError:
                continue
            except Exception as e:
                return SyncSummary(False, f"Failed syncing list '{l.get('title', list_id)}': {e}", has_changes=has_any_changes)

        return SyncSummary(True, f"Synced {synced_count} list(s)", has_changes=has_any_changes)

    def sync_task_lists(self) -> Tuple[List[Dict[str, Any]], bool]:
        """
        Synchronize task lists from Google Tasks into SQLite cache and Sources.
        Returns: (lists, is_offline)
        """
        if not self.is_authenticated():
            return self.get_cached_task_lists(), True

        now_iso = self.get_now().isoformat()
        try:
            remote_lists = self.client.get_task_lists()
        except (GoogleTasksNetworkError, GoogleTasksAuthError, requests_exception(), GoogleTasksAPIError):
            # Offline, auth, or API error: return cached lists
            return self.get_cached_task_lists(), True

        remote_ids = set()
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            for r_list in remote_lists:
                list_id = r_list.get("id", "")
                title = r_list.get("title", "")
                updated = r_list.get("updated", "")
                etag = r_list.get("etag", "")
                if not list_id or not title:
                    continue

                remote_ids.add(list_id)

                # Upsert into cache table
                cursor.execute(
                    """
                    INSERT INTO google_task_lists_cache (id, title, updated, etag, cached_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        title = excluded.title,
                        updated = excluded.updated,
                        etag = excluded.etag,
                        cached_at = excluded.cached_at,
                        updated_at = excluded.updated_at
                    """,
                    (list_id, title, updated, etag, now_iso, now_iso),
                )

                # Ensure corresponding Source entity exists
                source_path = f"google_tasks://list/{list_id}"
                cursor.execute(
                    "SELECT id FROM sources WHERE path = ?",
                    (source_path,),
                )
                existing_source = cursor.fetchone()
                if not existing_source:
                    cursor.execute(
                        """
                        INSERT INTO sources (name, path, scan_status, scanned_at, created_at, updated_at)
                        VALUES (?, ?, 'completed', ?, ?, ?)
                        """,
                        (title, source_path, now_iso, now_iso, now_iso),
                    )
                else:
                    cursor.execute(
                        "UPDATE sources SET name = ?, updated_at = ? WHERE id = ?",
                        (title, now_iso, existing_source["id"]),
                    )

            # Reconcile deleted remote lists: prune from google_task_lists_cache
            # Local tasks, sources, and daily_tasks remain intact.
            if remote_ids:
                placeholders = ",".join("?" for _ in remote_ids)
                cursor.execute(
                    f"DELETE FROM google_task_lists_cache WHERE id NOT IN ({placeholders})",
                    list(remote_ids),
                )
            else:
                cursor.execute("DELETE FROM google_task_lists_cache")

            conn.commit()

        return self.get_cached_task_lists(), False

    def get_source_for_list(self, list_id: str, list_title: str = "") -> Source:
        """Find or create the Source record for a Google Task list."""
        source_path = f"google_tasks://list/{list_id}"
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM sources WHERE path = ?", (source_path,))
            row = cursor.fetchone()
            if row:
                return Source(
                    id=row["id"],
                    name=row["name"],
                    path=row["path"],
                    canonical_uri=row["canonical_uri"],
                    scan_status=row["scan_status"],
                    scanned_at=row["scanned_at"],
                    last_error=row["last_error"],
                    created_at=row["created_at"],
                    updated_at=row["updated_at"],
                )

            now_iso = self.get_now().isoformat()
            title = list_title or f"Google Tasks ({list_id[:8]})"
            cursor.execute(
                """
                INSERT INTO sources (name, path, scan_status, scanned_at, created_at, updated_at)
                VALUES (?, ?, 'completed', ?, ?, ?)
                """,
                (title, source_path, now_iso, now_iso, now_iso),
            )
            s_id = cursor.lastrowid
            conn.commit()

        return Source(
            id=s_id,
            name=title,
            path=source_path,
            created_at=now_iso,
            updated_at=now_iso,
        )

    def get_tasks_for_list(self, list_id: str, include_completed: bool = True) -> List[Task]:
        """Retrieve local tasks linked to this Google Task list."""
        source_path = f"google_tasks://list/{list_id}"
        query = """
            SELECT DISTINCT t.id, t.title, t.description, t.source_type, t.source_id, t.status,
                   t.created_at, t.updated_at
            FROM tasks t
            LEFT JOIN file_items fi ON t.source_id = fi.id
            LEFT JOIN sources s ON fi.source_id = s.id
            LEFT JOIN task_google_tasks_map m ON m.task_id = t.id
            WHERE (
                (s.path = ? AND t.source_type = 'google_tasks')
                OR (m.list_id = ? AND NOT m.remote_id LIKE 'local_%')
            )
        """
        if not include_completed:
            query += " AND t.status = 'pending'"
        query += " ORDER BY t.created_at ASC"

        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(query, (source_path, list_id))
            rows = cursor.fetchall()

        return [
            Task(
                id=r["id"],
                title=r["title"],
                description=r["description"],
                source_type=r["source_type"],
                source_id=r["source_id"],
                status=r["status"],
                created_at=r["created_at"],
                updated_at=r["updated_at"],
            )
            for r in rows
        ]

    def sync_tasks_for_list(
        self,
        list_id: str,
        updated_min: Optional[str] = None,
    ) -> SyncResult:
        """
        Sync tasks from Google Tasks API into local SQLite database.
        Maps list to Source, tasks to FileItems and Tasks, and preserves local scheduling.
        Returns: SyncResult(tasks, is_offline, has_changes=...)
        """
        source = self.get_source_for_list(list_id)

        if not self.is_authenticated():
            return SyncResult(self.get_tasks_for_list(list_id), True, has_changes=False)

        try:
            remote_tasks = self.client.get_tasks(
                list_id,
                show_completed=True,
                show_hidden=True,
                updated_min=updated_min,
            )
        except (GoogleTasksNetworkError, GoogleTasksAuthError, requests_exception()):
            # Fallback to local cache
            return SyncResult(self.get_tasks_for_list(list_id), True, has_changes=False)
        except (GoogleTasksNotFoundError, GoogleTasksAPIError) as e:
            if getattr(e, "status_code", None) == 404 or isinstance(e, GoogleTasksNotFoundError):
                self.prune_cached_task_list(list_id)
                raise GoogleTasksNotFoundError(
                    f"Task list '{list_id}' not found on Google Tasks.",
                    response_body=getattr(e, "response_body", None),
                ) from e
            raise

        if updated_min and len(remote_tasks) == 0:
            return SyncResult(self.get_tasks_for_list(list_id, include_completed=True), False, has_changes=False)

        has_changes = False
        now_iso = self.get_now().isoformat()
        with self.db.get_connection() as conn:
            cursor = conn.cursor()

            for r_task in remote_tasks:
                task_id = r_task.get("id")
                if not task_id:
                    continue

                title = r_task.get("title", "").strip()
                if not title:
                    title = "(Untitled Task)"

                notes = r_task.get("notes", "") or ""
                due = r_task.get("due")
                status = r_task.get("status", "needsAction")
                is_remote_completed = (status == "completed")
                remote_completed_at = r_task.get("completed") if is_remote_completed else None

                metadata = {
                    "id": task_id,
                    "list_id": list_id,
                    "notes": notes,
                    "due": due,
                    "status": status,
                    "etag": r_task.get("etag", ""),
                    "updated": r_task.get("updated", ""),
                }
                meta_json = json.dumps(metadata, ensure_ascii=False)

                # 1. Resolve existing local task:
                # Check task_google_tasks_map first (covers manual, universal, and mapped google_tasks)
                cursor.execute(
                    """
                    SELECT m.task_id, m.list_id, m.etag,
                           t.id AS t_id, t.title AS t_title, t.description AS t_desc,
                           t.source_type, t.source_id, t.status AS t_status
                    FROM task_google_tasks_map m
                    JOIN tasks t ON t.id = m.task_id
                    WHERE m.remote_id = ?
                    """,
                    (task_id,),
                )
                mapped_row = cursor.fetchone()

                existing_task = None
                existing_fi = None
                fi_id = None
                t_id = None

                if mapped_row:
                    t_id = mapped_row["t_id"]
                    existing_task = {
                        "id": mapped_row["t_id"],
                        "title": mapped_row["t_title"],
                        "description": mapped_row["t_desc"] or "",
                        "source_type": mapped_row["source_type"],
                        "source_id": mapped_row["source_id"],
                        "status": mapped_row["t_status"],
                    }
                    if mapped_row["source_type"] == "google_tasks" and mapped_row["source_id"]:
                        cursor.execute(
                            "SELECT id, title, completed, completed_at, metadata FROM file_items WHERE id = ?",
                            (mapped_row["source_id"],),
                        )
                        existing_fi = cursor.fetchone()
                        if existing_fi:
                            fi_id = existing_fi["id"]
                else:
                    # Check file_items by path if not found in task_google_tasks_map
                    cursor.execute(
                        """
                        SELECT id, title, completed, completed_at, metadata FROM file_items
                        WHERE source_id = ? AND path = ?
                        """,
                        (source.id, task_id),
                    )
                    existing_fi = cursor.fetchone()
                    if existing_fi:
                        fi_id = existing_fi["id"]
                        cursor.execute(
                            """
                            SELECT id, title, description, source_type, source_id, status FROM tasks
                            WHERE source_type = 'google_tasks' AND source_id = ?
                            """,
                            (fi_id,),
                        )
                        t_row = cursor.fetchone()
                        if t_row:
                            t_id = t_row["id"]
                            existing_task = {
                                "id": t_row["id"],
                                "title": t_row["title"],
                                "description": t_row["description"] or "",
                                "source_type": t_row["source_type"],
                                "source_id": t_row["source_id"],
                                "status": t_row["status"],
                            }

                # Check if there are pending outbox items for this task
                has_pending_outbox = False
                pending_actions = set()
                if t_id:
                    cursor.execute(
                        "SELECT id, action, payload FROM google_tasks_outbox WHERE task_id = ?",
                        (t_id,),
                    )
                    outbox_rows = cursor.fetchall()
                    if outbox_rows:
                        has_pending_outbox = True
                        pending_actions = {r["action"] for r in outbox_rows}

                if has_pending_outbox:
                    # Keep local status until outbox is drained
                    task_status = existing_task["status"] if existing_task else "pending"
                    final_completed = (task_status == "completed")
                    final_completed_at = existing_fi["completed_at"] if existing_fi else None
                    if "update" in pending_actions and existing_task:
                        title_to_save = existing_task["title"]
                        notes_to_save = existing_task["description"] or ""
                    else:
                        title_to_save = title
                        notes_to_save = notes
                else:
                    # Remote status is authoritative
                    task_status = "completed" if is_remote_completed else "pending"
                    final_completed = is_remote_completed
                    final_completed_at = remote_completed_at or (now_iso if is_remote_completed else None)
                    title_to_save = title
                    notes_to_save = notes

                if not existing_task:
                    has_changes = True
                    if not fi_id:
                        cursor.execute(
                            """
                            INSERT INTO file_items (source_id, path, title, type, metadata, completed, completed_at, created_at, updated_at)
                            VALUES (?, ?, ?, 'task', ?, ?, ?, ?, ?)
                            """,
                            (source.id, task_id, title_to_save, meta_json, 1 if final_completed else 0, final_completed_at, now_iso, now_iso),
                        )
                        fi_id = cursor.lastrowid

                    cursor.execute(
                        """
                        INSERT INTO tasks (title, description, source_type, source_id, status, created_at, updated_at)
                        VALUES (?, ?, 'google_tasks', ?, ?, ?, ?)
                        """,
                        (title_to_save, notes_to_save, fi_id, task_status, now_iso, now_iso),
                    )
                    t_id = cursor.lastrowid
                else:
                    # Update existing task
                    if (
                        existing_task["title"] != title_to_save
                        or existing_task["description"] != notes_to_save
                        or existing_task["status"] != task_status
                    ):
                        has_changes = True
                        cursor.execute(
                            """
                            UPDATE tasks
                            SET title = ?, description = ?, status = ?, updated_at = ?
                            WHERE id = ?
                            """,
                            (title_to_save, notes_to_save, task_status, now_iso, t_id),
                        )

                    if fi_id is not None:
                        old_meta = json.loads(existing_fi["metadata"] or "{}") if existing_fi else {}
                        if (
                            existing_fi["title"] != title_to_save
                            or bool(existing_fi["completed"]) != final_completed
                            or old_meta.get("notes", "") != notes_to_save
                            or old_meta.get("due") != due
                            or old_meta.get("status") != status
                        ):
                            has_changes = True
                        cursor.execute(
                            """
                            UPDATE file_items
                            SET title = ?, metadata = ?, completed = ?, completed_at = ?, updated_at = ?
                            WHERE id = ?
                            """,
                            (title_to_save, meta_json, 1 if final_completed else 0, final_completed_at, now_iso, fi_id),
                        )

                # Persist mapping in task_google_tasks_map
                cursor.execute(
                    """
                    INSERT INTO task_google_tasks_map (task_id, list_id, remote_id, etag, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(task_id) DO UPDATE SET
                        list_id = excluded.list_id,
                        remote_id = excluded.remote_id,
                        etag = excluded.etag,
                        updated_at = excluded.updated_at
                    """,
                    (t_id, list_id, task_id, r_task.get("etag", ""), now_iso),
                )

                # Reconcile completion in daily_tasks
                if final_completed:
                    cursor.execute(
                        """
                        UPDATE daily_tasks
                        SET completed_at = COALESCE(completed_at, ?)
                        WHERE task_id = ?
                        """,
                        (final_completed_at or now_iso, t_id),
                    )
                else:
                    cursor.execute(
                        """
                        UPDATE daily_tasks
                        SET completed_at = NULL
                        WHERE task_id = ?
                        """,
                        (t_id,),
                    )

                # Due date / scheduling reconciliation
                due_date_str = parse_rfc3339_due_date(due) if due else None
                cursor.execute(
                    "SELECT id, date, completed_at FROM daily_tasks WHERE task_id = ?",
                    (t_id,),
                )
                sched_rows = cursor.fetchall()

                if due_date_str:
                    # Task has a remote due date
                    if sched_rows:
                        for s_row in sched_rows:
                            if s_row["completed_at"] is None and s_row["date"] != due_date_str:
                                has_changes = True
                                cursor.execute(
                                    "UPDATE daily_tasks SET date = ? WHERE id = ?",
                                    (due_date_str, s_row["id"]),
                                )
                    elif task_status == "pending":
                        has_changes = True
                        cursor.execute(
                            "SELECT COALESCE(MAX(position), 0) AS max_pos FROM daily_tasks WHERE date = ?",
                            (due_date_str,),
                        )
                        max_pos = cursor.fetchone()["max_pos"]
                        cursor.execute(
                            """
                            INSERT INTO daily_tasks (task_id, date, position, completed_at)
                            VALUES (?, ?, ?, NULL)
                            """,
                            (t_id, due_date_str, max_pos + 1),
                        )
                else:
                    # Task does NOT have a due date:
                    # Only if this is a google_tasks item without due date, unschedule pending daily entries
                    if existing_task and existing_task.get("source_type") == "google_tasks":
                        if sched_rows:
                            pending_sched = [s for s in sched_rows if s["completed_at"] is None]
                            if pending_sched:
                                has_changes = True
                            cursor.execute(
                                """
                                DELETE FROM daily_tasks
                                WHERE task_id = ? AND completed_at IS NULL
                                """,
                                (t_id,),
                            )

            # Update source scanned_at and updated_at
            cursor.execute(
                "UPDATE sources SET scanned_at = ?, updated_at = ? WHERE id = ?",
                (now_iso, now_iso, source.id),
            )
            conn.commit()

        return SyncResult(
            self.get_tasks_for_list(list_id, include_completed=True),
            False,
            has_changes=has_changes,
        )

    def create_task(
        self,
        list_id: str,
        title: str,
        notes: str = "",
        due_date: Optional[str] = None,
        schedule_date: Optional[str] = None,
        sync_remote: Optional[bool] = None,
    ) -> Task:
        """
        Create a new task both in Google Tasks (if connected) and in local SQLite.
        Schedules to schedule_date or due_date if requested.
        """
        clean_title = title.strip()
        if not clean_title:
            raise ValueError("Task title cannot be empty.")

        source = self.get_source_for_list(list_id)
        remote_task: Optional[Dict[str, Any]] = None

        if sync_remote is None:
            sync_remote = (self.sync_manager is None)

        if sync_remote and self.is_authenticated():
            try:
                remote_task = self.client.create_task(
                    list_id=list_id,
                    title=clean_title,
                    notes=notes.strip(),
                    due_date=due_date,
                )
            except Exception:
                # If network or auth fails, proceed with local creation
                remote_task = None

        task_path = remote_task.get("id") if remote_task else f"local_{int(time.time() * 1000)}"
        now_iso = self.get_now().isoformat()
        due_val = remote_task.get("due") if remote_task else (format_rfc3339_due_date(due_date) if due_date else None)

        metadata = {
            "id": task_path,
            "list_id": list_id,
            "notes": notes.strip(),
            "due": due_val,
            "status": "needsAction",
            "etag": remote_task.get("etag", "") if remote_task else "",
            "updated": remote_task.get("updated", now_iso) if remote_task else now_iso,
        }
        meta_json = json.dumps(metadata, ensure_ascii=False)

        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO file_items (source_id, path, title, type, metadata, completed, created_at, updated_at)
                VALUES (?, ?, ?, 'task', ?, 0, ?, ?)
                """,
                (source.id, task_path, clean_title, meta_json, now_iso, now_iso),
            )
            fi_id = cursor.lastrowid

            cursor.execute(
                """
                INSERT INTO tasks (title, description, source_type, source_id, status, created_at, updated_at)
                VALUES (?, ?, 'google_tasks', ?, 'pending', ?, ?)
                """,
                (clean_title, notes.strip(), fi_id, now_iso, now_iso),
            )
            t_id = cursor.lastrowid
            cursor.execute(
                """
                INSERT INTO task_google_tasks_map (task_id, list_id, remote_id, etag, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    list_id=excluded.list_id,
                    remote_id=excluded.remote_id,
                    etag=excluded.etag,
                    updated_at=excluded.updated_at
                """,
                (t_id, list_id, task_path, remote_task.get("etag", "") if remote_task else "", now_iso),
            )
            conn.commit()

        task = Task(
            id=t_id,
            title=clean_title,
            description=notes.strip(),
            source_type="google_tasks",
            source_id=fi_id,
            status="pending",
            created_at=now_iso,
            updated_at=now_iso,
        )

        if not remote_task:
            self.enqueue_outbox(
                action="create",
                task_id=t_id,
                payload={
                    "list_id": list_id,
                    "title": clean_title,
                    "notes": notes.strip(),
                    "due_date": due_date,
                    "local_task_id": t_id,
                },
            )

        target_sched = schedule_date or due_date
        if target_sched:
            self.daily_service.add_task_to_date(t_id, target_sched[:10])

        if self.sync_manager:
            self.sync_manager.notify_change()
            self.sync_manager.trigger_sync()

        return task

    def update_task(
        self,
        task_id: int,
        title: Optional[str] = None,
        notes: Optional[str] = None,
        due_date: Optional[str] = None,
        clear_due: bool = False,
        sync_remote: Optional[bool] = None,
    ) -> Optional[Task]:
        """
        Update task title, description/notes, and due date.
        Propagates changes to Google Tasks API and local SQLite tables.
        """
        task = self.task_service.get_task_by_id(task_id)
        if not task:
            return None

        if sync_remote is None:
            sync_remote = (self.sync_manager is None)

        # Push to Google Tasks if linked
        if task.source_type == "google_tasks" and task.source_id:
            with self.db.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT path, metadata FROM file_items WHERE id = ?",
                    (task.source_id,),
                )
                fi_row = cursor.fetchone()

            if fi_row:
                remote_id = fi_row["path"]
                meta = json.loads(fi_row["metadata"] or "{}")
                list_id = meta.get("list_id", "@default")

                pushed = False
                if sync_remote and not remote_id.startswith("local_") and self.is_authenticated():
                    try:
                        self.client.update_task(
                            list_id=list_id,
                            task_id=remote_id,
                            title=title,
                            notes=notes,
                            due_date=due_date,
                            clear_due=clear_due,
                        )
                        pushed = True
                    except (GoogleTasksNetworkError, requests_exception()):
                        pushed = False

                if not pushed:
                    self.enqueue_outbox(
                        action="update",
                        task_id=task_id,
                        payload={
                            "list_id": list_id,
                            "task_id": remote_id,
                            "title": title,
                            "notes": notes,
                            "due_date": due_date,
                            "clear_due": clear_due,
                        },
                    )

                # Update metadata in file_items
                if title is not None:
                    meta["title"] = title.strip()
                if notes is not None:
                    meta["notes"] = notes.strip()
                if clear_due:
                    meta["due"] = None
                elif due_date is not None:
                    meta["due"] = format_rfc3339_due_date(due_date)

                now_iso = self.get_now().isoformat()
                with self.db.get_connection() as conn:
                    cursor = conn.cursor()
                    cursor.execute(
                        "UPDATE file_items SET metadata = ?, updated_at = ? WHERE id = ?",
                        (json.dumps(meta, ensure_ascii=False), now_iso, task.source_id),
                    )
                    conn.commit()

        # Update local task title and description
        updated_task = self.task_service.update_task(task_id, title=title, description=notes)

        # Update schedule if due_date or clear_due specified
        if clear_due:
            self.daily_service.unschedule_task(task_id)
        elif due_date is not None and due_date.strip():
            self.daily_service.reschedule_task(task_id, due_date[:10])

        if self.sync_manager:
            self.sync_manager.notify_change()
            self.sync_manager.trigger_sync()

        return updated_task

    def toggle_task_completion(
        self,
        task_id: int,
        sync_remote: Optional[bool] = None,
    ) -> Optional[Task]:
        """Toggle task completion and synchronize with Google Tasks."""
        task = self.task_service.get_task_by_id(task_id)
        if not task:
            return None

        if sync_remote is None:
            sync_remote = (self.sync_manager is None)

        new_status = "completed" if task.status == "pending" else "pending"
        self.task_service.update_task_status(task_id, new_status)

        # Sync remote if google_tasks
        if task.source_type == "google_tasks" and task.source_id:
            with self.db.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT path, metadata FROM file_items WHERE id = ?",
                    (task.source_id,),
                )
                fi_row = cursor.fetchone()

            if fi_row:
                remote_id = fi_row["path"]
                meta = json.loads(fi_row["metadata"] or "{}")
                list_id = meta.get("list_id", "@default")
                action = "complete" if new_status == "completed" else "reopen"

                # Update daily_tasks completion if scheduled
                now_iso = self.get_now().isoformat()
                with self.db.get_connection() as conn:
                    cursor = conn.cursor()
                    cursor.execute(
                        "UPDATE daily_tasks SET completed_at = ? WHERE task_id = ?",
                        (now_iso if new_status == "completed" else None, task_id),
                    )
                    cursor.execute(
                        "UPDATE file_items SET completed = ?, completed_at = ?, updated_at = ? WHERE id = ?",
                        (1 if new_status == "completed" else 0, now_iso if new_status == "completed" else None, now_iso, task.source_id),
                    )
                    conn.commit()

                pushed = False
                if sync_remote and not remote_id.startswith("local_") and self.is_authenticated():
                    try:
                        self.client.set_task_completed(
                            list_id=list_id,
                            task_id=remote_id,
                            completed=(new_status == "completed"),
                        )
                        pushed = True
                    except Exception:
                        pushed = False

                if not pushed:
                    self.enqueue_outbox(
                        action=action,
                        task_id=task_id,
                        payload={"list_id": list_id, "task_id": remote_id},
                    )

        if self.sync_manager:
            self.sync_manager.notify_change()
            self.sync_manager.trigger_sync()

        return self.task_service.get_task_by_id(task_id)

    def delete_task(
        self,
        task_id: int,
        sync_remote: Optional[bool] = None,
    ) -> bool:
        """Permanently delete a task locally and from Google Tasks."""
        task = self.task_service.get_task_by_id(task_id)
        if not task:
            return False

        if sync_remote is None:
            sync_remote = (self.sync_manager is None)

        if task.source_type == "google_tasks" and task.source_id:
            with self.db.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT path, metadata FROM file_items WHERE id = ?",
                    (task.source_id,),
                )
                fi_row = cursor.fetchone()

            if fi_row and fi_row["path"].startswith("local_"):
                with self.db.get_connection() as conn:
                    cursor = conn.cursor()
                    cursor.execute("DELETE FROM google_tasks_outbox WHERE task_id = ?", (task_id,))
                    conn.commit()
            elif fi_row:
                remote_id = fi_row["path"]
                meta = json.loads(fi_row["metadata"] or "{}")
                list_id = meta.get("list_id", "@default")
                pushed = False
                if sync_remote and self.is_authenticated():
                    try:
                        self.client.delete_task(list_id=list_id, task_id=remote_id)
                        pushed = True
                    except Exception:
                        pushed = False

                if not pushed:
                    self.enqueue_outbox(
                        action="delete",
                        task_id=task_id,
                        payload={"list_id": list_id, "task_id": remote_id},
                    )

            if task.source_id:
                with self.db.get_connection() as conn:
                    cursor = conn.cursor()
                    cursor.execute("DELETE FROM file_items WHERE id = ?", (task.source_id,))
                    conn.commit()

        res = self.task_service.delete_task(task_id)
        if self.sync_manager:
            self.sync_manager.notify_change()
            self.sync_manager.trigger_sync()
        return res

    def sync_task_created(
        self,
        task: Task,
        scheduled_date: Optional[str] = None,
        list_id: Optional[str] = None,
        status: Optional[str] = None,
        completed: Optional[str] = None,
    ) -> Optional[int]:
        """
        Synchronize a newly created manual or universal task to Google Tasks.
        Idempotent: if already mapped, does not enqueue duplicate.
        """
        if not getattr(self.config, "google_tasks_enabled", True):
            return None
        if task.source_type in ("filemeta", "telegram", "youtube"):
            return None
        if self.get_task_google_mapping(task.id):
            return None

        target_list = list_id or getattr(self.config, "google_default_task_list", "@default")
        temp_remote_id = f"local_{task.id}_{int(time.time() * 1000)}"
        now_iso = self.get_now().isoformat()

        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO task_google_tasks_map (task_id, list_id, remote_id, etag, updated_at)
                VALUES (?, ?, ?, '', ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    list_id=excluded.list_id,
                    remote_id=excluded.remote_id,
                    updated_at=excluded.updated_at
                """,
                (task.id, target_list, temp_remote_id, now_iso),
            )
            conn.commit()

        if status is None and getattr(task, "completed", False):
            status = "completed"
        if completed is None and getattr(task, "completed", False) and getattr(task, "completed_at", None):
            completed = task.completed_at

        payload = {
            "list_id": target_list,
            "title": task.title,
            "notes": task.description or "",
            "due_date": scheduled_date,
            "local_task_id": task.id,
            "task_id": temp_remote_id,
        }
        if status:
            payload["status"] = status
        if completed:
            payload["completed"] = completed

        outbox_id = self.enqueue_outbox(
            action="create",
            task_id=task.id,
            payload=payload,
        )
        if self.sync_manager:
            self.sync_manager.trigger_sync()
        return outbox_id

    def sync_task_updated(
        self,
        task: Task,
        title: Optional[str] = None,
        notes: Optional[str] = None,
        due_date: Optional[str] = None,
        clear_due: bool = False,
    ) -> None:
        """Propagate title, notes, or due-date changes to Google Tasks if task is mapped."""
        mapping = self.get_task_google_mapping(task.id)
        if not mapping:
            return
        list_id = mapping["list_id"]
        remote_id = mapping["remote_id"]

        self.enqueue_outbox(
            action="update",
            task_id=task.id,
            payload={
                "list_id": list_id,
                "task_id": remote_id,
                "title": title if title is not None else task.title,
                "notes": notes if notes is not None else task.description,
                "due_date": due_date,
                "clear_due": clear_due,
            },
        )
        if self.sync_manager:
            self.sync_manager.trigger_sync()

    def sync_task_completed(self, task_id: int, is_completed: bool) -> None:
        """Propagate completion status to Google Tasks if task is mapped."""
        mapping = self.get_task_google_mapping(task_id)
        if not mapping:
            return
        list_id = mapping["list_id"]
        remote_id = mapping["remote_id"]
        action = "complete" if is_completed else "reopen"
        self.enqueue_outbox(
            action=action,
            task_id=task_id,
            payload={
                "list_id": list_id,
                "task_id": remote_id,
            },
        )
        if self.sync_manager:
            self.sync_manager.trigger_sync()

    def sync_task_deleted(self, task_id: int) -> None:
        """Delete task from Google Tasks if mapped and clean up mapping."""
        mapping = self.get_task_google_mapping(task_id)
        if mapping:
            list_id = mapping["list_id"]
            remote_id = mapping["remote_id"]
            if not remote_id.startswith("local_"):
                self.enqueue_outbox(
                    action="delete",
                    task_id=task_id,
                    payload={
                        "list_id": list_id,
                        "task_id": remote_id,
                    },
                )
            with self.db.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("DELETE FROM task_google_tasks_map WHERE task_id = ?", (task_id,))
                conn.commit()
            if self.sync_manager:
                self.sync_manager.trigger_sync()


def requests_exception():
    """Helper to catch requests exceptions without importing requests at top level."""
    import requests
    return requests.RequestException
