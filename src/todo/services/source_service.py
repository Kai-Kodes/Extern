import json
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set, Tuple

from todo.database import Database
from todo.integrations.filemeta import FilemetaScanResult, ScannedFile
from todo.integrations.telegram import TelegramChannel
from todo.models import FileItem, Source, SourceFolder


class SourceService:
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

    def add_or_update_source(
        self, scan_result: FilemetaScanResult, is_rescan: bool = False
    ) -> Tuple[Source, int, int]:
        """
        Store or update a Filemeta source, its folder hierarchy, and associated items.
        Handles duplicates by updating existing records rather than creating duplicates.
        Completion state is preserved across rescans.
        Returns: (source, added_items_count, updated_items_count)
        """
        now = self.get_now().isoformat()
        added_count = 0
        updated_count = 0

        with self.db.get_connection() as conn:
            cursor = conn.cursor()

            canonical_uri = getattr(scan_result, "canonical_uri", None)
            scan_status = getattr(scan_result, "scan_status", "completed")

            # Check if source with this path or canonical_uri already exists
            if canonical_uri:
                cursor.execute(
                    "SELECT id, name, path, canonical_uri, scan_status, created_at, updated_at FROM sources WHERE path = ? OR canonical_uri = ?",
                    (scan_result.source_path, canonical_uri),
                )
            else:
                cursor.execute(
                    "SELECT id, name, path, canonical_uri, scan_status, created_at, updated_at FROM sources WHERE path = ?",
                    (scan_result.source_path,),
                )
            row = cursor.fetchone()

            if row:
                source_id = row["id"]
                created_at = row["created_at"]
                cursor.execute(
                    """
                    UPDATE sources
                    SET name = ?, path = ?, canonical_uri = COALESCE(?, canonical_uri), scan_status = ?,
                        scanned_at = ?, last_error = NULL, updated_at = ?
                    WHERE id = ?
                    """,
                    (scan_result.source_name, scan_result.source_path, canonical_uri, scan_status, now, now, source_id),
                )
            else:
                cursor.execute(
                    """
                    INSERT INTO sources (name, path, canonical_uri, scan_status, scanned_at, last_error, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, NULL, ?, ?)
                    """,
                    (scan_result.source_name, scan_result.source_path, canonical_uri, scan_status, now, now, now),
                )
                source_id = cursor.lastrowid
                created_at = now

            # -------------------------------------------------------------
            # Persist Folder Hierarchy
            # -------------------------------------------------------------
            folder_id_map: Dict[str, Optional[int]] = {"": None, ".": None}
            incoming_folders = getattr(scan_result, "folders", []) or []

            # Sort folders so parent folders are inserted before child folders
            for folder in sorted(incoming_folders, key=lambda f: f.relative_path.count("/")):
                cursor.execute(
                    "SELECT id FROM source_folders WHERE source_id = ? AND relative_path = ?",
                    (source_id, folder.relative_path),
                )
                existing_f = cursor.fetchone()
                parent_id = folder_id_map.get(folder.parent_relative_path or "")

                if existing_f:
                    f_id = existing_f["id"]
                    cursor.execute(
                        """
                        UPDATE source_folders
                        SET name = ?, parent_id = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (folder.name, parent_id, now, f_id),
                    )
                else:
                    cursor.execute(
                        """
                        INSERT INTO source_folders (source_id, parent_id, name, relative_path, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (source_id, parent_id, folder.name, folder.relative_path, now, now),
                    )
                    f_id = cursor.lastrowid

                folder_id_map[folder.relative_path] = f_id

            # -------------------------------------------------------------
            # Upsert File Items (Preserving completion state)
            # -------------------------------------------------------------
            seen_item_ids: Set[int] = set()
            for item in scan_result.items:
                meta_json = json.dumps(item.metadata, ensure_ascii=False)
                parent_f_id = folder_id_map.get(getattr(item, "parent_relative_path", "") or "")
                rel_path = getattr(item, "relative_path", None) or Path(item.path).name
                file_size = getattr(item, "size", 0) or 0
                ext = getattr(item, "extension", "") or Path(item.path).suffix.lower()

                # Look up existing item by relative_path or path (survives mount path changes across sessions)
                cursor.execute(
                    """
                    SELECT id, completed, completed_at FROM file_items
                    WHERE source_id = ? AND (
                        (relative_path IS NOT NULL AND relative_path = ?) OR path = ?
                    )
                    """,
                    (source_id, rel_path, item.path),
                )
                existing_item = cursor.fetchone()

                if existing_item:
                    item_db_id = existing_item["id"]
                    seen_item_ids.add(item_db_id)
                    # Update metadata and title, but PRESERVE completed and completed_at!
                    cursor.execute(
                        """
                        UPDATE file_items
                        SET path = ?, relative_path = ?, folder_id = ?, title = ?,
                            type = ?, file_size = ?, extension = ?, metadata = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            item.path,
                            rel_path,
                            parent_f_id,
                            item.title,
                            item.type,
                            file_size,
                            ext,
                            meta_json,
                            now,
                            item_db_id,
                        ),
                    )
                    updated_count += 1
                else:
                    cursor.execute(
                        """
                        INSERT INTO file_items (
                            source_id, folder_id, path, relative_path, file_size, extension,
                            title, type, metadata, completed, completed_at, created_at, updated_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, ?, ?)
                        """,
                        (
                            source_id,
                            parent_f_id,
                            item.path,
                            rel_path,
                            file_size,
                            ext,
                            item.title,
                            item.type,
                            meta_json,
                            now,
                            now,
                        ),
                    )
                    seen_item_ids.add(cursor.lastrowid)
                    added_count += 1

            # Prune stale records if this was an explicit rescan
            if is_rescan and seen_item_ids:
                cursor.execute(
                    f"DELETE FROM file_items WHERE source_id = ? AND id NOT IN ({','.join('?' * len(seen_item_ids))})",
                    [source_id] + list(seen_item_ids),
                )
                if incoming_folders:
                    keep_folder_ids = [fid for fid in folder_id_map.values() if fid is not None]
                    if keep_folder_ids:
                        cursor.execute(
                            f"DELETE FROM source_folders WHERE source_id = ? AND id NOT IN ({','.join('?' * len(keep_folder_ids))})",
                            [source_id] + keep_folder_ids,
                        )

            conn.commit()

        # Fetch progress and item count
        completed_cnt, total_items, _ = self.get_source_progress(source_id)
        source = Source(
            id=source_id,
            name=scan_result.source_name,
            path=scan_result.source_path,
            canonical_uri=canonical_uri,
            scan_status=scan_status,
            scanned_at=now,
            created_at=created_at,
            updated_at=now,
            item_count=total_items,
            completed_count=completed_cnt,
        )
        return source, added_count, updated_count

    def get_sources(self, source_type: Optional[str] = None) -> List[Source]:
        """Retrieve all imported sources with their item counts and completion stats."""
        query = """
            SELECT s.id, s.name, s.path, s.canonical_uri, s.scan_status, s.scanned_at, s.last_error,
                   s.created_at, s.updated_at,
                   COUNT(fi.id) AS item_count,
                   COALESCE(SUM(CASE WHEN fi.completed = 1 THEN 1 ELSE 0 END), 0) AS completed_count
            FROM sources s
            LEFT JOIN file_items fi ON fi.source_id = s.id
        """
        params = []
        if source_type == "filemeta":
            query += " WHERE s.path NOT LIKE 'telegram://%' AND s.path NOT LIKE 'youtube://%'"
        elif source_type == "telegram":
            query += " WHERE s.path LIKE 'telegram://%'"
        elif source_type == "youtube":
            query += " WHERE s.path LIKE 'youtube://%'"

        query += " GROUP BY s.id ORDER BY s.name ASC"

        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(query, params)
            rows = cursor.fetchall()

        return [
            Source(
                id=r["id"],
                name=r["name"],
                path=r["path"],
                canonical_uri=r["canonical_uri"] if "canonical_uri" in r.keys() else None,
                scan_status=r["scan_status"] if "scan_status" in r.keys() else "completed",
                scanned_at=r["scanned_at"] if "scanned_at" in r.keys() else None,
                last_error=r["last_error"] if "last_error" in r.keys() else None,
                created_at=r["created_at"],
                updated_at=r["updated_at"],
                item_count=r["item_count"],
                completed_count=r["completed_count"],
            )
            for r in rows
        ]

    def get_source_by_path(self, path: str) -> Optional[Source]:
        """Retrieve a source by path/URI."""
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT s.id, s.name, s.path, s.canonical_uri, s.scan_status, s.scanned_at, s.last_error,
                       s.created_at, s.updated_at,
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
            canonical_uri=r["canonical_uri"] if "canonical_uri" in r.keys() else None,
            scan_status=r["scan_status"] if "scan_status" in r.keys() else "completed",
            scanned_at=r["scanned_at"] if "scanned_at" in r.keys() else None,
            last_error=r["last_error"] if "last_error" in r.keys() else None,
            created_at=r["created_at"],
            updated_at=r["updated_at"],
            item_count=r["item_count"],
            completed_count=r["completed_count"],
        )

    def get_source_by_id(self, source_id: int) -> Optional[Source]:
        """Retrieve a source by ID."""
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT s.id, s.name, s.path, s.canonical_uri, s.scan_status, s.scanned_at, s.last_error,
                       s.created_at, s.updated_at,
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
            canonical_uri=r["canonical_uri"] if "canonical_uri" in r.keys() else None,
            scan_status=r["scan_status"] if "scan_status" in r.keys() else "completed",
            scanned_at=r["scanned_at"] if "scanned_at" in r.keys() else None,
            last_error=r["last_error"] if "last_error" in r.keys() else None,
            created_at=r["created_at"],
            updated_at=r["updated_at"],
            item_count=r["item_count"],
            completed_count=r["completed_count"],
        )

    def mark_scan_failed(self, source_id: int, error_message: str) -> None:
        """Record scan failure on a source."""
        now = self.get_now().isoformat()
        with self.db.get_connection() as conn:
            conn.execute(
                """
                UPDATE sources
                SET scan_status = 'failed', last_error = ?, updated_at = ?
                WHERE id = ?
                """,
                (error_message, now, source_id),
            )
            conn.commit()

    def get_folders(
        self, source_id: int, parent_id: Optional[int] = None
    ) -> List[SourceFolder]:
        """Retrieve immediate subfolders in a folder (or root if parent_id is None)."""
        if parent_id is None:
            where_clause = "f.source_id = ? AND f.parent_id IS NULL"
            params = [source_id, source_id]
        else:
            where_clause = "f.source_id = ? AND f.parent_id = ?"
            params = [source_id, source_id, parent_id]

        query = f"""
            WITH RECURSIVE folder_descendants(root_id, desc_id) AS (
                SELECT id, id FROM source_folders WHERE source_id = ?
                UNION ALL
                SELECT fd.root_id, sf.id
                FROM source_folders sf
                JOIN folder_descendants fd ON sf.parent_id = fd.desc_id
            )
            SELECT f.id, f.source_id, f.parent_id, f.name, f.relative_path, f.created_at, f.updated_at,
                   COUNT(fi.id) AS item_count,
                   COALESCE(SUM(CASE WHEN fi.completed = 1 THEN 1 ELSE 0 END), 0) AS completed_count
            FROM source_folders f
            LEFT JOIN folder_descendants fd ON fd.root_id = f.id
            LEFT JOIN file_items fi ON fi.folder_id = fd.desc_id
            WHERE {where_clause}
            GROUP BY f.id
            ORDER BY f.name ASC
        """
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(query, params)
            rows = cursor.fetchall()

        return [
            SourceFolder(
                id=r["id"],
                source_id=r["source_id"],
                parent_id=r["parent_id"],
                name=r["name"],
                relative_path=r["relative_path"],
                created_at=r["created_at"],
                updated_at=r["updated_at"],
                item_count=r["item_count"],
                completed_count=r["completed_count"],
            )
            for r in rows
        ]

    def get_folder_by_id(self, folder_id: int) -> Optional[SourceFolder]:
        """Retrieve a folder by ID with its item counts and progress."""
        query = """
            WITH RECURSIVE folder_descendants(root_id, desc_id) AS (
                SELECT id, id FROM source_folders WHERE id = ?
                UNION ALL
                SELECT fd.root_id, sf.id
                FROM source_folders sf
                JOIN folder_descendants fd ON sf.parent_id = fd.desc_id
            )
            SELECT f.id, f.source_id, f.parent_id, f.name, f.relative_path, f.created_at, f.updated_at,
                   COUNT(fi.id) AS item_count,
                   COALESCE(SUM(CASE WHEN fi.completed = 1 THEN 1 ELSE 0 END), 0) AS completed_count
            FROM source_folders f
            LEFT JOIN folder_descendants fd ON fd.root_id = f.id
            LEFT JOIN file_items fi ON fi.folder_id = fd.desc_id
            WHERE f.id = ?
            GROUP BY f.id
        """
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(query, (folder_id, folder_id))
            r = cursor.fetchone()

        if not r:
            return None

        return SourceFolder(
            id=r["id"],
            source_id=r["source_id"],
            parent_id=r["parent_id"],
            name=r["name"],
            relative_path=r["relative_path"],
            created_at=r["created_at"],
            updated_at=r["updated_at"],
            item_count=r["item_count"],
            completed_count=r["completed_count"],
        )

    def has_subfolders(self, source_id: int) -> bool:
        """Check if source contains any subfolders in SQLite."""
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT 1 FROM source_folders WHERE source_id = ? LIMIT 1",
                (source_id,),
            )
            return cursor.fetchone() is not None

    def get_items_in_folder(
        self, source_id: int, folder_id: Optional[int] = None
    ) -> List[FileItem]:
        """Retrieve file items located directly in the specified folder (or root if folder_id is None)."""
        if folder_id is None:
            where_clause = "source_id = ? AND folder_id IS NULL"
            params = [source_id]
        else:
            where_clause = "source_id = ? AND folder_id = ?"
            params = [source_id, folder_id]

        query = f"""
            SELECT id, source_id, folder_id, path, relative_path, file_size, extension,
                   title, type, metadata, completed, completed_at, created_at, updated_at
            FROM file_items
            WHERE {where_clause}
            ORDER BY
                CASE
                    WHEN json_extract(metadata, '$.position') IS NOT NULL
                    THEN CAST(json_extract(metadata, '$.position') AS INTEGER)
                    ELSE NULL
                END ASC,
                title ASC,
                path ASC,
                id ASC
        """
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(query, params)
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
                    folder_id=r["folder_id"] if "folder_id" in r.keys() else None,
                    path=r["path"],
                    relative_path=r["relative_path"] if "relative_path" in r.keys() else None,
                    file_size=r["file_size"] if "file_size" in r.keys() else 0,
                    extension=r["extension"] if "extension" in r.keys() else None,
                    title=r["title"],
                    type=r["type"],
                    metadata=meta,
                    completed=bool(r["completed"]),
                    completed_at=r["completed_at"],
                    created_at=r["created_at"],
                    updated_at=r["updated_at"],
                )
            )

        def item_sort_key(item: FileItem):
            pos = item.metadata.get("position")
            if pos is not None:
                try:
                    return (0, int(pos), item.id or 0)
                except (ValueError, TypeError):
                    pass
            msg_id = item.metadata.get("message_id")
            if msg_id is not None:
                try:
                    return (1, int(msg_id), item.id or 0)
                except (ValueError, TypeError):
                    pass
            return (2, item.title, item.id or 0)

        items.sort(key=item_sort_key)
        return items

    def get_items_by_source(self, source_id: int) -> List[FileItem]:
        """Retrieve all file items belonging to a source with natural ordering."""
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, source_id, folder_id, path, relative_path, file_size, extension,
                       title, type, metadata, completed, completed_at, created_at, updated_at
                FROM file_items
                WHERE source_id = ?
                ORDER BY
                    CASE
                        WHEN json_extract(metadata, '$.position') IS NOT NULL
                        THEN CAST(json_extract(metadata, '$.position') AS INTEGER)
                        WHEN json_extract(metadata, '$.message_id') IS NOT NULL
                        THEN CAST(json_extract(metadata, '$.message_id') AS INTEGER)
                        ELSE NULL
                    END ASC,
                    path ASC,
                    id ASC
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
                    folder_id=r["folder_id"] if "folder_id" in r.keys() else None,
                    path=r["path"],
                    relative_path=r["relative_path"] if "relative_path" in r.keys() else None,
                    file_size=r["file_size"] if "file_size" in r.keys() else 0,
                    extension=r["extension"] if "extension" in r.keys() else None,
                    title=r["title"],
                    type=r["type"],
                    metadata=meta,
                    completed=bool(r["completed"]),
                    completed_at=r["completed_at"],
                    created_at=r["created_at"],
                    updated_at=r["updated_at"],
                )
            )

        def item_sort_key(item: FileItem):
            pos = item.metadata.get("position")
            if pos is not None:
                try:
                    return (0, int(pos), item.id or 0)
                except (ValueError, TypeError):
                    pass
            msg_id = item.metadata.get("message_id")
            if msg_id is not None:
                try:
                    return (1, int(msg_id), item.id or 0)
                except (ValueError, TypeError):
                    pass
            return (2, item.path, item.id or 0)

        items.sort(key=item_sort_key)
        return items

    def get_item_by_id(self, item_id: int) -> Optional[FileItem]:
        """Retrieve a single file item by ID."""
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, source_id, folder_id, path, relative_path, file_size, extension,
                       title, type, metadata, completed, completed_at, created_at, updated_at
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
            folder_id=r["folder_id"] if "folder_id" in r.keys() else None,
            path=r["path"],
            relative_path=r["relative_path"] if "relative_path" in r.keys() else None,
            file_size=r["file_size"] if "file_size" in r.keys() else 0,
            extension=r["extension"] if "extension" in r.keys() else None,
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
        now = self.get_now().isoformat()
        today_str = self.get_now().date().isoformat()

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

            # Two-way sync: update associated task if it exists (excluding chunky tasks)
            new_status = "completed" if new_completed else "pending"
            cursor.execute(
                """
                SELECT id FROM tasks
                WHERE source_type IN ('filemeta', 'telegram', 'youtube', 'google_tasks')
                  AND source_id = ?
                  AND id NOT IN (SELECT task_id FROM chunky_tasks)
                """,
                (item_id,),
            )
            tasks = cursor.fetchall()
            for t in tasks:
                cursor.execute(
                    "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
                    (new_status, now, t["id"]),
                )
                # If scheduled, sync latest daily task completed_at
                cursor.execute(
                    """
                    UPDATE daily_tasks
                    SET completed_at = ?
                    WHERE id = (
                        SELECT id FROM daily_tasks
                        WHERE task_id = ?
                        ORDER BY date DESC, id DESC
                        LIMIT 1
                    )
                    """,
                    (new_completed_at, t["id"]),
                )

            conn.commit()

        # Update chunky progress if associated with Chunky Tasks
        from todo.services.chunky_service import ChunkyService
        ChunkyService(self.db, date_provider=self.date_provider).on_file_item_completion_changed(
            item_id, bool(new_completed)
        )

        return self.get_item_by_id(item_id)

    def set_item_completion(self, item_id: int, completed: bool) -> None:
        """Set completion status directly (called when an associated task is completed)."""
        now = self.get_now().isoformat()
        comp_val = 1 if completed else 0
        comp_at = now if completed else None

        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE file_items SET completed = ?, completed_at = ?, updated_at = ? WHERE id = ?",
                (comp_val, comp_at, now, item_id),
            )
            conn.commit()

        # Update chunky progress if associated with Chunky Tasks
        from todo.services.chunky_service import ChunkyService
        ChunkyService(self.db, date_provider=self.date_provider).on_file_item_completion_changed(
            item_id, completed
        )

    def delete_sources_bulk(self, source_ids: List[int]) -> int:
        """
        Delete multiple Filemeta, Telegram, or YouTube sources from the Todo database in a single transaction.
        Cleans up FileItems in SQLite and unlinks tasks without deleting user tasks.
        NEVER touches or deletes anything from the filesystem or external services.
        """
        if not source_ids:
            return 0
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            placeholders = ",".join("?" for _ in source_ids)
            # 1. Unlink any user tasks referencing these sources' file items
            cursor.execute(
                f"""
                UPDATE tasks
                SET source_id = NULL
                WHERE source_type IN ('filemeta', 'telegram', 'youtube')
                  AND source_id IN (SELECT id FROM file_items WHERE source_id IN ({placeholders}))
                """,
                source_ids,
            )

            # 2. Delete sources (file_items cascade via SQLite foreign key)
            cursor.execute(
                f"DELETE FROM sources WHERE id IN ({placeholders})",
                source_ids,
            )
            deleted = cursor.rowcount
            conn.commit()

        return deleted

    def delete_source(self, source_id: int) -> bool:
        """
        Delete a single Filemeta, Telegram, or YouTube source from the Todo database.
        """
        return self.delete_sources_bulk([source_id]) > 0

    def delete_file_items_bulk(self, item_ids: List[int]) -> int:
        """
        Delete multiple FileItems in a single transaction.
        Unlinks any tasks referencing these items without deleting the tasks.
        NEVER touches filesystem or external services.
        """
        if not item_ids:
            return 0
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            placeholders = ",".join("?" for _ in item_ids)
            cursor.execute(
                f"""
                UPDATE tasks
                SET source_id = NULL
                WHERE source_type IN ('filemeta', 'telegram', 'youtube')
                  AND source_id IN ({placeholders})
                """,
                item_ids,
            )
            cursor.execute(
                f"DELETE FROM file_items WHERE id IN ({placeholders})",
                item_ids,
            )
            deleted = cursor.rowcount
            conn.commit()
        return deleted

    def delete_file_item(self, item_id: int) -> bool:
        """Delete a single FileItem."""
        return self.delete_file_items_bulk([item_id]) > 0

    def get_cached_telegram_channels(self) -> List[TelegramChannel]:
        """Retrieve all locally cached Telegram channels ordered alphabetically by title."""
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, title, username, is_channel, is_group
                FROM telegram_channels_cache
                ORDER BY title COLLATE NOCASE ASC
                """
            )
            rows = cursor.fetchall()

        return [
            TelegramChannel(
                id=r["id"],
                title=r["title"],
                username=r["username"],
                is_channel=bool(r["is_channel"]),
                is_group=bool(r["is_group"]),
            )
            for r in rows
        ]

    def save_cached_telegram_channels(self, channels: List[TelegramChannel]) -> None:
        """Replace or update locally cached Telegram channels in SQLite."""
        now = datetime.now().isoformat()
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM telegram_channels_cache")
            cursor.executemany(
                """
                INSERT INTO telegram_channels_cache (id, title, username, is_channel, is_group, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        c.id,
                        c.title,
                        c.username,
                        1 if c.is_channel else 0,
                        1 if c.is_group else 0,
                        now,
                    )
                    for c in channels
                ],
            )
            conn.commit()

    def clear_cached_telegram_channels(self) -> None:
        """Purge all cached Telegram channels from SQLite."""
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM telegram_channels_cache")
            conn.commit()

