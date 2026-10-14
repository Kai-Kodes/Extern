"""Pomodoro preset and state persistence service."""

from datetime import datetime
from typing import Callable, List, Optional
from todo.database import Database
from todo.models import PomodoroPreset, PomodoroStateRecord


class PomodoroService:
    def __init__(
        self,
        db: Database,
        date_provider: Optional[Callable[[], datetime]] = None,
    ):
        self.db = db
        self.date_provider = date_provider or datetime.now

    def _now_iso(self) -> str:
        return self.date_provider().isoformat()

    def validate_preset(self, preset: PomodoroPreset) -> None:
        """Validate preset fields, raising ValueError with clear message if invalid."""
        if not preset.name or not preset.name.strip():
            raise ValueError("Name cannot be empty")
        if preset.work_duration < 1 or preset.work_duration > 360:
            raise ValueError(f"Work duration must be between 1 and 360 minutes, got {preset.work_duration}")
        if preset.short_break_duration < 1 or preset.short_break_duration > 120:
            raise ValueError(f"Short break duration must be between 1 and 120 minutes, got {preset.short_break_duration}")
        if preset.long_break_duration < 1 or preset.long_break_duration > 180:
            raise ValueError(f"Long break duration must be between 1 and 180 minutes, got {preset.long_break_duration}")
        if preset.sessions_before_long_break < 1 or preset.sessions_before_long_break > 24:
            raise ValueError(f"Sessions count before long break must be between 1 and 24, got {preset.sessions_before_long_break}")

    def list_presets(self) -> List[PomodoroPreset]:
        """Return all saved Pomodoro presets ordered by id."""
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, name, work_duration, short_break_duration, long_break_duration,
                       sessions_before_long_break, auto_advance, sound_enabled,
                       created_at, updated_at
                FROM pomodoro_presets
                ORDER BY id ASC
                """
            )
            rows = cursor.fetchall()
            return [
                PomodoroPreset(
                    id=row["id"],
                    name=row["name"],
                    work_duration=row["work_duration"],
                    short_break_duration=row["short_break_duration"],
                    long_break_duration=row["long_break_duration"],
                    sessions_before_long_break=row["sessions_before_long_break"],
                    auto_advance=bool(row["auto_advance"]),
                    sound_enabled=bool(row["sound_enabled"]),
                    created_at=row["created_at"],
                    updated_at=row["updated_at"],
                )
                for row in rows
            ]

    def get_preset(self, preset_id: int) -> Optional[PomodoroPreset]:
        """Fetch a preset by id."""
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, name, work_duration, short_break_duration, long_break_duration,
                       sessions_before_long_break, auto_advance, sound_enabled,
                       created_at, updated_at
                FROM pomodoro_presets
                WHERE id = ?
                """,
                (preset_id,),
            )
            row = cursor.fetchone()
            if not row:
                return None
            return PomodoroPreset(
                id=row["id"],
                name=row["name"],
                work_duration=row["work_duration"],
                short_break_duration=row["short_break_duration"],
                long_break_duration=row["long_break_duration"],
                sessions_before_long_break=row["sessions_before_long_break"],
                auto_advance=bool(row["auto_advance"]),
                sound_enabled=bool(row["sound_enabled"]),
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )

    def create_preset(self, preset: PomodoroPreset) -> PomodoroPreset:
        """Validate and create a new preset."""
        self.validate_preset(preset)
        now_str = self._now_iso()
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO pomodoro_presets (
                    name, work_duration, short_break_duration, long_break_duration,
                    sessions_before_long_break, auto_advance, sound_enabled,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    preset.name.strip(),
                    preset.work_duration,
                    preset.short_break_duration,
                    preset.long_break_duration,
                    preset.sessions_before_long_break,
                    1 if preset.auto_advance else 0,
                    1 if preset.sound_enabled else 0,
                    now_str,
                    now_str,
                ),
            )
            conn.commit()
            preset.id = cursor.lastrowid
            preset.created_at = now_str
            preset.updated_at = now_str
            return preset

    def update_preset(self, preset: PomodoroPreset) -> None:
        """Validate and update an existing preset."""
        if not preset.id:
            raise ValueError("Preset ID required for update")
        self.validate_preset(preset)
        now_str = self._now_iso()
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE pomodoro_presets
                SET name = ?, work_duration = ?, short_break_duration = ?,
                    long_break_duration = ?, sessions_before_long_break = ?,
                    auto_advance = ?, sound_enabled = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    preset.name.strip(),
                    preset.work_duration,
                    preset.short_break_duration,
                    preset.long_break_duration,
                    preset.sessions_before_long_break,
                    1 if preset.auto_advance else 0,
                    1 if preset.sound_enabled else 0,
                    now_str,
                    preset.id,
                ),
            )
            conn.commit()

    def delete_preset(self, preset_id: int, active_preset_id: Optional[int] = None) -> bool:
        """Delete a preset if not active."""
        if active_preset_id is not None and preset_id == active_preset_id:
            raise ValueError("Cannot delete active running preset")

        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM pomodoro_presets WHERE id = ?", (preset_id,))
            conn.commit()
            return cursor.rowcount > 0

    def save_session_state(self, record: PomodoroStateRecord) -> None:
        """Persist authoritative session state for crash/restart recovery."""
        now_str = self._now_iso()
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO pomodoro_state (
                    id, preset_id, phase, state, deadline_wall_clock,
                    remaining_seconds_when_paused, completed_work_sessions,
                    current_session_index, updated_at
                ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    preset_id = excluded.preset_id,
                    phase = excluded.phase,
                    state = excluded.state,
                    deadline_wall_clock = excluded.deadline_wall_clock,
                    remaining_seconds_when_paused = excluded.remaining_seconds_when_paused,
                    completed_work_sessions = excluded.completed_work_sessions,
                    current_session_index = excluded.current_session_index,
                    updated_at = excluded.updated_at
                """,
                (
                    record.preset_id,
                    record.phase,
                    record.state,
                    record.deadline_wall_clock,
                    record.remaining_seconds_when_paused,
                    record.completed_work_sessions,
                    record.current_session_index,
                    now_str,
                ),
            )
            conn.commit()

    def load_session_state(self) -> Optional[PomodoroStateRecord]:
        """Load persisted session state if present."""
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT preset_id, phase, state, deadline_wall_clock,
                       remaining_seconds_when_paused, completed_work_sessions,
                       current_session_index, updated_at
                FROM pomodoro_state
                WHERE id = 1
                """
            )
            row = cursor.fetchone()
            if not row:
                return None
            return PomodoroStateRecord(
                preset_id=row["preset_id"],
                phase=row["phase"],
                state=row["state"],
                deadline_wall_clock=row["deadline_wall_clock"],
                remaining_seconds_when_paused=row["remaining_seconds_when_paused"],
                completed_work_sessions=row["completed_work_sessions"],
                current_session_index=row["current_session_index"],
                id=1,
                updated_at=row["updated_at"],
            )

    def clear_session_state(self) -> None:
        """Clear active session state."""
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM pomodoro_state WHERE id = 1")
            conn.commit()
