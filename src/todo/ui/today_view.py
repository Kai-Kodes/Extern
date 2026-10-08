"""Today view screen and interaction logic for full-screen TUI."""

from typing import List, Optional

from rich import box
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from todo.models import DailyTask
from todo.services.daily_service import DailyService
from todo.services.task_service import TaskService
from todo.ui.console import (
    clear_viewport,
    console,
    format_today_title,
    print_status_message,
    prompt_input,
    render_footer,
    render_header,
)


class TodayView:
    def __init__(self, daily_service: DailyService, task_service: Optional[TaskService] = None):
        self.daily_service = daily_service
        self.task_service = task_service

    def render(
        self,
        tasks: List[DailyTask],
        status_msg: Optional[str] = None,
        status_style: str = "green",
        selected_idx: int = 0,
        clear: bool = True,
    ) -> None:
        """Render the Today screen within the TUI viewport."""
        if clear:
            clear_viewport()

        render_header(format_today_title())

        if status_msg:
            print_status_message(status_msg, style=status_style)

        if not tasks:
            console.print("  [dim italic]No tasks scheduled for today.[/dim italic]\n")
            hints = [
                ("+", "Add task"),
                ("u", "Universal tasks"),
                ("q", "Quit"),
            ]
            render_footer(hints)
        else:
            table = Table.grid(padding=(0, 1))
            table.add_column("cursor", justify="right", style="cyan bold", width=2)
            table.add_column("num", justify="right", style="cyan bold", width=4)
            table.add_column("icon", justify="center", width=3)
            table.add_column("title")

            for idx, dt in enumerate(tasks):
                task_title = dt.task.title if dt.task else f"Task #{dt.task_id}"
                cursor = "▶" if idx == selected_idx else " "

                if dt.is_completed:
                    icon = Text("✓", style="bold green")
                    title = Text(task_title, style="dim strike")
                else:
                    icon = Text("○", style="bright_yellow")
                    title = Text(task_title, style="bold white")

                table.add_row(cursor, f"{idx + 1}.", icon, title)

            console.print(table)
            console.print()

            hints = [
                ("↑/↓", "Navigate"),
                ("Enter / 1-" + str(len(tasks)), "Toggle"),
                ("+", "Add task"),
                ("d", "Delete"),
                ("u", "Universal"),
                ("q", "Quit"),
            ]
            render_footer(hints)

    def handle_delete(self, tasks: List[DailyTask], current_idx: int = 0) -> Optional[str]:
        """Selection-based task deletion workflow respecting task semantics."""
        if not tasks:
            return "No tasks to delete."

        clear_viewport()
        render_header("DELETE TASK")
        console.print("Select task to remove from Today:\n")

        for idx, dt in enumerate(tasks, start=1):
            title = dt.task.title if dt.task else f"Task #{dt.task_id}"
            src_tag = f"[{dt.task.source_type}]" if dt.task else ""
            console.print(f"  [bold cyan]{idx}.[/bold cyan] [bold white]{title}[/bold white] [dim]{src_tag}[/dim]")

        console.print("\n  [bold cyan]\\[b][/bold cyan] Cancel\n")
        choice = prompt_input(f"Select task [1-{len(tasks)}, b]: ")

        if choice.lower() in ("b", "back", "cancel", "q", "quit"):
            return None

        if not choice.isdigit() or not (1 <= int(choice) <= len(tasks)):
            return "Invalid task selection."

        selected_dt = tasks[int(choice) - 1]
        task = selected_dt.task
        title = task.title if task else f"Task #{selected_dt.task_id}"

        # 1. Universal task deletion semantics
        if task and task.source_type == "universal":
            clear_viewport()
            render_header("DELETE UNIVERSAL TASK", subtitle=title)
            console.print(f"Task: [bold white]{title}[/bold white]\n")
            console.print("  [bold cyan]1.[/bold cyan] Remove from Today only [dim](keeps in Universal list)[/dim]")
            console.print("  [bold cyan]2.[/bold cyan] Permanently delete Universal task [dim](removes everywhere)[/dim]\n")
            console.print("  [bold cyan]\\[b][/bold cyan] Cancel\n")

            sub_choice = prompt_input("Option [1-2, b]: ")
            if sub_choice == "1":
                self.daily_service.remove_daily_task(selected_dt.id)
                return f"Removed \"{title}\" from today's schedule."
            elif sub_choice == "2":
                confirm = prompt_input(f"Permanently delete \"{title}\" from database? [y/n]: ")
                if confirm.lower() in ("y", "yes"):
                    if self.task_service:
                        self.task_service.delete_task(task.id)
                    else:
                        self.daily_service.delete_daily_task_and_underlying(selected_dt.id)
                    return f"Permanently deleted \"{title}\"."
                return "Deletion cancelled."
            return None

        # 2. Filemeta task deletion semantics
        elif task and task.source_type == "filemeta":
            clear_viewport()
            render_header("REMOVE FROM TODAY", subtitle=title)
            console.print(f"Remove [bold white]\"{title}\"[/bold white] from today's schedule?")
            console.print("[dim]The course item and file will remain untouched.[/dim]\n")
            confirm = prompt_input("[y/n]: ")
            if confirm.lower() in ("y", "yes"):
                self.daily_service.remove_daily_task(selected_dt.id)
                return f"Removed \"{title}\" from today's schedule."
            return "Cancelled."

        # 3. Manual task deletion semantics
        else:
            clear_viewport()
            render_header("DELETE TASK", subtitle=title)
            console.print(f"Delete [bold white]\"{title}\"[/bold white] from today's schedule?\n")
            confirm = prompt_input("[y/n]: ")
            if confirm.lower() in ("y", "yes"):
                self.daily_service.delete_daily_task_and_underlying(selected_dt.id)
                return f"Deleted \"{title}\"."
            return "Cancelled."
