"""Manual task creation workflow for full-screen TUI."""

from datetime import datetime
from typing import Optional

from rich.table import Table

from todo.services.daily_service import DailyService
from todo.services.task_service import TaskService
from todo.ui.console import clear_viewport, console, render_footer, render_header
from todo.ui.key_reader import Key, key_reader


class ManualView:
    def __init__(self, task_service: TaskService, daily_service: DailyService):
        self.task_service = task_service
        self.daily_service = daily_service

    def run(self) -> Optional[str]:
        """Prompt user for manual task title and destination."""
        clear_viewport()
        render_header("MANUAL TASK")

        console.print("Enter task title (or 'b' to cancel):\n")
        task_title = key_reader.read_text_input("Task: > ")

        if not task_title or task_title.lower() in ("b", "back", "cancel", "q", "quit"):
            return None

        # Destination selection with keyboard support
        options = ["Today", "Universal list"]
        sel_idx = 0

        while True:
            clear_viewport()
            render_header("MANUAL TASK", subtitle=task_title)
            console.print(f"Task: [bold white]{task_title}[/bold white]\n")
            console.print("Choose destination:\n")

            table = Table.grid(padding=(0, 1))
            table.add_column("cursor", justify="right", style="cyan bold", width=2)
            table.add_column("num", justify="right", style="cyan bold", width=4)
            table.add_column("name", style="bold white")

            for idx, opt_name in enumerate(options):
                cursor = "▶" if idx == sel_idx else " "
                table.add_row(cursor, f"{idx + 1}.", opt_name)

            console.print(table)
            console.print()
            render_footer(
                [
                    ("↑/↓", "Navigate"),
                    ("Enter / 1-2", "Select"),
                    ("b", "Cancel"),
                ]
            )

            k = key_reader.read_key()

            if k in (Key.ESCAPE, "b", "q", "cancel"):
                return None

            if k == Key.UP:
                sel_idx = max(0, sel_idx - 1)
                continue

            if k == Key.DOWN:
                sel_idx = min(len(options) - 1, sel_idx + 1)
                continue

            if k in (Key.ENTER, "enter"):
                break

            if k == "1":
                sel_idx = 0
                break

            if k == "2":
                sel_idx = 1
                break

        if sel_idx == 0:
            task = self.task_service.create_task(
                title=task_title,
                source_type="manual",
                status="pending",
            )
            today_str = datetime.now().date().isoformat()
            self.daily_service.add_task_to_date(task.id, today_str)
            return None
        else:
            self.task_service.create_task(
                title=task_title,
                source_type="universal",
                status="pending",
            )
            return None

