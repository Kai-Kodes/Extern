"""Main interactive terminal application controller with full-screen TUI."""

import sys
from datetime import datetime
from typing import Callable, List, Optional, Set

from todo.config import Config
from todo.database import Database
from todo.integrations.devices import DeviceManager
from todo.integrations.filemeta import FilemetaAdapter
from todo.integrations.telegram import TelegramAdapter
from todo.integrations.youtube import YouTubeAdapter
from todo.models import DailyTask
from todo.services.audio_service import AudioService
from todo.services.daily_service import DailyService
from todo.services.pomodoro_service import PomodoroService
from todo.services.pomodoro_timer import PomodoroTimer
from todo.services.source_service import SourceService
from todo.services.task_service import TaskService
from todo.ui.add_task_view import AddTaskView
from todo.ui.console import (
    clear_viewport,
    console,
    read_key,
    render_footer,
    render_header,
    tui_mode,
)
from todo.ui.filemeta_view import FilemetaView
from todo.ui.focus_detector import FocusDetector
from todo.ui.key_reader import Key, key_reader
from todo.ui.manual_view import ManualView
from todo.ui.pomodoro_view import PomodoroView
from todo.ui.telegram_view import TelegramView
from todo.ui.today_view import TodayView
from todo.ui.universal_view import UniversalView
from todo.ui.youtube_view import YouTubeView


