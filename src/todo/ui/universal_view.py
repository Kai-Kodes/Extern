"""Universal task list view."""

from datetime import datetime
from typing import Optional

from rich import box
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from todo.models import Task
from todo.services.daily_service import DailyService
from todo.services.task_service import TaskService
from todo.ui.console import console, print_status_message, prompt_input


class UniversalView:
    def __init__(self, task_service: TaskService, daily_service: DailyService):
        self.task_service = task_service
        self.daily_service = daily_service

    def run(self, select_mode: bool = True) -> Optional[str]:
        """
        Display Universal tasks.
        If select_mode=True, selecting a task schedules it for Today.
        Returns status message or None.
        """
        status_msg: Optional[str] = None
        status_style: str = "green"

        while True:
            tasks = self.task_service.get_universal_tasks()

            title_text = Text("UNIVERSAL TASKS", style="bold bright_white", justify="center")
            console.print(
                Panel(
                    title_text,
                    box=box.ROUNDED,
                    border_style="cyan",
                    padding=(0, 2),
                )
            )

            if status_msg:
                print_status_message(status_msg, style=status_style)
                status_msg = None
                status_style = "green"

            if not tasks:
                console.print("  [dim italic]No universal tasks created yet.[/dim italic]\n")
                console.print("  [bold cyan]\\[+][/bold cyan] Add universal task")
                console.print("  [bold cyan]\\[b][/bold cyan] Back\n")
            else:
                table = Table.grid(padding=(0, 1))
                table.add_column("num", justify="right", style="cyan bold")
                table.add_column("title")

                for idx, t in enumerate(tasks, start=1):
                    table.add_row(f"{idx}.", Text(t.title, style="bold white"))

                console.print(table)
                console.print()
                console.print("  [bold cyan]\\[+][/bold cyan] Add universal task")
                console.print("  [bold cyan]\\[b][/bold cyan] Back\n")

            hint = f"Select task to add to Today [1-{len(tasks)}, +, b]: " if tasks else "Option [+, b]: "
            choice = prompt_input(hint)

            if choice.lower() in ("b", "back", "q", "quit"):
                return None

            if choice in ("+", "add"):
                new_msg = self.handle_create_universal_task()
                if new_msg:
                    status_msg = new_msg
                    status_style = "green"
                continue

            if choice.isdigit() and tasks:
                idx = int(choice)
                if 1 <= idx <= len(tasks):
                    selected_task = tasks[idx - 1]
                    today_str = datetime.now().date().isoformat()
                    _, is_new = self.daily_service.add_task_to_date(selected_task.id, today_str)

                    if is_new:
                        return f"Added \"{selected_task.title}\" to today's tasks!"
                    else:
                        return f"\"{selected_task.title}\" is already scheduled for today!"
                else:
                    status_msg = f"Invalid selection: #{choice}. Please select 1-{len(tasks)}."
                    status_style = "red"
            else:
                status_msg = "Please enter a valid option."
                status_style = "yellow"

    def handle_create_universal_task(self) -> Optional[str]:
        """Prompt to create a new universal task."""
        console.print("\n[bold]New Universal Task[/bold]")
        title = prompt_input("Task: > ")
        if not title or title.lower() in ("b", "back", "cancel"):
            return None

        self.task_service.create_task(
            title=title,
            source_type="universal",
            status="pending",
        )
        return f"Created universal task: \"{title}\""
