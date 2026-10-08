"""Console and rendering helpers for the terminal UI."""

import os
import sys
from datetime import datetime
from typing import Optional

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

console = Console()


def format_today_title(date_obj: Optional[datetime] = None) -> str:
    """Format title like 'TODAY · 8 OCT 2026'."""
    if date_obj is None:
        date_obj = datetime.now()
    day = date_obj.strftime("%-d")
    month_year = date_obj.strftime("%b %Y").upper()
    return f"TODAY · {day} {month_year}"


def render_header(title: str, subtitle: Optional[str] = None) -> None:
    """Render a styled rounded top panel header."""
    header_text = Text()
    header_text.append(f" {title.upper()} ", style="bold bright_white on dark_blue")
    if subtitle:
        header_text.append(f"\n{subtitle}", style="dim")

    console.print(
        Panel(
            header_text,
            box=box.ROUNDED,
            border_style="bright_blue",
            padding=(0, 2),
        )
    )


def print_status_message(message: str, style: str = "green") -> None:
    """Print an action confirmation or alert message."""
    console.print(f"[{style}]  {message}[/{style}]\n")


def prompt_input(prompt_text: str = "> ") -> str:
    """Prompt for input with clean EOF and interrupt propagation."""
    return console.input(prompt_text).strip()
