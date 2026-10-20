"""Service for managing core Task entities."""

from datetime import datetime
from typing import Callable, List, Optional, Tuple

from todo.database import Database
from todo.models import FileItem, Task


class TaskService:
    def __init__(
        self,
        db: Database,
        date_provider: Optional[Callable[[], datetime]] = None,
    ):
        self.db = db
        self.date_provider = date_provider

    def get_now(self) -> datetime:
        """Return the current datetime, supporting injected test clocks."""
        return self.date_provider() if self.date_provider else datetime.now()

    def create_task(
        self,
        title: str,
        description: str = "",
        source_type: str = "manual",
        source_id: Optional[int] = None,
        status: str = "pending",
    ) -> Task:
        """Create a new task entity."""
        now = self.get_now().isoformat()
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO tasks (title, description, source_type, source_id, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (title.strip(), description.strip(), source_type, source_id, status, now, now),
            )
            task_id = cursor.lastrowid
            conn.commit()

        return Task(
            id=task_id,
            title=title.strip(),
            description=description.strip(),
            source_type=source_type,
            source_id=source_id,
            status=status,
            created_at=now,
            updated_at=now,
        )

    def get_task_by_id(self, task_id: int) -> Optional[Task]:
        """Retrieve a task by ID."""
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, title, description, source_type, source_id, status, created_at, updated_at
                FROM tasks
                WHERE id = ?
                """,
                (task_id,),
            )
            r = cursor.fetchone()

        if not r:
            return None

        return Task(
            id=r["id"],
            title=r["title"],
            description=r["description"],
            source_type=r["source_type"],
            source_id=r["source_id"],
            status=r["status"],
            created_at=r["created_at"],
            updated_at=r["updated_at"],
        )

    def get_universal_tasks(self) -> List[Task]:
        """
        Retrieve all universal tasks.
        Includes user universal tasks and pending Google tasks without an active daily schedule.
        """
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, title, description, source_type, source_id, status, created_at, updated_at
                FROM tasks
                WHERE (source_type = 'universal')
                   OR (
                       source_type = 'google_tasks'
                       AND status = 'pending'
                       AND id NOT IN (
                           SELECT task_id FROM daily_tasks WHERE completed_at IS NULL
                       )
                   )
                ORDER BY created_at ASC
                """
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

    def get_or_create_source_task(
        self,
        file_item: FileItem,
        source_name: str = "",
        source_type: str = "filemeta",
    ) -> Task:
        """
        Get an existing task for a Filemeta or Telegram item, or create a new one.
        Ensures a single task entity per source item.
        """
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, title, description, source_type, source_id, status, created_at, updated_at
                FROM tasks
                WHERE source_type = ? AND source_id = ?
                """,
                (source_type, file_item.id),
            )
            row = cursor.fetchone()

        if row:
            return Task(
                id=row["id"],
                title=row["title"],
                description=row["description"],
                source_type=row["source_type"],
                source_id=row["source_id"],
                status=row["status"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )

        desc = f"Source: {source_name}" if source_name else ""
        initial_status = "completed" if file_item.completed else "pending"
        return self.create_task(
            title=file_item.title,
            description=desc,
            source_type=source_type,
            source_id=file_item.id,
            status=initial_status,
        )

    def get_or_create_filemeta_task(
        self, file_item: FileItem, source_name: str = ""
    ) -> Task:
        """Alias for backward compatibility."""
        return self.get_or_create_source_task(
            file_item, source_name=source_name, source_type="filemeta"
        )

    def get_or_create_telegram_task(
        self, file_item: FileItem, source_name: str = ""
    ) -> Task:
        """Get or create task for a Telegram lesson."""
        return self.get_or_create_source_task(
            file_item, source_name=source_name, source_type="telegram"
        )

    def get_or_create_youtube_task(
        self, file_item: FileItem, source_name: str = ""
    ) -> Task:
        """Get or create task for a YouTube playlist video."""
        return self.get_or_create_source_task(
            file_item, source_name=source_name, source_type="youtube"
        )

    def get_or_create_video_task(
        self,
        title: str,
        canonical_url: str,
        video_id: Optional[str] = None,
    ) -> Tuple[Task, bool]:
        """
        Get or create a task for a standalone YouTube video.
        Prevents duplicate tasks using the canonical URL or video ID.
        Returns: (Task, is_new: bool)
        """
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            # Search by canonical URL in description
            cursor.execute(
                """
                SELECT id, title, description, source_type, source_id, status, created_at, updated_at
                FROM tasks
                WHERE source_type = 'youtube' AND description = ?
                """,
                (canonical_url,),
            )
            row = cursor.fetchone()

            if not row and video_id:
                # Fallback search if video_id is stored in description
                cursor.execute(
                    """
                    SELECT id, title, description, source_type, source_id, status, created_at, updated_at
                    FROM tasks
                    WHERE source_type = 'youtube' AND description LIKE ?
                    """,
                    (f"%{video_id}%",),
                )
                row = cursor.fetchone()

        if row:
            return (
                Task(
                    id=row["id"],
                    title=row["title"],
                    description=row["description"],
                    source_type=row["source_type"],
                    source_id=row["source_id"],
                    status=row["status"],
                    created_at=row["created_at"],
                    updated_at=row["updated_at"],
                ),
                False,
            )

        task = self.create_task(
            title=title,
            description=canonical_url,
            source_type="youtube",
            source_id=None,
            status="pending",
        )
        return task, True

    def update_task(
        self,
        task_id: int,
        title: Optional[str] = None,
        description: Optional[str] = None,
    ) -> Optional[Task]:
        """
        Update task title and/or description.
        Preserves id, status, created_at, and foreign links.
        If the task is linked to a FileItem, update file_items title as well.
        Returns the updated Task entity, or None if task not found.
        """
        now = self.get_now().isoformat()
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, title, description, source_type, source_id, status, created_at, updated_at
                FROM tasks
                WHERE id = ?
                """,
                (task_id,),
            )
            row = cursor.fetchone()
            if not row:
                return None

            new_title = title.strip() if title is not None else row["title"]
            if not new_title:
                raise ValueError("Task title cannot be empty.")
            new_desc = description.strip() if description is not None else (row["description"] or "")

            cursor.execute(
                """
                UPDATE tasks
                SET title = ?, description = ?, updated_at = ?
                WHERE id = ?
                """,
                (new_title, new_desc, now, task_id),
            )
            conn.commit()

        # If linked to a FileItem, update file_items title
        if row["source_id"] and row["source_type"] in ("filemeta", "telegram", "youtube", "google_tasks"):
            with self.db.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    UPDATE file_items
                    SET title = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (new_title, now, row["source_id"]),
                )
                conn.commit()

        return Task(
            id=row["id"],
            title=new_title,
            description=new_desc,
            source_type=row["source_type"],
            source_id=row["source_id"],
            status=row["status"],
            created_at=row["created_at"],
            updated_at=now,
        )

    def update_task_status(self, task_id: int, status: str) -> None:
        """Update the base status of a task and sync with FileItem if applicable."""
        now = self.get_now().isoformat()
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT source_type, source_id FROM tasks WHERE id = ?",
                (task_id,),
            )
            row = cursor.fetchone()

            cursor.execute(
                "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
                (status, now, task_id),
            )
            conn.commit()

        if row and row["source_type"] in ("filemeta", "telegram", "youtube", "google_tasks") and row["source_id"]:
            from todo.services.source_service import SourceService

            SourceService(self.db, date_provider=self.date_provider).set_item_completion(
                row["source_id"], status == "completed"
            )

    def delete_tasks_bulk(self, task_ids: List[int]) -> int:
        """
        Delete multiple tasks permanently in a single transaction.
        All associated daily_tasks are cascade-deleted by foreign keys.
        """
        if not task_ids:
            return 0
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            placeholders = ",".join("?" for _ in task_ids)
            cursor.execute(f"DELETE FROM tasks WHERE id IN ({placeholders})", task_ids)
            deleted = cursor.rowcount
            conn.commit()
        return deleted

    def delete_task(self, task_id: int) -> bool:
        """
        Delete a task permanently.
        All associated daily_tasks are cascade-deleted by foreign keys.
        """
        return self.delete_tasks_bulk([task_id]) > 0
