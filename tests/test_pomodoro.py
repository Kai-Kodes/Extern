"""Unit and integration tests for Pomodoro subsystem."""

import sqlite3
import pytest
from pathlib import Path
from todo.database import Database
from todo.models import (
    PomodoroPreset,
    PomodoroSessionPhase,
    PomodoroTimerState,
    PomodoroStateRecord,
)


def test_pomodoro_models():
    preset = PomodoroPreset(name="Deep Work", work_duration=50, short_break_duration=10)
    assert preset.name == "Deep Work"
    assert preset.work_duration == 50
    assert preset.short_break_duration == 10
    assert preset.long_break_duration == 15
    assert preset.sessions_before_long_break == 4
    assert not preset.auto_advance
    assert preset.sound_enabled

    state = PomodoroStateRecord(
        preset_id=1,
        phase=PomodoroSessionPhase.WORK.value,
        state=PomodoroTimerState.RUNNING.value,
        completed_work_sessions=2,
    )
    assert state.phase == "work"
    assert state.state == "running"
    assert state.completed_work_sessions == 2


def test_database_initialization_seeds_default_preset(tmp_path: Path):
    db_file = tmp_path / "test.db"
    db = Database(db_file)

    with db.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM pomodoro_presets")
        rows = cursor.fetchall()
        assert len(rows) >= 1
        default_preset = rows[0]
        assert default_preset["name"] == "Classic"
        assert default_preset["work_duration"] == 25
        assert default_preset["short_break_duration"] == 5
        assert default_preset["long_break_duration"] == 15
        assert default_preset["sessions_before_long_break"] == 4
        assert default_preset["auto_advance"] == 0
        assert default_preset["sound_enabled"] == 1

        # Check pomodoro_state table exists
        cursor.execute("SELECT count(*) as count FROM pomodoro_state")
        assert cursor.fetchone()["count"] == 0


def test_database_migration_from_older_schema(tmp_path: Path):
    db_file = tmp_path / "legacy.db"
    # Create legacy db with only sources and tasks
    conn = sqlite3.connect(str(db_file))
    conn.execute(
        """
        CREATE TABLE sources (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            path TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """
    )
    conn.execute(
        "INSERT INTO sources (name, path, created_at, updated_at) VALUES ('src1', '/path1', 'now', 'now')"
    )
    conn.commit()
    conn.close()

    # Now open with Database class, which triggers init_db() and migrations
    db = Database(db_file)

    with db.get_connection() as conn:
        cursor = conn.cursor()
        # Verify legacy data preserved
        cursor.execute("SELECT name FROM sources")
        assert cursor.fetchone()[0] == "src1"

        # Verify pomodoro tables exist and default seeded
        cursor.execute("SELECT name FROM pomodoro_presets")
        assert cursor.fetchone()[0] == "Classic"


def test_pomodoro_service_crud_and_validation(tmp_path: Path):
    from todo.services.pomodoro_service import PomodoroService

    db = Database(tmp_path / "test.db")
    service = PomodoroService(db)

    # List presets should contain default "Classic"
    presets = service.list_presets()
    assert len(presets) == 1
    assert presets[0].name == "Classic"
    classic_id = presets[0].id

    # Create new preset
    new_preset = PomodoroPreset(
        name="Study 50/10",
        work_duration=50,
        short_break_duration=10,
        long_break_duration=20,
        sessions_before_long_break=3,
        auto_advance=True,
        sound_enabled=True,
    )
    created = service.create_preset(new_preset)
    assert created.id is not None
    assert created.name == "Study 50/10"

    # Validation: empty name
    with pytest.raises(ValueError, match="Name cannot be empty"):
        service.create_preset(PomodoroPreset(name="", work_duration=25))

    # Validation: invalid work duration
    with pytest.raises(ValueError, match="Work duration"):
        service.create_preset(PomodoroPreset(name="Zero Work", work_duration=0))

    # Validation: invalid break duration
    with pytest.raises(ValueError, match="Short break"):
        service.create_preset(PomodoroPreset(name="Zero Break", work_duration=25, short_break_duration=0))

    # Validation: invalid sessions count
    with pytest.raises(ValueError, match="Sessions count"):
        service.create_preset(PomodoroPreset(name="Zero Sessions", sessions_before_long_break=0))

    # Update preset
    created.name = "Study 45/15"
    created.work_duration = 45
    service.update_preset(created)

    fetched = service.get_preset(created.id)
    assert fetched is not None
    assert fetched.name == "Study 45/15"
    assert fetched.work_duration == 45

    # Cannot delete active running preset
    with pytest.raises(ValueError, match="active"):
        service.delete_preset(created.id, active_preset_id=created.id)

    # Delete non-active preset
    assert service.delete_preset(created.id, active_preset_id=classic_id) is True
    assert service.get_preset(created.id) is None


def test_pomodoro_service_state_persistence(tmp_path: Path):
    from todo.services.pomodoro_service import PomodoroService

    db = Database(tmp_path / "test.db")
    service = PomodoroService(db)

    # Initially empty
    assert service.load_session_state() is None

    # Save state
    record = PomodoroStateRecord(
        preset_id=1,
        phase="work",
        state="running",
        deadline_wall_clock="2026-10-14T15:00:00",
        remaining_seconds_when_paused=None,
        completed_work_sessions=1,
        current_session_index=2,
    )
    service.save_session_state(record)

    loaded = service.load_session_state()
    assert loaded is not None
    assert loaded.preset_id == 1
    assert loaded.phase == "work"
    assert loaded.state == "running"
    assert loaded.deadline_wall_clock == "2026-10-14T15:00:00"
    assert loaded.completed_work_sessions == 1
    assert loaded.current_session_index == 2

    # Clear state
    service.clear_session_state()
    assert service.load_session_state() is None


