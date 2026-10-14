"""Pomodoro TUI view, presets manager, and full-screen digital clock renderer."""

from typing import Dict, List, Optional
from rich import box
from rich.align import Align
from rich.console import ConsoleOptions, Group
from rich.layout import Layout
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from todo.models import PomodoroPreset, PomodoroSessionPhase, PomodoroTimerState
from todo.services.audio_service import AudioService
from todo.services.pomodoro_service import PomodoroService
from todo.services.pomodoro_timer import PomodoroTimer
from todo.ui.console import (
    clear_viewport,
    console,
    prompt_input,
    read_key,
    render_footer,
    render_header,
    render_progress_bar,
)
from todo.ui.focus_detector import FocusDetector
from todo.ui.key_reader import Key

DIGITS_7SEG_5ROW: Dict[str, List[str]] = {
    "0": [
        "█████",
        "█   █",
        "█   █",
        "█   █",
        "█████",
    ],
    "1": [
        "   ██",
        "    █",
        "    █",
        "    █",
        "    █",
    ],
    "2": [
        "█████",
        "    █",
        "█████",
        "█    ",
        "█████",
    ],
    "3": [
        "█████",
        "    █",
        "█████",
        "    █",
        "█████",
    ],
    "4": [
        "█   █",
        "█   █",
        "█████",
        "    █",
        "    █",
    ],
    "5": [
        "█████",
        "█    ",
        "█████",
        "    █",
        "█████",
    ],
    "6": [
        "█████",
        "█    ",
        "█████",
        "█   █",
        "█████",
    ],
    "7": [
        "█████",
        "    █",
        "    █",
        "    █",
        "    █",
    ],
    "8": [
        "█████",
        "█   █",
        "█████",
        "█   █",
        "█████",
    ],
    "9": [
        "█████",
        "█   █",
        "█████",
        "    █",
        "█████",
    ],
    ":": [
        "  ",
        "██",
        "  ",
        "██",
        "  ",
    ],
    " ": [
        "     ",
        "     ",
        "     ",
        "     ",
        "     ",
    ],
}


def calculate_clock_scale(
    time_str: str,
    term_width: int,
    term_height: int,
    has_progress: bool = False,
    margin_x: int = 2,
) -> tuple[int, int, bool]:
    """
    Calculate the optimal balanced integer (scale_x, scale_y) for a digital clock display.

    Returns:
        (scale_x, scale_y, can_fit): If can_fit is False, terminal is too small for seven-segment
        and compact text fallback should be used.
    """
    base_h = 5
    base_w = 0
    for i, ch in enumerate(time_str):
        if i > 0:
            base_w += 1
        base_w += 2 if ch == ":" else 5

    if term_height >= 22:
        reserved_h = 7 if has_progress else 5
    elif term_height >= 14:
        reserved_h = 5 if has_progress else 4
    elif term_height >= 9:
        reserved_h = 3
    else:
        return 1, 1, False

    avail_w = term_width - 2 * margin_x
    avail_h = term_height - reserved_h

    if avail_w < base_w or avail_h < base_h:
        return 1, 1, False

    max_sx = avail_w // base_w
    max_sy = avail_h // base_h

    if max_sx < 1 or max_sy < 1:
        return 1, 1, False

    # Choose largest balanced scale that maximizes clock area without clipping
    best_sx, best_sy = 1, 1
    best_score = -1
    for sx in range(1, max_sx + 1):
        for sy in range(1, max_sy + 1):
            ratio = sx / sy
            if 0.5 <= ratio <= 2.0:
                area = (sx * base_w) * (sy * base_h)
                score = area - 5 * abs(sx - sy)
                if score > best_score:
                    best_score = score
                    best_sx, best_sy = sx, sy

    return best_sx, best_sy, True


def build_7segment_time(time_str: str, scale_x: int = 1, scale_y: int = 1) -> List[str]:
    """
    Render a time string into scaled lines of solid block glyphs using cell-width-safe characters.

    scale_x: Horizontal multiplier (repeats horizontal characters and separator spaces).
    scale_y: Vertical multiplier (repeats glyph rows vertically).
    """
    sx = max(1, scale_x)
    sy = max(1, scale_y)
    height = 5 * sy
    lines = ["" for _ in range(height)]

    for i, ch in enumerate(time_str):
        glyph = DIGITS_7SEG_5ROW.get(ch, DIGITS_7SEG_5ROW[" "])
        sep = (" " * sx) if i > 0 else ""
        for r_base, row_str in enumerate(glyph):
            scaled_row_str = "".join(c * sx for c in row_str)
            for dy in range(sy):
                r_out = r_base * sy + dy
                lines[r_out] += sep + scaled_row_str

    return lines


