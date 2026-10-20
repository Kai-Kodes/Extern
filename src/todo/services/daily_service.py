"""Service for managing daily scheduled tasks."""

import json
from datetime import datetime
from typing import Any, Callable, List, Optional, Tuple

from todo.database import Database
from todo.models import DailyTask, Task


class DailyService:
    def __init__(
        self,
        db: Database,
        date_provider: Optional[Callable[[], datetime]] = None,
        google_tasks_service: Optional[Any] = None,
    ):
        self.db = db
        self.date_provider = date_provider
        self.google_tasks_service = google_tasks_service

    def get_now(self) -> datetime:
        """Return the current datetime, supporting injected test clocks."""
        return self.date_provider() if self.date_provider else datetime.now()

    def get_today_str(self) -> str:
        """Return current local date string in YYYY-MM-DD format."""
        return self.get_now().date().isoformat()

    def get_active_tasks_for_date(self, date_str: Optional[str] = None) -> List[DailyTask]:
        """
        Retrieve active tasks for a given date:
        - Includes pending tasks scheduled on or before date_str (carried forward).
        - Includes tasks completed on date_str, keeping them visible for the remainder of that day.
        - Excludes tasks completed on previous dates.
        - Excludes tasks moved to Universal Tasks or deleted.
        """
        if date_str is None:
            date_str = self.get_today_str()

        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT dt.id AS daily_id, dt.task_id, dt.date, dt.position, dt.completed_at,
                       t.id AS t_id, t.title, t.description, t.source_type, t.source_id,
                       t.status, t.created_at, t.updated_at
                FROM daily_tasks dt
                JOIN tasks t ON dt.task_id = t.id
                WHERE (
                    -- Tasks completed on the current date
                    dt.completed_at LIKE ?
                    OR
                    -- Pending tasks scheduled on or before the current date
                    (
                        t.status = 'pending'
                        AND dt.completed_at IS NULL
                        AND dt.date <= ?
                        AND dt.id = (
                            SELECT MAX(id)
                            FROM daily_tasks
                            WHERE task_id = dt.task_id
                              AND date <= ?
                              AND completed_at IS NULL
                        )
                    )
                )
                ORDER BY dt.date ASC, dt.position ASC, dt.id ASC
                """,
                (f"{date_str}%", date_str, date_str),
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

    def get_progress_for_date(
        self, date_str: Optional[str] = None
    ) -> Tuple[int, int, int]:
        """
        Return (completed_count, total_count, percentage) for the given date.
        Calculated from active tasks displayed today (pending tasks + tasks completed today).
        Tasks completed on previous days are excluded.
        """
        if date_str is None:
            date_str = self.get_today_str()

        tasks = self.get_active_tasks_for_date(date_str)
        total_count = len(tasks)
        completed_count = sum(1 for dt in tasks if dt.is_completed)
        pct = round((completed_count / total_count) * 100) if total_count > 0 else 0
        return completed_count, total_count, pct

    def get_tasks_for_date(self, date_str: str) -> List[DailyTask]:
        """Retrieve all tasks scheduled for a given date (historical/direct query)."""
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
        Returns (DailyTask, is_new: bool).
        If already scheduled and active (including carried forward pending), returns existing and False.
        """
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            # 1. Check if already scheduled for this exact date
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

            # 2. Check if already active from an earlier date (carried forward pending, or completed today)
            if not existing:
                cursor.execute(
                    """
                    SELECT dt.id, dt.task_id, dt.date, dt.position, dt.completed_at,
                           t.id AS t_id, t.title, t.description, t.source_type, t.source_id,
                           t.status, t.created_at, t.updated_at
                    FROM daily_tasks dt
                    JOIN tasks t ON dt.task_id = t.id
                    WHERE dt.task_id = ? AND (
                        (dt.date < ? AND dt.completed_at IS NULL AND t.status = 'pending')
                        OR
                        (dt.completed_at LIKE ?)
                    )
                    ORDER BY dt.date DESC, dt.id DESC
                    LIMIT 1
                    """,
                    (task_id, date_str, f"{date_str}%"),
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
            now = self.get_now().isoformat()
            cursor.execute(
                "UPDATE tasks SET status = 'pending', updated_at = ? WHERE id = ?",
                (now, task_id),
            )
            conn.commit()

        # Retrieve created task info
        from todo.services.task_service import TaskService
        task = TaskService(self.db, date_provider=self.date_provider).get_task_by_id(task_id)

        # If it is a Google task, sync or queue its due date
        if task and task.source_type == "google_tasks" and task.source_id:
            self._set_google_task_due_date(task.id, task.source_id, date_str)

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
        now = self.get_now().isoformat()
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
        if source_type in ("filemeta", "telegram", "youtube", "google_tasks") and source_id:
            from todo.services.source_service import SourceService
            SourceService(self.db, date_provider=self.date_provider).set_item_completion(source_id, is_completed)

        # Propagate completion to Google Tasks or outbox
        if source_type == "google_tasks" and source_id:
            self._sync_google_task_completion(task_id, source_id, is_completed)

        # Fetch updated task
        from todo.services.task_service import TaskService
        task = TaskService(self.db, date_provider=self.date_provider).get_task_by_id(task_id)
        return DailyTask(
            id=daily_task_id,
            task_id=task_id,
            date=row["date"],
            position=row["position"],
            completed_at=new_completed_at,
            task=task,
        )

    def move_tasks_to_universal_bulk(self, daily_task_ids: List[int]) -> int:
        """
        Move daily tasks to Universal Tasks:
        - For standard tasks: sets task source_type to 'universal'
        - For Google Tasks: retains source_type='google_tasks', clears remote due date
        - Removes active (pending) daily_tasks schedule entries so it disappears from Today and does not carry forward
        - Preserves completed historical daily_tasks entries
        - Transactional
        """
        if not daily_task_ids:
            return 0

        now = self.get_now().isoformat()
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            placeholders = ",".join("?" for _ in daily_task_ids)
            cursor.execute(
                f"SELECT DISTINCT task_id FROM daily_tasks WHERE id IN ({placeholders})",
                daily_task_ids,
            )
            rows = cursor.fetchall()
            if not rows:
                return 0

            task_ids = [r["task_id"] for r in rows]
            task_placeholders = ",".join("?" for _ in task_ids)

            # Query source_type and source_id for these tasks
            cursor.execute(
                f"SELECT id, source_type, source_id FROM tasks WHERE id IN ({task_placeholders})",
                task_ids,
            )
            t_rows = cursor.fetchall()
            google_task_items = [(r["id"], r["source_id"]) for r in t_rows if r["source_type"] == "google_tasks"]
            other_task_ids = [r["id"] for r in t_rows if r["source_type"] != "google_tasks"]

            # 1. Update non-Google tasks to 'universal'
            if other_task_ids:
                other_ph = ",".join("?" for _ in other_task_ids)
                cursor.execute(
                    f"""
                    UPDATE tasks
                    SET source_type = 'universal', updated_at = ?
                    WHERE id IN ({other_ph})
                    """,
                    [now] + other_task_ids,
                )

            # 2. Delete pending daily_tasks entries for all selected tasks
            cursor.execute(
                f"""
                DELETE FROM daily_tasks
                WHERE task_id IN ({task_placeholders}) AND completed_at IS NULL
                """,
                task_ids,
            )

            # 3. Also delete the active daily_tasks records that were selected and moved
            cursor.execute(
                f"""
                DELETE FROM daily_tasks
                WHERE id IN ({placeholders})
                """,
                daily_task_ids,
            )
            conn.commit()

        # 4. For Google tasks, clear due date locally and on remote / outbox
        for t_id, s_id in google_task_items:
            self._clear_google_task_due_date(t_id, s_id)

        return len(task_ids)

    def _sync_google_task_completion(self, task_id: int, source_id: Optional[int], is_completed: bool) -> None:
        """Propagate completion toggle to Google Tasks API or enqueue in outbox."""
        if not source_id:
            return
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT path, metadata FROM file_items WHERE id = ?", (source_id,))
            fi_row = cursor.fetchone()
        if not fi_row:
            return
        remote_id = fi_row["path"]
        meta = json.loads(fi_row["metadata"] or "{}")
        list_id = meta.get("list_id", "@default")
        now_iso = self.get_now().isoformat()
        action = "complete" if is_completed else "reopen"
        payload = {"list_id": list_id, "task_id": remote_id}

        pushed = False
        g_svc = getattr(self, "google_tasks_service", None)
        if g_svc and g_svc.is_authenticated() and not remote_id.startswith("local_"):
            try:
                g_svc.client.set_task_completed(list_id=list_id, task_id=remote_id, completed=is_completed)
                pushed = True
            except Exception:
                pushed = False

        if not pushed and not remote_id.startswith("local_"):
            with self.db.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    INSERT INTO google_tasks_outbox (task_id, action, payload, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (task_id, action, json.dumps(payload, ensure_ascii=False), now_iso),
                )
                conn.commit()

    def _set_google_task_due_date(self, task_id: int, source_id: Optional[int], date_str: str) -> None:
        """Update due date for a Google Task when scheduled into daily_tasks."""
        if not source_id:
            return
        from todo.integrations.google_tasks import format_rfc3339_due_date
        now_iso = self.get_now().isoformat()
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT path, metadata FROM file_items WHERE id = ?", (source_id,))
            fi_row = cursor.fetchone()
            if not fi_row:
                return
            remote_id = fi_row["path"]
            meta = json.loads(fi_row["metadata"] or "{}")
            list_id = meta.get("list_id", "@default")
            meta["due"] = format_rfc3339_due_date(date_str)
            cursor.execute(
                "UPDATE file_items SET metadata = ?, updated_at = ? WHERE id = ?",
                (json.dumps(meta, ensure_ascii=False), now_iso, source_id),
            )
            conn.commit()

        pushed = False
        g_svc = getattr(self, "google_tasks_service", None)
        if g_svc and g_svc.is_authenticated() and not remote_id.startswith("local_"):
            try:
                g_svc.client.update_task(list_id=list_id, task_id=remote_id, due_date=date_str)
                pushed = True
            except Exception:
                pushed = False

        if not pushed and not remote_id.startswith("local_"):
            payload = {
                "list_id": list_id,
                "task_id": remote_id,
                "due_date": date_str,
            }
            with self.db.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    INSERT INTO google_tasks_outbox (task_id, action, payload, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (task_id, "update", json.dumps(payload, ensure_ascii=False), now_iso),
                )
                conn.commit()

    def _clear_google_task_due_date(self, task_id: int, source_id: Optional[int]) -> None:
        """Clear due date for a Google Task when moved to universal."""
        if not source_id:
            return
        now_iso = self.get_now().isoformat()
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT path, metadata FROM file_items WHERE id = ?", (source_id,))
            fi_row = cursor.fetchone()
            if not fi_row:
                return
            remote_id = fi_row["path"]
            meta = json.loads(fi_row["metadata"] or "{}")
            list_id = meta.get("list_id", "@default")
            meta["due"] = None
            cursor.execute(
                "UPDATE file_items SET metadata = ?, updated_at = ? WHERE id = ?",
                (json.dumps(meta, ensure_ascii=False), now_iso, source_id),
            )
            conn.commit()

        pushed = False
        g_svc = getattr(self, "google_tasks_service", None)
        if g_svc and g_svc.is_authenticated() and not remote_id.startswith("local_"):
            try:
                g_svc.client.update_task(list_id=list_id, task_id=remote_id, clear_due=True)
                pushed = True
            except Exception:
                pushed = False

        if not pushed and not remote_id.startswith("local_"):
            payload = {
                "list_id": list_id,
                "task_id": remote_id,
                "clear_due": True,
            }
            with self.db.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    INSERT INTO google_tasks_outbox (task_id, action, payload, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (task_id, "update", json.dumps(payload, ensure_ascii=False), now_iso),
                )
                conn.commit()

    def move_task_to_universal(self, daily_task_id: int) -> bool:
        """Move a single daily task to Universal Tasks."""
        return self.move_tasks_to_universal_bulk([daily_task_id]) > 0

    def delete_daily_tasks_bulk(self, daily_task_ids: List[int]) -> int:
        """
        Bulk delete daily tasks:
        - For manual tasks: deletes the underlying task (cascades to daily_tasks)
        - For other tasks (universal, filemeta, telegram, youtube): removes from daily schedule only
        - Transactional
        """
        if not daily_task_ids:
            return 0

        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            placeholders = ",".join("?" for _ in daily_task_ids)
            cursor.execute(
                f"""
                SELECT dt.id AS dt_id, dt.task_id, t.source_type
                FROM daily_tasks dt
                JOIN tasks t ON dt.task_id = t.id
                WHERE dt.id IN ({placeholders})
                """,
                daily_task_ids,
            )
            rows = cursor.fetchall()
            if not rows:
                return 0

            manual_task_ids = [r["task_id"] for r in rows if r["source_type"] == "manual"]
            other_dt_ids = [r["dt_id"] for r in rows if r["source_type"] != "manual"]

            if manual_task_ids:
                m_placeholders = ",".join("?" for _ in manual_task_ids)
                cursor.execute(
                    f"DELETE FROM tasks WHERE id IN ({m_placeholders})",
                    manual_task_ids,
                )
            if other_dt_ids:
                o_placeholders = ",".join("?" for _ in other_dt_ids)
                cursor.execute(
                    f"DELETE FROM daily_tasks WHERE id IN ({o_placeholders})",
                    other_dt_ids,
                )
            conn.commit()
            return len(rows)

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
        return TaskService(self.db, date_provider=self.date_provider).delete_task(task_id)

    def get_timeline_data_for_date(self, date_str: str) -> dict:
        """
        Retrieve timeline data for a specific date:
        - Past dates (date_str < today): Completed tasks on that exact date (completed_at LIKE date_str%).
        - Today (date_str == today): Active tasks (pending carried forward + completed today).
        - Future dates (date_str > today): Tasks scheduled specifically for that date (t.status = 'pending' and completed_at IS NULL).
        """
        today_str = self.get_today_str()

        if date_str < today_str:
            domain = "past"
            with self.db.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT dt.id AS daily_id, dt.task_id, dt.date, dt.position, dt.completed_at,
                           t.id AS t_id, t.title, t.description, t.source_type, t.source_id,
                           t.status, t.created_at, t.updated_at
                    FROM daily_tasks dt
                    JOIN tasks t ON dt.task_id = t.id
                    WHERE dt.completed_at LIKE ?
                    ORDER BY dt.completed_at ASC, dt.id ASC
                    """,
                    (f"{date_str}%",),
                )
                rows = cursor.fetchall()

            tasks = []
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
                tasks.append(dt)

            completed_cnt = len(tasks)
            total_cnt = len(tasks)
            pct = 100 if total_cnt > 0 else 0

        elif date_str == today_str:
            domain = "today"
            tasks = self.get_active_tasks_for_date(today_str)
            completed_cnt, total_cnt, pct = self.get_progress_for_date(today_str)

        else:
            domain = "future"
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
                      AND t.status = 'pending'
                      AND dt.completed_at IS NULL
                    ORDER BY dt.position ASC, dt.id ASC
                    """,
                    (date_str,),
                )
                rows = cursor.fetchall()

            tasks = []
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
                tasks.append(dt)

            completed_cnt = 0
            total_cnt = len(tasks)
            pct = 0

        return {
            "selected_date": date_str,
            "today_str": today_str,
            "domain": domain,
            "tasks": tasks,
            "completed_count": completed_cnt,
            "total_count": total_cnt,
            "progress_pct": pct,
        }

    def reschedule_task(self, task_id: int, target_date: str) -> DailyTask:
        """
        Reschedule an existing pending task to a target date:
        - Ensures exactly one active pending schedule entry exists for the task.
        - Preserves historical completed entries (completed_at IS NOT NULL).
        - If the task is completed, prevents rescheduling.
        - Transactional and idempotent.
        """
        now = self.get_now().isoformat()
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT id, status FROM tasks WHERE id = ?", (task_id,))
            t_row = cursor.fetchone()
            if not t_row:
                raise ValueError(f"Task #{task_id} not found")
            if t_row["status"] == "completed":
                raise ValueError(
                    f"Cannot reschedule completed task #{task_id}. Historical completed tasks cannot be rescheduled."
                )

            # Determine next position on target date
            cursor.execute(
                "SELECT COALESCE(MAX(position), 0) AS max_pos FROM daily_tasks WHERE date = ?",
                (target_date,),
            )
            next_pos = cursor.fetchone()["max_pos"] + 1

            # Check if entry already exists on target date
            cursor.execute(
                "SELECT id, completed_at FROM daily_tasks WHERE task_id = ? AND date = ?",
                (task_id, target_date),
            )
            existing_on_target = cursor.fetchone()

            if existing_on_target:
                if existing_on_target["completed_at"] is not None:
                    raise ValueError(
                        f"Task #{task_id} was already completed on {target_date}."
                    )
                target_dt_id = existing_on_target["id"]
                # Delete any other pending rows
                cursor.execute(
                    "DELETE FROM daily_tasks WHERE task_id = ? AND completed_at IS NULL AND id != ?",
                    (task_id, target_dt_id),
                )
            else:
                # Find any other pending entry to update
                cursor.execute(
                    "SELECT id FROM daily_tasks WHERE task_id = ? AND completed_at IS NULL ORDER BY id ASC",
                    (task_id,),
                )
                pending_rows = cursor.fetchall()
                if pending_rows:
                    target_dt_id = pending_rows[0]["id"]
                    cursor.execute(
                        "UPDATE daily_tasks SET date = ?, position = ? WHERE id = ?",
                        (target_date, next_pos, target_dt_id),
                    )
                    # Clean up any extra pending entries if any existed
                    if len(pending_rows) > 1:
                        other_ids = [r["id"] for r in pending_rows[1:]]
                        ph = ",".join("?" for _ in other_ids)
                        cursor.execute(
                            f"DELETE FROM daily_tasks WHERE id IN ({ph})",
                            other_ids,
                        )
                else:
                    # No pending entry (e.g. from Universal list)
                    cursor.execute(
                        """
                        INSERT INTO daily_tasks (task_id, date, position, completed_at)
                        VALUES (?, ?, ?, NULL)
                        """,
                        (task_id, target_date, next_pos),
                    )
                    target_dt_id = cursor.lastrowid

            cursor.execute(
                "UPDATE tasks SET status = 'pending', updated_at = ? WHERE id = ?",
                (now, task_id),
            )
            conn.commit()

        from todo.services.task_service import TaskService
        task = TaskService(self.db, date_provider=self.date_provider).get_task_by_id(task_id)
        return DailyTask(
            id=target_dt_id,
            task_id=task_id,
            date=target_date,
            position=next_pos,
            completed_at=None,
            task=task,
        )

    def reschedule_daily_task(self, daily_task_id: int, target_date: str) -> DailyTask:
        """Reschedule a daily task by its daily_tasks.id to target_date."""
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT task_id, completed_at FROM daily_tasks WHERE id = ?",
                (daily_task_id,),
            )
            row = cursor.fetchone()
            if not row:
                raise ValueError(f"Daily task #{daily_task_id} not found")
            if row["completed_at"] is not None:
                raise ValueError("Cannot reschedule a completed historical task record.")
            task_id = row["task_id"]

        return self.reschedule_task(task_id, target_date)

    def create_task_for_date(
        self, title: str, date_str: str, description: str = ""
    ) -> DailyTask:
        """Create a new manual task scheduled directly for a specific date."""
        from todo.services.task_service import TaskService
        task_svc = TaskService(self.db, date_provider=self.date_provider)
        task = task_svc.create_task(
            title=title,
            description=description,
            source_type="manual",
            status="pending",
        )
        dt, _ = self.add_task_to_date(task.id, date_str)
        return dt

    def get_task_scheduled_date(self, task_id: int) -> Optional[str]:
        """Get the current scheduled date for a task if active/pending."""
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT date FROM daily_tasks
                WHERE task_id = ? AND completed_at IS NULL
                ORDER BY date DESC, id DESC
                LIMIT 1
                """,
                (task_id,),
            )
            row = cursor.fetchone()
            if row:
                return row["date"]
            return None

    def unschedule_task(self, task_id: int) -> bool:
        """Remove any active/pending daily schedule entries for a task without deleting the task entity."""
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM daily_tasks WHERE task_id = ? AND completed_at IS NULL",
                (task_id,),
            )
            deleted = cursor.rowcount > 0
            conn.commit()
            return deleted

