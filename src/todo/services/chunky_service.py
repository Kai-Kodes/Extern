"""Service for managing Chunky Tasks, range intervals, progress tracking, and resource completion."""

from datetime import datetime
import math
import sqlite3
from typing import Any, Callable, Dict, List, Optional, Tuple

from todo.database import Database
from todo.models import ChunkyPreset, ChunkyResource, ChunkyTask, FileItem, Task


# =============================================================================
# Pure Interval Math & Formatting Functions
# =============================================================================


def seconds_to_hms(seconds: float | int) -> str:
    """Format numeric seconds into HH:MM:SS string."""
    try:
        total = max(0, int(seconds))
    except (ValueError, TypeError):
        return "00:00:00"

    hours = total // 3600
    minutes = (total % 3600) // 60
    secs = total % 60
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def format_human_duration(seconds: float | int) -> str:
    """
    Format numeric seconds into a human-friendly duration string.
    Preserves exact integer seconds without floating-point artifacts.

    Examples:
        0 -> '0s'
        45 -> '45s'
        1200 -> '20m'
        400 -> '6m 40s'
        1600 -> '26m 40s'
        2800 -> '46m 40s'
        3600 -> '1h'
        5400 -> '1h 30m'
        7245 -> '2h 45s'
    """
    try:
        total = max(0, int(round(float(seconds))))
    except (ValueError, TypeError):
        return "0s"

    if total == 0:
        return "0s"

    hours = total // 3600
    minutes = (total % 3600) // 60
    secs = total % 60

    parts: List[str] = []
    if hours > 0:
        parts.append(f"{hours}h")
    if minutes > 0:
        parts.append(f"{minutes}m")
    if secs > 0:
        parts.append(f"{secs}s")

    return " ".join(parts) if parts else "0s"


def format_remaining(unit: str, amount: float | int) -> str:
    """
    Format remaining quantity based on the resource's natural unit.
    For 'seconds', formats into human-friendly minutes/seconds ('26m 40s').
    For 'pages', formats as integer pages ('750 pages').
    """
    if unit == "seconds":
        return format_human_duration(amount)
    elif unit == "pages":
        amt = max(0, int(round(float(amount))))
        return f"{amt} page" if amt == 1 else f"{amt} pages"
    else:
        amt = max(0, int(round(float(amount))))
        return f"{amt} {unit}"


def format_range(unit: str, start: float | int, end: float | int) -> str:
    """Format a range into a human-friendly string based on its natural unit."""
    if unit == "seconds":
        return f"{seconds_to_hms(start)}–{seconds_to_hms(end)}"
    elif unit == "pages":
        return f"p. {int(start)}–{int(end)}"
    else:
        return f"{int(start)}–{int(end)}"


def merge_intervals(
    intervals: List[Tuple[float, float]], total_range: Optional[float] = None
) -> List[Tuple[float, float]]:
    """
    Sort and merge overlapping or contiguous intervals.
    Clamps intervals to [0, total_range] if total_range is specified.
    Returns a sorted list of disjoint intervals [(start, end), ...].
    """
    if not intervals:
        return []

    valid: List[Tuple[float, float]] = []
    for s, e in intervals:
        try:
            s_val = float(s)
            e_val = float(e)
        except (ValueError, TypeError):
            continue

        if s_val < 0.0:
            s_val = 0.0
        if total_range is not None:
            if e_val > total_range:
                e_val = total_range
            if s_val > total_range:
                s_val = total_range

        if s_val < e_val:
            valid.append((s_val, e_val))

    if not valid:
        return []

    # Sort ascending by start, then by end
    valid.sort(key=lambda it: (it[0], it[1]))

    merged: List[Tuple[float, float]] = []
    for current_start, current_end in valid:
        if not merged:
            merged.append((current_start, current_end))
        else:
            prev_start, prev_end = merged[-1]
            if current_start <= prev_end:
                # Overlapping or contiguous: extend previous
                merged[-1] = (prev_start, max(prev_end, current_end))
            else:
                merged.append((current_start, current_end))

    return merged


