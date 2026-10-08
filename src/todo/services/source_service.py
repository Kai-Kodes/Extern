"""Service for managing Filemeta sources and scanned file items."""

import json
from datetime import datetime
from typing import List, Optional, Tuple

from todo.database import Database
from todo.integrations.filemeta import FilemetaScanResult, ScannedFile
from todo.models import FileItem, Source


class SourceService:
    def __init__(self, db: Database):
        self.db = db

    def add_or_update_source(
        self, scan_result: FilemetaScanResult
    ) -> Tuple[Source, int, int]:
        """
        Store or update a Filemeta source and its associated items.
        Handles duplicates by updating existing records rather than creating duplicates.
        Completion state is preserved across rescans.
        Returns: (source, added_items_count, updated_items_count)
        """
        now = datetime.now().isoformat()
        added_count = 0
        updated_count = 0

        with self.db.get_connection() as conn:
            cursor = conn.cursor()

            # Check if source with this path already exists
            cursor.execute(
                "SELECT id, name, path, created_at, updated_at FROM sources WHERE path = ?",
                (scan_result.source_path,),
            )
            row = cursor.fetchone()

            if row:
                source_id = row["id"]
                created_at = row["created_at"]
                cursor.execute(
                    "UPDATE sources SET name = ?, updated_at = ? WHERE id = ?",
                    (scan_result.source_name, now, source_id),
                )
            else:
                cursor.execute(
                    "INSERT INTO sources (name, path, created_at, updated_at) VALUES (?, ?, ?, ?)",
                    (scan_result.source_name, scan_result.source_path, now, now),
                )
                source_id = cursor.lastrowid
                created_at = now

            # Upsert file items (preserving existing completion state)
            for item in scan_result.items:
                meta_json = json.dumps(item.metadata, ensure_ascii=False)
                cursor.execute(
                    "SELECT id, completed, completed_at FROM file_items WHERE source_id = ? AND path = ?",
                    (source_id, item.path),
                )
                existing_item = cursor.fetchone()

                if existing_item:
                    # Update metadata and title, but PRESERVE completed and completed_at!
                    cursor.execute(
                        """
                        UPDATE file_items
                        SET title = ?, type = ?, metadata = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (item.title, item.type, meta_json, now, existing_item["id"]),
                    )
                    updated_count += 1
                else:
                    cursor.execute(
                        """
                        INSERT INTO file_items (source_id, path, title, type, metadata, completed, completed_at, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, 0, NULL, ?, ?)
                        """,
                        (source_id, item.path, item.title, item.type, meta_json, now, now),
                    )
                    added_count += 1

            conn.commit()

        # Fetch progress and item count
        completed_cnt, total_items, _ = self.get_source_progress(source_id)
        source = Source(
            id=source_id,
            name=scan_result.source_name,
            path=scan_result.source_path,
            created_at=created_at,
            updated_at=now,
            item_count=total_items,
            completed_count=completed_cnt,
        )
        return source, added_count, updated_count

    def get_sources(self) -> List[Source]:
        """Retrieve all imported sources with their item counts and completion stats."""
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT s.id, s.name, s.path, s.created_at, s.updated_at,
                       COUNT(fi.id) AS item_count,
                       COALESCE(SUM(CASE WHEN fi.completed = 1 THEN 1 ELSE 0 END), 0) AS completed_count
                FROM sources s
                LEFT JOIN file_items fi ON fi.source_id = s.id
                GROUP BY s.id
                ORDER BY s.name ASC
                """
            )
            rows = cursor.fetchall()

        return [
            Source(
                id=r["id"],
                name=r["name"],
                path=r["path"],
                created_at=r["created_at"],
                updated_at=r["updated_at"],
                item_count=r["item_count"],
                completed_count=r["completed_count"],
            )
            for r in rows
        ]

    def get_source_by_id(self, source_id: int) -> Optional[Source]:
        """Retrieve a source by ID."""
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT s.id, s.name, s.path, s.created_at, s.updated_at,
                       COUNT(fi.id) AS item_count,
                       COALESCE(SUM(CASE WHEN fi.completed = 1 THEN 1 ELSE 0 END), 0) AS completed_count
                FROM sources s
                LEFT JOIN file_items fi ON fi.source_id = s.id
                WHERE s.id = ?
                GROUP BY s.id
                """,
                (source_id,),
            )
            r = cursor.fetchone()

        if not r:
            return None

        return Source(
            id=r["id"],
            name=r["name"],
            path=r["path"],
            created_at=r["created_at"],
            updated_at=r["updated_at"],
            item_count=r["item_count"],
            completed_count=r["completed_count"],
        )

    def get_source_by_path(self, path: str) -> Optional[Source]:
        """Retrieve a source by normalized path."""
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT s.id, s.name, s.path, s.created_at, s.updated_at,
                       COUNT(fi.id) AS item_count,
                       COALESCE(SUM(CASE WHEN fi.completed = 1 THEN 1 ELSE 0 END), 0) AS completed_count
                FROM sources s
                LEFT JOIN file_items fi ON fi.source_id = s.id
                WHERE s.path = ?
                GROUP BY s.id
                """,
                (path,),
            )
            r = cursor.fetchone()

        if not r:
            return None

        return Source(
            id=r["id"],
            name=r["name"],
            path=r["path"],
            created_at=r["created_at"],
            updated_at=r["updated_at"],
            item_count=r["item_count"],
            completed_count=r["completed_count"],
        )

    def get_items_by_source(self, source_id: int) -> List[FileItem]:
        """Retrieve all file items belonging to a source."""
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, source_id, path, title, type, metadata, completed, completed_at, created_at, updated_at
                FROM file_items
                WHERE source_id = ?
                ORDER BY path ASC
                """,
                (source_id,),
            )
            rows = cursor.fetchall()

        items = []
        for r in rows:
            try:
                meta = json.loads(r["metadata"]) if r["metadata"] else {}
            except Exception:
                meta = {}
            items.append(
                FileItem(
                    id=r["id"],
                    source_id=r["source_id"],
                    path=r["path"],
                    title=r["title"],
                    type=r["type"],
                    metadata=meta,
                    completed=bool(r["completed"]),
                    completed_at=r["completed_at"],
                    created_at=r["created_at"],
                    updated_at=r["updated_at"],
                )
            )
        return items

    def get_item_by_id(self, item_id: int) -> Optional[FileItem]:
        """Retrieve a single file item by ID."""
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, source_id, path, title, type, metadata, completed, completed_at, created_at, updated_at
                FROM file_items
                WHERE id = ?
                """,
                (item_id,),
            )
            r = cursor.fetchone()

        if not r:
            return None

        try:
            meta = json.loads(r["metadata"]) if r["metadata"] else {}
        except Exception:
            meta = {}

        return FileItem(
            id=r["id"],
            source_id=r["source_id"],
            path=r["path"],
            title=r["title"],
            type=r["type"],
            metadata=meta,
            completed=bool(r["completed"]),
            completed_at=r["completed_at"],
            created_at=r["created_at"],
            updated_at=r["updated_at"],
        )

    def get_source_progress(self, source_id: int) -> Tuple[int, int, int]:
        """Return (completed_items, total_items, percentage)."""
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT COUNT(id) AS total,
                       COALESCE(SUM(CASE WHEN completed = 1 THEN 1 ELSE 0 END), 0) AS completed
                FROM file_items
                WHERE source_id = ?
                """,
                (source_id,),
            )
            row = cursor.fetchone()
            total = row["total"] if row else 0
            completed = row["completed"] if row else 0
            pct = int((completed / total) * 100) if total > 0 else 0
            return completed, total, pct

    def toggle_item_completion(self, item_id: int) -> Optional[FileItem]:
        """
        Toggle the completed state of a FileItem in SQLite.
        Syncs state two-way with associated Task entities and today's schedule.
        Files on disk remain 100% untouched.
        """
        now = datetime.now().isoformat()
        today_str = datetime.now().date().isoformat()

        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, source_id, completed FROM file_items WHERE id = ?",
                (item_id,),
            )
            row = cursor.fetchone()
            if not row:
                return None

            new_completed = 0 if row["completed"] else 1
            new_completed_at = now if new_completed else None

            cursor.execute(
                "UPDATE file_items SET completed = ?, completed_at = ?, updated_at = ? WHERE id = ?",
                (new_completed, new_completed_at, now, item_id),
            )

            # Two-way sync: update associated task if it exists
            new_status = "completed" if new_completed else "pending"
            cursor.execute(
                "SELECT id FROM tasks WHERE source_type = 'filemeta' AND source_id = ?",
                (item_id,),
            )
            tasks = cursor.fetchall()
            for t in tasks:
                cursor.execute(
                    "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
                    (new_status, now, t["id"]),
                )
                # If scheduled for today, sync daily task completed_at
                cursor.execute(
                    "UPDATE daily_tasks SET completed_at = ? WHERE task_id = ? AND date = ?",
                    (new_completed_at, t["id"], today_str),
                )

            conn.commit()

        return self.get_item_by_id(item_id)

    def set_item_completion(self, item_id: int, completed: bool) -> None:
        """Set completion status directly (called when an associated task is completed)."""
        now = datetime.now().isoformat()
        comp_val = 1 if completed else 0
        comp_at = now if completed else None

        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE file_items SET completed = ?, completed_at = ?, updated_at = ? WHERE id = ?",
                (comp_val, comp_at, now, item_id),
            )
            conn.commit()

    def delete_source(self, source_id: int) -> bool:
        """
        Delete a Filemeta source from the Todo database.
        Cleans up FileItems in SQLite and unlinks tasks without deleting user tasks.
        NEVER touches or deletes anything from the filesystem.
        """
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            # 1. Unlink any user tasks referencing this source's file items
            cursor.execute(
                """
                UPDATE tasks
                SET source_id = NULL
                WHERE source_type = 'filemeta'
                  AND source_id IN (SELECT id FROM file_items WHERE source_id = ?)
                """,
                (source_id,),
            )

            # 2. Delete source (file_items cascade via SQLite foreign key)
            cursor.execute("DELETE FROM sources WHERE id = ?", (source_id,))
            deleted = cursor.rowcount > 0
            conn.commit()

        return deleted
