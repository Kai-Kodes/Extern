"""TUI and interaction tests for Chunky Tasks."""

import pytest
from unittest.mock import MagicMock, patch

from todo.models import ChunkyResource, ChunkyTask, DailyTask, FileItem, Source, Task
from todo.ui.app import TodoApp
from todo.ui.chunky_view import ChunkyTaskView
from todo.ui.key_reader import Key
from todo.ui.today_view import TodayView


@pytest.fixture
def mock_chunky_view():
    chunky_service = MagicMock()
    filemeta_view = MagicMock()
    youtube_view = MagicMock()
    telegram_view = MagicMock()
    task_service = MagicMock()
    daily_service = MagicMock()
    daily_service.get_today_str.return_value = "2026-10-23"

    view = ChunkyTaskView(
        chunky_service=chunky_service,
        filemeta_view=filemeta_view,
        youtube_view=youtube_view,
        telegram_view=telegram_view,
        task_service=task_service,
        daily_service=daily_service,
    )
    return view, chunky_service, filemeta_view, youtube_view, telegram_view, task_service, daily_service


def test_category_picker_options_and_cancel(mock_chunky_view):
    view, _, filemeta_view, youtube_view, telegram_view, _, _ = mock_chunky_view

    # Pressing 'b' returns None
    with patch("todo.ui.chunky_view.key_reader.read_key", return_value="b"):
        assert view.run() is None

    # Pressing Escape returns None
    with patch("todo.ui.chunky_view.key_reader.read_key", return_value=Key.ESCAPE):
        assert view.run() is None

    filemeta_view.run.assert_not_called()
    youtube_view.run.assert_not_called()
    telegram_view.run.assert_not_called()


def test_category_picker_dispatches_filemeta(mock_chunky_view):
    view, _, filemeta_view, _, _, _, _ = mock_chunky_view
    filemeta_view.run.return_value = None  # user cancels inside filemeta

    with patch("todo.ui.chunky_view.key_reader.read_key", side_effect=["1", "b"]):
        res = view.run()
        assert res is None
    filemeta_view.run.assert_called_once_with(select_item_mode=True)


def test_category_picker_dispatches_youtube(mock_chunky_view):
    view, _, _, youtube_view, _, _, _ = mock_chunky_view
    youtube_view.run.return_value = None

    with patch("todo.ui.chunky_view.key_reader.read_key", side_effect=["2", "b"]):
        res = view.run()
        assert res is None
    youtube_view.run.assert_called_once_with(select_item_mode=True)


def test_category_picker_dispatches_telegram(mock_chunky_view):
    view, _, _, _, telegram_view, _, _ = mock_chunky_view
    telegram_view.run.return_value = None

    with patch("todo.ui.chunky_view.key_reader.read_key", side_effect=["3", "b"]):
        res = view.run()
        assert res is None
    telegram_view.run.assert_called_once_with(select_item_mode=True)


def test_fully_completed_resource_blocked(mock_chunky_view):
    view, chunky_service, _, _, _, _, _ = mock_chunky_view
    source = Source(id=1, name="YT Playlist", path="youtube://playlist/1")
    item = FileItem(
        id=10,
        source_id=1,
        path="https://youtube.com/watch?v=1",
        title="Completed Video",
        type="video",
        completed=True,
        metadata={"duration": 1800},
    )

    resource_mock = ChunkyResource(
        id=5,
        file_item_id=10,
        source_type="youtube",
        canonical_uri="https://youtube.com/watch?v=1",
        title="Completed Video",
        range_unit="seconds",
        total_range=1800.0,
        completed_range=1800.0,
        is_fully_completed=True,
        fully_completed_at="2026-10-23T10:00:00",
        created_at="2026-10-23T09:00:00",
        updated_at="2026-10-23T10:00:00",
    )
    chunky_service.get_or_create_resource.return_value = resource_mock
    chunky_service.get_completed_ranges.return_value = [(0.0, 1800.0)]

    with patch.object(view, "_show_info_screen") as mock_info:
        res = view._process_resource(source, item)
        assert res is None
        mock_info.assert_called_once()
        assert "RESOURCE FULLY COMPLETED" in mock_info.call_args[0][0]