class TodoApp:
    def __init__(
        self,
        config: Optional[Config] = None,
        date_provider: Optional[Callable[[], datetime]] = None,
    ):
        self.config = config or Config.load()
        self.date_provider = date_provider
        self.db = Database(self.config.db_path)
        self.source_service = SourceService(self.db, date_provider=self.date_provider)
        self.task_service = TaskService(self.db, date_provider=self.date_provider)
        self.daily_service = DailyService(self.db, date_provider=self.date_provider)
        self.filemeta_adapter = FilemetaAdapter(
            command=self.config.filemeta_command,
            timeout=self.config.filemeta_timeout,
            device_timeout=self.config.filemeta_device_timeout,
        )
        self.telegram_adapter = TelegramAdapter(
            self.config.telegram_session_path,
            self.config.telegram_api_id,
            self.config.telegram_api_hash,
        )
        self.youtube_adapter = YouTubeAdapter()
        self.device_manager = DeviceManager()

        self.today_view = TodayView(self.daily_service, self.task_service)
        self.add_task_view = AddTaskView()
        self.filemeta_view = FilemetaView(
            self.source_service,
            self.task_service,
            self.daily_service,
            self.filemeta_adapter,
            device_manager=self.device_manager,
        )
        self.telegram_view = TelegramView(
            self.telegram_adapter,
            self.source_service,
            self.task_service,
            self.daily_service,
        )
        self.youtube_view = YouTubeView(
            self.youtube_adapter,
            self.source_service,
            self.task_service,
            self.daily_service,
        )
        self.universal_view = UniversalView(self.task_service, self.daily_service)
        self.manual_view = ManualView(self.task_service, self.daily_service)

        self.pomodoro_service = PomodoroService(self.db, date_provider=self.date_provider)
        self.audio_service = AudioService(
            player_cmd=self.config.pomodoro_audio_player,
            work_sound=self.config.pomodoro_work_sound,
            break_sound=self.config.pomodoro_break_sound,
        )
        self.focus_detector = FocusDetector()
        self.pomodoro_timer = PomodoroTimer(
            pomodoro_service=self.pomodoro_service,
            audio_service=self.audio_service,
        )
        self.pomodoro_view = PomodoroView(
            pomodoro_service=self.pomodoro_service,
            timer=self.pomodoro_timer,
            audio_service=self.audio_service,
            focus_detector=self.focus_detector,
        )

    def run(self) -> None:
        """Run the main interactive application loop inside the alternate terminal screen."""
        try:
            with tui_mode():
                self._main_loop()
        finally:
            self.telegram_adapter.disconnect()

    def _main_loop(self) -> None:
        status_msg: Optional[str] = None
        status_style: str = "green"
        selected_idx = 0
        selected_task_ids: Set[int] = set()
        mode: Optional[str] = None

        while True:
            try:
                today_str = self.daily_service.get_today_str()
                tasks = self.daily_service.get_active_tasks_for_date(today_str)
                completed_cnt, total_cnt, _ = self.daily_service.get_progress_for_date(today_str)

                # Clean up any stale selected_task_ids not in tasks
                valid_ids = {dt.id for dt in tasks}
                selected_task_ids.intersection_update(valid_ids)

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
                    current_date=today_str,
                    completed_count=completed_cnt,
                    total_count=total_cnt,
                    selected_ids=selected_task_ids,
                    mode=mode,
                )
                status_msg = None
                status_style = "green"

                choice = read_key()

                if mode == "delete":
                    if choice in (Key.ESCAPE, "escape", "q", "cancel"):
                        selected_task_ids.clear()
                        mode = None
                        continue
                    if choice in (Key.UP, "up") and tasks:
                        selected_idx = max(0, selected_idx - 1)
                        continue
                    if choice in (Key.DOWN, "down") and tasks:
                        selected_idx = min(len(tasks) - 1, selected_idx + 1)
                        continue
                    if choice in (Key.SPACE, "space", " ") and tasks:
                        cur_dt = tasks[selected_idx]
                        if cur_dt.id in selected_task_ids:
                            selected_task_ids.remove(cur_dt.id)
                        else:
                            selected_task_ids.add(cur_dt.id)
                        continue
                    if choice in (Key.ENTER, "enter"):
                        if not selected_task_ids:
                            continue
                        self._confirm_and_delete_bulk_today(selected_task_ids, tasks)
                        selected_task_ids.clear()
                        mode = None
                        continue
                    # Ignore all other keys in delete mode
                    continue

                elif mode == "move":
                    if choice in (Key.ESCAPE, "escape", "q", "cancel"):
                        selected_task_ids.clear()
                        mode = None
                        continue
                    if choice in (Key.UP, "up") and tasks:
                        selected_idx = max(0, selected_idx - 1)
                        continue
                    if choice in (Key.DOWN, "down") and tasks:
                        selected_idx = min(len(tasks) - 1, selected_idx + 1)
                        continue
                    if choice in (Key.SPACE, "space", " ") and tasks:
                        cur_dt = tasks[selected_idx]
                        if cur_dt.id in selected_task_ids:
                            selected_task_ids.remove(cur_dt.id)
                        else:
                            selected_task_ids.add(cur_dt.id)
                        continue
                    if choice in (Key.ENTER, "enter"):
                        if not selected_task_ids:
                            continue
                        self.daily_service.move_tasks_to_universal_bulk(list(selected_task_ids))
                        selected_task_ids.clear()
                        mode = None
                        continue
                    # Ignore all other keys in move mode
                    continue

                else:
                    if choice in (Key.ESCAPE, "escape", "q", "quit", "exit"):
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

                    # Enter delete selection mode with 'd'
                    if choice.lower() in ("d", "delete"):
                        if tasks:
                            mode = "delete"
                            selected_task_ids.clear()
                        continue

                    # Enter move selection mode with 'm'
                    if choice.lower() in ("m", "move"):
                        if tasks:
                            mode = "move"
                            selected_task_ids.clear()
                        continue

                    if choice in ("+", Key.PLUS, "add"):
                        self.handle_add_task_menu()
                        continue

                    if choice.lower() in ("u", "universal"):
                        self.universal_view.run(select_mode=True)
                        continue

                    if choice.lower() in ("f", "filemeta"):
                        self.filemeta_view.run()
                        continue

                    if choice.lower() in ("t", "tg", "telegram"):
                        self.telegram_view.run()
                        continue

                    if choice.lower() in ("y", "yt", "youtube"):
                        self.youtube_view.run()
                        continue

                    if choice.lower() in ("p", "pomodoro"):
                        self.pomodoro_view.run()
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
                        continue

            except (KeyboardInterrupt, EOFError):
                break

    def _confirm_and_delete_bulk_today(
        self, selected_task_ids: Set[int], tasks: List[DailyTask]
    ) -> Optional[str]:
        """Confirm and execute bulk deletion of daily tasks."""
        clear_viewport()
        count = len(selected_task_ids)
        render_header("DELETE TASKS")
        console.print(f"Remove [bold white]{count} tasks[/bold white] from Today?\n")
        render_footer([("y", "Yes, delete"), ("n", "Cancel")])
        conf = read_key()
        if conf.lower() in ("y", "yes", Key.ENTER, "enter"):
            self.daily_service.delete_daily_tasks_bulk(list(selected_task_ids))
            selected_task_ids.clear()
        return None

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
                selected_idx = min(4, selected_idx + 1)
                continue

            if choice in (Key.ENTER, "enter"):
                choice = str(selected_idx + 1)

            if choice in ("1", "f"):
                self.filemeta_view.run()
                return None

            elif choice in ("2", "t", "tg"):
                self.telegram_view.run()
                return None

            elif choice in ("3", "y", "yt"):
                self.youtube_view.run()
                return None

            elif choice in ("4", "u"):
                self.universal_view.run(select_mode=True)
                return None

            elif choice in ("5", "m"):
                self.manual_view.run()
                return None

            else:
                add_status = "Invalid option. Please choose 1, 2, 3, 4, 5, or 'b' to go back."

