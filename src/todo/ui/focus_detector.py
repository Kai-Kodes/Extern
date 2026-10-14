"""Window focus detection service for X11/Debian KDE."""

import os
import shutil
import subprocess
import time
from typing import Optional


class FocusDetector:
    """
    Detects whether the terminal running Extern currently has window focus under X11/Debian.
    Includes rate-limiting/caching to prevent CPU usage, and graceful fallback when X11 tools are missing.
    """

    def __init__(
        self,
        poll_interval: float = 0.5,
        terminal_window_id: Optional[int] = None,
    ):
        self.poll_interval = poll_interval
        self._cached_focused: bool = True
        self._last_check_time: float = 0.0
        self._xdotool_available = shutil.which("xdotool") is not None
        self._xprop_available = shutil.which("xprop") is not None
        self._terminal_window_id: Optional[int] = terminal_window_id
        if self._terminal_window_id is None:
            self._terminal_window_id = self._detect_initial_terminal_window()

    def _detect_initial_terminal_window(self) -> Optional[int]:
        """Attempt to identify the terminal's window ID at startup."""
        # 1. Check WINDOWID environment variable
        env_winid = os.environ.get("WINDOWID")
        if env_winid and env_winid.isdigit():
            return int(env_winid)

        # 2. Check active window right now (assuming process starts with focus)
        active = self.get_active_window_id()
        if active is not None:
            return active

        return None

    def set_terminal_window_id(self, window_id: int) -> None:
        """Manually override or record the terminal window ID."""
        self._terminal_window_id = window_id

    def get_terminal_window_id(self) -> Optional[int]:
        return self._terminal_window_id

    def is_available(self) -> bool:
        """Return True if X11 focus tools are available and terminal window was identified."""
        return bool((self._xdotool_available or self._xprop_available) and (self._terminal_window_id is not None))

    def get_active_window_id(self) -> Optional[int]:
        """Query X11 for the current active window ID."""
        if self._xdotool_available:
            try:
                res = subprocess.run(
                    ["xdotool", "getactivewindow"],
                    capture_output=True,
                    text=True,
                    timeout=0.3,
                )
                if res.returncode == 0 and res.stdout.strip().isdigit():
                    return int(res.stdout.strip())
            except Exception:
                pass

        if self._xprop_available:
            try:
                res = subprocess.run(
                    ["xprop", "-root", "_NET_ACTIVE_WINDOW"],
                    capture_output=True,
                    text=True,
                    timeout=0.3,
                )
                if res.returncode == 0:
                    for part in res.stdout.strip().split():
                        if part.startswith("0x"):
                            return int(part, 16)
            except Exception:
                pass

        return None

    def is_terminal_focused(self, force_refresh: bool = False) -> bool:
        """
        Check if terminal currently has focus.
        Results are cached for self.poll_interval seconds to avoid subprocess spam.
        """
        now = time.monotonic()
        if not force_refresh and (now - self._last_check_time < self.poll_interval):
            return self._cached_focused

        self._last_check_time = now

        if not self.is_available():
            self._cached_focused = True
            return True

        active_win = self.get_active_window_id()
        if active_win is None:
            return self._cached_focused

        self._cached_focused = (active_win == self._terminal_window_id)
        return self._cached_focused
