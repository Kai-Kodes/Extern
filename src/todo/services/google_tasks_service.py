"""Service for managing Google Tasks synchronization, local persistence, and offline caching."""

import json
import time
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

from todo.config import Config
from todo.database import Database
from todo.integrations.google_tasks import (
    GoogleOAuthManager,
    GoogleTasksAPIError,
    GoogleTasksAuthError,
    GoogleTasksClient,
    GoogleTasksNetworkError,
    format_rfc3339_due_date,
    parse_rfc3339_due_date,
)
from todo.models import FileItem, Source, Task
from todo.services.daily_service import DailyService
from todo.services.source_service import SourceService
from todo.services.task_service import TaskService


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

        if oauth_manager:
            self.oauth_manager = oauth_manager
        else:
            self.oauth_manager = GoogleOAuthManager(
                token_path=config.google_token_path or (config.db_path.parent / "google_token.json"),
                client_id=config.google_client_id,
                client_secret=config.google_client_secret,
                credentials_file=config.google_credentials_file,
            )

        self.client = client or GoogleTasksClient(self.oauth_manager)
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
        except (GoogleTasksNetworkError, GoogleTasksAuthError, requests_exception()):
            # Offline or auth error: return cached lists
            return self.get_cached_task_lists(), True

        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            for r_list in remote_lists:
                list_id = r_list.get("id", "")
                title = r_list.get("title", "")
                updated = r_list.get("updated", "")
                etag = r_list.get("etag", "")
                if not list_id or not title:
                    continue

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

    def get_tasks_for_list(self, list_id: str) -> List[Task]:
        """Retrieve local tasks linked to this Google Task list."""
        source_path = f"google_tasks://list/{list_id}"
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT t.id, t.title, t.description, t.source_type, t.source_id, t.status,
                       t.created_at, t.updated_at
                FROM tasks t
                JOIN file_items fi ON t.source_id = fi.id
                JOIN sources s ON fi.source_id = s.id
                WHERE s.path = ? AND t.source_type = 'google_tasks'
                ORDER BY t.created_at ASC
                """,
                (source_path,),
            )
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

    def sync_tasks_for_list(self, list_id: str) -> Tuple[List[Task], bool]:
        """
        Sync tasks from Google Tasks API into local SQLite database.
        Maps list to Source, tasks to FileItems and Tasks, and preserves local scheduling.
        Returns: (tasks, is_offline)
        """
        source = self.get_source_for_list(list_id)

        if not self.is_authenticated():
            return self.get_tasks_for_list(list_id), True

        try:
            remote_tasks = self.client.get_tasks(list_id, show_completed=True, show_hidden=True)
        except (GoogleTasksNetworkError, GoogleTasksAuthError, requests_exception()):
            # Fallback to local cache
            return self.get_tasks_for_list(list_id), True

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
                is_completed = (status == "completed")
                completed_at = r_task.get("completed") if is_completed else None

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

                # Look up existing FileItem by path (which stores task_id)
                cursor.execute(
                    """
                    SELECT id, completed, completed_at FROM file_items
                    WHERE source_id = ? AND path = ?
                    """,
                    (source.id, task_id),
                )
                existing_fi = cursor.fetchone()

                if existing_fi:
                    fi_id = existing_fi["id"]
                    # If locally completed, preserve local completed status and timestamp
                    local_completed = bool(existing_fi["completed"])
                    final_completed = local_completed or is_completed
                    final_completed_at = existing_fi["completed_at"] or completed_at

                    cursor.execute(
                        """
                        UPDATE file_items
                        SET title = ?, metadata = ?, completed = ?, completed_at = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (title, meta_json, 1 if final_completed else 0, final_completed_at, now_iso, fi_id),
                    )
                else:
                    cursor.execute(
                        """
                        INSERT INTO file_items (source_id, path, title, type, metadata, completed, completed_at, created_at, updated_at)
                        VALUES (?, ?, ?, 'task', ?, ?, ?, ?, ?)
                        """,
                        (source.id, task_id, title, meta_json, 1 if is_completed else 0, completed_at, now_iso, now_iso),
                    )
                    fi_id = cursor.lastrowid

                # Ensure linked Task entity exists
                cursor.execute(
                    """
                    SELECT id, status FROM tasks
                    WHERE source_type = 'google_tasks' AND source_id = ?
                    """,
                    (fi_id,),
                )
                existing_task = cursor.fetchone()
                task_status = "completed" if (is_completed or (existing_fi and existing_fi["completed"])) else "pending"

                if existing_task:
                    t_id = existing_task["id"]
                    # Update title, description, and status
                    cursor.execute(
                        """
                        UPDATE tasks
                        SET title = ?, description = ?, status = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (title, notes, task_status, now_iso, t_id),
                    )
                else:
                    cursor.execute(
                        """
                        INSERT INTO tasks (title, description, source_type, source_id, status, created_at, updated_at)
                        VALUES (?, ?, 'google_tasks', ?, ?, ?, ?)
                        """,
                        (title, notes, fi_id, task_status, now_iso, now_iso),
                    )
                    t_id = cursor.lastrowid

                # If due date is present in RFC 3339 format, map to calendar date YYYY-MM-DD
                if due:
                    due_date_str = parse_rfc3339_due_date(due)
                    if due_date_str:
                        # Check if scheduled on daily_tasks
                        cursor.execute(
                            "SELECT id FROM daily_tasks WHERE task_id = ?",
                            (t_id,),
                        )
                        sched_row = cursor.fetchone()
                        if not sched_row and task_status == "pending":
                            # Schedule for due_date_str
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

            # Update source scanned_at and updated_at
            cursor.execute(
                "UPDATE sources SET scanned_at = ?, updated_at = ? WHERE id = ?",
                (now_iso, now_iso, source.id),
            )
            conn.commit()

        return self.get_tasks_for_list(list_id), False

    def create_task(
        self,
        list_id: str,
        title: str,
        notes: str = "",
        due_date: Optional[str] = None,
        schedule_date: Optional[str] = None,
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

        if self.is_authenticated():
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

        target_sched = schedule_date or due_date
        if target_sched:
            self.daily_service.add_task_to_date(t_id, target_sched[:10])

        return task

    def update_task(
        self,
        task_id: int,
        title: Optional[str] = None,
        notes: Optional[str] = None,
        due_date: Optional[str] = None,
        clear_due: bool = False,
    ) -> Optional[Task]:
        """
        Update task title, description/notes, and due date.
        Propagates changes to Google Tasks API and local SQLite tables.
        """
        task = self.task_service.get_task_by_id(task_id)
        if not task:
            return None

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

                if not remote_id.startswith("local_") and self.is_authenticated():
                    self.client.update_task(
                        list_id=list_id,
                        task_id=remote_id,
                        title=title,
                        notes=notes,
                        due_date=due_date,
                        clear_due=clear_due,
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

        return updated_task

    def toggle_task_completion(self, task_id: int) -> Optional[Task]:
        """Toggle task completion and synchronize with Google Tasks."""
        task = self.task_service.get_task_by_id(task_id)
        if not task:
            return None

        new_status = "completed" if task.status == "pending" else "pending"
        self.task_service.update_task_status(task_id, new_status)

        # Sync remote if google_tasks
        if task.source_type == "google_tasks" and task.source_id and self.is_authenticated():
            with self.db.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT path, metadata FROM file_items WHERE id = ?",
                    (task.source_id,),
                )
                fi_row = cursor.fetchone()

            if fi_row and not fi_row["path"].startswith("local_"):
                meta = json.loads(fi_row["metadata"] or "{}")
                list_id = meta.get("list_id", "@default")
                try:
                    self.client.set_task_completed(
                        list_id=list_id,
                        task_id=fi_row["path"],
                        completed=(new_status == "completed"),
                    )
                except Exception:
                    pass

        return self.task_service.get_task_by_id(task_id)

    def delete_task(self, task_id: int) -> bool:
        """Permanently delete a task locally and from Google Tasks."""
        task = self.task_service.get_task_by_id(task_id)
        if not task:
            return False

        if task.source_type == "google_tasks" and task.source_id:
            with self.db.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT path, metadata FROM file_items WHERE id = ?",
                    (task.source_id,),
                )
                fi_row = cursor.fetchone()

            if fi_row and not fi_row["path"].startswith("local_") and self.is_authenticated():
                meta = json.loads(fi_row["metadata"] or "{}")
                list_id = meta.get("list_id", "@default")
                try:
                    self.client.delete_task(list_id=list_id, task_id=fi_row["path"])
                except Exception:
                    pass

            if task.source_id:
                with self.db.get_connection() as conn:
                    cursor = conn.cursor()
                    cursor.execute("DELETE FROM file_items WHERE id = ?", (task.source_id,))
                    conn.commit()

        return self.task_service.delete_task(task_id)


def requests_exception():
    """Helper to catch requests exceptions without importing requests at top level."""
    import requests
    return requests.RequestException
