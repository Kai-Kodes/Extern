"""Service for managing daily scheduled tasks."""

from datetime import datetime
from typing import List, Optional, Tuple

from todo.database import Database
from todo.models import DailyTask, Task


class DailyService:
    def __init__(self, db: Database):
        self.db = db

    def get_tasks_for_date(self, date_str: str) -> List[DailyTask]:
        """Retrieve all tasks scheduled for a given date."""
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT dt.id AS daily_id, dt.task_id, dt.date, dt.position, dt.completed_at,
                       t.id AS t_id, t.title, t.description, t.source_type, t.source_id,
                       t.status, t.created_at, t.updated_at
                FROM daily_tasks dt
                JOIN tasks t ON dt.task_id = t.id
                WHERE dt.date = ?
                ORDER BY dt.position ASC, dt.id ASC
                """,
                (date_str,),
            )
            rows = cursor.fetchall()

        daily_tasks = []
        for r in rows:
            task = Task(
                id=r["t_id"],
                title=r["title"],
                description=r["description"],
                source_type=r["source_type"],
                source_id=r["source_id"],
                status=r["status"],
                created_at=r["created_at"],
                updated_at=r["updated_at"],
            )
            dt = DailyTask(
                id=r["daily_id"],
                task_id=r["task_id"],
                date=r["date"],
                position=r["position"],
                completed_at=r["completed_at"],
                task=task,
            )
            daily_tasks.append(dt)

        return daily_tasks

    def add_task_to_date(self, task_id: int, date_str: str) -> Tuple[DailyTask, bool]:
        """
        Schedule an existing task for a specific date.
        Returns (DailyTask, is_new: bool). If already scheduled, returns existing and False.
        """
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT dt.id, dt.task_id, dt.date, dt.position, dt.completed_at,
                       t.id AS t_id, t.title, t.description, t.source_type, t.source_id,
                       t.status, t.created_at, t.updated_at
                FROM daily_tasks dt
                JOIN tasks t ON dt.task_id = t.id
                WHERE dt.task_id = ? AND dt.date = ?
                """,
                (task_id, date_str),
            )
            existing = cursor.fetchone()

            if existing:
                task = Task(
                    id=existing["t_id"],
                    title=existing["title"],
                    description=existing["description"],
                    source_type=existing["source_type"],
                    source_id=existing["source_id"],
                    status=existing["status"],
                    created_at=existing["created_at"],
                    updated_at=existing["updated_at"],
                )
                dt = DailyTask(
                    id=existing["id"],
                    task_id=existing["task_id"],
                    date=existing["date"],
                    position=existing["position"],
                    completed_at=existing["completed_at"],
                    task=task,
                )
                return dt, False

            # Determine next position
            cursor.execute(
                "SELECT COALESCE(MAX(position), 0) AS max_pos FROM daily_tasks WHERE date = ?",
                (date_str,),
            )
            max_pos = cursor.fetchone()["max_pos"]
            next_pos = max_pos + 1

            cursor.execute(
                """
                INSERT INTO daily_tasks (task_id, date, position, completed_at)
                VALUES (?, ?, ?, NULL)
                """,
                (task_id, date_str, next_pos),
            )
            daily_id = cursor.lastrowid
            conn.commit()

        # Retrieve created task info
        from todo.services.task_service import TaskService
        task = TaskService(self.db).get_task_by_id(task_id)
        dt = DailyTask(
            id=daily_id,
            task_id=task_id,
            date=date_str,
            position=next_pos,
            completed_at=None,
            task=task,
        )
        return dt, True

    def toggle_task_completion(self, daily_task_id: int) -> Optional[DailyTask]:
        """
        Toggle completed status of a daily task.
        Updates daily_tasks.completed_at, task.status, and syncs FileItem if applicable.
        """
        now = datetime.now().isoformat()
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT dt.id, dt.task_id, dt.date, dt.position, dt.completed_at,
                       t.source_type, t.source_id
                FROM daily_tasks dt
                JOIN tasks t ON dt.task_id = t.id
                WHERE dt.id = ?
                """,
                (daily_task_id,),
            )
            row = cursor.fetchone()

            if not row:
                return None

            task_id = row["task_id"]
            source_type = row["source_type"]
            source_id = row["source_id"]

            if row["completed_at"] is None:
                new_completed_at = now
                new_task_status = "completed"
                is_completed = True
            else:
                new_completed_at = None
                new_task_status = "pending"
                is_completed = False

            cursor.execute(
                "UPDATE daily_tasks SET completed_at = ? WHERE id = ?",
                (new_completed_at, daily_task_id),
            )
            cursor.execute(
                "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
                (new_task_status, now, task_id),
            )
            conn.commit()

        # Two-way sync: if originated from a FileItem, update its completed state
        if source_type == "filemeta" and source_id:
            from todo.services.source_service import SourceService
            SourceService(self.db).set_item_completion(source_id, is_completed)

        # Fetch updated task
        from todo.services.task_service import TaskService
        task = TaskService(self.db).get_task_by_id(task_id)
        return DailyTask(
            id=daily_task_id,
            task_id=task_id,
            date=row["date"],
            position=row["position"],
            completed_at=new_completed_at,
            task=task,
        )

    def remove_daily_task(self, daily_task_id: int) -> bool:
        """Remove a task from the daily schedule without deleting the task itself."""
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM daily_tasks WHERE id = ?", (daily_task_id,))
            conn.commit()
            return cursor.rowcount > 0

    def delete_daily_task_and_underlying(self, daily_task_id: int) -> bool:
        """Remove the daily task and permanently delete the underlying task entity."""
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT task_id FROM daily_tasks WHERE id = ?", (daily_task_id,))
            row = cursor.fetchone()
            if not row:
                return False
            task_id = row["task_id"]

        from todo.services.task_service import TaskService
        return TaskService(self.db).delete_task(task_id)