def test_audio_service_player_discovery_and_nonblocking(monkeypatch, tmp_path):
    from todo.services.audio_service import AudioService
    import subprocess

    dummy_work = tmp_path / "work.oga"
    dummy_work.write_text("audio")
    dummy_break = tmp_path / "break.oga"
    dummy_break.write_text("audio")

    # Mock subprocess.Popen
    spawned_cmds = []

    class DummyPopen:
        def __init__(self, cmd, *args, **kwargs):
            spawned_cmds.append(cmd)

    monkeypatch.setattr(subprocess, "Popen", DummyPopen)

    service = AudioService(
        player_cmd="paplay",
        work_sound=str(dummy_work),
        break_sound=str(dummy_break),
        enabled=True,
    )
    assert service.is_available()

    # Play work sound
    ok, msg = service.play_work_sound()
    assert ok is True
    assert len(spawned_cmds) == 1
    assert spawned_cmds[0] == [service.get_player(), str(dummy_work)]

    # Play break sound
    ok, msg = service.play_break_sound()
    assert ok is True
    assert len(spawned_cmds) == 2
    assert spawned_cmds[1] == [service.get_player(), str(dummy_break)]

    # Test sound disabled
    service.enabled = False
    ok, msg = service.play_work_sound()
    assert ok is False
    assert "disabled" in msg.lower()
    assert len(spawned_cmds) == 2  # No new subprocess


def test_audio_service_handles_missing_player(monkeypatch, tmp_path):
    from todo.services.audio_service import AudioService
    import shutil

    monkeypatch.setattr(shutil, "which", lambda cmd: None)

    service = AudioService(player_cmd=None)
    assert not service.is_available()

    ok, msg = service.play_work_sound()
    assert ok is False
    assert "player" in msg.lower()


def test_focus_detector_focused_and_unfocused(monkeypatch):
    from todo.ui.focus_detector import FocusDetector

    detector = FocusDetector(poll_interval=0.1, terminal_window_id=1001)

    # Mock active window query returning matching ID
    monkeypatch.setattr(detector, "get_active_window_id", lambda: 1001)
    assert detector.is_terminal_focused(force_refresh=True) is True

    # Mock active window query returning different ID (unfocused)
    monkeypatch.setattr(detector, "get_active_window_id", lambda: 2002)
    assert detector.is_terminal_focused(force_refresh=True) is False

    # Mock caching: within poll_interval, does not call get_active_window_id again
    monkeypatch.setattr(detector, "get_active_window_id", lambda: 9999)
    assert detector.is_terminal_focused(force_refresh=False) is False  # Still cached False


def test_focus_detector_fallback_when_unavailable(monkeypatch):
    from todo.ui.focus_detector import FocusDetector

    detector = FocusDetector(poll_interval=0.1, terminal_window_id=None)
    detector._xdotool_available = False
    detector._xprop_available = False

    assert detector.is_available() is False
    # Graceful fallback always returns True (focused) so app is not stuck in clock mode
    assert detector.is_terminal_focused() is True


class FakeClock:
    def __init__(self, start_monotonic: float = 1000.0, start_time: str = "2026-10-14T10:00:00"):
        from datetime import datetime
        self._monotonic = start_monotonic
        self._dt = datetime.fromisoformat(start_time)

    def monotonic(self) -> float:
        return self._monotonic

    def now(self):
        return self._dt

    def advance(self, seconds: float):
        from datetime import timedelta
        self._monotonic += seconds
        self._dt += timedelta(seconds=seconds)


def test_pomodoro_timer_lifecycle_and_monotonic_accuracy():
    from todo.services.pomodoro_timer import PomodoroTimer
    from todo.models import PomodoroPreset, PomodoroSessionPhase, PomodoroTimerState

    clock = FakeClock()
    preset = PomodoroPreset(
        name="Test Preset",
        work_duration=25,
        short_break_duration=5,
        long_break_duration=15,
        sessions_before_long_break=2,
        auto_advance=True,
    )
    timer = PomodoroTimer(preset=preset, clock=clock)
    assert timer.state == PomodoroTimerState.IDLE

    # Start timer
    timer.start()
    assert timer.state == PomodoroTimerState.RUNNING
    assert timer.phase == PomodoroSessionPhase.WORK
    assert timer.current_session_index == 1
    assert timer.get_remaining_seconds() == 25 * 60

    # Advance 10 minutes
    clock.advance(10 * 60)
    timer.tick()
    assert timer.get_remaining_seconds() == 15 * 60
    assert timer.state == PomodoroTimerState.RUNNING

    # Pause
    timer.pause()
    assert timer.state == PomodoroTimerState.PAUSED
    assert timer.get_remaining_seconds() == 15 * 60

    # Advance clock while paused (time shouldn't elapse)
    clock.advance(5 * 60)
    assert timer.get_remaining_seconds() == 15 * 60

    # Resume
    timer.resume()
    assert timer.state == PomodoroTimerState.RUNNING
    assert timer.get_remaining_seconds() == 15 * 60

    # Advance past work duration (15 min) -> transition to Short Break
    clock.advance(15 * 60)
    events = timer.tick()
    assert timer.phase == PomodoroSessionPhase.SHORT_BREAK
    assert timer.completed_work_sessions == 1
    assert timer.get_remaining_seconds() == 5 * 60
    assert any(e.name == "phase_change" for e in events)

    # Advance past short break duration -> Work session 2
    clock.advance(5 * 60)
    timer.tick()
    assert timer.phase == PomodoroSessionPhase.WORK
    assert timer.current_session_index == 2
    assert timer.get_remaining_seconds() == 25 * 60

    # Advance past work session 2 -> Long Break (since sessions_before_long_break = 2)
    clock.advance(25 * 60)
    timer.tick()
    assert timer.phase == PomodoroSessionPhase.LONG_BREAK
    assert timer.completed_work_sessions == 2
    assert timer.get_remaining_seconds() == 15 * 60

    # Advance past long break -> Cycle Completed
    clock.advance(15 * 60)
    timer.tick()
    assert timer.state == PomodoroTimerState.COMPLETED


