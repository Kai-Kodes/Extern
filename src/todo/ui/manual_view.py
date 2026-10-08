"""Manual task creation workflow."""

from datetime import datetime
from typing import Optional

from rich import box
from rich.panel import Panel
from rich.text import Text

from todo.services.daily_service import DailyService
from todo.services.task_service import TaskService
from todo.ui.console import console, print_status_message, prompt_input


class ManualView:
    def __init__(self, task_service: TaskService, daily_service: DailyService):
        self.task_service = task_service
        self.daily_service = daily_service

    def run(self) -> Optional[str]:
        """Prompt user for manual task title and destination."""
        title_text = Text("MANUAL TASK", style="bold bright_white", justify="center")
        console.print(
            Panel(
                title_text,
                box=box.ROUNDED,
                border_style="cyan",
                padding=(0, 2),
            )
        )

        console.print("Task title (or 'b' to cancel):")
        task_title = prompt_input("Task: > ")

        if not task_title or task_title.lower() in ("b", "back", "cancel", "q", "quit"):
            return None

        console.print("\nAdd to:")
        console.print("  [bold cyan]1.[/bold cyan] Today")
        console.print("  [bold cyan]2.[/bold cyan] Universal list\n")
        console.print("  [bold cyan]\\[b][/bold cyan] Cancel\n")

        dest_choice = prompt_input("Select destination [1-2, b]: ")

        if dest_choice.lower() in ("b", "back", "cancel", "q", "quit"):
            return None

        if dest_choice == "1":
            # Create task for today
            task = self.task_service.create_task(
                title=task_title,
                source_type="manual",
                status="pending",
            )
            today_str = datetime.now().date().isoformat()
            self.daily_service.add_task_to_date(task.id, today_str)
            return f"Added \"{task.title}\" to today's tasks!"

        elif dest_choice == "2":
            # Create reusable universal task
            task = self.task_service.create_task(
                title=task_title,
                source_type="universal",
                status="pending",
            )
            return f"Added \"{task.title}\" to universal task list!"

        else:
            return "Cancelled: invalid destination selection."
