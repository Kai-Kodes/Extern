"""Add task menu view."""

from typing import Optional

from rich import box
from rich.panel import Panel
from rich.text import Text

from todo.ui.console import console, print_status_message


class AddTaskView:
    def render(self, status_msg: Optional[str] = None) -> None:
        title_text = Text("ADD TASK", style="bold bright_white", justify="center")
        console.print(
            Panel(
                title_text,
                box=box.ROUNDED,
                border_style="cyan",
                padding=(0, 2),
            )
        )

        if status_msg:
            print_status_message(status_msg)

        console.print("  [bold cyan]1.[/bold cyan] Filemeta")
        console.print("  [bold cyan]2.[/bold cyan] Universal task")
        console.print("  [bold cyan]3.[/bold cyan] Manual task\n")
        console.print("  [bold cyan]\\[b][/bold cyan] Back\n")