def test_pomodoro_timer_skip_does_not_increment_completed_sessions():
    from todo.services.pomodoro_timer import PomodoroTimer
    from todo.models import PomodoroPreset, PomodoroSessionPhase

    clock = FakeClock()
    preset = PomodoroPreset(
        name="Test Skip",
        work_duration=25,
        short_break_duration=5,
        long_break_duration=15,
        sessions_before_long_break=4,
        auto_advance=True,
    )
    timer = PomodoroTimer(preset=preset, clock=clock)
    timer.start()
    assert timer.phase == PomodoroSessionPhase.WORK

    # Skip work session
    timer.skip()
    # Must advance to break
    assert timer.phase == PomodoroSessionPhase.SHORT_BREAK
    # Crucial: completed_work_sessions must NOT increment
    assert timer.completed_work_sessions == 0


def test_pomodoro_timer_manual_advance():
    from todo.services.pomodoro_timer import PomodoroTimer
    from todo.models import PomodoroPreset, PomodoroSessionPhase, PomodoroTimerState

    clock = FakeClock()
    preset = PomodoroPreset(
        name="Manual Preset",
        work_duration=25,
        short_break_duration=5,
        auto_advance=False,
    )
    timer = PomodoroTimer(preset=preset, clock=clock)
    timer.start()

    clock.advance(25 * 60)
    timer.tick()

    # With auto_advance=False, transitions to next phase but waits in PAUSED state
    assert timer.phase == PomodoroSessionPhase.SHORT_BREAK
    assert timer.state == PomodoroTimerState.PAUSED
    assert timer.completed_work_sessions == 1
    assert timer.get_remaining_seconds() == 5 * 60


def test_pomodoro_timer_restart_recovery(tmp_path: Path):
    from todo.services.pomodoro_service import PomodoroService
    from todo.services.pomodoro_timer import PomodoroTimer
    from todo.models import PomodoroPreset, PomodoroSessionPhase, PomodoroTimerState

    db = Database(tmp_path / "test.db")
    service = PomodoroService(db)
    presets = service.list_presets()
    classic = presets[0]

    clock = FakeClock()
    timer = PomodoroTimer(preset=classic, pomodoro_service=service, clock=clock)
    timer.start()

    # Advance 10 minutes into 25 min work session
    clock.advance(10 * 60)
    timer.tick()

    # Now simulate application restart with a new timer instance loading from DB
    recovered_timer = PomodoroTimer(pomodoro_service=service, clock=clock)
    recovered = recovered_timer.recover()
    assert recovered is True
    assert recovered_timer.state == PomodoroTimerState.RUNNING
    assert recovered_timer.phase == PomodoroSessionPhase.WORK
    assert abs(recovered_timer.get_remaining_seconds() - 15 * 60) <= 2


def test_glyph_invariants_and_all_individual_digits():
    """Verify that every digit 0-9, colon, and space forms a coherent glyph with valid dimensions and cell lengths."""
    from todo.ui.pomodoro_view import DIGITS_7SEG_5ROW
    from rich.cells import cell_len

    for ch in "0123456789: ":
        assert ch in DIGITS_7SEG_5ROW, f"Glyph '{ch}' missing from DIGITS_7SEG_5ROW"
        rows = DIGITS_7SEG_5ROW[ch]
        assert len(rows) == 5, f"Glyph '{ch}' must have exactly 5 rows"
        glyph_width = len(rows[0])
        for r_idx, row in enumerate(rows):
            assert len(row) == glyph_width, f"Glyph '{ch}' row {r_idx} length {len(row)} != {glyph_width}"
            assert cell_len(row) == glyph_width, f"Glyph '{ch}' row {r_idx} cell_len != {glyph_width}"
            for c in row:
                assert c in ("█", " "), f"Forbidden character {c!r} in glyph '{ch}' row {r_idx}"


