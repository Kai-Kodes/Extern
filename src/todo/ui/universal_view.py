"""Universal task list view with keyboard navigation and deletion."""

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
    render_footer,
    render_header,
)
from todo.ui.key_reader import Key, key_reader


class UniversalView:
    def __init__(self, task_service: TaskService, daily_service: DailyService):
        self.task_service = task_service
        self.daily_service = daily_service

    def run(self, select_mode: bool = True) -> Optional[str]:
        """Display Universal tasks in the TUI viewport with keyboard navigation."""
        status_msg: Optional[str] = None
        status_style: str = "green"
        selected_idx = 0

        while True:
            clear_viewport()
            tasks = self.task_service.get_universal_tasks()
            render_header("UNIVERSAL TASKS")

            if status_msg:
                print_status_message(status_msg, style=status_style)
                status_msg = None
                status_style = "green"

            if not tasks:
                console.print(
                    "  [dim italic]No universal tasks created yet.[/dim italic]\n"
                )
                hints = [("+", "Add universal task"), ("b", "Back")]
                render_footer(hints)
            else:
                if selected_idx >= len(tasks):
                    selected_idx = max(0, len(tasks) - 1)

                table = Table.grid(padding=(0, 1))
                table.add_column("cursor", justify="right", style="cyan bold", width=2)
                table.add_column("num", justify="right", style="cyan bold", width=4)
                table.add_column("title")

                for idx, t in enumerate(tasks):
                    cursor = "▶" if idx == selected_idx else " "
                    table.add_row(cursor, f"{idx + 1}.", Text(t.title, style="bold white"))

                console.print(table)
                console.print()

                hints = [
                    ("↑/↓", "Navigate"),
                    ("Enter", "Add to Today"),
                    ("+", "Add task"),
                    ("d", "Delete"),
                    ("b", "Back"),
                ]
                render_footer(hints)

            k = key_reader.read_key()

            if k in (Key.ESCAPE, "b", "q", "cancel"):
                return None

            if k == Key.UP and tasks:
                selected_idx = max(0, selected_idx - 1)
                continue

            if k == Key.DOWN and tasks:
                selected_idx = min(len(tasks) - 1, selected_idx + 1)
                continue

            if k in ("+", Key.PLUS, "add"):
                new_msg = self.handle_create_universal_task()
                if new_msg:
                    status_msg = new_msg
                    status_style = "green"
                continue

            if k in ("d", "delete") and tasks:
                del_msg = self.handle_delete_universal_task(tasks, current_idx=selected_idx)
                if del_msg:
                    status_msg = del_msg
                    status_style = "yellow"
                continue

            if (k in (Key.ENTER, "enter")) and tasks:
                selected_task = tasks[selected_idx]
                today_str = datetime.now().date().isoformat()
                _, is_new = self.daily_service.add_task_to_date(
                    selected_task.id, today_str
                )
                if is_new:
                    return f'Added "{selected_task.title}" to today\'s tasks!'
                else:
                    return f'"{selected_task.title}" is already scheduled for today!'

            if k.isdigit() and tasks:
                idx = int(k)
                if 1 <= idx <= len(tasks):
                    selected_task = tasks[idx - 1]
                    today_str = datetime.now().date().isoformat()
                    _, is_new = self.daily_service.add_task_to_date(
                        selected_task.id, today_str
                    )
                    if is_new:
                        return f'Added "{selected_task.title}" to today\'s tasks!'
                    else:
                        return f'"{selected_task.title}" is already scheduled for today!'
                else:
                    status_msg = f"Invalid selection: #{k}."
                    status_style = "red"

    def handle_create_universal_task(self) -> Optional[str]:
        """Prompt to create a new universal task using controlled text input."""
        clear_viewport()
        render_header("NEW UNIVERSAL TASK")
        console.print("Enter task title (or 'b' to cancel):\n")
        title = key_reader.read_text_input("Task: > ")
        if not title or title.lower() in ("b", "back", "cancel"):
            return None

        self.task_service.create_task(
            title=title,
            source_type="universal",
            status="pending",
        )
        return f'Created universal task: "{title}"'

    def handle_delete_universal_task(
        self, tasks: list[Task], current_idx: int = 0
    ) -> Optional[str]:
        """Prompt to permanently delete a universal task."""
        if not tasks:
            return "No universal tasks to delete."

        del_idx = current_idx
        while True:
            clear_viewport()
            render_header("DELETE UNIVERSAL TASK")
            console.print("Select universal task to delete permanently:\n")

            table = Table.grid(padding=(0, 1))
            table.add_column("cursor", justify="right", style="cyan bold", width=2)
            table.add_column("num", justify="right", style="cyan bold", width=4)
            table.add_column("title", style="bold white")

            for idx, t in enumerate(tasks):
                cursor = "▶" if idx == del_idx else " "
                table.add_row(cursor, f"{idx + 1}.", t.title)

            console.print(table)
            console.print()
            render_footer(
                [
                    ("↑/↓", "Navigate"),
                    ("Enter", "Select"),
                    ("b", "Cancel"),
                ]
            )

            k = key_reader.read_key()
            if k in (Key.ESCAPE, "b", "q", "cancel"):
                return None
            elif k == Key.UP:
                del_idx = max(0, del_idx - 1)
            elif k == Key.DOWN:
                del_idx = min(len(tasks) - 1, del_idx + 1)
            elif k in (Key.ENTER, "enter"):
                break
            elif k.isdigit() and 1 <= int(k) <= len(tasks):
                del_idx = int(k) - 1
                break

        selected_task = tasks[del_idx]
        clear_viewport()
        render_header("CONFIRM PERMANENT DELETE", subtitle=selected_task.title)
        console.print(
            f"Permanently delete [bold white]\"{selected_task.title}\"[/bold white] from database?\n"
        )
        render_footer([("y", "Yes, delete"), ("n", "Cancel")])

        k_conf = key_reader.read_key()
        if k_conf.lower() in ("y", "yes", Key.ENTER, "enter"):
            self.task_service.delete_task(selected_task.id)
            return f'Deleted universal task "{selected_task.title}".'
        return "Deletion cancelled."
