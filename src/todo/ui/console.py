"""Console, alternate screen buffer, and keyboard input helpers for full-screen TUI."""

import atexit
import contextlib
import io
import re
import select
import signal
import sys
import termios
import tty
from datetime import date, datetime
from typing import Any, List, Optional, Tuple, Union

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from todo.ui.key_reader import Key, KeyReader, key_reader

console = Console()


def render_readline_prompt(
    console_instance: Console, prompt: Any, markup: bool = True, emoji: bool = True
) -> str:
    """
    Render a prompt string or Rich renderable into an ANSI string with GNU Readline
    non-printing sequence markers (\\001 and \\002) so Readline correctly calculates
    prompt width and cursor position without line-wrapping, drift, or erasure.
    """
    if not prompt:
        return ""

    if isinstance(prompt, str) and "[" not in prompt and "\x1b" not in prompt:
        return prompt

    buf = io.StringIO()
    cs = console_instance.color_system or "standard"
    temp_console = Console(file=buf, force_terminal=True, color_system=cs, no_color=False)
    temp_console.print(prompt, markup=markup, emoji=emoji, end="")
    rendered = buf.getvalue()

    if "\x1b" not in rendered:
        return rendered

    clean = rendered.replace("\001", "").replace("\002", "")
    return re.sub(r"(\x1b\[[0-9;]*[a-zA-Z])", r"\001\1\002", clean)


def _safe_console_input(
    prompt: Any = "",
    *,
    markup: bool = True,
    emoji: bool = True,
    password: bool = False,
    stream: Optional[Any] = None,
) -> str:
    """
    Drop-in replacement for Console.input that passes the rendered prompt
    directly to GNU readline / Python input() rather than printing it separately
    with an empty readline prompt. This prevents GNU readline from overwriting or
    erasing the prompt during backspaces, typing, line wrapping, or empty-input states.
    """
    if stream:
        if prompt:
            console.print(prompt, markup=markup, emoji=emoji, end="")
        return stream.readline()

    prompt_str = render_readline_prompt(console, prompt, markup=markup, emoji=emoji) if prompt else ""

    if password:
        import getpass

        return getpass.getpass(prompt_str)

    return input(prompt_str)


# Attach drop-in safe input method to Console instance
console.input = _safe_console_input

# Keep track of alt-screen state to ensure restoration on exit
_IN_ALT_SCREEN = False


class ViewportManager:
    """
    Coordinates atomic, tear-free viewport rendering for full-screen TUI views.

    Eliminates visible screen flickering and blank flashes by:
    1. Intercepting clear_viewport() to begin buffering frame output instead of emitting
       destructive terminal wipe sequences (\\x1b[2J).
    2. Overwriting the viewport in-place from home (\\x1b[H), erasing trailing characters on
       each row with \\x1b[K and erasing trailing lines below the view with \\x1b[J.
    3. Framing all frame updates in DEC Mode 2026 Synchronized Output (\\x1b[?2026h ... \\x1b[?2026l)
       which modern terminal emulators (Kitty, WezTerm, Alacritty) render atomically on vsync.
    4. Automatically committing the frame buffer immediately before reading user keyboard or text
       input via KeyReader hooks.
    """

    def __init__(self, console_instance: Console):
        self.console = console_instance
        self._capturing = False
        self._enabled = True
        self._force_active = False

    @property
    def is_active(self) -> bool:
        """Return True if alt screen is active and atomic viewport management is enabled."""
        return (self._force_active or _IN_ALT_SCREEN) and self._enabled and self.console.file is not None

    def begin_frame(self) -> None:
        """Begin capturing a new frame or clear the screen."""
        if not self.is_active:
            if sys.stdout.isatty() and getattr(self.console, "_buffer_index", 0) == 0:
                self.console.clear()
            return

        # If already capturing an uncommitted frame (e.g. rapid consecutive clears), discard first
        if self._capturing:
            self.discard_frame()

        # If an external capture is active (e.g. tests using 'with console.capture():'), do not interfere
        if getattr(self.console, "_buffer_index", 0) > 0:
            return

        self.console.begin_capture()
        self._capturing = True

    def commit_frame(self) -> None:
        """Commit the captured frame buffer atomically to the terminal."""
        if not self._capturing:
            return

        captured = self.console.end_capture()
        self._capturing = False

        if captured is None:
            return

        lines = captured.splitlines()
        if not lines:
            formatted_content = "\x1b[H\x1b[J"
        else:
            formatted_content = "\x1b[H" + "\x1b[K\n".join(lines) + "\x1b[K\x1b[J"

        payload = f"\x1b[?2026h{formatted_content}\x1b[?2026l"

        try:
            self.console.file.write(payload)
            self.console.file.flush()
        except Exception:
            pass

    def discard_frame(self) -> None:
        """Discard any currently capturing frame buffer without writing to terminal."""
        if self._capturing:
            try:
                self.console.end_capture()
            except Exception:
                pass
            self._capturing = False