def test_calculate_clock_scale_dimensions():
    """Verify calculate_clock_scale selects appropriate scales for different terminal profiles."""
    from todo.ui.pomodoro_view import calculate_clock_scale

    # 1. Typical 80x24 terminal
    sx, sy, ok = calculate_clock_scale("25:00", 80, 24, has_progress=True)
    assert ok is True
    assert (sx, sy) == (2, 3)
    assert 26 * sx == 52
    assert 5 * sy == 15

    # HH:MM:SS on 80x24 (base width 41)
    sx_hh, sy_hh, ok_hh = calculate_clock_scale("01:25:00", 80, 24, has_progress=True)
    assert ok_hh is True
    assert (sx_hh, sy_hh) == (1, 2)
    assert 41 * sx_hh == 41

    # 2. Maximized 160x50 terminal
    sx_max, sy_max, ok_max = calculate_clock_scale("25:00", 160, 50, has_progress=True)
    assert ok_max is True
    assert (sx_max, sy_max) == (6, 8)
    assert 26 * sx_max == 156
    assert 5 * sy_max == 40

    # 3. Wide but short terminal (120x15)
    sx_ws, sy_ws, ok_ws = calculate_clock_scale("25:00", 120, 15, has_progress=True)
    assert ok_ws is True
    assert (sx_ws, sy_ws) == (4, 2)

    # 4. Narrow but tall terminal (40x40)
    sx_nt, sy_nt, ok_nt = calculate_clock_scale("25:00", 40, 40, has_progress=True)
    assert ok_nt is True
    assert (sx_nt, sy_nt) == (1, 2)

    # 5. Small terminal (60x12)
    sx_sm, sy_sm, ok_sm = calculate_clock_scale("25:00", 60, 12, has_progress=False)
    assert ok_sm is True
    assert (sx_sm, sy_sm) == (2, 1)

    # 6. Fallback cases where terminal is too small for seven-segment
    assert calculate_clock_scale("25:00", 25, 8)[2] is False
    assert calculate_clock_scale("25:00", 20, 5)[2] is False
    assert calculate_clock_scale("25:00", 30, 6)[2] is False
    assert calculate_clock_scale("01:25:00", 40, 40)[2] is False  # 41 > 40-4


def test_build_7segment_time_scaling_invariants():
    """Verify scaling invariants across scales 1 to 6 for both MM:SS and HH:MM:SS."""
    from todo.ui.pomodoro_view import build_7segment_time
    from rich.cells import cell_len

    for val in ["25:00", "01:25:00"]:
        base_w = 41 if len(val) == 8 else 26
        for sx in range(1, 6):
            for sy in range(1, 6):
                lines = build_7segment_time(val, scale_x=sx, scale_y=sy)
                assert len(lines) == 5 * sy
                expected_w = base_w * sx
                for r, line in enumerate(lines):
                    assert len(line) == expected_w, f"Width mismatch at scale ({sx},{sy}) row {r}"
                    assert cell_len(line) == expected_w
                    for ch in line:
                        assert ch in ("█", " ")


def test_clock_fixed_values_and_pipeline_rendering():
    """Verify fixed test values (00:00, 08:08, 22:46, 25:00, 59:59) survive Rich rendering pipeline intact."""
    from todo.ui.pomodoro_view import build_7segment_time, create_digital_clock_layout
    from rich.console import Console

    test_values = ["00:00", "08:08", "22:46", "25:00", "59:59"]
    for val in test_values:
        # Base scale verification
        lines = build_7segment_time(val, scale_x=1, scale_y=1)
        assert len(lines) == 5
        assert all(len(l) == 26 for l in lines)

        # Scaled layout rendering on standard 80x24 (scale 2, 3 -> 52x15)
        c = Console(width=80, height=24, record=True)
        layout = create_digital_clock_layout(
            time_str=val,
            phase_label="WORK",
            state_str="RUNNING",
            term_width=80,
            term_height=24,
            progress_pct=45,
        )
        c.print(layout, end="")
        output = c.export_text()
        clock_rows = [l for l in output.splitlines() if "█" in l]
        assert len(clock_rows) == 15, f"Expected 15 clock rows for {val} at scale (2,3), got {len(clock_rows)}"

        # Verify colon dots at rows 3..5 and rows 9..11 at columns 38..41 (centered at col 14)
        for r in range(3, 6):
            assert clock_rows[r][38:42] == "████", f"{val}: missing colon block at row {r}"
        for r in range(9, 12):
            assert clock_rows[r][38:42] == "████", f"{val}: missing colon block at row {r}"
        for r in [0, 1, 2, 6, 7, 8, 12, 13, 14]:
            assert clock_rows[r][38:42] == "    ", f"{val}: stray marks at colon row {r}"

    # Specific check for 22:46 alignment at 80x24 (scale 2, 3)
    c = Console(width=80, height=24, record=True)
    layout = create_digital_clock_layout(
        time_str="22:46",
        phase_label="WORK",
        state_str="RUNNING",
        term_width=80,
        term_height=24,
        progress_pct=45,
    )
    c.print(layout, end="")
    output = c.export_text()
    clock_rows = [l for l in output.splitlines() if "█" in l]
    assert clock_rows[0].index("██████████") == 14
    assert clock_rows[3].index("██") == 22
    assert clock_rows[6].index("██████████") == 14
    assert clock_rows[9].index("██") == 14
    assert clock_rows[12].index("██████████") == 14


def test_maximized_terminal_160x50_rendering():
    """Verify digital clock occupying nearly the entire 160x50 window without clipping."""
    from todo.ui.pomodoro_view import create_digital_clock_layout, NonScrollingLayout
    from rich.console import Console

    # MM:SS on 160x50 -> scale (6, 8), clock 156x40
    c = Console(width=160, height=50, record=True)
    layout = create_digital_clock_layout("25:00", "WORK 1/4", "RUNNING", 160, 50, progress_pct=50)
    c.print(NonScrollingLayout(layout), end="")
    output = c.export_text()
    lines = output.splitlines()
    assert len(lines) == 50
    clock_rows = [l for l in lines if "█" in l]
    assert len(clock_rows) == 40
    for l in lines:
        assert len(l) <= 160

    # HH:MM:SS on 160x50 -> scale (3, 6), clock 123x30
    c_hh = Console(width=160, height=50, record=True)
    layout_hh = create_digital_clock_layout("01:25:00", "LONG BREAK", "PAUSED", 160, 50, progress_pct=25)
    c_hh.print(NonScrollingLayout(layout_hh), end="")
    output_hh = c_hh.export_text()
    lines_hh = output_hh.splitlines()
    assert len(lines_hh) == 50
    clock_rows_hh = [l for l in lines_hh if "█" in l]
    assert len(clock_rows_hh) == 30
    for l in lines_hh:
        assert len(l) <= 160