# Backwards compatibility alias
build_large_time_ascii = build_7segment_time


def create_digital_clock_layout(
    time_str: str,
    phase_label: str,
    state_str: str,
    term_width: int,
    term_height: int,
    progress_pct: Optional[int] = None,
) -> Layout:
    """
    Build a robust, responsive Rich Layout for the unfocused full-screen digital clock.
    Dynamically scales the seven-segment countdown to maximize window occupancy.
    Falls back to compact text display if terminal is too narrow or short.
    """
    layout = Layout()
    header_style = "bold cyan" if "WORK" in phase_label else "bold green"
    header_text = Text(f"{phase_label} · {state_str}", style=header_style)
    clock_style = "bold white" if state_str == "RUNNING" else "bold yellow"

    has_progress = progress_pct is not None
    sx, sy, can_fit_large = calculate_clock_scale(
        time_str=time_str,
        term_width=term_width,
        term_height=term_height,
        has_progress=has_progress,
    )

    if can_fit_large:
        clock_lines = build_7segment_time(time_str, scale_x=sx, scale_y=sy)
        clock_height = len(clock_lines)

        header_size = 2 if term_height >= 16 else 1
        footer_size = 2 if term_height >= 16 else 1

        layout.split_column(
            Layout(name="header", size=header_size),
            Layout(name="body", ratio=1),
            Layout(name="footer", size=footer_size),
        )
        layout["header"].update(Align.center(header_text, vertical="bottom" if header_size > 1 else "middle"))

        # Render clock text WITHOUT individual line justify='center' to preserve column alignments
        clock_text = Text("\n".join(clock_lines), style=clock_style)
        body_renderables = [Align.center(clock_text)]
        if progress_pct is not None and (term_height - clock_height >= 6):
            if term_height - clock_height >= 8:
                body_renderables.append(Text(""))
            body_renderables.append(Align.center(Text(f"{progress_pct}% completed", style="dim")))

        body_group = Group(*body_renderables)
        layout["body"].update(Align(body_group, align="center", vertical="middle"))
        layout["footer"].update(
            Align.center(
                Text("[Focus terminal or press any key to return]", style="dim"),
                vertical="top" if footer_size > 1 else "middle",
            )
        )
    else:
        # Graceful compact fallback for narrow / short terminals using Rich text
        combined = Text()
        combined.append(header_text)
        combined.append("\n\n")
        combined.append(Text(time_str, style=clock_style))
        if progress_pct is not None and term_height >= 8:
            combined.append(Text(f"\n{progress_pct}%", style="dim"))
        if term_height >= 6:
            combined.append("\n\n")
            combined.append(Text("[Focus terminal to return]", style="dim"))
        layout.update(Align(combined, align="center", vertical="middle"))

    return layout


class NonScrollingLayout:
    """Wrapper around Rich Layout to prevent trailing newline from scrolling the terminal buffer."""

    def __init__(self, layout: Layout):
        self.layout = layout

    def __rich_console__(self, console, options: ConsoleOptions):
        segments = list(self.layout.__rich_console__(console, options))
        if segments and segments[-1].text == "\n":
            segments.pop()
        yield from segments


