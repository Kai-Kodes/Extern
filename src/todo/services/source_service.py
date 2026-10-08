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

            # Upsert file items
            for item in scan_result.items:
                meta_json = json.dumps(item.metadata, ensure_ascii=False)
                cursor.execute(
                    "SELECT id FROM file_items WHERE source_id = ? AND path = ?",
                    (source_id, item.path),
                )
                existing_item = cursor.fetchone()

                if existing_item:
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
                        INSERT INTO file_items (source_id, path, title, type, metadata, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (source_id, item.path, item.title, item.type, meta_json, now, now),
                    )
                    added_count += 1

            conn.commit()

        # Fetch total item count
        total_items = self.get_item_count_for_source(source_id)
        source = Source(
            id=source_id,
            name=scan_result.source_name,
            path=scan_result.source_path,
            created_at=created_at,
            updated_at=now,
            item_count=total_items,
        )
        return source, added_count, updated_count

    def get_sources(self) -> List[Source]:
        """Retrieve all imported sources with their item counts."""
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT s.id, s.name, s.path, s.created_at, s.updated_at,
                       COUNT(fi.id) AS item_count
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
                       COUNT(fi.id) AS item_count
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
        )

    def get_source_by_path(self, path: str) -> Optional[Source]:
        """Retrieve a source by normalized path."""
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT s.id, s.name, s.path, s.created_at, s.updated_at,
                       COUNT(fi.id) AS item_count
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
        )

    def get_items_by_source(self, source_id: int) -> List[FileItem]:
        """Retrieve all file items belonging to a source."""
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, source_id, path, title, type, metadata, created_at, updated_at
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
                SELECT id, source_id, path, title, type, metadata, created_at, updated_at
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
            created_at=r["created_at"],
            updated_at=r["updated_at"],
        )

    def get_item_count_for_source(self, source_id: int) -> int:
        """Count items for a given source ID."""
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT COUNT(id) AS cnt FROM file_items WHERE source_id = ?",
                (source_id,),
            )
            row = cursor.fetchone()
            return row["cnt"] if row else 0
