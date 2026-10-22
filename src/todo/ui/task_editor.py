"""Interactive full-screen task editor modal for modifying title, notes, and scheduled date."""

from datetime import datetime
from typing import Any, Optional, Tuple

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


class TaskEditor:
    """Reusable, keyboard-driven task editor accessible throughout Extern."""

    def __init__(
        self,
        task_service: TaskService,
        daily_service: DailyService,
        google_tasks_service: Optional[Any] = None,
        google_calendar_service: Optional[Any] = None,
    ):
        self.task_service = task_service
        self.daily_service = daily_service
        self.google_tasks_service = google_tasks_service
        self.google_calendar_service = google_calendar_service

    def edit_task(
        self,
        task: Task,
        current_date: Optional[str] = None,
    ) -> Tuple[bool, str]:
        """
        Open the task editor for an existing task.
        Prepopulates title, notes/description, and scheduled date with current values.
        Returns: (saved: bool, message: str)
        """
        title = task.title
        description = task.description or ""
        initial_title = title
        initial_desc = description

        # Resolve current scheduled date
        if current_date:
            scheduled_date: Optional[str] = current_date
        else:
            scheduled_date = self.daily_service.get_task_scheduled_date(task.id)
        initial_date = scheduled_date

        is_completed = (task.status == "completed")
        selected_idx = 0
        status_msg: Optional[str] = None
        status_style = "green"

        while True:
            clear_viewport()
            render_header(f"EDIT TASK #{task.id}", subtitle=title[:40])

            if status_msg:
                print_status_message(status_msg, style=status_style)
                status_msg = None

            # Render form fields table
            table = Table.grid(padding=(0, 2))
            table.add_column("cursor", justify="right", style="cyan bold", width=2)
            table.add_column("num", justify="right", style="cyan bold", width=4)
            table.add_column("label", style="bold white", width=18)
            table.add_column("value", style="bold cyan")

            date_display = scheduled_date or "[dim](none / unscheduled)[/dim]"
            if is_completed:
                date_display = f"{date_display} [yellow](completed · date locked)[/yellow]"

            desc_display = (
                description.replace("\r\n", "\n").replace("\n", " ⏎ ")
                if description
                else "[dim](none)[/dim]"
            )
            # Truncate description preview if very long
            if len(desc_display) > 60:
                desc_display = desc_display[:57] + "..."

            fields = [
                ("1.", "Title:", title),
                ("2.", "Description / Notes:", desc_display),
                ("3.", "Scheduled Date:", date_display),
                ("s.", "[bold green]Save Changes[/bold green]", ""),
                ("b.", "[bold red]Cancel[/bold red]", ""),
            ]

            for idx, (num, label, val) in enumerate(fields):
                cursor = "▶" if idx == selected_idx else " "
                table.add_row(cursor, num, label, val)

            console.print(table)
            console.print()

            render_footer(
                [
                    ("↑/↓", "Navigate"),
                    ("Enter", "Edit field / Select"),
                    ("1-3", "Quick edit"),
                    ("s", "Save"),
                    ("b / Esc", "Cancel"),
                ]
            )

            k = key_reader.read_key()

            if k in (Key.ESCAPE, "b", "q", "cancel"):
                return False, "Edit cancelled."

            if k in (Key.UP, "up"):
                selected_idx = max(0, selected_idx - 1)
                continue

            if k in (Key.DOWN, "down"):
                selected_idx = min(len(fields) - 1, selected_idx + 1)
                continue

            if k in ("s", "S"):
                # Save action
                ok, err = self._validate_and_save(
                    task=task,
                    title=title,
                    description=description,
                    scheduled_date=scheduled_date,
                    initial_title=initial_title,
                    initial_desc=initial_desc,
                    initial_date=initial_date,
                    is_completed=is_completed,
                )
                if ok:
                    return True, f"Updated task #{task.id} ('{title}')."
                else:
                    status_msg = err
                    status_style = "red"
                    continue

            action_field: Optional[int] = None
            if k == "1":
                action_field = 0
            elif k == "2":
                action_field = 1
            elif k == "3":
                action_field = 2
            elif k in (Key.ENTER, "enter"):
                action_field = selected_idx

            if action_field == 0:
                # Edit Title - prepopulate current title
                console.print(
                    "\n[bold cyan]Edit Task Title[/bold cyan] (Enter to keep current, '-' to clear):"
                )
                raw_title = key_reader.read_text_input("Title: ", prefill=title)
                clean_title = raw_title.strip()
                if clean_title in ("-", "clear"):
                    title = ""
                    status_msg = "Title cleared. Title cannot be empty to save."
                    status_style = "yellow"
                elif clean_title:
                    title = clean_title
                continue

            elif action_field == 1:
                # Edit Description - prepopulate current description (encode newlines as \n)
                console.print(
                    "\n[bold cyan]Edit Task Description / Notes[/bold cyan] (Enter to keep, '-' to clear, '\\n' for newlines):"
                )
                prefill_desc = description.replace("\r\n", "\\n").replace("\n", "\\n")
                raw_desc = key_reader.read_text_input("Description: ", prefill=prefill_desc)
                clean_desc = raw_desc.strip()
                if clean_desc in ("-", "clear", "none"):
                    description = ""
                elif raw_desc == prefill_desc:
                    # Preserved exactly as original
                    pass
                else:
                    description = raw_desc.replace("\\n", "\n")
                continue

            elif action_field == 2:
                # Edit Scheduled Date - prepopulate current date or empty if unscheduled
                if is_completed:
                    status_msg = "Historical completed tasks cannot be rescheduled."
                    status_style = "yellow"
                    continue

                console.print(
                    "\n[bold cyan]Edit Scheduled Date (YYYY-MM-DD)[/bold cyan] (Enter to keep, '-' or blank to unschedule):"
                )
                prefill_date = scheduled_date or ""
                raw_date = key_reader.read_text_input("Date (YYYY-MM-DD): ", prefill=prefill_date)
                clean_d = raw_date.strip()
                if clean_d in ("", "-", "none", "clear", "unschedule"):
                    scheduled_date = None
                elif clean_d:
                    scheduled_date = clean_d
                    try:
                        datetime.strptime(clean_d, "%Y-%m-%d")
                    except ValueError:
                        status_msg = f"Invalid date: '{clean_d}'. Must be YYYY-MM-DD (e.g. 2026-10-25)."
                        status_style = "red"
                continue

            elif action_field == 3:
                # Save Changes selected via Enter
                ok, err = self._validate_and_save(
                    task=task,
                    title=title,
                    description=description,
                    scheduled_date=scheduled_date,
                    initial_title=initial_title,
                    initial_desc=initial_desc,
                    initial_date=initial_date,
                    is_completed=is_completed,
                )
                if ok:
                    return True, f"Updated task #{task.id} ('{title}')."
                else:
                    status_msg = err
                    status_style = "red"
                    continue

            elif action_field == 4:
                # Cancel selected via Enter
                return False, "Edit cancelled."

    def create_task(
        self,
        target_date: Optional[str] = None,
        source_type: str = "manual",
    ) -> Tuple[bool, str, Optional[Task]]:
        """
        Open the task editor for creating a new task from scratch.
        Always starts with empty fields (does not inherit or display stale values from previously edited tasks).
        Returns: (saved: bool, message: str, created_task: Optional[Task])
        """
        title = ""
        description = ""
        scheduled_date = target_date

        selected_idx = 0
        status_msg: Optional[str] = None
        status_style = "green"

        while True:
            clear_viewport()
            render_header("CREATE TASK", subtitle="New Task")

            if status_msg:
                print_status_message(status_msg, style=status_style)
                status_msg = None

            table = Table.grid(padding=(0, 2))
            table.add_column("cursor", justify="right", style="cyan bold", width=2)
            table.add_column("num", justify="right", style="cyan bold", width=4)
            table.add_column("label", style="bold white", width=18)
            table.add_column("value", style="bold cyan")

            date_display = scheduled_date or "[dim](none / unscheduled)[/dim]"
            desc_display = (
                description.replace("\r\n", "\n").replace("\n", " ⏎ ")
                if description
                else "[dim](none)[/dim]"
            )
            if len(desc_display) > 60:
                desc_display = desc_display[:57] + "..."

            title_display = title if title else "[dim](required)[/dim]"

            fields = [
                ("1.", "Title:", title_display),
                ("2.", "Description / Notes:", desc_display),
                ("3.", "Scheduled Date:", date_display),
                ("s.", "[bold green]Create Task[/bold green]", ""),
                ("b.", "[bold red]Cancel[/bold red]", ""),
            ]

            for idx, (num, label, val) in enumerate(fields):
                cursor = "▶" if idx == selected_idx else " "
                table.add_row(cursor, num, label, val)

            console.print(table)
            console.print()

            render_footer(
                [
                    ("↑/↓", "Navigate"),
                    ("Enter", "Edit field / Select"),
                    ("1-3", "Quick edit"),
                    ("s", "Create"),
                    ("b / Esc", "Cancel"),
                ]
            )

            k = key_reader.read_key()

            if k in (Key.ESCAPE, "b", "q", "cancel"):
                return False, "Task creation cancelled.", None

            if k in (Key.UP, "up"):
                selected_idx = max(0, selected_idx - 1)
                continue

            if k in (Key.DOWN, "down"):
                selected_idx = min(len(fields) - 1, selected_idx + 1)
                continue

            if k in ("s", "S"):
                ok, err, new_task = self._validate_and_create(
                    title=title,
                    description=description,
                    scheduled_date=scheduled_date,
                    source_type=source_type,
                )
                if ok:
                    return True, f"Created task '{title}'.", new_task
                else:
                    status_msg = err
                    status_style = "red"
                    continue

            action_field: Optional[int] = None
            if k == "1":
                action_field = 0
            elif k == "2":
                action_field = 1
            elif k == "3":
                action_field = 2
            elif k in (Key.ENTER, "enter"):
                action_field = selected_idx

            if action_field == 0:
                console.print("\n[bold cyan]Task Title[/bold cyan]:")
                raw_title = key_reader.read_text_input("Title: ", prefill=title)
                clean_title = raw_title.strip()
                if clean_title in ("-", "clear"):
                    title = ""
                else:
                    title = clean_title
                continue

            elif action_field == 1:
                console.print(
                    "\n[bold cyan]Task Description / Notes[/bold cyan] (optional, '\\n' for newlines):"
                )
                prefill_desc = description.replace("\r\n", "\\n").replace("\n", "\\n")
                raw_desc = key_reader.read_text_input("Description: ", prefill=prefill_desc)
                clean_desc = raw_desc.strip()
                if clean_desc in ("-", "clear", "none"):
                    description = ""
                elif raw_desc == prefill_desc:
                    pass
                else:
                    description = raw_desc.replace("\\n", "\n")
                continue

            elif action_field == 2:
                console.print(
                    "\n[bold cyan]Scheduled Date (YYYY-MM-DD)[/bold cyan] (optional, Enter for none):"
                )
                raw_date = key_reader.read_text_input("Date (YYYY-MM-DD): ", prefill=scheduled_date or "")
                clean_d = raw_date.strip()
                if clean_d in ("", "-", "none", "clear", "unschedule"):
                    scheduled_date = None
                elif clean_d:
                    scheduled_date = clean_d
                    try:
                        datetime.strptime(clean_d, "%Y-%m-%d")
                    except ValueError:
                        status_msg = f"Invalid date: '{clean_d}'. Must be YYYY-MM-DD (e.g. 2026-10-25)."
                        status_style = "red"
                continue

            elif action_field == 3:
                ok, err, new_task = self._validate_and_create(
                    title=title,
                    description=description,
                    scheduled_date=scheduled_date,
                    source_type=source_type,
                )
                if ok:
                    return True, f"Created task '{title}'.", new_task
                else:
                    status_msg = err
                    status_style = "red"
                    continue

            elif action_field == 4:
                return False, "Task creation cancelled.", None

    def _validate_and_create(
        self,
        title: str,
        description: str,
        scheduled_date: Optional[str],
        source_type: str = "manual",
    ) -> Tuple[bool, Optional[str], Optional[Task]]:
        """Validate inputs and persist a new task."""
        clean_title = title.strip()
        if not clean_title:
            return False, "Task title cannot be empty.", None

        clean_date = scheduled_date.strip() if scheduled_date else None
        if clean_date:
            try:
                datetime.strptime(clean_date, "%Y-%m-%d")
            except ValueError:
                return False, f"Invalid date: '{clean_date}'. Format must be YYYY-MM-DD.", None

        new_task = self.task_service.create_task(
            title=clean_title,
            description=description.strip() if description else "",
            source_type=source_type,
            status="pending",
        )

        if clean_date:
            self.daily_service.add_task_to_date(new_task.id, clean_date)

        return True, None, new_task

    def _validate_and_save(
        self,
        task: Task,
        title: str,
        description: str,
        scheduled_date: Optional[str],
        initial_title: str,
        initial_desc: str,
        initial_date: Optional[str],
        is_completed: bool,
    ) -> Tuple[bool, Optional[str]]:
        """Validate inputs and persist updates across tasks, daily_tasks, and Google Tasks."""
        clean_title = title.strip()
        if not clean_title:
            return False, "Task title cannot be empty."

        clean_date = scheduled_date.strip() if scheduled_date else None
        if clean_date:
            try:
                datetime.strptime(clean_date, "%Y-%m-%d")
            except ValueError:
                return False, f"Invalid date: '{clean_date}'. Format must be YYYY-MM-DD."

        # If completed task, ensure date wasn't altered
        if is_completed and clean_date != initial_date:
            return False, "Cannot change scheduled date of historical completed tasks."

        title_changed = (clean_title != initial_title)
        desc_changed = (description != initial_desc)
        date_changed = (clean_date != initial_date)

        # Check if Google Tasks service should handle cloud sync
        is_google = (task.source_type == "google_tasks")
        if is_google and self.google_tasks_service:
            clear_due = (clean_date is None and initial_date is not None)
            new_due = clean_date if date_changed else None
            try:
                self.google_tasks_service.update_task(
                    task_id=task.id,
                    title=clean_title if title_changed else None,
                    notes=description if desc_changed else None,
                    due_date=new_due,
                    clear_due=clear_due,
                )
            except Exception as e:
                return False, f"Google Tasks update failed: {e}"
        else:
            # Update local task only for modified fields
            if title_changed or desc_changed:
                self.task_service.update_task(
                    task_id=task.id,
                    title=clean_title if title_changed else None,
                    description=description if desc_changed else None,
                )
            # Update schedule only if date changed
            if date_changed:
                if clean_date is None:
                    self.daily_service.unschedule_task(task.id)
                else:
                    self.daily_service.reschedule_task(task.id, clean_date)

        return True, None
