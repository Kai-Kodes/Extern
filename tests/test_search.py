"""Comprehensive unit and integration tests for real-time live fuzzy search.

Tests cover:
- LiveSearchController state transitions
- Real-time keystroke filtering and backspace
- Keystroke swallowing (preventing accidental task/deletion triggers while typing)
- Navigation within filtered results using Up/Down arrows
- Confirmation with Enter (exits typing mode, preserves filtered results)
- Clearing with Esc (restores unfiltered list)
- Multi-token fuzzy ranking and tolerance
"""

from typing import List
from unittest.mock import MagicMock, patch

import pytest

from todo.models import FileItem, Source, Task
from todo.search import LiveSearchController, fuzzy_score, fuzzy_search
from todo.ui.key_reader import Key


# ============================================================================
# LiveSearchController Unit Tests
# ============================================================================


def test_live_search_controller_initial_state():
    controller = LiveSearchController[str]()
    assert not controller.is_active
    assert controller.query == ""

    items = ["Apple", "Banana", "Cherry"]
    assert controller.filter(items) == items


def test_live_search_controller_activate_with_slash():
    controller = LiveSearchController[str]()
    handled, idx = controller.handle_key("/", current_len=5, selected_idx=2)
    assert handled is True
    assert idx == 2
    assert controller.is_active is True
    assert controller.query == ""


def test_live_search_controller_keystrokes_and_backspace():
    controller = LiveSearchController[str]()
    controller.is_active = True

    # Type 'a'
    handled, idx = controller.handle_key("a", current_len=5, selected_idx=3)
    assert handled is True
    assert controller.query == "a"
    assert idx == 0

    # Type 'p'
    handled, idx = controller.handle_key("p", current_len=5, selected_idx=0)
    assert handled is True
    assert controller.query == "ap"

    # Type 'p'
    handled, idx = controller.handle_key("p", current_len=5, selected_idx=0)
    assert handled is True
    assert controller.query == "app"

    # Backspace
    handled, idx = controller.handle_key(Key.BACKSPACE, current_len=5, selected_idx=0)
    assert handled is True
    assert controller.query == "ap"

    # Space key
    handled, idx = controller.handle_key(" ", current_len=5, selected_idx=0)
    assert handled is True
    assert controller.query == "ap "

    handled, idx = controller.handle_key(Key.SPACE, current_len=5, selected_idx=0)
    assert handled is True
    assert controller.query == "ap  "


def test_live_search_controller_swallow_action_keys_while_active():
    """While typing in search mode, action keys ('d', 't', '+', 'n', etc.) must NOT trigger actions."""
    controller = LiveSearchController[str]()
    controller.is_active = True

    # Typing 'd' appends 'd' to query rather than deleting an item
    handled, _ = controller.handle_key("d", current_len=3, selected_idx=0)
    assert handled is True
    assert controller.query == "d"

    # Typing '+' appends '+' to query
    handled, _ = controller.handle_key("+", current_len=3, selected_idx=0)
    assert handled is True
    assert controller.query == "d+"

    # Special key representations like Key.PLUS or arbitrary control characters
    handled, _ = controller.handle_key(Key.PLUS, current_len=3, selected_idx=0)
    assert handled is True


def test_live_search_controller_navigation_while_active():
    """Arrow Up / Down navigate within filtered items even while search is active."""
    controller = LiveSearchController[str]()
    controller.is_active = True

    # Down arrow
    handled, idx = controller.handle_key(Key.DOWN, current_len=5, selected_idx=1)
    assert handled is True
    assert idx == 2

    # Up arrow
    handled, idx = controller.handle_key(Key.UP, current_len=5, selected_idx=2)
    assert handled is True
    assert idx == 1

    # Bounds check
    handled, idx = controller.handle_key(Key.UP, current_len=5, selected_idx=0)
    assert handled is True
    assert idx == 0

    handled, idx = controller.handle_key(Key.DOWN, current_len=5, selected_idx=4)
    assert handled is True
    assert idx == 4


def test_live_search_controller_enter_confirms_and_preserves_filter():
    """Enter exits search mode but keeps the filtered query active."""
    controller = LiveSearchController[str]()
    controller.is_active = True
    controller.query = "python"

    handled, idx = controller.handle_key(Key.ENTER, current_len=3, selected_idx=2)
    assert handled is True
    assert idx == 2
    assert controller.is_active is False
    assert controller.query == "python"

    # Query remains active for filtering
    items = ["python basics", "rust intro", "python advanced"]
    assert len(controller.filter(items)) == 2


def test_live_search_controller_escape_cancels_and_clears():
    """Escape while typing clears query and exits search mode."""
    controller = LiveSearchController[str]()
    controller.is_active = True
    controller.query = "testing"

    handled, idx = controller.handle_key(Key.ESCAPE, current_len=3, selected_idx=2)
    assert handled is True
    assert idx == 0
    assert controller.is_active is False
    assert controller.query == ""


def test_live_search_controller_escape_when_inactive_with_query():
    """Escape when search is inactive but query is present clears the query."""
    controller = LiveSearchController[str]()
    controller.is_active = False
    controller.query = "hello"

    handled, idx = controller.handle_key(Key.ESCAPE, current_len=3, selected_idx=1)
    assert handled is True
    assert idx == 0
    assert controller.query == ""


def test_live_search_controller_reset():
    controller = LiveSearchController[str]()
    controller.is_active = True
    controller.query = "something"
    controller.reset()
    assert controller.is_active is False
    assert controller.query == ""


def test_live_search_filtering_with_custom_models():
    """Verify live search filtering on custom objects like FileItem, Task, Source."""
    items = [
        FileItem(id=1, source_id=1, path="1", title="01 - Docker Setup", completed=False),
        FileItem(id=2, source_id=1, path="2", title="02 - Kubernetes Basics", completed=False),
        FileItem(id=3, source_id=1, path="3", title="03 - Docker Compose", completed=True),
    ]

    controller = LiveSearchController[FileItem]()

    # Filter with 'dock'
    controller.query = "dock"
    filtered = controller.filter(items)
    assert len(filtered) == 2
    assert [f.id for f in filtered] == [1, 3]

    # Filter with 'kube'
    controller.query = "kube"
    filtered = controller.filter(items)
    assert len(filtered) == 1
    assert filtered[0].id == 2
