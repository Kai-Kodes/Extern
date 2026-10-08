"""Main interactive terminal application controller with full-screen TUI."""

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
from todo.ui.console import (
    clear_viewport,
    console,
    read_key,
    tui_mode,
)
from todo.ui.filemeta_view import FilemetaView
from todo.ui.key_reader import Key, key_reader
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

        self.today_view = TodayView(self.daily_service, self.task_service)
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
        """Run the main interactive application loop inside the alternate terminal screen."""
        with tui_mode():
            self._main_loop()

    def _main_loop(self) -> None:
        status_msg: Optional[str] = None
        status_style: str = "green"
        selected_idx = 0

        while True:
            try:
                today_str = datetime.now().date().isoformat()
                tasks = self.daily_service.get_tasks_for_date(today_str)

                # Clamp selected_idx
                if tasks:
                    selected_idx = max(0, min(selected_idx, len(tasks) - 1))
                else:
                    selected_idx = 0

                # Render Today viewport
                self.today_view.render(
                    tasks,
                    status_msg=status_msg,
                    status_style=status_style,
                    selected_idx=selected_idx,
                    clear=True,
                )
                status_msg = None
                status_style = "green"

                choice = read_key()

                if choice in (Key.ESCAPE, "q", "quit", "exit"):
                    break

                if choice in (Key.UP, "up"):
                    if tasks:
                        selected_idx = max(0, selected_idx - 1)
                    continue

                if choice in (Key.DOWN, "down"):
                    if tasks:
                        selected_idx = min(len(tasks) - 1, selected_idx + 1)
                    continue

                if choice in (Key.ENTER, "enter"):
                    if tasks:
                        selected_daily = tasks[selected_idx]
                        self.daily_service.toggle_task_completion(selected_daily.id)
                    continue

                if choice in ("+", Key.PLUS, "add"):
                    status_msg = self.handle_add_task_menu()
                    continue

                if choice.lower() in ("d", "delete"):
                    del_msg = self.today_view.handle_delete(tasks, current_idx=selected_idx)
                    if del_msg:
                        status_msg = del_msg
                        status_style = "yellow"
                    continue

                if choice.lower() in ("u", "universal"):
                    status_msg = self.universal_view.run(select_mode=True)
                    continue

                if choice.isdigit() and tasks:
                    idx = int(choice)
                    if 1 <= idx <= len(tasks):
                        selected_daily = tasks[idx - 1]
                        selected_idx = idx - 1
                        self.daily_service.toggle_task_completion(selected_daily.id)
                    else:
                        status_msg = f"Invalid selection: #{choice}. Please enter 1-{len(tasks)}."
                        status_style = "red"
                else:
                    if choice:
                        status_msg = "Unknown option. Use [↑/↓] to navigate, [Enter] to toggle, '+' to add, 'd' to delete, or 'q' to quit."
                        status_style = "yellow"

            except (KeyboardInterrupt, EOFError):
                break

    def handle_add_task_menu(self) -> Optional[str]:
        """Show Add Task menu and dispatch to the corresponding workflow."""
        add_status: Optional[str] = None
        selected_idx = 0

        while True:
            self.add_task_view.render(
                status_msg=add_status, selected_idx=selected_idx, clear=True
            )
            add_status = None

            choice = read_key()

            if choice in (Key.ESCAPE, "b", "back", "q", "quit", "cancel"):
                return None

            if choice in (Key.UP, "up"):
                selected_idx = max(0, selected_idx - 1)
                continue

            if choice in (Key.DOWN, "down"):
                selected_idx = min(2, selected_idx + 1)
                continue

            if choice in (Key.ENTER, "enter"):
                choice = str(selected_idx + 1)

            if choice == "1":
                result = self.filemeta_view.run()
                if result:
                    return result

            elif choice == "2":
                result = self.universal_view.run(select_mode=True)
                if result:
                    return result

            elif choice == "3":
                result = self.manual_view.run()
                if result:
                    return result

            else:
                add_status = "Invalid option. Please choose 1, 2, 3, or 'b' to go back."

