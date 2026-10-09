"""Today view screen and interaction logic for full-screen TUI."""

from typing import List, Optional

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
    render_footer,
    render_header,
    render_today_header,
)
from todo.ui.key_reader import Key, key_reader


class TodayView:
    def __init__(
        self, daily_service: DailyService, task_service: Optional[TaskService] = None
    ):
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

        total = len(tasks)
        completed = sum(1 for dt in tasks if dt.is_completed)
        render_today_header(format_today_title(), completed=completed, total=total)

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
                    title = Text(task_title, style="green")
                else:
                    icon = Text("○", style="bright_yellow")
                    title = Text(task_title, style="bold white")

                table.add_row(cursor, f"{idx + 1}.", icon, title)

            console.print(table)
            console.print()

            hints = [
                ("↑/↓", "Navigate"),
                ("Enter", "Toggle"),
                ("+", "Add task"),
                ("d", "Delete"),
                ("u", "Universal"),
                ("q", "Quit"),
            ]
            render_footer(hints)

    def handle_delete(
        self, tasks: List[DailyTask], current_idx: int = 0
    ) -> Optional[str]:
        """Selection-based task deletion workflow respecting task semantics."""
        if not tasks:
            return None

        del_idx = current_idx
        while True:
            clear_viewport()
            render_header("DELETE TASK")
            console.print("Select task to remove from Today:\n")

            table = Table.grid(padding=(0, 1))
            table.add_column("cursor", justify="right", style="cyan bold", width=2)
            table.add_column("num", justify="right", style="cyan bold", width=4)
            table.add_column("title", style="bold white")
            table.add_column("tag", style="dim")

            for idx, dt in enumerate(tasks):
                cursor = "▶" if idx == del_idx else " "
                title = dt.task.title if dt.task else f"Task #{dt.task_id}"
                src_tag = f"[{dt.task.source_type}]" if dt.task else ""
                table.add_row(cursor, f"{idx + 1}.", title, src_tag)

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
            elif k == Key.ENTER:
                break
            elif k.isdigit() and 1 <= int(k) <= len(tasks):
                del_idx = int(k) - 1
                break

        selected_dt = tasks[del_idx]
        task = selected_dt.task
        title = task.title if task else f"Task #{selected_dt.task_id}"

        # 1. Universal task deletion semantics
        if task and task.source_type == "universal":
            clear_viewport()
            render_header("DELETE UNIVERSAL TASK", subtitle=title)
            console.print(f"Task: [bold white]{title}[/bold white]\n")
            console.print(
                "  [bold cyan]1.[/bold cyan] Remove from Today only [dim](keeps in Universal list)[/dim]"
            )
            console.print(
                "  [bold cyan]2.[/bold cyan] Permanently delete Universal task [dim](removes everywhere)[/dim]\n"
            )
            render_footer([("1", "Remove from Today"), ("2", "Delete permanently"), ("b", "Cancel")])

            sub_k = key_reader.read_key()
            if sub_k == "1":
                ok = self.daily_service.remove_daily_task(selected_dt.id)
                if not ok:
                    return "Unable to remove task from today's schedule."
                return None
            elif sub_k == "2":
                clear_viewport()
                render_header("CONFIRM PERMANENT DELETE", subtitle=title)
                console.print(
                    f"Permanently delete [bold white]\"{title}\"[/bold white] from database?\n"
                )
                render_footer([("y", "Yes, delete"), ("n", "Cancel")])
                conf = key_reader.read_key()
                if conf.lower() in ("y", "yes", Key.ENTER):
                    if self.task_service:
                        ok = self.task_service.delete_task(task.id)
                    else:
                        ok = self.daily_service.delete_daily_task_and_underlying(
                            selected_dt.id
                        )
                    if not ok:
                        return "Unable to delete universal task."
                    return None
                return None
            return None

        # 2. Filemeta / Telegram task deletion semantics
        elif task and task.source_type in ("filemeta", "telegram"):
            clear_viewport()
            render_header("REMOVE FROM TODAY", subtitle=title)
            console.print(
                f"Remove [bold white]\"{title}\"[/bold white] from today\'s schedule?"
            )
            console.print(
                "[dim]The course item will remain untouched.[/dim]\n"
            )
            render_footer([("y", "Yes, remove"), ("n", "Cancel")])
            conf = key_reader.read_key()
            if conf.lower() in ("y", "yes", Key.ENTER):
                ok = self.daily_service.remove_daily_task(selected_dt.id)
                if not ok:
                    return "Unable to remove task from today's schedule."
                return None
            return None

        # 3. Manual task deletion semantics
        else:
            clear_viewport()
            render_header("DELETE TASK", subtitle=title)
            console.print(
                f"Delete [bold white]\"{title}\"[/bold white] from today\'s schedule?\n"
            )
            render_footer([("y", "Yes, delete"), ("n", "Cancel")])
            conf = key_reader.read_key()
            if conf.lower() in ("y", "yes", Key.ENTER):
                ok = self.daily_service.delete_daily_task_and_underlying(selected_dt.id)
                if not ok:
                    return "Unable to delete task."
                return None
            return None
