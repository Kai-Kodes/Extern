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
from todo.services.google_calendar_service import GoogleCalendarService
from todo.services.google_tasks_service import GoogleTasksService
from todo.services.google_tasks_sync_manager import GoogleTasksSyncManager
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
from todo.ui.google_tasks_view import GoogleTasksView
from todo.ui.key_reader import Key, key_reader
from todo.ui.manual_view import ManualView
from todo.ui.pomodoro_view import PomodoroView
from todo.ui.task_editor import TaskEditor
from todo.ui.telegram_view import TelegramView
from todo.ui.timeline_view import TimelineView
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
        self.google_tasks_service = GoogleTasksService(
            db=self.db,
            config=self.config,
            date_provider=self.date_provider,
        )
        self.google_calendar_service = GoogleCalendarService(
            db=self.db,
            config=self.config,
            date_provider=self.date_provider,
            oauth_manager=self.google_tasks_service.oauth_manager,
        )
        self.google_tasks_sync_manager = GoogleTasksSyncManager(
            google_tasks_service=self.google_tasks_service,
            google_calendar_service=self.google_calendar_service,
            interval=self.config.google_sync_interval,
            idle_interval=self.config.google_sync_idle_interval,
            error_interval=self.config.google_sync_error_interval,
            date_provider=self.date_provider,
        )
        self.google_tasks_service.sync_manager = self.google_tasks_sync_manager
        self.google_calendar_service.sync_manager = self.google_tasks_sync_manager
        self.task_service.google_tasks_service = self.google_tasks_service
        self.task_service.google_calendar_service = self.google_calendar_service
        self.daily_service.google_tasks_service = self.google_tasks_service
        self.daily_service.google_calendar_service = self.google_calendar_service
        self.daily_service.google_tasks_sync_manager = self.google_tasks_sync_manager
        self.task_editor = TaskEditor(
            task_service=self.task_service,
            daily_service=self.daily_service,
            google_tasks_service=self.google_tasks_service,
            google_calendar_service=self.google_calendar_service,
        )
        self.google_tasks_view = GoogleTasksView(
            google_tasks_service=self.google_tasks_service,
            daily_service=self.daily_service,
            task_editor=self.task_editor,
            sync_manager=self.google_tasks_sync_manager,
        )
        self.universal_view = UniversalView(
            self.task_service,
            self.daily_service,
            task_editor=self.task_editor,
            sync_manager=self.google_tasks_sync_manager,
        )
        self.manual_view = ManualView(self.task_service, self.daily_service)
        self.timeline_view = TimelineView(
            daily_service=self.daily_service,
            task_service=self.task_service,
            add_task_menu_fn=self.handle_add_task_menu,
            date_provider=self.date_provider,
            task_editor=self.task_editor,
            sync_manager=self.google_tasks_sync_manager,
        )

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
            self.google_tasks_sync_manager.start()
            with tui_mode():
                self._main_loop()
        finally:
            self.google_tasks_sync_manager.stop()
            self.telegram_adapter.disconnect()

    def _main_loop(self) -> None:
        status_msg: Optional[str] = None
        status_style: str = "green"
        selected_idx = 0
        selected_task_id: Optional[int] = None
        selected_task_ids: Set[int] = set()
        mode: Optional[str] = None
        last_rendered_version = -1
        last_rendered_date = ""
        last_rendered_mode = None
        last_rendered_sync_status = ""
        needs_redraw = True

        while True:
            try:
                today_str = self.daily_service.get_today_str()
                current_version = self.google_tasks_sync_manager.change_version
                cur_sync_status = self.google_tasks_sync_manager.get_status_text()

                if (
                    needs_redraw
                    or current_version != last_rendered_version
                    or today_str != last_rendered_date
                    or mode != last_rendered_mode
                    or cur_sync_status != last_rendered_sync_status
                    or status_msg is not None
                ):
                    tasks = self.daily_service.get_active_tasks_for_date(today_str)
                    completed_cnt, total_cnt, _ = self.daily_service.get_progress_for_date(today_str)

                    # Clean up any stale selected_task_ids not in tasks
                    valid_ids = {dt.id for dt in tasks}
                    selected_task_ids.intersection_update(valid_ids)

                    # Anchor cursor stably by task ID across remote updates
                    if tasks:
                        if selected_task_id is not None:
                            found_idx = next((i for i, dt in enumerate(tasks) if dt.id == selected_task_id), None)
                            if found_idx is not None:
                                selected_idx = found_idx
                            else:
                                selected_idx = max(0, min(selected_idx, len(tasks) - 1))
                        else:
                            selected_idx = max(0, min(selected_idx, len(tasks) - 1))
                        selected_task_id = tasks[selected_idx].id
                    else:
                        selected_idx = 0
                        selected_task_id = None

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
                        sync_status=cur_sync_status,
                    )
                    last_rendered_version = current_version
                    last_rendered_date = today_str
                    last_rendered_mode = mode
                    last_rendered_sync_status = cur_sync_status
                    status_msg = None
                    status_style = "green"
                    needs_redraw = False

                choice = read_key(timeout=0.2)
                if choice is None:
                    # Timeout elapsed: no key pressed.
                    # Redraw only if remote sync mutated local data, date changed, or status text changed
                    cur_today = self.daily_service.get_today_str()
                    cur_ver = self.google_tasks_sync_manager.change_version
                    cur_status = self.google_tasks_sync_manager.get_status_text()
                    if (
                        cur_ver != last_rendered_version
                        or cur_today != last_rendered_date
                        or cur_status != last_rendered_sync_status
                    ):
                        needs_redraw = True
                    continue

                self.google_tasks_sync_manager.record_user_activity()
                needs_redraw = True

                if mode == "delete":
                    if choice in (Key.ESCAPE, "escape", "q", "cancel"):
                        selected_task_ids.clear()
                        mode = None
                        continue
                    if choice in (Key.UP, "up") and tasks:
                        selected_idx = max(0, selected_idx - 1)
                        selected_task_id = tasks[selected_idx].id
                        continue
                    if choice in (Key.DOWN, "down") and tasks:
                        selected_idx = min(len(tasks) - 1, selected_idx + 1)
                        selected_task_id = tasks[selected_idx].id
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
                        selected_task_id = tasks[selected_idx].id
                        continue
                    if choice in (Key.DOWN, "down") and tasks:
                        selected_idx = min(len(tasks) - 1, selected_idx + 1)
                        selected_task_id = tasks[selected_idx].id
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
                            selected_task_id = tasks[selected_idx].id
                        continue

                    if choice in (Key.DOWN, "down"):
                        if tasks:
                            selected_idx = min(len(tasks) - 1, selected_idx + 1)
                            selected_task_id = tasks[selected_idx].id
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

                    if choice.lower() in ("e", "edit"):
                        if tasks and 0 <= selected_idx < len(tasks):
                            selected_daily = tasks[selected_idx]
                            updated, msg = self.task_editor.edit_task(
                                selected_daily.task, current_date=selected_daily.date
                            )
                            if updated:
                                status_msg = msg
                                status_style = "green"
                            elif msg:
                                status_msg = msg
                                status_style = "yellow"
                        continue

                    if choice.lower() in ("g", "google", "gtasks"):
                        self.google_tasks_view.run()
                        continue

                    if choice.lower() in ("l", "timeline"):
                        self.timeline_view.run()
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

    def handle_add_task_menu(self, target_date: Optional[str] = None) -> Optional[str]:
        """Show Add Task menu and dispatch to the corresponding workflow."""
        add_status: Optional[str] = None
        selected_idx = 0

        while True:
            self.add_task_view.render(
                status_msg=add_status,
                selected_idx=selected_idx,
                clear=True,
                target_date=target_date,
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
                self.universal_view.run(select_mode=True, target_date=target_date)
                return None

            elif choice in ("5", "m"):
                self.manual_view.run(target_date=target_date)
                return None

            else:
                add_status = "Invalid option. Please choose 1, 2, 3, 4, 5, or 'b' to go back."