def test_various_terminal_dimensions_property():
    """Property test across a matrix of terminal dimensions ensuring no line wrapping or crashing."""
    from todo.ui.pomodoro_view import create_digital_clock_layout, NonScrollingLayout
    from rich.console import Console

    widths = [25, 30, 40, 60, 80, 100, 120, 160]
    heights = [5, 8, 10, 14, 20, 24, 30, 40, 50]
    for w in widths:
        for h in heights:
            for val in ["25:00", "01:25:00"]:
                c = Console(width=w, height=h, record=True)
                layout = create_digital_clock_layout(val, "WORK", "RUNNING", w, h, progress_pct=35)
                c.print(NonScrollingLayout(layout), end="")
                out = c.export_text()
                lines = out.split("\n")
                assert len(lines) == h, f"Line count mismatch on {w}x{h} ({val}): {len(lines)} != {h}"
                for r_idx, line in enumerate(lines):
                    assert len(line) <= w, f"Width exceeded on {w}x{h} ({val}) row {r_idx}: {len(line)} > {w}"


def test_non_scrolling_layout_prevents_viewport_overflow():
    """Verify NonScrollingLayout suppresses trailing newline to avoid scrolling 24-row terminal."""
    from todo.ui.pomodoro_view import create_digital_clock_layout, NonScrollingLayout
    from rich.console import Console

    c = Console(width=80, height=24, record=True)
    layout = create_digital_clock_layout(
        time_str="25:00",
        phase_label="WORK",
        state_str="RUNNING",
        term_width=80,
        term_height=24,
    )
    c.print(NonScrollingLayout(layout), end="")
    output = c.export_text()
    lines = output.split("\n")
    # Exactly 24 lines, with NO trailing empty newline (which would mean 25)
    assert len(lines) == 24
    assert not output.endswith("\n")


def test_clock_layout_centering_multiple_terminal_sizes():
    from todo.ui.pomodoro_view import create_digital_clock_layout
    from rich.console import Console
    import io

    sizes = [(80, 24), (120, 40), (100, 30)]
    for w, h in sizes:
        buf = io.StringIO()
        c = Console(file=buf, width=w, height=h, color_system=None)
        layout = create_digital_clock_layout(
            time_str="25:00",
            phase_label="WORK 1/4",
            state_str="RUNNING",
            term_width=w,
            term_height=h,
            progress_pct=50,
        )
        c.print(layout, end="")
        output = buf.getvalue()
        rendered_lines = output.split("\n")
        # Layout fits the requested console height
        assert len(rendered_lines) == h + 1  # trailing newline split
        # None of the lines exceed terminal width
        for line in rendered_lines[:-1]:
            assert len(line) <= w, f"Line exceeded width {w}: {len(line)}"


def test_narrow_and_short_terminals_graceful_fallback():
    from todo.ui.pomodoro_view import create_digital_clock_layout
    from rich.console import Console
    import io

    # Narrow and short configurations
    small_sizes = [(25, 8), (20, 5), (30, 6)]
    for w, h in small_sizes:
        buf = io.StringIO()
        c = Console(file=buf, width=w, height=h, color_system=None)
        layout = create_digital_clock_layout(
            time_str="25:00",
            phase_label="WORK",
            state_str="RUNNING",
            term_width=w,
            term_height=h,
        )
        c.print(layout, end="")
        output = buf.getvalue()
        # Verify 25:00 is present
        assert "25:00" in output
        # Verify no lines exceed width
        for line in output.split("\n"):
            assert len(line) <= w, f"Line exceeded width {w} in small size {w}x{h}: {line}"


def test_terminal_resize_while_clock_displayed():
    from todo.ui.pomodoro_view import create_digital_clock_layout
    from rich.console import Console
    import io

    # Simulate dynamic resize sequence: Large -> Narrow -> Normal
    sequence = [(100, 30), (30, 10), (80, 24)]
    for w, h in sequence:
        buf = io.StringIO()
        c = Console(file=buf, width=w, height=h, color_system=None)
        layout = create_digital_clock_layout(
            time_str="15:30",
            phase_label="SHORT BREAK",
            state_str="PAUSED",
            term_width=w,
            term_height=h,
        )
        c.print(layout, end="")
        output = buf.getvalue()
        assert "SHORT BREAK" in output
        assert len(output.split("\n")) == h + 1


def test_key_reader_timeout(monkeypatch):
    from todo.ui.key_reader import KeyReader
    import select

    reader = KeyReader()
    # Mock sys.stdin.isatty() True and fileno
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdin.fileno", lambda: 0)
    monkeypatch.setattr(reader, "enter", lambda: None)
    monkeypatch.setattr(reader, "exit", lambda: None)
    # Mock select.select returning empty list (timeout)
    monkeypatch.setattr(select, "select", lambda r, w, x, t: ([], [], []))

    ch = reader.read_key(timeout=0.01)
    assert ch is None


