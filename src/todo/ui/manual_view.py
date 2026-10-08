"""Manual task creation workflow for full-screen TUI."""

from datetime import datetime
from typing import Optional

from todo.services.daily_service import DailyService
from todo.services.task_service import TaskService
from todo.ui.console import clear_viewport, console, prompt_input, render_footer, render_header


class ManualView:
    def __init__(self, task_service: TaskService, daily_service: DailyService):
        self.task_service = task_service
        self.daily_service = daily_service

    def run(self) -> Optional[str]:
        """Prompt user for manual task title and destination."""
        clear_viewport()
        render_header("MANUAL TASK")

        console.print("Task title (or 'b' to cancel):\n")
        task_title = prompt_input("Task: > ")

        if not task_title or task_title.lower() in ("b", "back", "cancel", "q", "quit"):
            return None

        console.print("\nAdd to:")
        console.print("  [bold cyan]1.[/bold cyan] Today")
        console.print("  [bold cyan]2.[/bold cyan] Universal list\n")

        render_footer([("1-2", "Select destination"), ("b", "Cancel")])

        dest_choice = prompt_input("Select destination [1-2, b]: ")

        if dest_choice.lower() in ("b", "back", "cancel", "q", "quit"):
            return None

        if dest_choice == "1":
            task = self.task_service.create_task(
                title=task_title,
                source_type="manual",
                status="pending",
            )
            today_str = datetime.now().date().isoformat()
            self.daily_service.add_task_to_date(task.id, today_str)
            return f"Added \"{task.title}\" to today's tasks!"

        elif dest_choice == "2":
            task = self.task_service.create_task(
                title=task_title,
                source_type="universal",
                status="pending",
            )
            return f"Added \"{task.title}\" to universal task list!"

        else:
            return "Cancelled: invalid destination selection."
