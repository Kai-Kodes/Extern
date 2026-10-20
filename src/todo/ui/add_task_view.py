"""Add task menu view for full-screen TUI."""

from typing import Optional

from rich.table import Table
from rich.text import Text

from todo.ui.console import (
    clear_viewport,
    console,
    print_status_message,
    render_footer,
    render_header,
)


class AddTaskView:
    def render(
        self,
        status_msg: Optional[str] = None,
        selected_idx: int = 0,
        clear: bool = True,
        target_date: Optional[str] = None,
    ) -> None:
        if clear:
            clear_viewport()

        subtitle = f"Schedule for {target_date}" if target_date else None
        render_header("ADD TASK", subtitle=subtitle)

        if status_msg:
            print_status_message(status_msg)

        options = [
            ("1.", "Filemeta", "(Import or select from course folders)"),
            ("2.", "Telegram", "(Browse channels and course videos)"),
            ("3.", "YouTube", "(Import playlists or single video tasks)"),
            ("4.", "Universal Tasks", "(Pick an existing universal task)"),
            ("5.", "Manual", "(Type a new task)"),
        ]

        table = Table.grid(padding=(0, 1))
        table.add_column("cursor", justify="right", style="cyan bold", width=2)
        table.add_column("num", justify="right", style="cyan bold", width=4)
        table.add_column("name", style="bold white", width=18)
        table.add_column("desc", style="dim")

        for idx, (num, name, desc) in enumerate(options):
            cursor = "▶" if idx == selected_idx else " "
            table.add_row(cursor, num, name, desc)

        console.print(table)
        console.print()

        render_footer([("↑/↓", "Navigate"), ("Enter / 1-5", "Select"), ("b", "Back")])
