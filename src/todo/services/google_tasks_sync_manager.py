"""Background synchronization manager for Google Tasks."""

import threading
import time
from datetime import datetime
from typing import Callable, Optional, Tuple

from todo.services.google_tasks_service import GoogleTasksService


class GoogleTasksSyncManager:
    """
    Coordinates asynchronous, non-blocking background synchronization for Google Tasks:
    - Auto-sync on startup without blocking the TUI.
    - Adaptive polling (2-5s active, backing off to 15s idle and 30s on errors).
    - Immediate on-demand synchronization via trigger_sync().
    - Thread-safe change_version increment to avoid UI screen flicker.
    - Thread-safe outbox draining and incremental sync.
    - Graceful startup and shutdown.
    """

    def __init__(
        self,
        google_tasks_service: GoogleTasksService,
        interval: Optional[float] = None,
        idle_interval: Optional[float] = None,
        error_interval: Optional[float] = None,
        date_provider: Optional[Callable[[], datetime]] = None,
    ):
        self.google_tasks_service = google_tasks_service
        cfg = getattr(google_tasks_service, "config", None)
        self.active_interval = float(interval if interval is not None else getattr(cfg, "google_sync_interval", 3.0))
        self.idle_interval = float(idle_interval if idle_interval is not None else getattr(cfg, "google_sync_idle_interval", 15.0))
        self.error_interval = float(error_interval if error_interval is not None else getattr(cfg, "google_sync_error_interval", 30.0))
        self.interval = self.active_interval
        self.date_provider = date_provider

        self._stop_event = threading.Event()
        self._sync_trigger = threading.Event()
        self._sync_lock = threading.Lock()
        self._change_lock = threading.Lock()
        self._change_version: int = 0
        self._worker_thread: Optional[threading.Thread] = None

        self.last_user_activity: float = time.time()
        self._in_error_backoff: bool = False
        self.is_syncing: bool = False
        self.last_sync_time: Optional[datetime] = None
        self.last_sync_status: str = "Idle"
        self.last_error: Optional[str] = None
        self.last_success: bool = False

    def get_now(self) -> datetime:
        return self.date_provider() if self.date_provider else datetime.now()

    @property
    def is_running(self) -> bool:
        return self._worker_thread is not None and self._worker_thread.is_alive()

    @property
    def change_version(self) -> int:
        """Thread-safe accessor for the current change version number."""
        with self._change_lock:
            return self._change_version

    def notify_change(self) -> None:
        """Increment change_version and signal connected UI views that data mutated."""
        with self._change_lock:
            self._change_version += 1

    def record_user_activity(self) -> None:
        """Record user input to keep sync polling in active mode."""
        self.last_user_activity = time.time()

    def get_current_interval(self) -> float:
        """Determine polling interval based on user activity and error state."""
        if self._in_error_backoff:
            return self.error_interval
        if time.time() - self.last_user_activity > 30.0:
            return self.idle_interval
        return self.active_interval

    def start(self) -> None:
        """Start the background synchronization worker daemon."""
        if self.is_running:
            return

        self._stop_event.clear()
        self._sync_trigger.set()  # Trigger initial sync on startup
        self._worker_thread = threading.Thread(
            target=self._worker_loop,
            name="GoogleTasksSyncWorker",
            daemon=True,
        )
        self._worker_thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        """Stop the background synchronization worker and wait for clean exit."""
        if not self.is_running:
            return

        self._stop_event.set()
        self._sync_trigger.set()  # Wake up worker if sleeping
        if self._worker_thread:
            self._worker_thread.join(timeout=timeout)
            self._worker_thread = None

    def trigger_sync(self) -> None:
        """Request an immediate synchronization run."""
        self._sync_trigger.set()

    def sync_now(self, incremental: bool = True) -> Tuple[bool, str]:
        """
        Execute a synchronization cycle immediately in the calling thread,
        respecting the synchronization lock.
        Returns: (success: bool, status_message: str)
        """
        if not self._sync_lock.acquire(blocking=True, timeout=10.0):
            return False, "Sync already in progress"

        try:
            self.is_syncing = True
            summary = self.google_tasks_service.sync_all_lists(incremental=incremental)
            success = summary[0]
            msg = summary[1]
            has_changes = getattr(summary, "has_changes", False)

            self.last_success = success
            self.last_sync_status = msg
            if success:
                self.last_sync_time = self.get_now()
                self.last_error = None
                self._in_error_backoff = False
                if has_changes:
                    self.notify_change()
            else:
                self.last_error = msg
                self._in_error_backoff = True
            return success, msg
        finally:
            self.is_syncing = False
            self._sync_lock.release()

    def _worker_loop(self) -> None:
        """Background worker thread loop with adaptive polling."""
        first_run = True
        while not self._stop_event.is_set():
            wait_time = 0.05 if first_run else self.get_current_interval()
            triggered = self._sync_trigger.wait(timeout=wait_time)
            self._sync_trigger.clear()
            first_run = False

            if self._stop_event.is_set():
                break

            # Skip background sync if not authenticated
            if not self.google_tasks_service.is_authenticated():
                continue

            try:
                self.sync_now(incremental=True)
            except Exception as e:
                self.last_error = str(e)
                self.last_success = False
                self._in_error_backoff = True

    def get_status_text(self) -> str:
        """Return a formatted status string for display in headers and footers."""
        if not self.google_tasks_service.has_credentials():
            return "● Not configured"
        if not self.google_tasks_service.is_authenticated():
            return "● Not connected"
        if self.is_syncing:
            return "● Syncing…"
        if self.last_error and not self.last_success:
            return "● Offline / Sync error"
        if self.last_sync_time:
            time_str = self.last_sync_time.strftime("%H:%M")
            return f"● Connected · Synced {time_str}"
        return "● Connected"
