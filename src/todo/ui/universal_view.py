"""Universal task list view with keyboard navigation and deletion."""

from datetime import datetime
from typing import Any, Optional, Set

from rich.table import Table
from rich.text import Text

from todo.models import Task
from todo.search import LiveSearchController
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
    def __init__(
        self,
        task_service: TaskService,
        daily_service: DailyService,
        task_editor: Optional[Any] = None,
        sync_manager: Optional[Any] = None,
    ):
        self.task_service = task_service
        self.daily_service = daily_service
        self.task_editor = task_editor
        self.sync_manager = sync_manager

    def run(
        self, select_mode: bool = True, target_date: Optional[str] = None
    ) -> Optional[str]:
        """Display Universal tasks in the TUI viewport with keyboard navigation and live search."""
        status_msg: Optional[str] = None
        status_style: str = "green"
        selected_idx = 0
        search = LiveSearchController[Task]()
        selected_task_ids: Set[int] = set()
        selected_schedule_ids: Set[int] = set()
        delete_mode = False

        while True:
            clear_viewport()
            subtitle = f"Schedule for {target_date}" if target_date else "Manage backlog tasks"
            if delete_mode:
                subtitle = f"DELETE MODE · {len(selected_task_ids)} selected"
            render_header("UNIVERSAL TASKS", subtitle=subtitle)

            tasks = self.task_service.get_universal_tasks()

            # Clean up stale selected ids
            valid_ids = {t.id for t in tasks}
            selected_task_ids.intersection_update(valid_ids)
            selected_schedule_ids.intersection_update(valid_ids)

            if status_msg:
                print_status_message(status_msg, style=status_style)
                status_msg = None
                status_style = "green"

            if search.is_active:
                console.print(f"  Search: [bold cyan]{search.query}[/bold cyan]█\n")
            elif search.query:
                console.print(f"  Search: [bold cyan]{search.query}[/bold cyan] [dim](filter active, Esc to clear)[/dim]\n")

            active_tasks = search.filter(tasks)

            if not active_tasks:
                if search.query:
                    console.print("  [dim italic]No matching universal tasks found.[/dim italic]\n")
                else:
                    console.print(
                        "  [dim italic]No universal tasks created yet.[/dim italic]\n"
                    )
                if search.is_active:
                    hints = [("Enter", "Done"), ("Esc", "Cancel / Clear")]
                elif delete_mode:
                    hints = [("Esc", "Cancel delete mode")]
                else:
                    hints = [("+", "Add universal task"), ("/", "Search"), ("b", "Back")]
                render_footer(hints)
            else:
                if selected_idx >= len(active_tasks):
                    selected_idx = max(0, len(active_tasks) - 1)

                show_checkbox = delete_mode or bool(selected_schedule_ids)

                table = Table.grid(padding=(0, 1))
                table.add_column("cursor", justify="right", style="cyan bold", width=2)
                if show_checkbox:
                    table.add_column("select", justify="center", width=4)
                table.add_column("num", justify="right", style="cyan bold", width=4)
                table.add_column("title")

                for idx, t in enumerate(active_tasks):
                    cursor = "▶" if idx == selected_idx else " "
                    row_items = [cursor]
                    if show_checkbox:
                        if delete_mode:
                            is_sel = t.id in selected_task_ids
                        else:
                            is_sel = t.id in selected_schedule_ids
                        chk = Text("[x]", style="bold cyan") if is_sel else Text("[ ]", style="dim")
                        row_items.append(chk)
                    row_items.extend([f"{idx + 1}.", Text(t.title, style="bold white")])
                    table.add_row(*row_items)

                console.print(table)
                console.print()

                if delete_mode:
                    hints = [
                        ("↑/↓", "Navigate"),
                        ("Space", "Select"),
                        ("Enter", f"Confirm delete ({len(selected_task_ids)})" if selected_task_ids else "Confirm delete"),
                        ("Esc", "Cancel"),
                    ]
                elif search.is_active:
                    hints = [
                        ("↑/↓", "Navigate"),
                        ("Enter", "Done"),
                        ("Esc", "Cancel / Clear"),
                    ]
                elif selected_schedule_ids:
                    hints = [
                        ("↑/↓", "Navigate"),
                        ("Space", "Select / Deselect"),
                        ("Enter", f"Add to Today ({len(selected_schedule_ids)})"),
                        ("+", "Add task"),
                        ("d", "Delete"),
                        ("Esc", "Clear selection"),
                        ("b", "Back"),
                    ]
                elif search.query:
                    hints = [
                        ("↑/↓", "Navigate"),
                        ("Space", "Select"),
                        ("Enter", "Add to Today"),
                        ("+", "Add task"),
                        ("d", "Delete"),
                        ("/", "Search"),
                        ("Esc", "Clear search"),
                        ("b", "Back"),
                    ]
                else:
                    hints = [
                        ("↑/↓", "Navigate"),
                        ("Space", "Select"),
                        ("Enter", "Add to Today"),
                        ("+", "Add task"),
                        ("e", "Edit"),
                        ("d", "Delete"),
                        ("/", "Search"),
                        ("b", "Back"),
                    ]
                render_footer(hints)

            k = key_reader.read_key()

            if delete_mode:
                if k in (Key.ESCAPE, "escape", "cancel"):
                    selected_task_ids.clear()
                    delete_mode = False
                    continue

                if k == Key.UP and active_tasks:
                    selected_idx = max(0, selected_idx - 1)
                    continue

                if k == Key.DOWN and active_tasks:
                    selected_idx = min(len(active_tasks) - 1, selected_idx + 1)
                    continue

                if k in (Key.SPACE, "space", " ") and active_tasks:
                    target_task = active_tasks[selected_idx]
                    if target_task.id in selected_task_ids:
                        selected_task_ids.remove(target_task.id)
                    else:
                        selected_task_ids.add(target_task.id)
                    continue

                if k in (Key.ENTER, "enter"):
                    if not selected_task_ids:
                        continue
                    clear_viewport()
                    render_header("CONFIRM BULK DELETE")
                    console.print(
                        f"Permanently delete [bold white]{len(selected_task_ids)} universal tasks[/bold white] from database?\n"
                    )
                    render_footer([("y", "Yes, delete"), ("n", "Cancel")])
                    k_conf = key_reader.read_key()
                    if k_conf.lower() in ("y", "yes", Key.ENTER, "enter"):
                        self.task_service.delete_tasks_bulk(list(selected_task_ids))
                    selected_task_ids.clear()
                    delete_mode = False
                    continue

                # Ignore all other keys in delete mode
                continue

            handled, selected_idx = search.handle_key(k, len(active_tasks), selected_idx)
            if handled:
                continue

            if k in (Key.ESCAPE, "escape"):
                if selected_schedule_ids:
                    selected_schedule_ids.clear()
                    continue
                return None

            if k in ("b", "q", "cancel"):
                return None

            if k == Key.UP and active_tasks:
                selected_idx = max(0, selected_idx - 1)
                continue

            if k == Key.DOWN and active_tasks:
                selected_idx = min(len(active_tasks) - 1, selected_idx + 1)
                continue

            if k in (Key.SPACE, "space", " ") and active_tasks:
                target_task = active_tasks[selected_idx]
                if target_task.id in selected_schedule_ids:
                    selected_schedule_ids.remove(target_task.id)
                else:
                    selected_schedule_ids.add(target_task.id)
                continue

            if k in ("+", Key.PLUS, "add"):
                err_msg = self.handle_create_universal_task()
                if err_msg:
                    status_msg = err_msg
                    status_style = "red"
                continue

            if k in ("e", "edit") and active_tasks and self.task_editor:
                target_task = active_tasks[selected_idx]
                updated, msg = self.task_editor.edit_task(target_task)
                if updated:
                    status_msg = msg
                    status_style = "green"
                elif msg:
                    status_msg = msg
                    status_style = "yellow"
                continue

            if k in ("d", "delete") and active_tasks:
                delete_mode = True
                selected_task_ids.clear()
                selected_schedule_ids.clear()
                continue

            if (k in (Key.ENTER, "enter")) and active_tasks:
                dest_date = target_date or self.daily_service.get_today_str()
                if selected_schedule_ids:
                    for t_id in selected_schedule_ids:
                        self.daily_service.add_task_to_date(t_id, dest_date)
                    selected_schedule_ids.clear()
                else:
                    selected_task = active_tasks[selected_idx]
                    self.daily_service.add_task_to_date(
                        selected_task.id, dest_date
                    )
                return None

            if k.isdigit() and active_tasks:
                idx = int(k)
                if 1 <= idx <= len(active_tasks):
                    selected_task = active_tasks[idx - 1]
                    dest_date = target_date or self.daily_service.get_today_str()
                    self.daily_service.add_task_to_date(
                        selected_task.id, dest_date
                    )
                    return None
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
        return None

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
            return None
        return None
