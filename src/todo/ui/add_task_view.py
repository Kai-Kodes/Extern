"""Add task menu view for full-screen TUI."""

from typing import Optional

from rich import box
from rich.panel import Panel
from rich.text import Text

from todo.ui.console import (
    clear_viewport,
    console,
    print_status_message,
    render_footer,
    render_header,
)


class AddTaskView:
    def render(self, status_msg: Optional[str] = None, clear: bool = True) -> None:
        if clear:
            clear_viewport()

        render_header("ADD TASK")

        if status_msg:
            print_status_message(status_msg)

        console.print("  [bold cyan]1.[/bold cyan] Filemeta       [dim](Import or select from course folders)[/dim]")
        console.print("  [bold cyan]2.[/bold cyan] Universal task [dim](Pick an existing universal task)[/dim]")
        console.print("  [bold cyan]3.[/bold cyan] Manual task    [dim](Type a new task)[/dim]\n")

        render_footer([("1-3", "Select"), ("b", "Back")])
