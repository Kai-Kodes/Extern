"""Keyboard input abstraction and raw/cbreak terminal reader."""

import atexit
import contextlib
import os
import select
import sys
import termios
import tty
from typing import Optional


class Key:
    UP = "up"
    DOWN = "down"
    LEFT = "left"
    RIGHT = "right"
    ENTER = "enter"
    ESCAPE = "escape"
    BACKSPACE = "backspace"
    SPACE = "space"
    TAB = "tab"
    DELETE = "delete"
    HOME = "home"
    END = "end"
    PAGE_UP = "pageup"
    PAGE_DOWN = "pagedown"
    PLUS = "+"
    MINUS = "-"


class KeyReader:
    """Centralized terminal keyboard reader supporting cbreak mode and escape sequences."""

    def __init__(self):
        self._orig_termios = None
        self._is_active = False

    def enter(self) -> None:
        """Enter terminal cbreak mode with echo disabled."""
        if sys.stdin.isatty() and not self._is_active:
            fd = sys.stdin.fileno()
            self._orig_termios = termios.tcgetattr(fd)
            # Enter cbreak mode (ICANON and ECHO disabled)
            tty.setcbreak(fd)
            self._is_active = True

    def exit(self) -> None:
        """Restore original terminal settings."""
        if sys.stdin.isatty() and self._is_active and self._orig_termios is not None:
            fd = sys.stdin.fileno()
            termios.tcsetattr(fd, termios.TCSADRAIN, self._orig_termios)
            self._is_active = False

    @contextlib.contextmanager
    def active_mode(self):
        """Context manager to enter cbreak mode and guarantee restoration on exit."""
        self.enter()
        try:
            yield self
        finally:
            self.exit()

    def read_key(self) -> str:
        """
        Read a single key event.
        Returns a Key constant (e.g. Key.UP, Key.ENTER, Key.ESCAPE) or a character string.
        """
        if not sys.stdin.isatty():
            # Non-interactive / piped / testing mode
            line = sys.stdin.readline()
            if not line:
                raise EOFError()
            raw = line.strip()
            if line in (" \n", " ") or raw == "space":
                return Key.SPACE
            if raw in ("up", "\x1b[A", "\x1bOA"):
                return Key.UP
            elif raw in ("down", "\x1b[B", "\x1bOB"):
                return Key.DOWN
            elif raw in ("left", "\x1b[D", "\x1bOD"):
                return Key.LEFT
            elif raw in ("right", "\x1b[C", "\x1bOC"):
                return Key.RIGHT
            elif raw in ("enter", "\r", "\n", ""):
                return Key.ENTER
            elif raw in ("escape", "esc", "\x1b"):
                return Key.ESCAPE
            elif raw in ("backspace", "\x7f", "\x08"):
                return Key.BACKSPACE
            return raw

        # Interactive TTY reading
        was_active = self._is_active
        if not was_active:
            self.enter()

        try:
            fd = sys.stdin.fileno()
            raw1 = os.read(fd, 1)
            if not raw1:
                raise EOFError()
            ch = raw1.decode("utf-8", errors="replace")

            if ch == "\x1b":
                # Check for escape sequence with short timeout (35ms)
                r, _, _ = select.select([fd], [], [], 0.035)
                if not r:
                    return Key.ESCAPE

                raw2 = os.read(fd, 1)
                ch2 = raw2.decode("utf-8", errors="replace")
                if ch2 in ("[", "O"):
                    r2, _, _ = select.select([fd], [], [], 0.035)
                    if not r2:
                        return Key.ESCAPE
                    raw3 = os.read(fd, 1)
                    ch3 = raw3.decode("utf-8", errors="replace")
                    if ch3 == "A":
                        return Key.UP
                    elif ch3 == "B":
                        return Key.DOWN
                    elif ch3 == "C":
                        return Key.RIGHT
                    elif ch3 == "D":
                        return Key.LEFT
                    elif ch3 == "H":
                        return Key.HOME
                    elif ch3 == "F":
                        return Key.END
                    elif ch3 in ("1", "2", "3", "4", "5", "6"):
                        # Possible ~ extension, e.g. [3~ for delete, [5~ for pageup
                        r3, _, _ = select.select([fd], [], [], 0.035)
                        if r3:
                            os.read(fd, 1)  # consumes '~'
                        if ch3 == "3":
                            return Key.DELETE
                        elif ch3 == "5":
                            return Key.PAGE_UP
                        elif ch3 == "6":
                            return Key.PAGE_DOWN
                        elif ch3 == "1":
                            return Key.HOME
                        elif ch3 == "4":
                            return Key.END
                    return Key.ESCAPE
                return Key.ESCAPE

            elif ch in ("\r", "\n"):
                return Key.ENTER
            elif ch == " ":
                return Key.SPACE
            elif ch == "\t":
                return Key.TAB
            elif ch in ("\x7f", "\x08"):
                return Key.BACKSPACE
            elif ch == "\x03":  # Ctrl+C
                raise KeyboardInterrupt()
            elif ch == "\x04":  # Ctrl+D
                raise EOFError()

            return ch
        finally:
            if not was_active:
                self.exit()

    def read_text_input(self, prompt_text: str = "> ") -> str:
        """
        Temporarily pause cbreak mode to read a line of text, then resume cbreak mode.
        """
        if not sys.stdin.isatty():
            line = sys.stdin.readline()
            if not line:
                raise EOFError()
            return line.strip()

        # Temporarily restore normal terminal mode for line input
        from todo.ui.console import console

        was_active = self._is_active
        if was_active:
            self.exit()

        console.show_cursor(True)
        try:
            val = console.input(prompt_text).strip()
        finally:
            console.show_cursor(False)
            if was_active:
                self.enter()

        return val


# Global shared reader instance
key_reader = KeyReader()
atexit.register(key_reader.exit)