def test_countdown_continuity_through_focus_changes(monkeypatch):
    from todo.services.pomodoro_timer import PomodoroTimer
    from todo.ui.focus_detector import FocusDetector
    from todo.models import PomodoroPreset, PomodoroSessionPhase

    clock = FakeClock()
    preset = PomodoroPreset(name="Focus Check", work_duration=25)
    timer = PomodoroTimer(preset=preset, clock=clock)
    timer.start()

    detector = FocusDetector(poll_interval=0.01, terminal_window_id=100)
    current_win = 100
    monkeypatch.setattr(detector, "get_active_window_id", lambda: current_win)

    # 1. Initially focused
    assert detector.is_terminal_focused(force_refresh=True) is True
    clock.advance(60)
    assert timer.get_remaining_seconds() == 24 * 60

    # 2. Window loses focus
    current_win = 200
    assert detector.is_terminal_focused(force_refresh=True) is False
    clock.advance(120)
    # Countdown continued uninterrupted!
    assert timer.get_remaining_seconds() == 22 * 60

    # 3. Window regains focus
    current_win = 100
    assert detector.is_terminal_focused(force_refresh=True) is True
    clock.advance(60)
    assert timer.get_remaining_seconds() == 21 * 60

    # 4. Window loses focus again
    current_win = 300
    assert detector.is_terminal_focused(force_refresh=True) is False
    clock.advance(300)
    assert timer.get_remaining_seconds() == 16 * 60


def test_exactly_once_transition_and_sound():
    from todo.services.pomodoro_timer import PomodoroTimer
    from todo.services.audio_service import AudioService
    from todo.models import PomodoroPreset, PomodoroSessionPhase

    played = []
    class MockAudio:
        def play_work_sound(self):
            played.append("work")
            return True, "ok"
        def play_break_sound(self):
            played.append("break")
            return True, "ok"

    clock = FakeClock()
    preset = PomodoroPreset(name="Single Transition", work_duration=25, auto_advance=False)
    timer = PomodoroTimer(preset=preset, audio_service=MockAudio(), clock=clock)
    timer.start()

    # Advance beyond deadline
    clock.advance(30 * 60)

    # First tick -> triggers transition and sound
    events1 = timer.tick()
    assert len(events1) > 0
    assert played == ["work"]
    assert timer.phase == PomodoroSessionPhase.SHORT_BREAK

    # Subsequent ticks -> must NOT trigger transition or sound again!
    events2 = timer.tick()
    assert len(events2) == 0
    assert played == ["work"]

    events3 = timer.tick()
    assert len(events3) == 0
    assert played == ["work"]


def test_restart_recovery_with_passed_deadline(tmp_path: Path):
    from todo.services.pomodoro_service import PomodoroService
    from todo.services.pomodoro_timer import PomodoroTimer
    from todo.models import PomodoroPreset, PomodoroSessionPhase, PomodoroTimerState

    db = Database(tmp_path / "test.db")
    service = PomodoroService(db)
    preset = service.list_presets()[0]

    clock = FakeClock(start_monotonic=1000.0, start_time="2026-10-14T10:00:00")
    timer = PomodoroTimer(preset=preset, pomodoro_service=service, clock=clock)
    timer.start()

    # Application closes. Time advances 40 minutes (past 25 min work session)
    clock.advance(40 * 60)

    # Restart application
    recovered_timer = PomodoroTimer(pomodoro_service=service, clock=clock)
    recovered = recovered_timer.recover()
    assert recovered is True
    # Timer should have processed the completed work session
    assert recovered_timer.phase == PomodoroSessionPhase.SHORT_BREAK
    assert recovered_timer.completed_work_sessions == 1


def test_terminal_cleanup_on_exception():
    from todo.ui.console import tui_mode, _IN_ALT_SCREEN, exit_alt_screen
    import sys

    # Simulate exception inside tui_mode()
    try:
        with tui_mode():
            raise RuntimeError("Testing simulated crash")
    except RuntimeError:
        pass

    # Verify alt screen is cleanly exited
    from todo.ui.console import _IN_ALT_SCREEN
    assert not _IN_ALT_SCREEN


def test_nintendo_switch_sound_asset_and_defaults():
    import hashlib
    from todo.services.audio_service import AudioService, get_default_sound_path

    asset_path = get_default_sound_path()
    assert asset_path.exists(), f"Asset {asset_path} does not exist"
    assert asset_path.name == "nintendo-switch-snap.m4a"

    # Size and SHA-256 Checksum Verification
    content = asset_path.read_bytes()
    assert len(content) == 66447
    sha256 = hashlib.sha256(content).hexdigest()
    assert sha256 == "ca5dd4266825f236040ea04829ea4887c6a7c898419192edcea89829d4771eae"

    # Defaults in AudioService
    service = AudioService()
    assert service.get_work_sound() == str(asset_path.resolve())
    assert service.get_break_sound() == str(asset_path.resolve())
    assert service.get_default_sound() == str(asset_path.resolve())


def test_m4a_player_selection_mpv_flags_and_candidates(monkeypatch, tmp_path):
    import shutil
    import subprocess
    from todo.services.audio_service import AudioService

    sound_m4a = tmp_path / "nintendo-switch-snap.m4a"
    sound_m4a.write_bytes(b"dummy m4a content")

    # When mpv, paplay, and aplay are present: mpv must be selected for m4a
    installed = {"mpv": "/usr/bin/mpv", "paplay": "/usr/bin/paplay", "aplay": "/usr/bin/aplay"}
    monkeypatch.setattr(shutil, "which", lambda cmd: installed.get(cmd))

    spawned = []
    class MockPopen:
        def __init__(self, cmd, *args, **kwargs):
            spawned.append(cmd)
            self._running = False
        def poll(self):
            return 0 if not self._running else None

    monkeypatch.setattr(subprocess, "Popen", MockPopen)

    service = AudioService(work_sound=str(sound_m4a))
    player = service.get_player(str(sound_m4a))
    assert player == "/usr/bin/mpv"

    ok, msg = service.play_work_sound()
    assert ok is True
    assert len(spawned) == 1
    # Check flags: --no-config, --no-video, --really-quiet
    assert spawned[0] == ["/usr/bin/mpv", "--no-config", "--no-video", "--really-quiet", str(sound_m4a)]