class PomodoroView:
    """TUI view handling Pomodoro presets management, active timer, and unfocused clock."""

    def __init__(
        self,
        pomodoro_service: PomodoroService,
        timer: PomodoroTimer,
        audio_service: Optional[AudioService] = None,
        focus_detector: Optional[FocusDetector] = None,
    ):
        self.pomodoro_service = pomodoro_service
        self.timer = timer
        self.audio_service = audio_service
        self.focus_detector = focus_detector or FocusDetector()
        self.manual_clock_mode: bool = False

    def run(self) -> None:
        """Main Pomodoro entry point. Dispatches to presets list or active timer."""
        # Check if an active timer is running or can be recovered
        if self.timer.state == PomodoroTimerState.IDLE:
            self.timer.recover()

        if self.timer.state in (PomodoroTimerState.RUNNING, PomodoroTimerState.PAUSED):
            self.run_active_session()
        else:
            self.run_presets_menu()

    def run_presets_menu(self) -> None:
        """Preset selection and CRUD menu."""
        selected_idx = 0
        status_msg: Optional[str] = None
        status_style = "green"

        while True:
            presets = self.pomodoro_service.list_presets()
            if not presets:
                # Seed default preset if list empty
                default_p = PomodoroPreset(name="Classic", work_duration=25, short_break_duration=5)
                self.pomodoro_service.create_preset(default_p)
                presets = self.pomodoro_service.list_presets()

            selected_idx = max(0, min(selected_idx, len(presets) - 1))

            clear_viewport()
            render_header("POMODORO · PRESETS")

            if status_msg:
                console.print(f"[{status_style}]  {status_msg}[/{status_style}]\n")
                status_msg = None
                status_style = "green"

            table = Table(box=box.SIMPLE, show_header=True, header_style="bold cyan")
            table.add_column("  ", width=2)
            table.add_column("Name", style="bold white")
            table.add_column("Work", justify="right")
            table.add_column("Short Break", justify="right")
            table.add_column("Long Break", justify="right")
            table.add_column("Interval", justify="right")
            table.add_column("Auto-Advance", justify="center")
            table.add_column("Sound", justify="center")

            for idx, p in enumerate(presets):
                cursor = "▶" if idx == selected_idx else " "
                auto_str = "[green]Yes[/green]" if p.auto_advance else "[dim]No[/dim]"
                sound_str = "[green]On[/green]" if p.sound_enabled else "[dim]Off[/dim]"
                table.add_row(
                    cursor,
                    p.name,
                    f"{p.work_duration}m",
                    f"{p.short_break_duration}m",
                    f"{p.long_break_duration}m",
                    f"Every {p.sessions_before_long_break}",
                    auto_str,
                    sound_str,
                )

            console.print(table)
            console.print()

            hints = [
                ("↑/↓", "Navigate"),
                ("Enter", "Start"),
                ("a", "Add preset"),
                ("e", "Edit"),
                ("d", "Delete"),
                ("t", "Test audio"),
                ("Esc/q", "Back"),
            ]
            render_footer(hints)

            key = read_key()
            if key in (Key.ESCAPE, "escape", "q", "quit", "b", "back"):
                break

            if key in (Key.UP, "up"):
                selected_idx = max(0, selected_idx - 1)
                continue

            if key in (Key.DOWN, "down"):
                selected_idx = min(len(presets) - 1, selected_idx + 1)
                continue

            if key in (Key.ENTER, "enter", Key.SPACE, "space"):
                chosen = presets[selected_idx]
                self.timer.start(preset=chosen)
                self.run_active_session()
                continue

            if key.lower() in ("a", "+", "add"):
                status_msg, status_style = self._handle_create_preset()
                continue

            if key.lower() in ("e", "edit"):
                chosen = presets[selected_idx]
                status_msg, status_style = self._handle_edit_preset(chosen)
                continue

            if key.lower() in ("d", "delete"):
                chosen = presets[selected_idx]
                status_msg, status_style = self._handle_delete_preset(chosen)
                continue

            if key.lower() in ("t", "test"):
                if self.audio_service:
                    ok, msg = self.audio_service.test_sound("work")
                    status_msg = msg
                    status_style = "green" if ok else "yellow"
                else:
                    status_msg = "Audio service not initialized"
                    status_style = "red"
                continue

    def _handle_create_preset(self) -> tuple[str, str]:
        """Prompt user to create a new preset."""
        clear_viewport()
        render_header("CREATE POMODORO PRESET")
        try:
            name = prompt_input("Preset Name: ").strip()
            if not name:
                return "Preset creation cancelled: name is required.", "yellow"

            work_str = prompt_input("Work duration (minutes, e.g. 25): ").strip()
            work_min = int(work_str) if work_str else 25

            sbreak_str = prompt_input("Short break (minutes, e.g. 5): ").strip()
            sbreak_min = int(sbreak_str) if sbreak_str else 5

            lbreak_str = prompt_input("Long break (minutes, e.g. 15): ").strip()
            lbreak_min = int(lbreak_str) if lbreak_str else 15

            sess_str = prompt_input("Sessions before long break (e.g. 4): ").strip()
            sess_cnt = int(sess_str) if sess_str else 4

            auto_str = prompt_input("Auto-advance sessions? [y/N]: ").strip().lower()
            auto_adv = auto_str in ("y", "yes", "true", "1")

            sound_str = prompt_input("Play transition sounds? [Y/n]: ").strip().lower()
            sound_en = sound_str not in ("n", "no", "false", "0")

            preset = PomodoroPreset(
                name=name,
                work_duration=work_min,
                short_break_duration=sbreak_min,
                long_break_duration=lbreak_min,
                sessions_before_long_break=sess_cnt,
                auto_advance=auto_adv,
                sound_enabled=sound_en,
            )
            created = self.pomodoro_service.create_preset(preset)
            return f"Created preset '{created.name}'.", "green"
        except ValueError as e:
            return f"Invalid input: {e}", "red"
        except (KeyboardInterrupt, EOFError):
            return "Cancelled.", "yellow"

    def _handle_edit_preset(self, preset: PomodoroPreset) -> tuple[str, str]:
        """Prompt user to edit an existing preset."""
        clear_viewport()
        render_header(f"EDIT PRESET · {preset.name}")
        try:
            name = prompt_input(f"Name [{preset.name}]: ").strip()
            if name:
                preset.name = name

            work_str = prompt_input(f"Work duration [{preset.work_duration}m]: ").strip()
            if work_str:
                preset.work_duration = int(work_str)

            sbreak_str = prompt_input(f"Short break [{preset.short_break_duration}m]: ").strip()
            if sbreak_str:
                preset.short_break_duration = int(sbreak_str)

            lbreak_str = prompt_input(f"Long break [{preset.long_break_duration}m]: ").strip()
            if lbreak_str:
                preset.long_break_duration = int(lbreak_str)

            sess_str = prompt_input(f"Sessions before long break [{preset.sessions_before_long_break}]: ").strip()
            if sess_str:
                preset.sessions_before_long_break = int(sess_str)

            auto_default = "Y" if preset.auto_advance else "N"
            auto_str = prompt_input(f"Auto-advance [{auto_default}]: ").strip().lower()
            if auto_str:
                preset.auto_advance = auto_str in ("y", "yes", "1")

            sound_default = "Y" if preset.sound_enabled else "N"
            sound_str = prompt_input(f"Sound [{sound_default}]: ").strip().lower()
            if sound_str:
                preset.sound_enabled = sound_str not in ("n", "no", "0")

            self.pomodoro_service.update_preset(preset)
            return f"Updated preset '{preset.name}'.", "green"
        except ValueError as e:
            return f"Invalid input: {e}", "red"
        except (KeyboardInterrupt, EOFError):
            return "Cancelled.", "yellow"

    def _handle_delete_preset(self, preset: PomodoroPreset) -> tuple[str, str]:
        """Confirm and delete a preset, preventing deletion if currently active."""
        if not preset.id:
            return "Cannot delete preset without id", "red"

        # Check if currently running in active timer
        active_preset_id = (
            self.timer.preset.id
            if (self.timer.preset and self.timer.state in (PomodoroTimerState.RUNNING, PomodoroTimerState.PAUSED))
            else None
        )
        if active_preset_id and preset.id == active_preset_id:
            return f"Cannot delete preset '{preset.name}': it is currently running.", "red"

        clear_viewport()
        render_header(f"DELETE PRESET · {preset.name}")
        console.print(f"Are you sure you want to delete preset [bold white]'{preset.name}'[/bold white]?\n")
        render_footer([("y", "Yes, delete"), ("n", "Cancel")])

        conf = read_key()
        if conf.lower() in ("y", "yes", Key.ENTER, "enter"):
            try:
                self.pomodoro_service.delete_preset(preset.id, active_preset_id=active_preset_id)
                return f"Deleted preset '{preset.name}'.", "green"
            except ValueError as e:
                return str(e), "red"

        return "Cancelled deletion.", "yellow"

    def run_active_session(self) -> None:
        """Interactive loop while a session is active. Renders countdown or digital clock when unfocused."""
        while True:
            # 1. Update authoritative timer
            self.timer.tick()

            if self.timer.state == PomodoroTimerState.COMPLETED:
                self._render_completed_screen()
                key = read_key()
                self.timer.stop()
                break

            # 2. Check focus state: if window is unfocused or manual clock mode is toggled
            is_focused = self.focus_detector.is_terminal_focused()
            if not is_focused or self.manual_clock_mode:
                self._render_digital_clock()
            else:
                self._render_active_timer()

            # 3. Read key with non-blocking 0.5s timeout for smooth 1-sec countdown refresh
            key = read_key(timeout=0.5)

            if key is None:
                continue

            # If user pressed any key in manual clock mode, exit clock mode
            if self.manual_clock_mode:
                self.manual_clock_mode = False
                continue

            if key in (Key.SPACE, "space", " "):
                if self.timer.state == PomodoroTimerState.RUNNING:
                    self.timer.pause()
                elif self.timer.state == PomodoroTimerState.PAUSED:
                    self.timer.resume()
                continue

            if key.lower() in ("s", "skip"):
                self.timer.skip()
                continue

            if key.lower() in ("r", "restart"):
                self.timer.reset_phase()
                continue

            if key in (Key.ENTER, "enter"):
                # If paused waiting for next phase, resume
                if self.timer.state == PomodoroTimerState.PAUSED:
                    self.timer.resume()
                continue

            if key.lower() in ("c", "clock"):
                self.manual_clock_mode = not self.manual_clock_mode
                continue

            if key in (Key.ESCAPE, "escape", "q", "quit", "b", "back"):
                # Ask whether to keep running in background or stop
                clear_viewport()
                render_header("PAUSE / LEAVE POMODORO")
                console.print("Timer is active.\n")
                console.print("  [bold cyan]1.[/bold cyan] Keep timer running in background and return to tasks")
                console.print("  [bold cyan]2.[/bold cyan] Stop timer completely and return to presets\n")
                render_footer([("1", "Background"), ("2", "Stop"), ("Esc", "Resume session")])
                opt = read_key()
                if opt in ("1", Key.ENTER):
                    break
                elif opt in ("2", "q"):
                    self.timer.stop()
                    break
                else:
                    continue

    def _render_active_timer(self) -> None:
        """Render standard rich TUI timer screen."""
        clear_viewport()
        preset_name = self.timer.preset.name if self.timer.preset else "Pomodoro"
        render_header(f"POMODORO · {preset_name.upper()}")

        info = self.timer.get_display_info()
        time_str = info["time_str"]
        phase_label = info["phase_label"]
        progress_pct = int(info["progress"] * 100)
        completed = info["completed_work_sessions"]

        if info["state"] == PomodoroTimerState.RUNNING:
            state_text = Text("RUNNING", style="bold green")
        elif info["state"] == PomodoroTimerState.PAUSED:
            state_text = Text("PAUSED", style="bold yellow")
        else:
            state_text = Text("COMPLETED", style="bold magenta")

        content = Table.grid(padding=(1, 2))
        content.add_column(justify="center")

        content.add_row(state_text)
        content.add_row(Text(f"Phase: {phase_label}", style="bold cyan"))
        content.add_row(Text(time_str, style="bold white text-center", justify="center"))

        bar = render_progress_bar(progress_pct, 100, width=28)
        content.add_row(Text(f"{bar}", justify="center"))
        content.add_row(Text(f"Completed Work Sessions: {completed}", style="dim"))

        panel = Panel(
            Align.center(content),
            box=box.ROUNDED,
            border_style="cyan" if info["state"] == PomodoroTimerState.RUNNING else "yellow",
            padding=(1, 4),
        )
        console.print(panel)
        console.print()

        hints = [
            ("Space", "Pause/Resume" if info["state"] == PomodoroTimerState.RUNNING else "Resume"),
            ("s", "Skip phase"),
            ("r", "Restart phase"),
            ("c", "Clock mode"),
            ("Esc/q", "Leave/Stop"),
        ]
        render_footer(hints)

    def _render_digital_clock(self) -> None:
        """Render minimal full-screen digital clock when terminal is unfocused or in clock mode."""
        clear_viewport()
        info = self.timer.get_display_info()
        time_str = info["time_str"]
        phase_label = info["phase_label"]
        state_str = "PAUSED" if info["state"] == PomodoroTimerState.PAUSED else "RUNNING"
        progress_pct = int(info["progress"] * 100)

        layout = create_digital_clock_layout(
            time_str=time_str,
            phase_label=phase_label,
            state_str=state_str,
            term_width=console.width,
            term_height=console.height,
            progress_pct=progress_pct,
        )
        console.print(NonScrollingLayout(layout), end="")

    def _render_completed_screen(self) -> None:
        """Render cycle completion congratulatory screen."""
        clear_viewport()
        render_header("POMODORO CYCLE COMPLETE")
        msg = Table.grid(padding=(1, 2))
        msg.add_column(justify="center")
        msg.add_row(Text("All scheduled sessions completed!", style="bold green"))
        msg.add_row(Text(f"Total work sessions finished: {self.timer.completed_work_sessions}", style="bold white"))
        msg.add_row(Text("Great focus session! Press Enter or Esc to return.", style="dim"))
        panel = Panel(Align.center(msg), box=box.ROUNDED, border_style="green", padding=(2, 4))
        console.print(panel)
        render_footer([("Enter", "Return to Presets")])
