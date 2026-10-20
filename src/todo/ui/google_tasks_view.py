"""Google Tasks TUI view for browsing, creating, editing, and syncing cloud tasks."""

import time
from datetime import datetime
from typing import Any, Dict, List, Optional

import requests
from rich.table import Table
from rich.text import Text

from todo.integrations.google_tasks import (
    GoogleTasksAPIError,
    GoogleTasksAuthError,
    GoogleTasksNetworkError,
    GoogleTasksNotFoundError,
)
from todo.models import Task
from todo.search import LiveSearchController
from todo.services.daily_service import DailyService
from todo.services.google_tasks_service import GoogleTasksService
from todo.ui.console import (
    clear_viewport,
    console,
    print_status_message,
    prompt_input,
    render_footer,
    render_header,
)
from todo.ui.key_reader import Key, key_reader
from todo.ui.task_editor import TaskEditor


class GoogleTasksView:
    """Full-screen TUI interface for Google Tasks integration."""

    def __init__(
        self,
        google_tasks_service: GoogleTasksService,
        daily_service: DailyService,
        task_editor: Optional[TaskEditor] = None,
        sync_manager: Optional[Any] = None,
    ):
        self.google_tasks_service = google_tasks_service
        self.daily_service = daily_service
        self.task_editor = task_editor
        self.sync_manager = sync_manager

    def run(self, target_date: Optional[str] = None) -> None:
        """Main entry point for Google Tasks screen."""
        # 1. Check credentials
        if not self.google_tasks_service.has_credentials():
            action = self._render_credentials_setup()
            if action != "retry":
                return
            if not self.google_tasks_service.has_credentials():
                return

        # 2. Check authentication
        if not self.google_tasks_service.is_authenticated():
            connected = self._handle_oauth_connect()
            if not connected and not self.google_tasks_service.get_cached_task_lists():
                return

        # 3. Retrieve task lists from local cache first (instant < 5ms)
        lists = self.google_tasks_service.get_cached_task_lists()
        if not lists:
            # First run with empty cache: fetch lists
            lists, is_offline = self.google_tasks_service.sync_task_lists()

        if self.sync_manager:
            self.sync_manager.trigger_sync()

        if not lists:
            clear_viewport()
            render_header("GOOGLE TASKS")
            console.print("No task lists found on Google Tasks.\n")
            console.print("Press [bold cyan]c[/bold cyan] to create a list, or [bold cyan]b[/bold cyan] to go back.\n")
            render_footer([("c", "Create list"), ("b", "Back")])
            k = key_reader.read_key()
            if k in ("c", "create"):
                new_title = prompt_input("New list title: ").strip()
                if new_title:
                    self.google_tasks_service.get_source_for_list("@default", new_title)
                    lists, _ = self.google_tasks_service.sync_task_lists()
            else:
                return

        if not lists:
            return

        current_list_idx = 0
        current_list = lists[current_list_idx]
        selected_idx = 0
        search = LiveSearchController[Task]()
        status_msg: Optional[str] = None
        status_style = "green"

        while True:
            # 4. Instant local-first retrieval (< 5ms): only pending tasks are fetched
            # Completed tasks are hidden from active views but preserved in SQLite and Timeline.
            try:
                if self.sync_manager:
                    tasks = self.google_tasks_service.get_tasks_for_list(current_list["id"], include_completed=False)
                else:
                    self.google_tasks_service.sync_tasks_for_list(current_list["id"])
                    tasks = self.google_tasks_service.get_tasks_for_list(current_list["id"], include_completed=False)
            except GoogleTasksNotFoundError as e:
                status_msg = f"Task list '{current_list.get('title')}' was not found on Google Tasks (pruned from cache)."
                status_style = "bold yellow"
                lists, _ = self.google_tasks_service.sync_task_lists()
                if not lists:
                    clear_viewport()
                    render_header("GOOGLE TASKS")
                    print_status_message(status_msg, style=status_style)
                    console.print("\nNo other task lists available on Google Tasks.\n")
                    render_footer([("b", "Back")])
                    prompt_input("Press Enter to return...")
                    return
                current_list_idx = 0
                current_list = lists[current_list_idx]
                selected_idx = 0
                continue
            except Exception:
                tasks = self.google_tasks_service.get_tasks_for_list(current_list["id"], include_completed=False)

            clear_viewport()
            if self.sync_manager:
                sub_status = self.sync_manager.get_status_text()
            elif self.google_tasks_service.is_authenticated():
                sub_status = "● Connected"
            else:
                sub_status = "● Offline mode (cached)"

            subtitle = f"{current_list.get('title', 'Tasks')} · {sub_status}"
            render_header("GOOGLE TASKS", subtitle=subtitle)

            if status_msg:
                print_status_message(status_msg, style=status_style)
                status_msg = None

            # Handle search filtering
            if search.is_active:
                console.print(f"  Search: [bold cyan]{search.query}[/bold cyan]█\n")
            elif search.query:
                console.print(f"  Search: [bold cyan]{search.query}[/bold cyan] [dim](filter active, Esc to clear)[/dim]\n")

            active_tasks = search.filter(tasks) if search.query else tasks

            if not active_tasks:
                if search.query:
                    console.print("  [dim]No tasks matching search filter.[/dim]\n")
                else:
                    console.print("  [dim]No active tasks in this list. Press '+' to add a task.[/dim]\n")
            else:
                if selected_idx >= len(active_tasks):
                    selected_idx = max(0, len(active_tasks) - 1)

                table = Table.grid(padding=(0, 1))
                table.add_column("cursor", justify="right", style="cyan bold", width=2)
                table.add_column("num", justify="right", style="cyan bold", width=4)
                table.add_column("title", style="bold white")
                table.add_column("due", style="cyan", width=16)
                table.add_column("notes", style="dim")

                for idx, t in enumerate(active_tasks):
                    cursor = "▶" if idx == selected_idx else " "
                    num_str = f"{idx + 1}."

                    # Parse due date if scheduled in daily_tasks or metadata
                    scheduled_date = self.daily_service.get_task_scheduled_date(t.id)
                    due_str = f"[due: {scheduled_date}]" if scheduled_date else ""

                    # Truncate notes
                    notes_preview = t.description[:35] + "..." if len(t.description) > 35 else t.description

                    table.add_row(cursor, num_str, Text(t.title, style="bold white"), due_str, notes_preview)

                console.print(table)
                console.print()

            # Footer hints
            if search.is_active:
                render_footer([("↑/↓", "Navigate"), ("Enter", "Done"), ("Esc", "Cancel / Clear")])
            elif search.query:
                render_footer([
                    ("↑/↓", "Navigate"),
                    ("Enter", "Complete"),
                    ("Space", "Schedule"),
                    ("Esc", "Clear search"),
                    ("b", "Back"),
                ])
            else:
                render_footer([
                    ("↑/↓", "Navigate"),
                    ("Enter", "Complete"),
                    ("Space", "Schedule"),
                    ("+", "Add task"),
                    ("e", "Edit"),
                    ("d", "Delete"),
                    ("r", "Sync"),
                    ("l", "Lists"),
                    ("c", "Connect / Account"),
                    ("/", "Search"),
                    ("b", "Back"),
                ])

            k = key_reader.read_key(timeout=0.2)
            if k is None:
                # Refresh render on background state change or timer
                continue

            if search.is_active:
                handled, selected_idx = search.handle_key(k, len(active_tasks), selected_idx)
                if handled:
                    continue

            if k in (Key.ESCAPE, "escape") and search.query:
                search.query = ""
                continue

            if k in (Key.ESCAPE, "b", "back", "q", "quit"):
                return

            if k in (Key.UP, "up") and active_tasks:
                selected_idx = max(0, selected_idx - 1)
                continue

            if k in (Key.DOWN, "down") and active_tasks:
                selected_idx = min(len(active_tasks) - 1, selected_idx + 1)
                continue

            if k == "/":
                search.is_active = True
                continue

            # Toggle completion
            if k in (Key.ENTER, "enter") and active_tasks:
                target_task = active_tasks[selected_idx]
                updated = self.google_tasks_service.toggle_task_completion(target_task.id)
                new_st = updated.status if updated else "updated"
                status_msg = f"Task '{target_task.title}' marked {new_st}."
                status_style = "green"
                continue

            # Schedule task for Today or target_date
            if k in (Key.SPACE, "space", " ") and active_tasks:
                target_task = active_tasks[selected_idx]
                dest_date = target_date or self.daily_service.get_today_str()
                self.daily_service.add_task_to_date(target_task.id, dest_date)
                # Also push due date to Google Tasks
                self.google_tasks_service.update_task(target_task.id, due_date=dest_date)
                status_msg = f"Scheduled '{target_task.title}' for {dest_date}."
                status_style = "green"
                continue

            # Add task
            if k in ("+", Key.PLUS, "add"):
                created_msg = self._handle_create_task(current_list["id"], target_date)
                if created_msg:
                    status_msg = created_msg
                    status_style = "green"
                continue

            # Edit task
            if k in ("e", "edit") and active_tasks:
                target_task = active_tasks[selected_idx]
                if self.task_editor:
                    cur_sched = self.daily_service.get_task_scheduled_date(target_task.id)
                    saved, msg = self.task_editor.edit_task(target_task, current_date=cur_sched)
                    if saved:
                        status_msg = msg
                        status_style = "green"
                    elif msg:
                        status_msg = msg
                        status_style = "yellow"
                continue

            # Delete task
            if k in ("d", "delete") and active_tasks:
                target_task = active_tasks[selected_idx]
                deleted = self._confirm_and_delete_task(target_task)
                if deleted:
                    status_msg = f"Deleted task '{target_task.title}'."
                    status_style = "yellow"
                    selected_idx = max(0, selected_idx - 1)
                continue

            # Refresh / Sync
            if k in ("r", "sync"):
                if self.sync_manager:
                    self.sync_manager.trigger_sync()
                    status_msg = "Background sync initiated."
                    status_style = "green"
                else:
                    lists, is_offline = self.google_tasks_service.sync_task_lists()
                    if not lists:
                        status_msg = "No task lists found on Google Tasks."
                        status_style = "yellow"
                    else:
                        if current_list_idx >= len(lists):
                            current_list_idx = 0
                        current_list = lists[current_list_idx]
                        status_msg = "Synced with Google Tasks." if not is_offline else "Offline · loaded from cache."
                        status_style = "green" if not is_offline else "yellow"
                continue

            # Switch lists
            if k in ("l", "list", "lists"):
                new_idx = self._handle_switch_list(lists, current_list_idx)
                if new_idx is not None and new_idx != current_list_idx:
                    current_list_idx = new_idx
                    current_list = lists[current_list_idx]
                    selected_idx = 0
                continue

            # Connection / Auth settings
            if k in ("c", "conn", "auth"):
                self._handle_connection_menu()
                lists, _ = self.google_tasks_service.sync_task_lists()
                if lists and current_list_idx < len(lists):
                    current_list = lists[current_list_idx]
                continue

    def _render_credentials_setup(self) -> str:
        """Render instructions when Google OAuth credentials are not found."""
        clear_viewport()
        render_header("GOOGLE TASKS SETUP")
        console.print("[bold yellow]Google Tasks credentials are not configured.[/bold yellow]\n")
        console.print("To connect Google Tasks with Extern:\n")
        console.print("  1. Visit the Google Cloud Console: [cyan]https://console.cloud.google.com/[/cyan]")
        console.print("  2. Create a project and enable the [bold white]Google Tasks API[/bold white].")
        console.print("  3. Create an [bold white]OAuth 2.0 Client ID[/bold white] (Application type: Desktop App).")
        console.print("  4. Download the credentials JSON and save it to:")
        console.print(f"     [bold cyan]{self.google_tasks_service.oauth_manager.token_path.parent / 'credentials.json'}[/bold cyan]\n")
        console.print("Alternatively, enter your Client ID and Client Secret now.\n")

        render_footer([("c", "Enter Client ID/Secret"), ("b", "Back")])
        k = key_reader.read_key()
        if k in ("c", "client"):
            c_id = prompt_input("Client ID: ").strip()
            c_secret = prompt_input("Client Secret: ").strip()
            if c_id and c_secret:
                self.google_tasks_service.oauth_manager.client_id = c_id
                self.google_tasks_service.oauth_manager.client_secret = c_secret
                return "retry"
        return "back"

    def _handle_oauth_connect(self) -> bool:
        """Run the OAuth connection flow (loopback + manual code entry)."""
        clear_viewport()
        render_header("CONNECT GOOGLE TASKS")

        try:
            from todo.integrations.google_tasks import run_loopback_auth_server
            auth_url, redirect_uri, captured = run_loopback_auth_server(
                self.google_tasks_service.oauth_manager, timeout_seconds=120
            )
        except Exception as e:
            console.print(f"[bold red]Failed to initialize authorization server:[/bold red] {e}\n")
            render_footer([("b", "Back")])
            key_reader.read_key()
            return False

        console.print("Opening browser for Google authorization...\n")
        console.print(f"Authorization URL:\n[bold cyan]{auth_url}[/bold cyan]\n")
        console.print("[dim]If your browser does not open automatically, copy and visit the URL above.[/dim]\n")
        console.print("Waiting for authorization... Or paste the authorization code or redirect URL below:\n")

        # Try to open browser in background
        try:
            import webbrowser
            webbrowser.open(auth_url)
        except Exception:
            pass

        code_input = prompt_input("Code / Redirect URL (or 'b' to cancel, Enter if authorized in browser): ").strip()
        code_to_exchange = None
        if code_input and code_input.lower() not in ("b", "back", "cancel", "q"):
            code_to_exchange = code_input
        elif not code_input and captured:
            code_to_exchange = captured[0]

        if not code_to_exchange or (code_input and code_input.lower() in ("b", "back", "cancel", "q")):
            return False

        try:
            self.google_tasks_service.oauth_manager.exchange_code(code_to_exchange, redirect_uri)
            clear_viewport()
            render_header("GOOGLE TASKS CONNECTED")
            console.print("[bold green]Successfully authenticated with Google Tasks![/bold green]\n")
            time.sleep(1)
            return True
        except Exception as e:
            clear_viewport()
            render_header("AUTHENTICATION FAILED")
            console.print(f"[bold red]Error exchanging authorization code:[/bold red] {e}\n")
            render_footer([("b", "Back")])
            key_reader.read_key()
            return False

    def _handle_create_task(self, list_id: str, default_date: Optional[str] = None) -> Optional[str]:
        """Prompt user to create a new task in the current list."""
        clear_viewport()
        render_header("CREATE GOOGLE TASK")
        console.print("Enter task title (or 'b' to cancel):\n")
        title = prompt_input("Title: > ").strip()
        if not title or title.lower() in ("b", "back", "cancel", "q"):
            return None

        console.print("\nEnter description/notes (optional, press Enter to skip):\n")
        notes = prompt_input("Notes: > ").strip()

        date_prompt = f"Due Date (YYYY-MM-DD) [{default_date or 'none'}]: > "
        console.print(f"\nEnter scheduled/due date (optional, press Enter for {default_date or 'none'}):\n")
        due_input = prompt_input(date_prompt).strip()
        due_date = due_input if due_input else default_date

        if due_date:
            try:
                datetime.strptime(due_date[:10], "%Y-%m-%d")
            except ValueError:
                console.print(f"[bold red]Invalid date format:[/bold red] {due_date}. Skipping due date.")
                due_date = None

        task = self.google_tasks_service.create_task(
            list_id=list_id,
            title=title,
            notes=notes,
            due_date=due_date,
            schedule_date=due_date,
        )
        return f"Created task '{task.title}'."

    def _confirm_and_delete_task(self, task: Task) -> bool:
        """Prompt confirmation before deleting a task."""
        clear_viewport()
        render_header("DELETE GOOGLE TASK", subtitle=task.title[:35])
        console.print(f"Are you sure you want to delete [bold white]'{task.title}'[/bold white]?\n")
        console.print("[dim]This will delete the task from Google Tasks and Extern.[/dim]\n")
        render_footer([("y", "Yes, delete"), ("n", "Cancel")])
        conf = key_reader.read_key()
        if conf.lower() in ("y", "yes", Key.ENTER, "enter"):
            return self.google_tasks_service.delete_task(task.id)
        return False

    def _handle_switch_list(self, lists: List[Dict[str, Any]], current_idx: int) -> Optional[int]:
        """List switcher modal."""
        sel_idx = current_idx
        while True:
            clear_viewport()
            render_header("SELECT TASK LIST")
            console.print("Choose a Google Task list:\n")

            table = Table.grid(padding=(0, 1))
            table.add_column("cursor", justify="right", style="cyan bold", width=2)
            table.add_column("num", justify="right", style="cyan bold", width=4)
            table.add_column("name", style="bold white")

            for idx, l in enumerate(lists):
                cursor = "▶" if idx == sel_idx else " "
                table.add_row(cursor, f"{idx + 1}.", l.get("title", f"List {idx + 1}"))

            console.print(table)
            console.print()
            render_footer([("↑/↓", "Navigate"), ("Enter", "Select"), ("b", "Cancel")])

            k = key_reader.read_key()
            if k in (Key.ESCAPE, "b", "q", "cancel"):
                return None
            if k in (Key.UP, "up"):
                sel_idx = max(0, sel_idx - 1)
                continue
            if k in (Key.DOWN, "down"):
                sel_idx = min(len(lists) - 1, sel_idx + 1)
                continue
            if k in (Key.ENTER, "enter"):
                return sel_idx
            if k.isdigit() and 1 <= int(k) <= len(lists):
                return int(k) - 1

    def _handle_connection_menu(self) -> None:
        """Connection and account management screen."""
        clear_viewport()
        render_header("GOOGLE TASKS ACCOUNT")
        is_auth = self.google_tasks_service.is_authenticated()
        status_text = "[bold green]Connected[/bold green]" if is_auth else "[bold yellow]Disconnected[/bold yellow]"
        console.print(f"Status: {status_text}\n")
        console.print(f"Token file: [dim]{self.google_tasks_service.oauth_manager.token_path}[/dim]\n")

        options = []
        if is_auth:
            options.append(("1.", "Disconnect account"))
            options.append(("2.", "Reconnect / Re-authenticate"))
        else:
            options.append(("1.", "Connect with Google"))

        table = Table.grid(padding=(0, 1))
        table.add_column("num", style="cyan bold", width=4)
        table.add_column("name", style="bold white")
        for num, name in options:
            table.add_row(num, name)

        console.print(table)
        console.print()
        render_footer([("1-2", "Select option"), ("b", "Back")])

        k = key_reader.read_key()
        if is_auth:
            if k in ("1", "disconnect"):
                self.google_tasks_service.disconnect()
                clear_viewport()
                render_header("DISCONNECTED")
                console.print("[bold yellow]Disconnected from Google Tasks.[/bold yellow]\n")
                time.sleep(1)
            elif k in ("2", "reconnect"):
                self._handle_oauth_connect()
        else:
            if k in ("1", "connect"):
                self._handle_oauth_connect()
