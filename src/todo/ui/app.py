"""Main interactive terminal application controller."""

import sys
from datetime import datetime
from typing import Optional

from todo.config import Config
from todo.database import Database
from todo.integrations.filemeta import FilemetaAdapter
from todo.services.daily_service import DailyService
from todo.services.source_service import SourceService
from todo.services.task_service import TaskService
from todo.ui.add_task_view import AddTaskView
from todo.ui.console import console, print_status_message, prompt_input
from todo.ui.filemeta_view import FilemetaView
from todo.ui.manual_view import ManualView
from todo.ui.today_view import TodayView
from todo.ui.universal_view import UniversalView


class TodoApp:
    def __init__(self, config: Optional[Config] = None):
        self.config = config or Config.load()
        self.db = Database(self.config.db_path)
        self.source_service = SourceService(self.db)
        self.task_service = TaskService(self.db)
        self.daily_service = DailyService(self.db)
        self.filemeta_adapter = FilemetaAdapter(self.config.filemeta_command)

        self.today_view = TodayView(self.daily_service)
        self.add_task_view = AddTaskView()
        self.filemeta_view = FilemetaView(
            self.source_service,
            self.task_service,
            self.daily_service,
            self.filemeta_adapter,
        )
        self.universal_view = UniversalView(self.task_service, self.daily_service)
        self.manual_view = ManualView(self.task_service, self.daily_service)

    def run(self) -> None:
        """Run the main interactive application loop."""
        status_msg: Optional[str] = None
        status_style: str = "green"

        while True:
            try:
                today_str = datetime.now().date().isoformat()
                tasks = self.daily_service.get_tasks_for_date(today_str)

                # Render main Today screen
                self.today_view.render(tasks, status_msg=status_msg)
                status_msg = None
                status_style = "green"

                choice = prompt_input("> ")

                if choice.lower() in ("q", "quit", "exit"):
                    console.print("[dim]Goodbye![/dim]\n")
                    break

                if choice in ("+", "add"):
                    status_msg = self.handle_add_task_menu()
                    continue

                if choice.lower() in ("u", "universal"):
                    status_msg = self.universal_view.run(select_mode=True)
                    continue

                if choice.isdigit() and tasks:
                    idx = int(choice)
                    if 1 <= idx <= len(tasks):
                        selected_daily = tasks[idx - 1]
                        updated = self.daily_service.toggle_task_completion(selected_daily.id)
                        if updated and updated.is_completed:
                            status_msg = f"✓ Completed: \"{updated.task.title}\""
                            status_style = "green"
                        elif updated:
                            status_msg = f"○ Marked pending: \"{updated.task.title}\""
                            status_style = "yellow"
                    else:
                        status_msg = f"Invalid selection: #{choice}. Please enter 1-{len(tasks)}."
                        status_style = "red"
                else:
                    if choice:
                        status_msg = "Unknown option. Use [1-N] to toggle, '+' to add task, or 'q' to quit."
                        status_style = "yellow"

            except (KeyboardInterrupt, EOFError):
                console.print("\n[dim]Goodbye![/dim]\n")
                break

    def handle_add_task_menu(self) -> Optional[str]:
        """Show Add Task menu and dispatch to the corresponding workflow."""
        add_status: Optional[str] = None

        while True:
            self.add_task_view.render(status_msg=add_status)
            add_status = None

            choice = prompt_input("Select option [1-3, b]: ")

            if choice.lower() in ("b", "back", "q", "quit"):
                return None

            if choice == "1":
                # Filemeta workflow
                result = self.filemeta_view.run()
                if result:
                    return result

            elif choice == "2":
                # Universal task workflow
                result = self.universal_view.run(select_mode=True)
                if result:
                    return result

            elif choice == "3":
                # Manual task workflow
                result = self.manual_view.run()
                if result:
                    return result

            else:
                add_status = "Invalid option. Please choose 1, 2, 3, or 'b' to go back."
