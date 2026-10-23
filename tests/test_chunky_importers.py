"""Tests for Chunky Task metadata extraction and importer integration."""

import pytest
from unittest.mock import MagicMock, patch

from todo.models import FileItem, Source
from todo.ui.chunky_view import ChunkyTaskView
from todo.ui.key_reader import Key


@pytest.fixture
def mock_services():
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


def test_youtube_natural_units_seconds(mock_services):
    view, chunky_service, _, _, _, _, _ = mock_services
    source = Source(id=1, name="Python Course", path="youtube://playlist/PL123")
    item = FileItem(
        id=10,
        source_id=1,
        path="https://youtube.com/watch?v=vid_1",
        title="Intro to Python",
        type="video",
        completed=False,
        metadata={"duration": 3600, "canonical_url": "https://youtube.com/watch?v=vid_1"},
    )

    resource_mock = MagicMock()
    resource_mock.id = 100
    resource_mock.total_range = 3600.0
    resource_mock.range_unit = "seconds"
    resource_mock.is_fully_completed = False
    resource_mock.title = "Intro to Python"
    chunky_service.get_or_create_resource.return_value = resource_mock
    chunky_service.get_completed_ranges.return_value = []

    with patch.object(view, "_prompt_custom_size", return_value=None), \
         patch.object(view, "_preview_and_confirm", return_value="cancel"), \
         patch("todo.ui.chunky_view.key_reader.read_key", side_effect=[Key.ENTER]):
        res = view._process_resource(source, item)
        assert res is None

    chunky_service.get_or_create_resource.assert_called_once_with(
        source_type="youtube",
        canonical_uri="https://youtube.com/watch?v=vid_1",
        title="Intro to Python",
        total_range=3600.0,
        range_unit="seconds",
        file_item_id=10,
    )


def test_youtube_missing_duration_shows_error(mock_services):
    view, chunky_service, _, _, _, _, _ = mock_services
    source = Source(id=1, name="Python Course", path="youtube://playlist/PL123")
    item = FileItem(
        id=10,
        source_id=1,
        path="https://youtube.com/watch?v=vid_no_dur",
        title="Live Stream",
        type="video",
        completed=False,
        metadata={},
    )

    with patch.object(view, "_show_info_screen") as mock_info:
        res = view._process_resource(source, item)
        assert res is None
        mock_info.assert_called_once()
        assert "CANNOT CHUNK YOUTUBE VIDEO" in mock_info.call_args[0][0]
    chunky_service.get_or_create_resource.assert_not_called()


def test_telegram_natural_units_seconds(mock_services):
    view, chunky_service, _, _, _, _, _ = mock_services
    source = Source(id=2, name="Math Channel", path="telegram://-100123")
    item = FileItem(
        id=20,
        source_id=2,
        path="telegram://-100123/42",
        title="Lesson 42: Calculus",
        type="video",
        completed=False,
        metadata={"duration": 1800},
    )

    resource_mock = MagicMock()
    resource_mock.id = 101
    resource_mock.total_range = 1800.0
    resource_mock.range_unit = "seconds"
    resource_mock.is_fully_completed = False
    chunky_service.get_or_create_resource.return_value = resource_mock
    chunky_service.get_completed_ranges.return_value = []

    with patch.object(view, "_preview_and_confirm", return_value="cancel"), \
         patch("todo.ui.chunky_view.key_reader.read_key", side_effect=[Key.ENTER]):
        res = view._process_resource(source, item)
        assert res is None

    chunky_service.get_or_create_resource.assert_called_once_with(
        source_type="telegram",
        canonical_uri="telegram://-100123/42",
        title="Lesson 42: Calculus",
        total_range=1800.0,
        range_unit="seconds",
        file_item_id=20,
    )


def test_telegram_missing_duration_rejected(mock_services):
    view, chunky_service, _, _, _, _, _ = mock_services
    source = Source(id=2, name="Math Channel", path="telegram://-100123")
    item = FileItem(
        id=21,
        source_id=2,
        path="telegram://-100123/43",
        title="Text Announcement",
        type="text",
        completed=False,
        metadata={"duration": 0},
    )

    with patch.object(view, "_show_info_screen") as mock_info:
        res = view._process_resource(source, item)
        assert res is None
        mock_info.assert_called_once()
        assert "CANNOT CHUNK TELEGRAM LESSON" in mock_info.call_args[0][0]
    chunky_service.get_or_create_resource.assert_not_called()


def test_filemeta_paginated_document_uses_pages(mock_services):
    view, chunky_service, _, _, _, _, _ = mock_services
    source = Source(id=3, name="Books", path="/home/user/books")
    item = FileItem(
        id=30,
        source_id=3,
        path="/home/user/books/book.pdf",
        title="Design Patterns Book",
        type="pdf",
        completed=False,
        metadata={"pages": 395},
    )

    resource_mock = MagicMock()
    resource_mock.id = 102
    resource_mock.total_range = 395.0
    resource_mock.range_unit = "pages"
    resource_mock.is_fully_completed = False
    chunky_service.get_or_create_resource.return_value = resource_mock
    chunky_service.get_completed_ranges.return_value = []

    with patch.object(view, "_preview_and_confirm", return_value="cancel"), \
         patch("todo.ui.chunky_view.key_reader.read_key", side_effect=[Key.ENTER]):
        res = view._process_resource(source, item)
        assert res is None

    chunky_service.get_or_create_resource.assert_called_once_with(
        source_type="filemeta",
        canonical_uri="/home/user/books/book.pdf",
        title="Design Patterns Book",
        total_range=395.0,
        range_unit="pages",
        file_item_id=30,
    )


def test_filemeta_media_with_duration_uses_seconds(mock_services):
    view, chunky_service, _, _, _, _, _ = mock_services
    source = Source(id=3, name="Recordings", path="/home/user/audio")
    item = FileItem(
        id=31,
        source_id=3,
        path="/home/user/audio/lecture.mp3",
        title="Physics Lecture",
        type="audio",
        completed=False,
        metadata={"duration": 5400},
    )

    resource_mock = MagicMock()
    resource_mock.id = 103
    resource_mock.total_range = 5400.0
    resource_mock.range_unit = "seconds"
    resource_mock.is_fully_completed = False
    chunky_service.get_or_create_resource.return_value = resource_mock
    chunky_service.get_completed_ranges.return_value = []

    with patch.object(view, "_preview_and_confirm", return_value="cancel"), \
         patch("todo.ui.chunky_view.key_reader.read_key", side_effect=[Key.ENTER]):
        res = view._process_resource(source, item)
        assert res is None

    chunky_service.get_or_create_resource.assert_called_once_with(
        source_type="filemeta",
        canonical_uri="/home/user/audio/lecture.mp3",
        title="Physics Lecture",
        total_range=5400.0,
        range_unit="seconds",
        file_item_id=31,
    )


def test_filemeta_missing_pages_and_duration_rejected(mock_services):
    view, chunky_service, _, _, _, _, _ = mock_services
    source = Source(id=3, name="Docs", path="/home/user/notes")
    item = FileItem(
        id=32,
        source_id=3,
        path="/home/user/notes/notes.txt",
        title="Quick Notes",
        type="text",
        completed=False,
        metadata={},
    )

    with patch.object(view, "_show_info_screen") as mock_info:
        res = view._process_resource(source, item)
        assert res is None
        mock_info.assert_called_once()
        assert "CANNOT CHUNK FILEMETA RESOURCE" in mock_info.call_args[0][0]
    chunky_service.get_or_create_resource.assert_not_called()