def test_successful_chunk_scheduling_workflow(mock_chunky_view):
    view, chunky_service, _, _, _, task_service, daily_service = mock_chunky_view
    source = Source(id=1, name="YT Playlist", path="youtube://playlist/1")
    item = FileItem(
        id=10,
        source_id=1,
        path="https://youtube.com/watch?v=1",
        title="Python Deep Dive",
        type="video",
        completed=False,
        metadata={"duration": 3600},
    )

    resource_mock = ChunkyResource(
        id=5,
        file_item_id=10,
        source_type="youtube",
        canonical_uri="https://youtube.com/watch?v=1",
        title="Python Deep Dive",
        range_unit="seconds",
        total_range=3600.0,
        completed_range=0.0,
        is_fully_completed=False,
        fully_completed_at=None,
        created_at="2026-10-23T09:00:00",
        updated_at="2026-10-23T09:00:00",
    )
    chunky_service.get_or_create_resource.return_value = resource_mock
    chunky_service.get_completed_ranges.return_value = []

    mock_task = Task(
        id=77,
        title="Python Deep Dive [00:00:00–00:20:00]",
        description="https://www.youtube.com/watch?v=vid_1&t=0s",
        source_type="youtube",
        source_id=10,
        status="pending",
    )
    mock_chunky_task = ChunkyTask(
        id=1,
        resource_id=5,
        task_id=77,
        chunk_index=1,
        start_pos=0.0,
        end_pos=1200.0,
        is_completed=False,
    )
    chunky_service.create_chunk_task.return_value = (mock_task, mock_chunky_task)

    # 1. Accept default chunk size (20 min) -> Key.ENTER
    # 2. Confirm preview -> Key.ENTER
    with patch("todo.ui.chunky_view.key_reader.read_key", side_effect=[Key.ENTER, Key.ENTER]):
        res = view._process_resource(source, item)
        assert res == "added"

    chunky_service.create_chunk_task.assert_called_once_with(
        resource=resource_mock,
        start_pos=0.0,
        end_pos=1200.0,
        task_service=task_service,
        task_title="Python Deep Dive [00:00:00–00:20:00]",
    )
    daily_service.add_task_to_date.assert_called_once_with(77, "2026-10-23")


def test_today_view_footer_hints_include_chunky():
    daily_service = MagicMock()
    task_service = MagicMock()
    today_view = TodayView(daily_service, task_service)

    # Empty tasks
    with patch("todo.ui.today_view.render_footer") as mock_render:
        today_view.render([], current_date="2026-10-23")
        hints = mock_render.call_args[0][0]
        hint_keys = [h[0] for h in hints]
        assert "c" in hint_keys
        assert any("Chunky" in h[1] for h in hints if h[0] == "c")

    # With tasks
    dummy_task = Task(id=1, title="Test Task", source_type="manual")
    dummy_daily = DailyTask(id=10, task_id=1, date="2026-10-23", completed_at=None, task=dummy_task)
    with patch("todo.ui.today_view.render_footer") as mock_render:
        today_view.render([dummy_daily], current_date="2026-10-23")
        hints = mock_render.call_args[0][0]
        hint_keys = [h[0] for h in hints]
        assert "c" in hint_keys
        assert any("Chunky" in h[1] for h in hints if h[0] == "c")


def test_key_binding_shift_plus_and_c_in_app():
    # Verify app routes 'c', 'C', and Key.SHIFT_PLUS to chunky_view.run
    with patch("todo.ui.app.Database"), \
         patch("todo.ui.app.SourceService"), \
         patch("todo.ui.app.TaskService"), \
         patch("todo.ui.app.DailyService"), \
         patch("todo.ui.app.GoogleTasksSyncManager"), \
         patch("todo.ui.app.GoogleTasksService"), \
         patch("todo.ui.app.GoogleCalendarService"), \
         patch("todo.ui.app.FilemetaAdapter"), \
         patch("todo.ui.app.TelegramAdapter"), \
         patch("todo.ui.app.YouTubeAdapter"):
        app = TodoApp()
        app.chunky_view = MagicMock()
        app.handle_add_task_menu = MagicMock()
        app.daily_service.get_progress_for_date.return_value = (0, 0, 0)
        app.daily_service.get_active_tasks_for_date.return_value = []
        app.daily_service.get_today_str.return_value = "2026-10-23"
        app.google_tasks_sync_manager.change_version = 1
        app.google_tasks_sync_manager.get_status_text.return_value = "Connected"

        # Mock read_key loop for test
        with patch("todo.ui.app.read_key", side_effect=["c", "C", Key.SHIFT_PLUS, "+", Key.ESCAPE]):
            app._main_loop()

        assert app.chunky_view.run.call_count == 3
        assert app.handle_add_task_menu.call_count == 1