def test_m4a_fallback_to_ffplay_and_cvlc(monkeypatch, tmp_path):
    import shutil
    import subprocess
    from todo.services.audio_service import AudioService

    sound_m4a = tmp_path / "test.m4a"
    sound_m4a.write_bytes(b"dummy")

    # If mpv is missing but ffplay is present
    installed_ffplay = {"ffplay": "/usr/bin/ffplay", "pw-play": "/usr/bin/pw-play"}
    monkeypatch.setattr(shutil, "which", lambda cmd: installed_ffplay.get(cmd))

    spawned = []
    class MockPopen:
        def __init__(self, cmd, *args, **kwargs):
            spawned.append(cmd)
        def poll(self):
            return 0

    monkeypatch.setattr(subprocess, "Popen", MockPopen)

    service = AudioService()
    player = service.get_player(str(sound_m4a))
    assert player == "/usr/bin/ffplay"

    ok, msg = service.play_sound(str(sound_m4a))
    assert ok is True
    assert spawned[0] == ["/usr/bin/ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", str(sound_m4a)]

    # If only cvlc is present
    spawned.clear()
    installed_cvlc = {"cvlc": "/usr/bin/cvlc"}
    monkeypatch.setattr(shutil, "which", lambda cmd: installed_cvlc.get(cmd))

    player = service.get_player(str(sound_m4a))
    assert player == "/usr/bin/cvlc"
    ok, msg = service.play_sound(str(sound_m4a))
    assert ok is True
    assert spawned[0] == ["/usr/bin/cvlc", "--play-and-exit", "--no-video", "--quiet", str(sound_m4a)]


def test_m4a_missing_player_and_file_not_found(monkeypatch, tmp_path):
    import shutil
    from todo.services.audio_service import AudioService

    sound_m4a = tmp_path / "sound.m4a"
    sound_m4a.write_bytes(b"audio")

    # Only paplay is present (which cannot play m4a)
    monkeypatch.setattr(shutil, "which", lambda cmd: "/usr/bin/paplay" if cmd == "paplay" else None)

    service = AudioService()
    ok, msg = service.play_sound(str(sound_m4a))
    assert ok is False
    assert "No M4A-compatible audio player found" in msg

    # File does not exist
    missing_file = tmp_path / "nonexistent.m4a"
    ok, msg = service.play_sound(str(missing_file))
    assert ok is False
    assert "not found" in msg.lower()


def test_audio_overlap_prevention_and_stop(monkeypatch, tmp_path):
    import shutil
    import subprocess
    from todo.services.audio_service import AudioService

    sound_file = tmp_path / "sound.m4a"
    sound_file.write_bytes(b"audio")
    monkeypatch.setattr(shutil, "which", lambda cmd: "/usr/bin/mpv" if cmd == "mpv" else None)

    class MockRunningPopen:
        def __init__(self, cmd, *args, **kwargs):
            self.running = True
            self.terminated = False
        def poll(self):
            return None if self.running else 0
        def terminate(self):
            self.running = False
            self.terminated = True

    active_mock = None
    def mock_popen(cmd, *args, **kwargs):
        nonlocal active_mock
        active_mock = MockRunningPopen(cmd)
        return active_mock

    monkeypatch.setattr(subprocess, "Popen", mock_popen)

    service = AudioService()
    # 1st call: succeeds and starts playback
    ok1, msg1 = service.play_sound(str(sound_file))
    assert ok1 is True
    assert service.is_playing() is True

    # 2nd call while 1st is still playing: rejected to prevent overlap!
    ok2, msg2 = service.play_sound(str(sound_file))
    assert ok2 is False
    assert "already in progress" in msg2

    # Test sound also rejected while playing
    ok_test, msg_test = service.test_sound("work")
    assert ok_test is False
    assert "already in progress" in msg_test

    # Stop audio terminates running process
    service.stop_audio()
    assert service.is_playing() is False
    assert active_mock.terminated is True

    # After stopped, new playback succeeds
    ok3, msg3 = service.play_sound(str(sound_file))
    assert ok3 is True


def test_work_and_break_completion_sound_lifecycle():
    from todo.services.pomodoro_timer import PomodoroTimer
    from todo.models import PomodoroPreset, PomodoroSessionPhase

    played = []
    class TrackingAudio:
        def play_work_sound(self):
            played.append("work")
            return True, "ok"
        def play_break_sound(self):
            played.append("break")
            return True, "ok"
        def stop_audio(self):
            played.append("stop")

    clock = FakeClock()
    preset = PomodoroPreset(name="Cycle Test", work_duration=25, short_break_duration=5, auto_advance=True)
    timer = PomodoroTimer(preset=preset, audio_service=TrackingAudio(), clock=clock)
    timer.start()

    # Advance past work duration
    clock.advance(25 * 60)
    timer.tick()
    assert played == ["work"]
    assert timer.phase == PomodoroSessionPhase.SHORT_BREAK

    # Subsequent ticks during break do not trigger work sound again
    clock.advance(60)
    timer.tick()
    assert played == ["work"]

    # Advance past break duration
    clock.advance(4 * 60)
    timer.tick()
    assert played == ["work", "break"]
    assert timer.phase == PomodoroSessionPhase.WORK

    # Stop timer stops audio
    timer.stop()
    assert played == ["work", "break", "stop"]


