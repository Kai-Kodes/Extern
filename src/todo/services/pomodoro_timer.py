"""Authoritative Pomodoro timer engine and state machine."""

import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional

from todo.models import (
    PomodoroPreset,
    PomodoroSessionPhase,
    PomodoroStateRecord,
    PomodoroTimerState,
)
from todo.services.audio_service import AudioService
from todo.services.pomodoro_service import PomodoroService


class SystemClock:
    """Default system clock provider using standard libraries."""

    def monotonic(self) -> float:
        return time.monotonic()

    def now(self) -> datetime:
        return datetime.now()


@dataclass
class TimerEvent:
    name: str  # 'phase_change', 'cycle_completed', 'sound_played', etc.
    payload: Dict[str, Any] = field(default_factory=dict)


class PomodoroTimer:
    """
    Authoritative Pomodoro state machine and timer.
    Uses monotonic time deadlines during live execution to prevent clock-drift,
    and wall-clock timestamps for crash/restart recovery in SQLite.
    """

    def __init__(
        self,
        preset: Optional[PomodoroPreset] = None,
        pomodoro_service: Optional[PomodoroService] = None,
        audio_service: Optional[AudioService] = None,
        clock: Optional[Any] = None,
    ):
        self.preset = preset
        self.pomodoro_service = pomodoro_service
        self.audio_service = audio_service
        self.clock = clock or SystemClock()

        self.state: PomodoroTimerState = PomodoroTimerState.IDLE
        self.phase: PomodoroSessionPhase = PomodoroSessionPhase.WORK
        self.current_session_index: int = 1
        self.completed_work_sessions: int = 0

        self.deadline_monotonic: float = 0.0
        self.deadline_wall_clock: Optional[datetime] = None
        self.remaining_seconds_when_paused: Optional[int] = None

    def get_phase_duration_seconds(self, phase: Optional[PomodoroSessionPhase] = None) -> int:
        """Return total duration in seconds for given phase (or current phase)."""
        if not self.preset:
            return 25 * 60

        target_phase = phase or self.phase
        if target_phase == PomodoroSessionPhase.WORK:
            return self.preset.work_duration * 60
        elif target_phase == PomodoroSessionPhase.SHORT_BREAK:
            return self.preset.short_break_duration * 60
        elif target_phase == PomodoroSessionPhase.LONG_BREAK:
            return self.preset.long_break_duration * 60
        return 25 * 60

    def get_remaining_seconds(self) -> int:
        """Return remaining seconds in current phase."""
        if self.state == PomodoroTimerState.IDLE:
            return self.get_phase_duration_seconds()
        if self.state in (PomodoroTimerState.PAUSED, PomodoroTimerState.COMPLETED):
            if self.remaining_seconds_when_paused is not None:
                return max(0, self.remaining_seconds_when_paused)
            return self.get_phase_duration_seconds()

        # RUNNING
        now = self.clock.monotonic()
        remaining = math.ceil(self.deadline_monotonic - now)
        return max(0, int(remaining))

    def start(self, preset: Optional[PomodoroPreset] = None) -> None:
        """Start a new Pomodoro cycle with given preset (or current preset)."""
        if preset is not None:
            self.preset = preset

        if not self.preset:
            raise ValueError("No preset configured for timer")

        self.phase = PomodoroSessionPhase.WORK
        self.current_session_index = 1
        self.completed_work_sessions = 0
        self.remaining_seconds_when_paused = None

        duration = self.get_phase_duration_seconds(PomodoroSessionPhase.WORK)
        now_mono = self.clock.monotonic()
        self.deadline_monotonic = now_mono + duration
        self.deadline_wall_clock = self.clock.now() + timedelta(seconds=duration)
        self.state = PomodoroTimerState.RUNNING

        self.persist_state()

    def pause(self) -> None:
        """Pause active countdown."""
        if self.state == PomodoroTimerState.RUNNING:
            self.remaining_seconds_when_paused = self.get_remaining_seconds()
            self.state = PomodoroTimerState.PAUSED
            self.deadline_wall_clock = None
            self.persist_state()

    def resume(self) -> None:
        """Resume paused countdown."""
        if self.state == PomodoroTimerState.PAUSED:
            remaining = (
                self.remaining_seconds_when_paused
                if self.remaining_seconds_when_paused is not None
                else self.get_phase_duration_seconds()
            )
            now_mono = self.clock.monotonic()
            self.deadline_monotonic = now_mono + remaining
            self.deadline_wall_clock = self.clock.now() + timedelta(seconds=remaining)
            self.remaining_seconds_when_paused = None
            self.state = PomodoroTimerState.RUNNING
            self.persist_state()

    def stop(self) -> None:
        """Stop current cycle and reset to IDLE."""
        self.state = PomodoroTimerState.IDLE
        self.remaining_seconds_when_paused = None
        self.deadline_monotonic = 0.0
        self.deadline_wall_clock = None
        if self.audio_service and hasattr(self.audio_service, "stop_audio"):
            self.audio_service.stop_audio()
        if self.pomodoro_service:
            self.pomodoro_service.clear_session_state()

    def reset_phase(self) -> None:
        """Reset countdown for the current phase back to its full duration."""
        duration = self.get_phase_duration_seconds()
        if self.state == PomodoroTimerState.RUNNING:
            now_mono = self.clock.monotonic()
            self.deadline_monotonic = now_mono + duration
            self.deadline_wall_clock = self.clock.now() + timedelta(seconds=duration)
            self.remaining_seconds_when_paused = None
        else:
            self.remaining_seconds_when_paused = duration
        self.persist_state()

    def skip(self) -> List[TimerEvent]:
        """
        Skip the current phase without incrementing completed work sessions.
        Advances directly to the next appropriate phase.
        """
        events: List[TimerEvent] = []
        if not self.preset:
            return events

        if self.phase == PomodoroSessionPhase.WORK:
            # Check whether next is long break or short break
            if self.current_session_index >= self.preset.sessions_before_long_break:
                next_phase = PomodoroSessionPhase.LONG_BREAK
            else:
                next_phase = PomodoroSessionPhase.SHORT_BREAK

            self.phase = next_phase
            duration = self.get_phase_duration_seconds(next_phase)
            events.append(TimerEvent("phase_change", {"phase": self.phase, "skipped": True}))

            if self.preset.auto_advance and self.state == PomodoroTimerState.RUNNING:
                self._start_phase_countdown(duration)
            else:
                self.state = PomodoroTimerState.PAUSED
                self.remaining_seconds_when_paused = duration

        elif self.phase == PomodoroSessionPhase.SHORT_BREAK:
            self.current_session_index += 1
            self.phase = PomodoroSessionPhase.WORK
            duration = self.get_phase_duration_seconds(PomodoroSessionPhase.WORK)
            events.append(TimerEvent("phase_change", {"phase": self.phase, "skipped": True}))

            if self.preset.auto_advance and self.state == PomodoroTimerState.RUNNING:
                self._start_phase_countdown(duration)
            else:
                self.state = PomodoroTimerState.PAUSED
                self.remaining_seconds_when_paused = duration

        elif self.phase == PomodoroSessionPhase.LONG_BREAK:
            self.state = PomodoroTimerState.COMPLETED
            self.remaining_seconds_when_paused = 0
            events.append(TimerEvent("cycle_completed", {}))

        self.persist_state()
        return events

    def _start_phase_countdown(self, duration: int) -> None:
        now_mono = self.clock.monotonic()
        self.deadline_monotonic = now_mono + duration
        self.deadline_wall_clock = self.clock.now() + timedelta(seconds=duration)
        self.remaining_seconds_when_paused = None
        self.state = PomodoroTimerState.RUNNING

    def tick(self, play_audio: bool = True) -> List[TimerEvent]:
        """
        Authoritative clock check. Called periodically by the event loop.
        Triggers phase completion exactly once when monotonic deadline is reached.
        """
        events: List[TimerEvent] = []
        if self.state != PomodoroTimerState.RUNNING:
            return events

        now_mono = self.clock.monotonic()
        if now_mono < self.deadline_monotonic:
            return events

        # Deadline reached! Execute phase transition
        if self.phase == PomodoroSessionPhase.WORK:
            self.completed_work_sessions += 1
            events.append(
                TimerEvent(
                    "work_completed",
                    {
                        "session_index": self.current_session_index,
                        "completed_total": self.completed_work_sessions,
                    },
                )
            )

            # Play audio if enabled
            if play_audio and self.preset and self.preset.sound_enabled and self.audio_service:
                self.audio_service.play_work_sound()
                events.append(TimerEvent("sound_played", {"type": "work"}))

            # Determine next phase
            if self.preset and self.current_session_index >= self.preset.sessions_before_long_break:
                next_phase = PomodoroSessionPhase.LONG_BREAK
            else:
                next_phase = PomodoroSessionPhase.SHORT_BREAK

            self.phase = next_phase
            duration = self.get_phase_duration_seconds(next_phase)
            events.append(TimerEvent("phase_change", {"phase": self.phase}))

            if self.preset and self.preset.auto_advance:
                self._start_phase_countdown(duration)
            else:
                self.state = PomodoroTimerState.PAUSED
                self.remaining_seconds_when_paused = duration

        elif self.phase in (PomodoroSessionPhase.SHORT_BREAK, PomodoroSessionPhase.LONG_BREAK):
            # Play break completion sound
            if play_audio and self.preset and self.preset.sound_enabled and self.audio_service:
                self.audio_service.play_break_sound()
                events.append(TimerEvent("sound_played", {"type": "break"}))

            if self.phase == PomodoroSessionPhase.LONG_BREAK:
                self.state = PomodoroTimerState.COMPLETED
                self.remaining_seconds_when_paused = 0
                events.append(TimerEvent("cycle_completed", {}))
            else:
                # SHORT_BREAK -> next WORK
                self.current_session_index += 1
                self.phase = PomodoroSessionPhase.WORK
                duration = self.get_phase_duration_seconds(PomodoroSessionPhase.WORK)
                events.append(TimerEvent("phase_change", {"phase": self.phase}))

                if self.preset and self.preset.auto_advance:
                    self._start_phase_countdown(duration)
                else:
                    self.state = PomodoroTimerState.PAUSED
                    self.remaining_seconds_when_paused = duration

        self.persist_state()
        return events

    def persist_state(self) -> None:
        """Persist active session state to SQLite database."""
        if not self.pomodoro_service or not self.preset or not self.preset.id:
            return

        if self.state in (PomodoroTimerState.IDLE, PomodoroTimerState.COMPLETED):
            self.pomodoro_service.clear_session_state()
            return

        deadline_iso = (
            self.deadline_wall_clock.isoformat()
            if self.deadline_wall_clock
            else None
        )
        record = PomodoroStateRecord(
            preset_id=self.preset.id,
            phase=self.phase.value if hasattr(self.phase, "value") else str(self.phase),
            state=self.state.value if hasattr(self.state, "value") else str(self.state),
            deadline_wall_clock=deadline_iso,
            remaining_seconds_when_paused=self.remaining_seconds_when_paused,
            completed_work_sessions=self.completed_work_sessions,
            current_session_index=self.current_session_index,
        )
        self.pomodoro_service.save_session_state(record)

    def recover(self) -> bool:
        """
        Attempt to recover interrupted Pomodoro session from database.
        Returns True if an active/paused session was successfully recovered.
        """
        if not self.pomodoro_service:
            return False

        record = self.pomodoro_service.load_session_state()
        if not record or not record.preset_id:
            return False

        preset = self.pomodoro_service.get_preset(record.preset_id)
        if not preset:
            return False

        self.preset = preset
        self.phase = PomodoroSessionPhase(record.phase)
        self.completed_work_sessions = record.completed_work_sessions
        self.current_session_index = record.current_session_index

        if record.state == "paused":
            self.state = PomodoroTimerState.PAUSED
            self.remaining_seconds_when_paused = record.remaining_seconds_when_paused
            return True

        if record.state == "running" and record.deadline_wall_clock:
            try:
                deadline_dt = datetime.fromisoformat(record.deadline_wall_clock)
                now_dt = self.clock.now()
                if now_dt < deadline_dt:
                    remaining = int((deadline_dt - now_dt).total_seconds())
                    self.state = PomodoroTimerState.RUNNING
                    now_mono = self.clock.monotonic()
                    self.deadline_monotonic = now_mono + remaining
                    self.deadline_wall_clock = deadline_dt
                    return True
                else:
                    # Deadline passed while offline: process transition without playing audio
                    self.state = PomodoroTimerState.RUNNING
                    self.deadline_monotonic = self.clock.monotonic() - 1
                    self.tick(play_audio=False)
                    return True
            except Exception:
                return False

        return False

    def get_display_info(self) -> Dict[str, Any]:
        """Return formatted UI metadata."""
        rem_sec = self.get_remaining_seconds()
        if rem_sec >= 3600:
            hours = rem_sec // 3600
            mins = (rem_sec % 3600) // 60
            secs = rem_sec % 60
            time_str = f"{hours:02d}:{mins:02d}:{secs:02d}"
        else:
            mins = rem_sec // 60
            secs = rem_sec % 60
            time_str = f"{mins:02d}:{secs:02d}"

        total_sec = self.get_phase_duration_seconds()
        progress = 1.0 - (rem_sec / total_sec) if total_sec > 0 else 0.0

        if self.phase == PomodoroSessionPhase.WORK:
            total_sessions = self.preset.sessions_before_long_break if self.preset else 4
            phase_label = f"WORK {self.current_session_index}/{total_sessions}"
        elif self.phase == PomodoroSessionPhase.SHORT_BREAK:
            phase_label = "SHORT BREAK"
        else:
            phase_label = "LONG BREAK"

        return {
            "time_str": time_str,
            "phase_label": phase_label,
            "phase": self.phase,
            "state": self.state,
            "remaining_seconds": rem_sec,
            "total_seconds": total_sec,
            "progress": max(0.0, min(1.0, progress)),
            "completed_work_sessions": self.completed_work_sessions,
            "current_session_index": self.current_session_index,
        }
