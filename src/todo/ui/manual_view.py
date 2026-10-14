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

    def run(self, target_date: Optional[str] = None) -> Optional[str]:
        """Prompt user for manual task title and destination."""
        clear_viewport()
        render_header("MANUAL TASK")

        console.print("Enter task title (or 'b' to cancel):\n")
        task_title = key_reader.read_text_input("Task: > ")

        if not task_title or task_title.lower() in ("b", "back", "cancel", "q", "quit"):
            return None

        today_str = self.daily_service.get_today_str()
        if target_date and target_date != today_str:
            options = [f"Scheduled date ({target_date})", "Today", "Universal list"]
        else:
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
            footer_keys = f"Enter / 1-{len(options)}"
            render_footer(
                [
                    ("↑/↓", "Navigate"),
                    (footer_keys, "Select"),
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

            if k.isdigit():
                digit = int(k)
                if 1 <= digit <= len(options):
                    sel_idx = digit - 1
                    break

        if target_date and target_date != today_str:
            if sel_idx == 0:
                task = self.task_service.create_task(
                    title=task_title,
                    source_type="manual",
                    status="pending",
                )
                self.daily_service.add_task_to_date(task.id, target_date)
                return None
            elif sel_idx == 1:
                task = self.task_service.create_task(
                    title=task_title,
                    source_type="manual",
                    status="pending",
                )
                self.daily_service.add_task_to_date(task.id, today_str)
                return None
            else:
                self.task_service.create_task(
                    title=task_title,
                    source_type="universal",
                    status="pending",
                )
                return None
        else:
            if sel_idx == 0:
                task = self.task_service.create_task(
                    title=task_title,
                    source_type="manual",
                    status="pending",
                )
                self.daily_service.add_task_to_date(task.id, today_str)
                return None
            else:
                self.task_service.create_task(
                    title=task_title,
                    source_type="universal",
                    status="pending",
                )
                return None

