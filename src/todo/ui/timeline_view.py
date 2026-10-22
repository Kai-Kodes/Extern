"""Timeline view for navigating past completions, today, and future scheduled tasks."""

import calendar
from datetime import date, datetime, timedelta
from typing import Any, Callable, List, Optional

from rich.table import Table
from rich.text import Text

from todo.models import DailyTask
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


class TimelineView:
    def __init__(
        self,
        daily_service: DailyService,
        task_service: TaskService,
        add_task_menu_fn: Optional[Callable[[Optional[str]], None]] = None,
        date_provider: Optional[Callable[[], datetime]] = None,
        task_editor: Optional[Any] = None,
        sync_manager: Optional[Any] = None,
    ):
        self.daily_service = daily_service
        self.task_service = task_service
        self.add_task_menu_fn = add_task_menu_fn
        self.date_provider = date_provider
        self.task_editor = task_editor
        self.sync_manager = sync_manager

    def get_today(self) -> date:
        """Return today's date from daily_service or injected clock."""
        today_str = self.daily_service.get_today_str()
        return datetime.strptime(today_str, "%Y-%m-%d").date()

    def _shift_month(self, dt: date, months: int) -> date:
        """Shift a date by a given number of months with month-end boundary clamping."""
        year = dt.year + (dt.month + months - 1) // 12
        month = (dt.month + months - 1) % 12 + 1
        max_day = calendar.monthrange(year, month)[1]
        day = min(dt.day, max_day)
        return date(year, month, day)

    def _parse_date(self, date_input: str) -> Optional[date]:
        """Parse YYYY-MM-DD date string, returning None if invalid."""
        try:
            return datetime.strptime(date_input.strip(), "%Y-%m-%d").date()
        except ValueError:
            return None

    def run(self, initial_date: Optional[str] = None) -> Optional[str]:
        """Main interaction loop for the Timeline view."""
        if initial_date:
            parsed = self._parse_date(initial_date)
            cur_date = parsed if parsed else self.get_today()
        else:
            cur_date = self.get_today()

        status_msg: Optional[str] = None
        status_style: str = "green"
        selected_idx = 0

        # Live search over DailyTask objects by task title and description
        search = LiveSearchController[DailyTask](
            key_fn=lambda dt: (
                f"{dt.task.title} {dt.task.description or ''}" if dt.task else ""
            )
        )

        while True:
            date_str = cur_date.isoformat()
            timeline_data = self.daily_service.get_timeline_data_for_date(date_str)
            domain = timeline_data["domain"]
            tasks: List[DailyTask] = timeline_data["tasks"]
            completed_cnt = timeline_data["completed_count"]
            total_cnt = timeline_data["total_count"]
            pct = timeline_data["progress_pct"]

            clear_viewport()

            # Format subtitle with domain tag
            formatted_date_title = cur_date.strftime("%A, %b %d, %Y")
            if domain == "past":
                domain_tag = "[dim yellow][PAST][/dim yellow]"
            elif domain == "today":
                domain_tag = "[bold green][TODAY][/bold green]"
            else:
                domain_tag = "[bold cyan][FUTURE][/bold cyan]"

            render_header(
                "TIMELINE",
                subtitle=f"{formatted_date_title} · {domain_tag}",
            )

            # Domain-specific progress and status banners
            if status_msg:
                print_status_message(status_msg, style=status_style)
                status_msg = None
                status_style = "green"

            if domain == "past":
                console.print(
                    f"  [dim]Completed on this day:[/dim] [bold white]{completed_cnt}[/bold white]\n"
                )
            elif domain == "today":
                console.print(
                    f"  [dim]Progress:[/dim] [bold green]{completed_cnt}/{total_cnt}[/bold green] "
                    f"[dim]({pct}%)[/dim]\n"
                )
            else:
                console.print(
                    f"  [dim]Tasks scheduled:[/dim] [bold cyan]{len(tasks)}[/bold cyan]\n"
                )

            # Search banner
            if search.is_active:
                console.print(f"  Search: [bold cyan]{search.query}[/bold cyan]█\n")
            elif search.query:
                console.print(
                    f"  Search: [bold cyan]{search.query}[/bold cyan] [dim](filter active, Esc to clear)[/dim]\n"
                )

            # Filter tasks
            active_tasks = search.filter(tasks)

            # Clamp selection index
            if active_tasks:
                selected_idx = max(0, min(selected_idx, len(active_tasks) - 1))
            else:
                selected_idx = 0

            # Render tasks table
            if not active_tasks:
                if search.query:
                    console.print("  [dim italic]No matching tasks found on this date.[/dim italic]\n")
                elif domain == "past":
                    console.print("  [dim italic]No tasks completed on this date.[/dim italic]\n")
                elif domain == "today":
                    console.print(
                        "  [dim italic]No tasks scheduled for today. Press '+' to add a task.[/dim italic]\n"
                    )
                else:
                    console.print(
                        "  [dim italic]No tasks scheduled for this date. Press '+' to schedule a task.[/dim italic]\n"
                    )
            else:
                table = Table.grid(padding=(0, 1))
                table.add_column("cursor", justify="right", style="cyan bold", width=2)
                table.add_column("num", justify="right", style="cyan bold", width=4)
                table.add_column("icon", justify="center", width=3)
                table.add_column("title")
                table.add_column("source", justify="right", style="dim", width=14)
                if domain == "past":
                    table.add_column("time", justify="right", style="dim", width=12)

                for idx, dt in enumerate(active_tasks):
                    task_title = dt.task.title if dt.task else f"Task #{dt.task_id}"
                    cursor = "▶" if idx == selected_idx else " "

                    if dt.is_completed:
                        icon = Text("✓", style="bold green")
                        title = Text(task_title, style="green")
                    else:
                        icon = Text("○", style="bright_yellow")
                        title = Text(task_title, style="bold white")

                    source_name = f"[{dt.task.source_type}]" if dt.task else "[manual]"

                    row_items = [cursor, f"{idx + 1}.", icon, title, source_name]

                    if domain == "past":
                        # Parse time from completed_at ISO string
                        completed_time = ""
                        if dt.completed_at and "T" in dt.completed_at:
                            time_part = dt.completed_at.split("T")[1]
                            completed_time = time_part[:8]
                        elif dt.completed_at and " " in dt.completed_at:
                            completed_time = dt.completed_at.split(" ")[1][:8]
                        row_items.append(completed_time)

                    table.add_row(*row_items)

                console.print(table)
                console.print()

            # Render contextual footer
            sync_status = self.sync_manager.get_status_text() if getattr(self, "sync_manager", None) else None
            if search.is_active:
                render_footer([("Enter", "Done"), ("Esc", "Cancel / Clear")], status=sync_status)
            else:
                hints = [
                    ("←/→", "Day"),
                    ("[ / ]", "Week"),
                    ("PgUp/PgDn", "Month"),
                ]
                if domain == "past":
                    hints.extend(
                        [
                            ("Enter", "Inspect"),
                            ("e", "Edit"),
                            ("t", "Today"),
                            ("g", "Go to date"),
                            ("/", "Search"),
                            ("b", "Back"),
                        ]
                    )
                elif domain == "today":
                    hints.extend(
                        [
                            ("+", "Add task"),
                            ("Enter", "Toggle"),
                            ("e", "Edit"),
                            ("g", "Go to date"),
                            ("/", "Search"),
                            ("b", "Back"),
                        ]
                    )
                else:
                    hints.extend(
                        [
                            ("+", "Schedule"),
                            ("Enter", "Manage"),
                            ("e", "Edit"),
                            ("t", "Today"),
                            ("g", "Go to date"),
                            ("/", "Search"),
                            ("b", "Back"),
                        ]
                    )
                render_footer(hints, status=sync_status)

            # Read keyboard event
            k = key_reader.read_key()

            # Handle live search input if search is active
            if search.is_active:
                handled, selected_idx = search.handle_key(
                    k, len(active_tasks), selected_idx
                )
                if handled:
                    continue

            # Clear search filter on Escape when search query exists
            if k in (Key.ESCAPE, "escape") and search.query:
                search.query = ""
                continue

            # Exit Timeline on back / escape / quit
            if k in (Key.ESCAPE, "b", "back", "q", "quit"):
                return None

            # Day navigation: Left / Right
            if k in (Key.LEFT, "left", "h"):
                cur_date -= timedelta(days=1)
                selected_idx = 0
                continue

            if k in (Key.RIGHT, "right"):
                cur_date += timedelta(days=1)
                selected_idx = 0
                continue

            # Week navigation: [ / ]
            if k == "[":
                cur_date -= timedelta(days=7)
                selected_idx = 0
                continue

            if k == "]":
                cur_date += timedelta(days=7)
                selected_idx = 0
                continue

            # Month navigation: PageUp / PageDown with boundary clamping
            if k in (Key.PAGE_UP, "pageup", "page_up"):
                cur_date = self._shift_month(cur_date, -1)
                selected_idx = 0
                continue

            if k in (Key.PAGE_DOWN, "pagedown", "page_down"):
                cur_date = self._shift_month(cur_date, 1)
                selected_idx = 0
                continue

            # Jump to today: 't'
            if k.lower() in ("t", "today"):
                cur_date = self.get_today()
                selected_idx = 0
                continue

            # Direct date jump: 'g'
            if k.lower() in ("g", "goto"):
                target_date, err = self._prompt_go_to_date()
                if target_date:
                    cur_date = target_date
                    selected_idx = 0
                elif err:
                    status_msg = err
                    status_style = "bold red"
                continue

            # Search activation: '/'
            if k == "/":
                search.is_active = True
                continue

            # List navigation: Up / Down
            if k in (Key.UP, "up") and active_tasks:
                selected_idx = max(0, selected_idx - 1)
                continue

            if k in (Key.DOWN, "down") and active_tasks:
                selected_idx = min(len(active_tasks) - 1, selected_idx + 1)
                continue

            # Add / Schedule task: '+'
            if k in ("+", Key.PLUS, "add"):
                if domain == "past":
                    status_msg = "Cannot schedule tasks to past dates. Historical records are read-only."
                    status_style = "bold yellow"
                else:
                    if self.add_task_menu_fn:
                        self.add_task_menu_fn(date_str)
                    selected_idx = 0
                continue

            # Task action: Enter
            if k in (Key.ENTER, "enter") and active_tasks:
                selected_dt = active_tasks[selected_idx]
                if domain == "past":
                    self._show_task_details(selected_dt)
                elif domain == "today":
                    self.daily_service.toggle_task_completion(selected_dt.id)
                else:
                    msg = self._manage_future_task(selected_dt, date_str)
                    if msg:
                        status_msg = msg
                continue

            # Edit task: 'e'
            if k in ("e", "edit") and active_tasks and self.task_editor:
                selected_dt = active_tasks[selected_idx]
                updated, msg = self.task_editor.edit_task(
                    selected_dt.task, current_date=date_str
                )
                if updated:
                    status_msg = msg
                    status_style = "bold green"
                elif msg:
                    status_msg = msg
                    status_style = "bold yellow"
                continue

            # Quick removal on future dates: 'd'
            if k in ("d", "delete") and active_tasks:
                selected_dt = active_tasks[selected_idx]
                if domain == "past":
                    status_msg = "Cannot remove historical completed tasks."
                    status_style = "bold yellow"
                elif domain == "future":
                    self.daily_service.remove_daily_task(selected_dt.id)
                    status_msg = "Removed task from future schedule."
                elif domain == "today":
                    self.daily_service.remove_daily_task(selected_dt.id)
                    status_msg = "Removed task from Today."
                continue

            # Quick move to universal: 'm'
            if k in ("m", "move") and active_tasks:
                selected_dt = active_tasks[selected_idx]
                if domain == "past":
                    status_msg = "Cannot move historical completed tasks."
                    status_style = "bold yellow"
                else:
                    self.daily_service.move_task_to_universal(selected_dt.id)
                    status_msg = "Moved task to Universal Tasks."
                continue

    def _prompt_go_to_date(self) -> tuple[Optional[date], Optional[str]]:
        """Prompt user for target date in YYYY-MM-DD format."""
        clear_viewport()
        render_header("GO TO DATE")
        console.print("  Enter target date (YYYY-MM-DD, or 'b' to cancel):\n")
        text = key_reader.read_text_input("Date: > ")
        if not text or text.lower() in ("b", "back", "cancel", "q", "quit"):
            return None, None
        parsed = self._parse_date(text)
        if not parsed:
            return None, f"Invalid date: '{text}'. Format must be YYYY-MM-DD (e.g. 2026-10-25)."
        return parsed, None

    def _show_task_details(self, dt: DailyTask) -> None:
        """Display immutable details modal for a historical task."""
        clear_viewport()
        render_header("TASK DETAILS", subtitle=f"Task #{dt.task_id}")
        task = dt.task
        title = task.title if task else f"Task #{dt.task_id}"
        console.print(f"  Title:        [bold white]{title}[/bold white]")
        if task and task.description:
            console.print(f"  Description:  [white]{task.description}[/white]")
        console.print(f"  Source:       [cyan]{task.source_type if task else 'manual'}[/cyan]")
        console.print(f"  Scheduled:    [dim]{dt.date}[/dim]")
        if dt.completed_at:
            console.print(f"  Completed At: [green]{dt.completed_at}[/green]")
        if task and task.created_at:
            console.print(f"  Created At:   [dim]{task.created_at}[/dim]")
        console.print()
        render_footer([("Enter / Esc / Space", "Close")])
        key_reader.read_key()

    def _manage_future_task(
        self, dt: DailyTask, current_date_str: str
    ) -> Optional[str]:
        """Manage modal for a future scheduled task (reschedule, move to today, move to universal, remove)."""
        task_title = dt.task.title if dt.task else f"Task #{dt.task_id}"
        options = [
            ("1.", "Reschedule to another date"),
            ("2.", "Move to Today"),
            ("3.", "Move to Universal Tasks"),
            ("4.", "Remove from schedule"),
        ]
        if self.task_editor:
            options.append(("5.", "Edit task details"))

        sel_idx = 0
        while True:
            clear_viewport()
            render_header("MANAGE SCHEDULED TASK", subtitle=task_title[:40])
            console.print(f"  Task: [bold white]{task_title}[/bold white]\n")
            console.print(f"  Scheduled for: [cyan]{dt.date}[/cyan]\n")
            console.print("  Select action:\n")

            table = Table.grid(padding=(0, 1))
            table.add_column("cursor", justify="right", style="cyan bold", width=4)
            table.add_column("num", justify="right", style="cyan bold", width=4)
            table.add_column("name", style="bold white")

            for idx, (num, name) in enumerate(options):
                cursor = "▶" if idx == sel_idx else " "
                table.add_row(cursor, num, name)

            console.print(table)
            console.print()
            footer_keys = f"Enter / 1-{len(options)}"
            render_footer(
                [
                    ("↑/↓", "Navigate"),
                    (footer_keys, "Select"),
                    ("b / Esc", "Cancel"),
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
                choice_num = sel_idx + 1
            elif k.isdigit() and 1 <= int(k) <= len(options):
                choice_num = int(k)
            else:
                continue

            if choice_num == 1:
                clear_viewport()
                render_header("RESCHEDULE TASK", subtitle=task_title[:40])
                console.print(f"  Task: [bold white]{task_title}[/bold white]\n")
                console.print("  Enter new date (YYYY-MM-DD, or 'b' to cancel):\n")
                new_date_str = key_reader.read_text_input("New Date: > ")
                if not new_date_str or new_date_str.lower() in (
                    "b",
                    "back",
                    "cancel",
                    "q",
                    "quit",
                ):
                    return None
                parsed = self._parse_date(new_date_str)
                if not parsed:
                    return f"Invalid date: '{new_date_str}'. Must be YYYY-MM-DD."
                try:
                    self.daily_service.reschedule_daily_task(dt.id, parsed.isoformat())
                    return f"Rescheduled to {parsed.isoformat()}."
                except Exception as e:
                    return f"Error: {e}"

            elif choice_num == 2:
                today_str = self.daily_service.get_today_str()
                try:
                    self.daily_service.reschedule_daily_task(dt.id, today_str)
                    return "Moved task to Today."
                except Exception as e:
                    return f"Error: {e}"

            elif choice_num == 3:
                self.daily_service.move_task_to_universal(dt.id)
                return "Moved task to Universal Tasks."

            elif choice_num == 4:
                self.daily_service.remove_daily_task(dt.id)
                return "Removed task from schedule."

            elif choice_num == 5 and self.task_editor:
                updated, msg = self.task_editor.edit_task(dt.task, current_date=current_date_str)
                return msg