def test_skips_and_focus_and_pauses_remain_silent():
    from todo.services.pomodoro_timer import PomodoroTimer
    from todo.models import PomodoroPreset, PomodoroSessionPhase

    audio_calls = []
    class SilentAudio:
        def play_work_sound(self):
            audio_calls.append("work")
            return True, "ok"
        def play_break_sound(self):
            audio_calls.append("break")
            return True, "ok"

    clock = FakeClock()
    preset = PomodoroPreset(name="Silent Test", work_duration=25, short_break_duration=5)
    timer = PomodoroTimer(preset=preset, audio_service=SilentAudio(), clock=clock)
    timer.start()

    # Pausing and resuming
    timer.pause()
    timer.resume()
    assert len(audio_calls) == 0

    # Resetting phase
    timer.reset_phase()
    assert len(audio_calls) == 0

    # Skipping phase
    timer.skip()
    assert timer.phase == PomodoroSessionPhase.SHORT_BREAK
    assert len(audio_calls) == 0

    timer.skip()
    assert timer.phase == PomodoroSessionPhase.WORK
    assert len(audio_calls) == 0


def test_recovery_offline_does_not_play_sound(tmp_path):
    from todo.services.pomodoro_service import PomodoroService
    from todo.services.pomodoro_timer import PomodoroTimer

    db = Database(tmp_path / "test.db")
    service = PomodoroService(db)
    preset = service.list_presets()[0]

    audio_calls = []
    class TrackingAudio:
        def play_work_sound(self):
            audio_calls.append("work")
            return True, "ok"
        def play_break_sound(self):
            audio_calls.append("break")
            return True, "ok"

    clock = FakeClock(start_monotonic=1000.0, start_time="2026-10-14T10:00:00")
    timer = PomodoroTimer(preset=preset, pomodoro_service=service, audio_service=TrackingAudio(), clock=clock)
    timer.start()

    # Time advances 40 minutes (past 25 min work session) while offline
    clock.advance(40 * 60)

    # Recover in new session
    recovered = PomodoroTimer(pomodoro_service=service, audio_service=TrackingAudio(), clock=clock)
    ok = recovered.recover()
    assert ok is True
    # Crucial: recovery must NOT trigger the sound!
    assert len(audio_calls) == 0


def test_audio_service_works_when_launched_outside_repo_dir(monkeypatch, tmp_path):
    from todo.services.audio_service import AudioService, get_default_sound_path
    import os
    from pathlib import Path

    # Change current working directory to a separate temporary directory
    outside_dir = tmp_path / "outside_workspace"
    outside_dir.mkdir()
    monkeypatch.chdir(outside_dir)

    # Asset must still resolve to valid project sound file
    resolved_path = get_default_sound_path()
    assert resolved_path.exists()
    assert resolved_path.name == "nintendo-switch-snap.m4a"

    service = AudioService()
    assert Path(service.get_work_sound()).exists()
    assert Path(service.get_break_sound()).exists()


def test_disabled_audio_preset_and_service():
    from todo.services.pomodoro_timer import PomodoroTimer
    from todo.services.audio_service import AudioService
    from todo.models import PomodoroPreset

    played = []
    class MockAudio:
        def play_work_sound(self):
            played.append("work")
            return True, "ok"
        def play_break_sound(self):
            played.append("break")
            return True, "ok"

    clock = FakeClock()
    # sound_enabled is False on preset
    preset = PomodoroPreset(name="Silent Preset", work_duration=25, sound_enabled=False)
    timer = PomodoroTimer(preset=preset, audio_service=MockAudio(), clock=clock)
    timer.start()

    clock.advance(25 * 60)
    timer.tick()
    assert len(played) == 0

    # AudioService itself disabled
    service = AudioService(enabled=False)
    ok, msg = service.play_work_sound()
    assert ok is False
    assert "disabled" in msg.lower()


def test_audio_test_shortcut_plays_once_and_prevents_overlap(monkeypatch, tmp_path):
    from todo.services.audio_service import AudioService
    import subprocess
    import shutil

    sound_file = tmp_path / "nintendo-switch-snap.m4a"
    sound_file.write_bytes(b"content")

    monkeypatch.setattr(shutil, "which", lambda cmd: "/usr/bin/mpv" if cmd == "mpv" else None)

    class MockRunningProcess:
        def __init__(self):
            self.running = True
        def poll(self):
            return None if self.running else 0

    running_proc = None
    spawn_count = 0
    def mock_popen(cmd, *args, **kwargs):
        nonlocal running_proc, spawn_count
        spawn_count += 1
        running_proc = MockRunningProcess()
        return running_proc

    monkeypatch.setattr(subprocess, "Popen", mock_popen)

    service = AudioService(work_sound=str(sound_file), break_sound=str(sound_file))

    # Test work sound shortcut ('t')
    ok1, msg1 = service.test_sound("work")
    assert ok1 is True
    assert spawn_count == 1
    assert "Playing sound" in msg1

    # Second press while playing
    ok2, msg2 = service.test_sound("work")
    assert ok2 is False
    assert "already in progress" in msg2
    assert spawn_count == 1  # No duplicate spawn

    # Finish process
    running_proc.running = False
    ok3, msg3 = service.test_sound("break")
    assert ok3 is True
    assert spawn_count == 2