def calculate_coverage(
    intervals: List[Tuple[float, float]],
    total_range: float,
    is_fully_completed: bool = False,
) -> Tuple[float, int]:
    """
    Compute total covered length and progress percentage from intervals.
    Prevents double counting by computing the mathematical union of intervals.
    """
    if is_fully_completed:
        return float(total_range), 100

    if total_range <= 0.0:
        return 0.0, 0

    merged = merge_intervals(intervals, total_range=total_range)
    covered = sum(e - s for s, e in merged)
    covered = min(float(total_range), max(0.0, covered))
    pct = int(min(100.0, (covered / total_range) * 100.0))
    return covered, pct


def propose_next_chunk(
    completed_intervals: List[Tuple[float, float]],
    total_range: float,
    chunk_size: float,
    is_fully_completed: bool = False,
) -> Optional[Tuple[float, float]]:
    """
    Propose the next uncompleted range [start, end] for a resource.
    Finds the earliest remaining gap in [0, total_range].
    Clamps the proposal so it does not extend beyond the gap or total_range.
    Returns None if resource is 100% completed or parameters are invalid.
    """
    if is_fully_completed or chunk_size <= 0.0 or total_range <= 0.0:
        return None

    merged = merge_intervals(completed_intervals, total_range=total_range)

    cursor = 0.0
    for start, end in merged:
        if cursor < start:
            # Found gap from cursor to start
            chunk_end = min(start, cursor + chunk_size)
            return (cursor, chunk_end)
        cursor = max(cursor, end)

    if cursor < total_range:
        chunk_end = min(total_range, cursor + chunk_size)
        return (cursor, chunk_end)

    return None


def get_remaining_intervals(
    completed_intervals: List[Tuple[float, float]],
    total_range: float,
) -> List[Tuple[float, float]]:
    """
    Return all disjoint uncompleted intervals within [0, total_range].
    """
    if total_range <= 0.0:
        return []
    merged = merge_intervals(completed_intervals, total_range=total_range)
    gaps: List[Tuple[float, float]] = []
    cursor = 0.0
    for s, e in merged:
        if cursor < s:
            gaps.append((cursor, s))
        cursor = max(cursor, e)
    if cursor < total_range:
        gaps.append((cursor, total_range))
    return gaps


def propose_entire_remaining_chunk(
    completed_intervals: List[Tuple[float, float]],
    total_range: float,
    is_fully_completed: bool = False,
) -> Optional[Tuple[float, float]]:
    """
    Propose the next uncompleted chunk covering the entire uncompleted extent
    of the earliest remaining gap.
    Safely respects interval boundaries so it never bridges across already-completed intervals.
    Returns None if resource is 100% completed or total_range <= 0.
    """
    if is_fully_completed or total_range <= 0.0:
        return None
    return propose_next_chunk(
        completed_intervals=completed_intervals,
        total_range=total_range,
        chunk_size=total_range,
        is_fully_completed=is_fully_completed,
    )


# =============================================================================
# ChunkyService
# =============================================================================


