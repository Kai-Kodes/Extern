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
    - Periodic polling (default 60s) for remote changes.
    - Immediate on-demand synchronization via trigger_sync().
    - Thread-safe outbox draining and two-way sync.
    - Graceful startup and shutdown.
    """

    def __init__(
        self,
        google_tasks_service: GoogleTasksService,
        interval: float = 60.0,
        date_provider: Optional[Callable[[], datetime]] = None,
    ):
        self.google_tasks_service = google_tasks_service
        self.interval = max(5.0, interval)
        self.date_provider = date_provider

        self._stop_event = threading.Event()
        self._sync_trigger = threading.Event()
        self._sync_lock = threading.Lock()
        self._worker_thread: Optional[threading.Thread] = None

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

    def sync_now(self) -> Tuple[bool, str]:
        """
        Execute a synchronization cycle immediately in the calling thread,
        respecting the synchronization lock.
        Returns: (success: bool, status_message: str)
        """
        if not self._sync_lock.acquire(blocking=True, timeout=10.0):
            return False, "Sync already in progress"

        try:
            self.is_syncing = True
            success, msg = self.google_tasks_service.sync_all_lists()
            self.last_success = success
            self.last_sync_status = msg
            if success:
                self.last_sync_time = self.get_now()
                self.last_error = None
            else:
                self.last_error = msg
            return success, msg
        finally:
            self.is_syncing = False
            self._sync_lock.release()

    def _worker_loop(self) -> None:
        """Background worker thread loop."""
        while not self._stop_event.is_set():
            # Wait for either next scheduled interval or manual trigger
            triggered = self._sync_trigger.wait(timeout=self.interval)
            self._sync_trigger.clear()

            if self._stop_event.is_set():
                break

            # Skip background sync if not authenticated
            if not self.google_tasks_service.is_authenticated():
                continue

            try:
                self.sync_now()
            except Exception as e:
                self.last_error = str(e)
                self.last_success = False

    def get_status_text(self) -> str:
        """Return a formatted status string for display in headers and footers."""
        if not self.google_tasks_service.has_credentials():
            return "● Not configured"
        if not self.google_tasks_service.is_authenticated():
            return "● Not connected"
        if self.is_syncing:
            return "● Syncing…"
        if self.last_sync_time:
            time_str = self.last_sync_time.strftime("%H:%M")
            return f"● Connected · Synced {time_str}"
        return "● Connected"
