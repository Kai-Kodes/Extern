"""Universal task list view with deletion and selection."""

from datetime import datetime
from typing import Optional

from rich.table import Table
from rich.text import Text

from todo.models import Task
from todo.services.daily_service import DailyService
from todo.services.task_service import TaskService
from todo.ui.console import (
    clear_viewport,
    console,
    print_status_message,
    prompt_input,
    render_footer,
    render_header,
)


class UniversalView:
    def __init__(self, task_service: TaskService, daily_service: DailyService):
        self.task_service = task_service
        self.daily_service = daily_service

    def run(self, select_mode: bool = True) -> Optional[str]:
        """
        Display Universal tasks in the TUI viewport.
        Allows selecting to add to Today, creating new, or deleting.
        """
        status_msg: Optional[str] = None
        status_style: str = "green"

        while True:
            clear_viewport()
            tasks = self.task_service.get_universal_tasks()
            render_header("UNIVERSAL TASKS")

            if status_msg:
                print_status_message(status_msg, style=status_style)
                status_msg = None
                status_style = "green"

            if not tasks:
                console.print("  [dim italic]No universal tasks created yet.[/dim italic]\n")
                hints = [("+", "Add universal task"), ("b", "Back")]
                render_footer(hints)
            else:
                table = Table.grid(padding=(0, 1))
                table.add_column("num", justify="right", style="cyan bold", width=4)
                table.add_column("title")

                for idx, t in enumerate(tasks, start=1):
                    table.add_row(f"{idx}.", Text(t.title, style="bold white"))

                console.print(table)
                console.print()

                hints = [
                    ("1-" + str(len(tasks)), "Add to Today"),
                    ("+", "Add task"),
                    ("d", "Delete"),
                    ("b", "Back"),
                ]
                render_footer(hints)

            hint = f"Select task to add to Today [1-{len(tasks)}, +, d, b]: " if tasks else "Option [+, b]: "
            choice = prompt_input(hint)

            if choice.lower() in ("b", "back", "q", "quit", "cancel"):
                return None

            if choice in ("+", "add"):
                new_msg = self.handle_create_universal_task()
                if new_msg:
                    status_msg = new_msg
                    status_style = "green"
                continue

            if choice.lower() in ("d", "delete"):
                del_msg = self.handle_delete_universal_task(tasks)
                if del_msg:
                    status_msg = del_msg
                    status_style = "yellow"
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
        clear_viewport()
        render_header("NEW UNIVERSAL TASK")
        console.print("Enter task title (or 'b' to cancel):\n")
        title = prompt_input("Task: > ")
        if not title or title.lower() in ("b", "back", "cancel"):
            return None

        self.task_service.create_task(
            title=title,
            source_type="universal",
            status="pending",
        )
        return f"Created universal task: \"{title}\""

    def handle_delete_universal_task(self, tasks: list[Task]) -> Optional[str]:
        """Prompt to permanently delete a universal task."""
        if not tasks:
            return "No universal tasks to delete."

        clear_viewport()
        render_header("DELETE UNIVERSAL TASK")
        console.print("Select universal task to delete permanently:\n")

        for idx, t in enumerate(tasks, start=1):
            console.print(f"  [bold cyan]{idx}.[/bold cyan] [bold white]{t.title}[/bold white]")

        console.print("\n  [bold cyan]\\[b][/bold cyan] Cancel\n")
        choice = prompt_input(f"Select task to delete [1-{len(tasks)}, b]: ")

        if choice.lower() in ("b", "back", "cancel"):
            return None

        if not choice.isdigit() or not (1 <= int(choice) <= len(tasks)):
            return "Invalid task selection."

        selected_task = tasks[int(choice) - 1]
        confirm = prompt_input(
            f"Permanently delete \"{selected_task.title}\" from database? [y/n]: "
        )
        if confirm.lower() in ("y", "yes"):
            self.task_service.delete_task(selected_task.id)
            return f"Deleted universal task \"{selected_task.title}\"."
        return "Deletion cancelled."
