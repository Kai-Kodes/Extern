"""Console, alternate screen buffer, and keyboard input helpers for full-screen TUI."""

import atexit
import contextlib
import select
import sys
import termios
import tty
from datetime import datetime
from typing import List, Optional, Tuple

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from todo.ui.key_reader import Key, KeyReader, key_reader

console = Console()

# Keep track of alt-screen state to ensure restoration on exit
_IN_ALT_SCREEN = False


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


@contextlib.contextmanager
def tui_mode():
    """Context manager combining alternate screen buffer and cbreak keyboard mode."""
    with alt_screen(), key_reader.active_mode():
        yield


def clear_viewport() -> None:
    """Clear the viewport screen."""
    console.clear()


def format_today_title(date_obj: Optional[datetime] = None) -> str:
    """Format title like 'TODAY · 8 OCT 2026'."""
    if date_obj is None:
        date_obj = datetime.now()
    day = date_obj.strftime("%-d")
    month_year = date_obj.strftime("%b %Y").upper()
    return f"TODAY · {day} {month_year}"


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


def render_footer(hints: List[Tuple[str, str]]) -> None:
    """Render a styled keyboard hints footer."""
    console.print("[dim]─" * 50 + "[/dim]")
    parts = []
    for key, action in hints:
        # Escape any square brackets in keys
        escaped_key = key.replace("[", "\\[").replace("]", "\\]")
        parts.append(f"[bold cyan]{escaped_key}[/bold cyan] [dim]{action}[/dim]")
    console.print("  " + "   ".join(parts) + "\n")


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


def read_key() -> str:
    """
    Read a single keypress or command via the central KeyReader.
    """
    return key_reader.read_key()


def prompt_input(prompt_text: str = "> ") -> str:
    """
    Prompt user for full line input with cursor shown via KeyReader.
    """
    return key_reader.read_text_input(prompt_text)