viewport_manager = ViewportManager(console)


def enter_alt_screen() -> None:
    """Enter terminal alternate screen buffer and hide cursor if in a TTY."""
    global _IN_ALT_SCREEN
    if sys.stdout.isatty() and not _IN_ALT_SCREEN:
        console.set_alt_screen(True)
        console.show_cursor(False)
        _IN_ALT_SCREEN = True


def exit_alt_screen() -> None:
    """Exit terminal alternate screen buffer and restore cursor."""
    global _IN_ALT_SCREEN
    viewport_manager.discard_frame()
    if sys.stdout.isatty() and _IN_ALT_SCREEN:
        console.show_cursor(True)
        console.set_alt_screen(False)
        _IN_ALT_SCREEN = False


# Register atexit handler for safe terminal restoration
atexit.register(exit_alt_screen)


@contextlib.contextmanager
def alt_screen():
    """Context manager for running in alternate screen buffer with guaranteed restoration."""
    enter_alt_screen()
    try:
        yield
    finally:
        exit_alt_screen()


_RESIZED = False


def _sigwinch_handler(signum: Any, frame: Any) -> None:
    global _RESIZED
    _RESIZED = True


def check_and_clear_resized() -> bool:
    """Return True if a terminal resize signal was received since last check, and reset flag."""
    global _RESIZED
    if _RESIZED:
        _RESIZED = False
        return True
    return False


@contextlib.contextmanager
def tui_mode():
    """Context manager combining alternate screen buffer, cbreak keyboard mode, and SIGWINCH handling."""
    old_handler = None
    if hasattr(signal, "SIGWINCH"):
        try:
            old_handler = signal.signal(signal.SIGWINCH, _sigwinch_handler)
        except Exception:
            old_handler = None

    try:
        with alt_screen(), key_reader.active_mode():
            yield
    finally:
        if hasattr(signal, "SIGWINCH") and old_handler is not None:
            try:
                signal.signal(signal.SIGWINCH, old_handler)
            except Exception:
                pass


def clear_viewport() -> None:
    """Clear the viewport screen or begin an atomic frame update."""
    viewport_manager.begin_frame()


def commit_viewport() -> None:
    """Commit any buffered viewport changes atomically."""
    viewport_manager.commit_frame()


# Register commit_viewport as the before-read hook for KeyReader
key_reader.set_before_read_hook(commit_viewport)


def format_today_title(date_obj: Optional[Union[datetime, date, str]] = None) -> str:
    """Format title like 'TODAY · 8 OCT 2026'."""
    if date_obj is None:
        d = datetime.now()
    elif isinstance(date_obj, str):
        try:
            d = datetime.fromisoformat(date_obj)
        except Exception:
            d = datetime.now()
    elif isinstance(date_obj, date) and not isinstance(date_obj, datetime):
        d = datetime(date_obj.year, date_obj.month, date_obj.day)
    else:
        d = date_obj
    day = d.strftime("%-d")
    month_year = d.strftime("%b %Y").upper()
    return f"TODAY · {day} {month_year}"


