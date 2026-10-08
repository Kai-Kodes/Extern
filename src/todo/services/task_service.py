"""Service for managing core Task entities."""

from datetime import datetime
from typing import List, Optional

from todo.database import Database
from todo.models import FileItem, Task


class TaskService:
    def __init__(self, db: Database):
        self.db = db

    def create_task(
        self,
        title: str,
        description: str = "",
        source_type: str = "manual",
        source_id: Optional[int] = None,
        status: str = "pending",
    ) -> Task:
        """Create a new task entity."""
        now = datetime.now().isoformat()
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
        """Retrieve all universal tasks."""
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, title, description, source_type, source_id, status, created_at, updated_at
                FROM tasks
                WHERE source_type = 'universal'
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

    def get_or_create_filemeta_task(
        self, file_item: FileItem, source_name: str = ""
    ) -> Task:
        """
        Get an existing task for a Filemeta item, or create a new one.
        Ensures a single task entity per filemeta item.
        """
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, title, description, source_type, source_id, status, created_at, updated_at
                FROM tasks
                WHERE source_type = 'filemeta' AND source_id = ?
                """,
                (file_item.id,),
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
        return self.create_task(
            title=file_item.title,
            description=desc,
            source_type="filemeta",
            source_id=file_item.id,
            status="pending",
        )

    def update_task_status(self, task_id: int, status: str) -> None:
        """Update the base status of a task."""
        now = datetime.now().isoformat()
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
                (status, now, task_id),
            )
            conn.commit()