class ChunkyService:
    def __init__(
        self,
        db: Database,
        date_provider: Optional[Callable[[], datetime]] = None,
    ):
        self.db = db
        self.date_provider = date_provider

    def get_now(self) -> datetime:
        return self.date_provider() if self.date_provider else datetime.now()

    def validate_preset(self, preset: ChunkyPreset) -> None:
        """Validate preset fields, raising ValueError with clear message if invalid."""
        if not preset.name or not str(preset.name).strip():
            raise ValueError("Preset name cannot be empty.")
        clean_name = str(preset.name).strip()
        if len(clean_name) > 60:
            raise ValueError("Preset name must not exceed 60 characters.")

        try:
            dur = float(preset.duration)
        except (ValueError, TypeError):
            raise ValueError("Duration must be a valid number.")

        if not math.isfinite(dur) or dur <= 0:
            raise ValueError("Duration must be a positive, finite number.")

        if preset.range_unit not in ("seconds", "pages"):
            raise ValueError(f"Unsupported range unit '{preset.range_unit}'.")

        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            if preset.id is not None:
                cursor.execute(
                    "SELECT id FROM chunky_presets WHERE range_unit = ? AND lower(name) = lower(?) AND id != ?",
                    (preset.range_unit, clean_name, preset.id),
                )
            else:
                cursor.execute(
                    "SELECT id FROM chunky_presets WHERE range_unit = ? AND lower(name) = lower(?)",
                    (preset.range_unit, clean_name),
                )
            if cursor.fetchone():
                raise ValueError(f"A preset named '{clean_name}' already exists for {preset.range_unit}.")

    def list_presets(self, range_unit: str = "seconds") -> List[ChunkyPreset]:
        """Return all saved Chunky presets for the given range unit, ordered by duration."""
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, name, duration, range_unit, is_default, is_builtin, created_at, updated_at
                FROM chunky_presets
                WHERE range_unit = ?
                ORDER BY duration ASC, id ASC
                """,
                (range_unit,),
            )
            rows = cursor.fetchall()
            if not rows:
                self.seed_default_presets(conn)
                cursor.execute(
                    """
                    SELECT id, name, duration, range_unit, is_default, is_builtin, created_at, updated_at
                    FROM chunky_presets
                    WHERE range_unit = ?
                    ORDER BY duration ASC, id ASC
                    """,
                    (range_unit,),
                )
                rows = cursor.fetchall()

            presets = [
                ChunkyPreset(
                    id=row["id"],
                    name=row["name"],
                    duration=float(row["duration"]),
                    range_unit=row["range_unit"],
                    is_default=bool(row["is_default"]),
                    is_builtin=bool(row["is_builtin"]),
                    created_at=row["created_at"],
                    updated_at=row["updated_at"],
                )
                for row in rows
            ]

            if presets and not any(p.is_default for p in presets):
                first = presets[0]
                cursor.execute("UPDATE chunky_presets SET is_default = 1 WHERE id = ?", (first.id,))
                conn.commit()
                first.is_default = True

            return presets

    def get_preset(self, preset_id: int) -> Optional[ChunkyPreset]:
        """Fetch a preset by id."""
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, name, duration, range_unit, is_default, is_builtin, created_at, updated_at
                FROM chunky_presets
                WHERE id = ?
                """,
                (preset_id,),
            )
            row = cursor.fetchone()
            if not row:
                return None
            return ChunkyPreset(
                id=row["id"],
                name=row["name"],
                duration=float(row["duration"]),
                range_unit=row["range_unit"],
                is_default=bool(row["is_default"]),
                is_builtin=bool(row["is_builtin"]),
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )

    def get_default_preset(self, range_unit: str = "seconds") -> Optional[ChunkyPreset]:
        """Return the default preset for a range unit, falling back to the first available preset."""
        presets = self.list_presets(range_unit=range_unit)
        for p in presets:
            if p.is_default:
                return p
        return presets[0] if presets else None

    def create_preset(self, preset: ChunkyPreset) -> ChunkyPreset:
        """Validate and insert a new ChunkyPreset."""
        self.validate_preset(preset)
        now_str = self.get_now().isoformat()
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            if preset.is_default:
                cursor.execute(
                    "UPDATE chunky_presets SET is_default = 0 WHERE range_unit = ?",
                    (preset.range_unit,),
                )
            cursor.execute(
                """
                INSERT INTO chunky_presets (
                    name, duration, range_unit, is_default, is_builtin, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    preset.name.strip(),
                    float(preset.duration),
                    preset.range_unit,
                    1 if preset.is_default else 0,
                    1 if preset.is_builtin else 0,
                    now_str,
                    now_str,
                ),
            )
            preset.id = cursor.lastrowid
            preset.created_at = now_str
            preset.updated_at = now_str
            conn.commit()
            return preset

    def update_preset(self, preset: ChunkyPreset, allow_builtin: bool = False) -> None:
        """Validate and update an existing ChunkyPreset. Built-in presets are protected by default."""
        if not preset.id:
            raise ValueError("Preset ID required for update.")
        existing = self.get_preset(preset.id)
        if existing and existing.is_builtin and not allow_builtin:
            raise ValueError(f"Built-in preset '{existing.name}' cannot be edited. Create a custom preset instead.")
        self.validate_preset(preset)
        now_str = self.get_now().isoformat()
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            if preset.is_default:
                cursor.execute(
                    "UPDATE chunky_presets SET is_default = 0 WHERE range_unit = ? AND id != ?",
                    (preset.range_unit, preset.id),
                )
            cursor.execute(
                """
                UPDATE chunky_presets
                SET name = ?, duration = ?, range_unit = ?, is_default = ?, is_builtin = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    preset.name.strip(),
                    float(preset.duration),
                    preset.range_unit,
                    1 if preset.is_default else 0,
                    1 if preset.is_builtin else 0,
                    now_str,
                    preset.id,
                ),
            )
            conn.commit()

    def set_default_preset(self, preset_id: int) -> None:
        """Set a preset as the active default for its range unit."""
        preset = self.get_preset(preset_id)
        if not preset:
            raise ValueError(f"Preset #{preset_id} not found.")
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE chunky_presets SET is_default = 0 WHERE range_unit = ?",
                (preset.range_unit,),
            )
            cursor.execute(
                "UPDATE chunky_presets SET is_default = 1 WHERE id = ?",
                (preset_id,),
            )
            conn.commit()

    def delete_preset(self, preset_id: int, allow_builtin: bool = False) -> bool:
        """Delete a preset, promoting another to default if necessary. Built-in presets are protected."""
        preset = self.get_preset(preset_id)
        if not preset:
            return False
        if preset.is_builtin and not allow_builtin:
            raise ValueError(f"Built-in preset '{preset.name}' cannot be deleted. Only custom presets can be deleted.")
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT count(*) as count FROM chunky_presets WHERE range_unit = ?",
                (preset.range_unit,),
            )
            count = cursor.fetchone()["count"]
            if count <= 1:
                raise ValueError("Cannot delete the only remaining preset for this resource type.")

            cursor.execute("DELETE FROM chunky_presets WHERE id = ?", (preset_id,))
            if preset.is_default:
                cursor.execute(
                    "SELECT id FROM chunky_presets WHERE range_unit = ? ORDER BY duration ASC, id ASC LIMIT 1",
                    (preset.range_unit,),
                )
                row = cursor.fetchone()
                if row:
                    cursor.execute("UPDATE chunky_presets SET is_default = 1 WHERE id = ?", (row["id"],))
            conn.commit()
            return True

    def seed_default_presets(self, conn: Optional[sqlite3.Connection] = None) -> None:
        """Seed default built-in presets for seconds and pages if they don't exist."""
        now_str = self.get_now().isoformat()
        should_close = False
        if conn is None:
            conn = self.db.get_connection()
            should_close = True

        try:
            cursor = conn.cursor()
            cursor.execute("SELECT count(*) as count FROM chunky_presets WHERE range_unit = 'seconds'")
            if cursor.fetchone()["count"] == 0:
                defaults_seconds = [
                    ("15 minutes", 900.0, False),
                    ("20 minutes", 1200.0, True),
                    ("30 minutes", 1800.0, False),
                    ("45 minutes", 2700.0, False),
                    ("60 minutes", 3600.0, False),
                ]
                for name, dur, is_def in defaults_seconds:
                    cursor.execute(
                        """
                        INSERT OR IGNORE INTO chunky_presets (
                            name, duration, range_unit, is_default, is_builtin, created_at, updated_at
                        ) VALUES (?, ?, 'seconds', ?, 1, ?, ?)
                        """,
                        (name, dur, 1 if is_def else 0, now_str, now_str),
                    )

            cursor.execute("SELECT count(*) as count FROM chunky_presets WHERE range_unit = 'pages'")
            if cursor.fetchone()["count"] == 0:
                defaults_pages = [
                    ("5 pages", 5.0, False),
                    ("10 pages", 10.0, True),
                    ("15 pages", 15.0, False),
                    ("20 pages", 20.0, False),
                    ("25 pages", 25.0, False),
                ]
                for name, dur, is_def in defaults_pages:
                    cursor.execute(
                        """
                        INSERT OR IGNORE INTO chunky_presets (
                            name, duration, range_unit, is_default, is_builtin, created_at, updated_at
                        ) VALUES (?, ?, 'pages', ?, 1, ?, ?)
                        """,
                        (name, dur, 1 if is_def else 0, now_str, now_str),
                    )

            conn.commit()
        finally:
            if should_close:
                conn.close()

    def get_remaining_intervals(
        self,
        completed_intervals: List[Tuple[float, float]],
        total_range: float,
    ) -> List[Tuple[float, float]]:
        """Return all disjoint uncompleted intervals within [0, total_range]."""
        return get_remaining_intervals(completed_intervals, total_range)

    def propose_entire_remaining_chunk(
        self,
        completed_intervals: List[Tuple[float, float]],
        total_range: float,
        is_fully_completed: bool = False,
    ) -> Optional[Tuple[float, float]]:
        """Propose the entire uncompleted interval of the earliest remaining gap."""
        return propose_entire_remaining_chunk(
            completed_intervals=completed_intervals,
            total_range=total_range,
            is_fully_completed=is_fully_completed,
        )


    def get_or_create_resource(
        self,
        source_type: str,
        canonical_uri: str,
        title: str,
        total_range: float,
        range_unit: str = "seconds",
        file_item_id: Optional[int] = None,
    ) -> ChunkyResource:
        """
        Retrieve an existing ChunkyResource by canonical_uri or create a new one.
        Survives restarts and metadata refreshes.
        """
        now = self.get_now().isoformat()
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, file_item_id, source_type, canonical_uri, title,
                       range_unit, total_range, completed_range, is_fully_completed,
                       fully_completed_at, created_at, updated_at
                FROM chunky_resources
                WHERE canonical_uri = ?
                """,
                (canonical_uri,),
            )
            row = cursor.fetchone()

            if row:
                res_id = row["id"]
                # If total_range or title changed, update but preserve completed progress
                cursor.execute(
                    """
                    UPDATE chunky_resources
                    SET title = ?, total_range = ?, file_item_id = COALESCE(?, file_item_id), updated_at = ?
                    WHERE id = ?
                    """,
                    (title, total_range, file_item_id, now, res_id),
                )
                conn.commit()

                # Re-fetch updated completed coverage
                cursor.execute(
                    "SELECT completed_range, is_fully_completed, fully_completed_at FROM chunky_resources WHERE id = ?",
                    (res_id,),
                )
                updated_row = cursor.fetchone()
                return ChunkyResource(
                    id=res_id,
                    file_item_id=row["file_item_id"] or file_item_id,
                    source_type=row["source_type"],
                    canonical_uri=row["canonical_uri"],
                    title=title,
                    range_unit=row["range_unit"],
                    total_range=total_range,
                    completed_range=updated_row["completed_range"],
                    is_fully_completed=bool(updated_row["is_fully_completed"]),
                    fully_completed_at=updated_row["fully_completed_at"],
                    created_at=row["created_at"],
                    updated_at=now,
                )

            # Insert new
            cursor.execute(
                """
                INSERT INTO chunky_resources (
                    file_item_id, source_type, canonical_uri, title, range_unit,
                    total_range, completed_range, is_fully_completed, fully_completed_at,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, 0.0, 0, NULL, ?, ?)
                """,
                (
                    file_item_id,
                    source_type,
                    canonical_uri,
                    title,
                    range_unit,
                    total_range,
                    now,
                    now,
                ),
            )
            res_id = cursor.lastrowid
            conn.commit()

        return ChunkyResource(
            id=res_id,
            file_item_id=file_item_id,
            source_type=source_type,
            canonical_uri=canonical_uri,
            title=title,
            range_unit=range_unit,
            total_range=total_range,
            completed_range=0.0,
            is_fully_completed=False,
            fully_completed_at=None,
            created_at=now,
            updated_at=now,
        )

    def get_resource_by_id(self, resource_id: int) -> Optional[ChunkyResource]:
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, file_item_id, source_type, canonical_uri, title,
                       range_unit, total_range, completed_range, is_fully_completed,
                       fully_completed_at, created_at, updated_at
                FROM chunky_resources
                WHERE id = ?
                """,
                (resource_id,),
            )
            row = cursor.fetchone()

        if not row:
            return None

        return ChunkyResource(
            id=row["id"],
            file_item_id=row["file_item_id"],
            source_type=row["source_type"],
            canonical_uri=row["canonical_uri"],
            title=row["title"],
            range_unit=row["range_unit"],
            total_range=row["total_range"],
            completed_range=row["completed_range"],
            is_fully_completed=bool(row["is_fully_completed"]),
            fully_completed_at=row["fully_completed_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def get_resource_for_file_item(self, file_item_id: int) -> Optional[ChunkyResource]:
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, file_item_id, source_type, canonical_uri, title,
                       range_unit, total_range, completed_range, is_fully_completed,
                       fully_completed_at, created_at, updated_at
                FROM chunky_resources
                WHERE file_item_id = ?
                """,
                (file_item_id,),
            )
            row = cursor.fetchone()

        if not row:
            return None

        return ChunkyResource(
            id=row["id"],
            file_item_id=row["file_item_id"],
            source_type=row["source_type"],
            canonical_uri=row["canonical_uri"],
            title=row["title"],
            range_unit=row["range_unit"],
            total_range=row["total_range"],
            completed_range=row["completed_range"],
            is_fully_completed=bool(row["is_fully_completed"]),
            fully_completed_at=row["fully_completed_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def get_completed_ranges(self, resource_id: int) -> List[Tuple[float, float]]:
        """Return all currently completed chunk ranges for a resource."""
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT start_pos, end_pos
                FROM chunky_tasks
                WHERE resource_id = ? AND is_completed = 1
                ORDER BY start_pos ASC
                """,
                (resource_id,),
            )
            rows = cursor.fetchall()
        return [(r["start_pos"], r["end_pos"]) for r in rows]

    def get_chunks_for_resource(self, resource_id: int) -> List[ChunkyTask]:
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, resource_id, task_id, chunk_index, start_pos, end_pos,
                       is_completed, completed_at, created_at, updated_at
                FROM chunky_tasks
                WHERE resource_id = ?
                ORDER BY start_pos ASC
                """,
                (resource_id,),
            )
            rows = cursor.fetchall()

        return [
            ChunkyTask(
                id=r["id"],
                resource_id=r["resource_id"],
                task_id=r["task_id"],
                chunk_index=r["chunk_index"],
                start_pos=r["start_pos"],
                end_pos=r["end_pos"],
                is_completed=bool(r["is_completed"]),
                completed_at=r["completed_at"],
                created_at=r["created_at"],
                updated_at=r["updated_at"],
            )
            for r in rows
        ]

    def is_chunk_task(self, task_id: int) -> bool:
        """Check if a task_id corresponds to an existing chunky task."""
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT 1 FROM chunky_tasks WHERE task_id = ?", (task_id,))
            return cursor.fetchone() is not None

    def create_chunk_task(
        self,
        resource: ChunkyResource,
        start_pos: float,
        end_pos: float,
        task_service: Any,
        task_title: Optional[str] = None,
        task_description: Optional[str] = None,
    ) -> Tuple[Task, ChunkyTask]:
        """
        Create one ordinary Extern task representing a chunk range and record its chunky definition.
        """
        now = self.get_now().isoformat()

        if start_pos < 0 or start_pos >= end_pos:
            raise ValueError(f"Invalid range: [{start_pos}, {end_pos}]")

        formatted_range = format_range(resource.range_unit, start_pos, end_pos)
        if not task_title:
            task_title = f"{resource.title} [{formatted_range}]"

        if not task_description:
            if resource.source_type == "youtube":
                # Add timestamp query param to YouTube URL
                vid_id = resource.canonical_uri.replace("youtube://video/", "")
                start_int = int(start_pos)
                task_description = f"https://www.youtube.com/watch?v={vid_id}&t={start_int}s"
            else:
                task_description = f"Source: {resource.title} · Range: {formatted_range}"

        with self.db.get_connection() as conn:
            cursor = conn.cursor()

            # Determine next chunk_index
            cursor.execute(
                "SELECT COALESCE(MAX(chunk_index), 0) AS max_idx FROM chunky_tasks WHERE resource_id = ?",
                (resource.id,),
            )
            chunk_idx = cursor.fetchone()["max_idx"] + 1

            # 1. Create ordinary task via task_service
            task = task_service.create_task(
                title=task_title,
                description=task_description,
                source_type=resource.source_type,
                source_id=resource.file_item_id,
                status="pending",
            )

            # 2. Record chunk definition
            cursor.execute(
                """
                INSERT INTO chunky_tasks (
                    resource_id, task_id, chunk_index, start_pos, end_pos,
                    is_completed, completed_at, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, 0, NULL, ?, ?)
                """,
                (resource.id, task.id, chunk_idx, start_pos, end_pos, now, now),
            )
            chunk_id = cursor.lastrowid
            conn.commit()

        chunky_task = ChunkyTask(
            id=chunk_id,
            resource_id=resource.id,
            task_id=task.id,
            chunk_index=chunk_idx,
            start_pos=start_pos,
            end_pos=end_pos,
            is_completed=False,
            completed_at=None,
            created_at=now,
            updated_at=now,
        )

        return task, chunky_task

    def on_task_completion_changed(
        self, task_id: int, is_completed: bool
    ) -> Optional[ChunkyTask]:
        """
        Hook called when an ordinary task's completion status is toggled.
        Updates chunky_tasks, logs immutable completion history, and recalculates resource progress.
        """
        now = self.get_now().isoformat()
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, resource_id, task_id, chunk_index, start_pos, end_pos,
                       is_completed, completed_at, created_at, updated_at
                FROM chunky_tasks
                WHERE task_id = ?
                """,
                (task_id,),
            )
            row = cursor.fetchone()
            if not row:
                return None

            chunk_id = row["id"]
            resource_id = row["resource_id"]
            start_pos = row["start_pos"]
            end_pos = row["end_pos"]
            prev_completed = bool(row["is_completed"])

            # Avoid redundant duplicate event processing
            if prev_completed == is_completed:
                return ChunkyTask(
                    id=chunk_id,
                    resource_id=resource_id,
                    task_id=task_id,
                    chunk_index=row["chunk_index"],
                    start_pos=start_pos,
                    end_pos=end_pos,
                    is_completed=is_completed,
                    completed_at=row["completed_at"],
                    created_at=row["created_at"],
                    updated_at=row["updated_at"],
                )

            new_completed_val = 1 if is_completed else 0
            new_completed_at = now if is_completed else None
            action = "completed" if is_completed else "reopened"

            # 1. Update chunky_tasks
            cursor.execute(
                """
                UPDATE chunky_tasks
                SET is_completed = ?, completed_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (new_completed_val, new_completed_at, now, chunk_id),
            )

            # 2. Append immutable history record
            cursor.execute(
                """
                INSERT INTO chunky_completion_history (
                    chunk_id, resource_id, task_id, start_pos, end_pos,
                    completed_at, action, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (chunk_id, resource_id, task_id, start_pos, end_pos, now, action, now),
            )

            # 3. Recalculate resource progress from scratch based on actual completed chunk intervals
            cursor.execute(
                "SELECT total_range, is_fully_completed, fully_completed_at, file_item_id FROM chunky_resources WHERE id = ?",
                (resource_id,),
            )
            res_row = cursor.fetchone()
            total_range = res_row["total_range"] if res_row else 0.0
            is_full = bool(res_row["is_fully_completed"]) if res_row else False
            existing_fully_at = res_row["fully_completed_at"] if res_row else None
            fi_id = res_row["file_item_id"] if res_row else None

            cursor.execute(
                "SELECT start_pos, end_pos FROM chunky_tasks WHERE resource_id = ? AND is_completed = 1",
                (resource_id,),
            )
            comp_ranges = [(r["start_pos"], r["end_pos"]) for r in cursor.fetchall()]

            # Calculate actual interval coverage of completed chunks (never assume 100% full override on chunk toggle)
            covered, pct = calculate_coverage(comp_ranges, total_range, is_fully_completed=False)

            # If all intervals are complete (accounting for float precision), mark resource fully completed
            # Otherwise, resource remains incomplete (< 100%)
            is_covered_100 = (total_range > 0 and covered >= total_range - 1e-6)
            new_is_full = 1 if is_covered_100 else 0
            fully_at = (existing_fully_at or now) if new_is_full else None

            cursor.execute(
                """
                UPDATE chunky_resources
                SET completed_range = ?, is_fully_completed = ?,
                    fully_completed_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (covered, new_is_full, fully_at, now, resource_id),
            )

            # If 100% complete, also mark parent file_item complete
            if new_is_full and fi_id:
                cursor.execute(
                    "UPDATE file_items SET completed = 1, completed_at = COALESCE(completed_at, ?), updated_at = ? WHERE id = ?",
                    (now, now, fi_id),
                )
            elif not new_is_full and fi_id:
                # If resource is not 100% complete, ensure parent file_item is reopened if it was marked completed
                cursor.execute(
                    "UPDATE file_items SET completed = 0, completed_at = NULL, updated_at = ? WHERE id = ?",
                    (now, fi_id),
                )

            conn.commit()

        return ChunkyTask(
            id=chunk_id,
            resource_id=resource_id,
            task_id=task_id,
            chunk_index=row["chunk_index"],
            start_pos=start_pos,
            end_pos=end_pos,
            is_completed=is_completed,
            completed_at=new_completed_at,
            created_at=row["created_at"],
            updated_at=now,
        )

    def on_file_item_completion_changed(
        self, file_item_id: int, is_completed: bool
    ) -> None:
        """
        Hook called when an imported FileItem is completed or reopened through its normal workflow.
        Completing the original resource sets progress to 100%, records full-completion timestamp,
        and transitions outstanding chunk tasks into completed terminal state.
        Reopening sets is_fully_completed = 0 and recalculates progress from remaining completed chunks.
        """
        now = self.get_now().isoformat()
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, total_range, is_fully_completed
                FROM chunky_resources
                WHERE file_item_id = ?
                """,
                (file_item_id,),
            )
            resources = cursor.fetchall()
            if not resources:
                return

            for res in resources:
                res_id = res["id"]
                total_range = res["total_range"]

                if is_completed:
                    # 1. Set resource to 100% complete
                    cursor.execute(
                        """
                        UPDATE chunky_resources
                        SET completed_range = total_range, is_fully_completed = 1,
                            fully_completed_at = COALESCE(fully_completed_at, ?), updated_at = ?
                        WHERE id = ?
                        """,
                        (now, now, res_id),
                    )

                    # 2. Transition outstanding (pending) chunk tasks into completed terminal state
                    cursor.execute(
                        """
                        SELECT id, task_id, start_pos, end_pos
                        FROM chunky_tasks
                        WHERE resource_id = ? AND is_completed = 0
                        """,
                        (res_id,),
                    )
                    pending_chunks = cursor.fetchall()

                    for ch in pending_chunks:
                        ch_id = ch["id"]
                        t_id = ch["task_id"]

                        # Complete chunky task
                        cursor.execute(
                            "UPDATE chunky_tasks SET is_completed = 1, completed_at = ?, updated_at = ? WHERE id = ?",
                            (now, now, ch_id),
                        )
                        # Complete standard task
                        cursor.execute(
                            "UPDATE tasks SET status = 'completed', updated_at = ? WHERE id = ?",
                            (now, t_id),
                        )
                        # Complete daily task if scheduled
                        cursor.execute(
                            """
                            UPDATE daily_tasks
                            SET completed_at = ?
                            WHERE id = (
                                SELECT id FROM daily_tasks WHERE task_id = ? ORDER BY date DESC, id DESC LIMIT 1
                            )
                            """,
                            (now, t_id),
                        )
                        # Append history entry
                        cursor.execute(
                            """
                            INSERT INTO chunky_completion_history (
                                chunk_id, resource_id, task_id, start_pos, end_pos,
                                completed_at, action, created_at
                            )
                            VALUES (?, ?, ?, ?, ?, ?, 'full_override', ?)
                            """,
                            (ch_id, res_id, t_id, ch["start_pos"], ch["end_pos"], now, now),
                        )

                else:
                    # Resource explicitly reopened
                    cursor.execute(
                        """
                        UPDATE chunky_resources
                        SET is_fully_completed = 0, fully_completed_at = NULL, updated_at = ?
                        WHERE id = ?
                        """,
                        (now, res_id),
                    )

                    # Recalculate progress from remaining completed chunks
                    cursor.execute(
                        "SELECT start_pos, end_pos FROM chunky_tasks WHERE resource_id = ? AND is_completed = 1",
                        (res_id,),
                    )
                    comp_ranges = [(r["start_pos"], r["end_pos"]) for r in cursor.fetchall()]
                    covered, _ = calculate_coverage(comp_ranges, total_range, is_fully_completed=False)

                    cursor.execute(
                        "UPDATE chunky_resources SET completed_range = ?, updated_at = ? WHERE id = ?",
                        (covered, now, res_id),
                    )

                    # Append history record for reopening
                    cursor.execute(
                        """
                        INSERT INTO chunky_completion_history (
                            chunk_id, resource_id, task_id, start_pos, end_pos,
                            completed_at, action, created_at
                        )
                        SELECT id, resource_id, task_id, start_pos, end_pos, ?, 'reopened', ?
                        FROM chunky_tasks
                        WHERE resource_id = ?
                        LIMIT 1
                        """,
                        (now, now, res_id),
                    )

            conn.commit()