def calculate_today_progress(completed: int, total: int) -> int:
    """Calculate integer completion percentage (0-100), avoiding division by zero."""
    if total <= 0:
        return 0
    return round((completed / total) * 100)


def format_today_progress(
    completed: int, total: int, avail_width: int = 80, left_len: int = 18
) -> str:
    """
    Format right-hand progress string.
    Contains ONLY 'percentage · completed_count/total_count'.
    Examples: '0% · 0/0', '0% · 0/5', '25% · 1/4', '50% · 2/4', '100% · 4/4'.
    No 'done', 'completed', 'complete', 'tasks', or 'finished'.
    """
    if total <= 0:
        return "0% · 0/0"
    pct = calculate_today_progress(completed, total)
    return f"{pct}% · {completed}/{total}"


def render_today_header(
    date_str: Optional[str] = None, completed: int = 0, total: int = 0
) -> None:
    """
    Render horizontal Today header with date on the left and progress on the right.
    Calculates left/right layout dynamically based on terminal width.
    """
    if date_str is None:
        date_str = format_today_title()

    term_width = console.width
    avail_width = max(10, term_width - 4)

    left_text = date_str.upper()
    right_text = format_today_progress(completed, total, avail_width, len(left_text))

    # Guard against narrow screens: preserve right_text and truncate left_text with ellipsis
    needed_right = len(right_text)
    if len(left_text) + 1 + needed_right > avail_width:
        max_left = max(4, avail_width - needed_right - 1)
        if len(left_text) > max_left:
            left_text = left_text[: max_left - 1] + "…"

    table = Table.grid(expand=True)
    table.add_column(justify="left", no_wrap=True)
    table.add_column(justify="right", no_wrap=True)
    table.add_row(
        Text(left_text, style="bold bright_white"),
        Text(right_text, style="bold cyan"),
    )

    console.print(
        Panel(
            table,
            box=box.ROUNDED,
            border_style="cyan",
            padding=(0, 1),
        )
    )



def render_header(title: str, subtitle: Optional[str] = None) -> None:
    """Render a styled rounded top panel header."""
    title_text = Text(title.upper(), style="bold bright_white", justify="center")
    sub = f"[dim]{subtitle}[/dim]" if subtitle else None
    console.print(
        Panel(
            title_text,
            box=box.ROUNDED,
            border_style="cyan",
            subtitle=sub,
            padding=(0, 2),
        )
    )


def render_footer(hints: List[Tuple[str, str]], status: Optional[str] = None) -> None:
    """Render a styled keyboard hints footer."""
    console.print("[dim]─" * 50 + "[/dim]")
    parts = []
    for key, action in hints:
        # Escape any square brackets in keys
        escaped_key = key.replace("[", "\\[").replace("]", "\\]")
        parts.append(f"[bold cyan]{escaped_key}[/bold cyan] [dim]{action}[/dim]")
    console.print("  " + "   ".join(parts))
    console.print()


def print_status_message(message: str, style: str = "green") -> None:
    """Print an action confirmation or alert message."""
    console.print(f"[{style}]  {message}[/{style}]\n")


def render_progress_bar(completed: int, total: int, width: int = 16) -> str:
    """Generate a clean unicode progress bar string."""
    if total <= 0:
        return "░" * width + " 0%"
    pct = int((completed / total) * 100)
    filled_len = int(width * completed / total)
    bar = "█" * filled_len + "░" * (width - filled_len)
    return f"{bar} {pct}%"


def read_key(timeout: Optional[float] = None) -> Optional[str]:
    """
    Read a single keypress or command via the central KeyReader, optionally with timeout.
    """
    return key_reader.read_key(timeout=timeout)


def prompt_input(prompt_text: str = "> ", prefill: str = "") -> str:
    """
    Prompt user for full line input with cursor shown via KeyReader, optionally with prefill.
    """
    return key_reader.read_text_input(prompt_text, prefill=prefill)

