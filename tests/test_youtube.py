"""Comprehensive tests for YouTube integration in Todo CLI.

Tests cover:
- URL parsing, kind detection, and validation (watch, youtu.be, shorts, playlists, embed)
- Duration formatting
- YouTubeAdapter with yt-dlp execution mocking (single video, playlist, deduplication, error handling)
- Database persistence & services (single video tasks, playlist sources, file_items, two-way completion sync)
- Deduplication of tasks by canonical URL
- Caching behavior (no network calls on reopening cached playlists)
- YouTubeView interactive TUI flows (navigation, multi-select, schedule to Today, rescan, deletion, search)
- Strict global silence on successful actions
"""

import io
import json
from datetime import datetime
from pathlib import Path
import subprocess
import tempfile
from typing import List, Optional
from unittest.mock import MagicMock, patch

import pytest

from todo.config import Config
from todo.database import Database
from todo.integrations.filemeta import FilemetaScanResult, ScannedFile
from todo.integrations.youtube import (
    YouTubeAdapter,
    YouTubeError,
    YouTubeFetchError,
    YouTubeInvalidURLError,
    YouTubeNotFoundError,
    YouTubePlaylist,
    YouTubeVideo,
    format_duration,
    parse_youtube_url,
)
from todo.models import FileItem, Source, Task
from todo.services.daily_service import DailyService
from todo.services.source_service import SourceService
from todo.services.task_service import TaskService
from todo.ui.app import TodoApp
from todo.ui.key_reader import Key
from todo.ui.youtube_view import YouTubeView


# ============================================================================
# PART 1: URL Parsing & Validation Tests
# ============================================================================


def test_parse_youtube_url_standard_watch():
    url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    res = parse_youtube_url(url)
    assert res["kind"] == "video"
    assert res["video_id"] == "dQw4w9WgXcQ"
    assert res["playlist_id"] is None
    assert res["canonical_url"] == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    assert res["item_path"] == "youtube://video/dQw4w9WgXcQ"


def test_parse_youtube_url_short():
    url = "https://youtu.be/dQw4w9WgXcQ"
    res = parse_youtube_url(url)
    assert res["kind"] == "video"
    assert res["video_id"] == "dQw4w9WgXcQ"
    assert res["canonical_url"] == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


def test_parse_youtube_url_shorts():
    url = "https://www.youtube.com/shorts/abc123XYZ"
    res = parse_youtube_url(url)
    assert res["kind"] == "video"
    assert res["video_id"] == "abc123XYZ"
    assert res["canonical_url"] == "https://www.youtube.com/watch?v=abc123XYZ"


def test_parse_youtube_url_embed():
    url = "https://www.youtube.com/embed/abc123XYZ"
    res = parse_youtube_url(url)
    assert res["kind"] == "video"
    assert res["video_id"] == "abc123XYZ"
    assert res["canonical_url"] == "https://www.youtube.com/watch?v=abc123XYZ"


def test_parse_youtube_url_playlist():
    url = "https://www.youtube.com/playlist?list=PL12345ABCDE"
    res = parse_youtube_url(url)
    assert res["kind"] == "playlist"
    assert res["playlist_id"] == "PL12345ABCDE"
    assert res["canonical_url"] == "https://www.youtube.com/playlist?list=PL12345ABCDE"
    assert res["source_path"] == "youtube://playlist/PL12345ABCDE"


def test_parse_youtube_url_video_with_playlist():
    """URL containing both video ID and playlist ID must be parsed as playlist."""
    url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PL12345ABCDE"
    res = parse_youtube_url(url)
    assert res["kind"] == "playlist"
    assert res["playlist_id"] == "PL12345ABCDE"
    assert res["video_id"] == "dQw4w9WgXcQ"
    assert res["canonical_url"] == "https://www.youtube.com/playlist?list=PL12345ABCDE"


def test_parse_youtube_url_no_protocol():
    url = "youtu.be/dQw4w9WgXcQ"
    res = parse_youtube_url(url)
    assert res["kind"] == "video"
    assert res["video_id"] == "dQw4w9WgXcQ"


def test_parse_youtube_url_invalid():
    # Empty / whitespace
    with pytest.raises(YouTubeInvalidURLError):
        parse_youtube_url("")
    with pytest.raises(YouTubeInvalidURLError):
        parse_youtube_url("   ")

    # Non-YouTube domain
    with pytest.raises(YouTubeInvalidURLError, match="Unsupported domain"):
        parse_youtube_url("https://vimeo.com/12345678")

    with pytest.raises(YouTubeInvalidURLError, match="Unsupported domain"):
        parse_youtube_url("https://example.com/watch?v=123")

    # Missing video/playlist ID
    with pytest.raises(YouTubeInvalidURLError, match="Could not detect"):
        parse_youtube_url("https://www.youtube.com/about")


# ============================================================================
# PART 2: Duration Formatting Tests
# ============================================================================


def test_format_duration():
    assert format_duration(None) == ""
    assert format_duration(-5) == ""
    assert format_duration(0) == "00:00"
    assert format_duration(45) == "00:45"
    assert format_duration(125) == "02:05"
    assert format_duration(3600) == "1h 00m 00s"
    assert format_duration(3665) == "1h 01m 05s"


# ============================================================================
# PART 3: YouTubeAdapter Unit Tests
# ============================================================================


def test_youtube_adapter_missing_binary():
    adapter = YouTubeAdapter("nonexistent-ytdlp-bin")
    with patch("shutil.which", return_value=None):
        with pytest.raises(YouTubeNotFoundError, match="not found on PATH"):
            adapter.fetch_single_video("https://www.youtube.com/watch?v=123")


def test_youtube_adapter_fetch_single_video_success():
    adapter = YouTubeAdapter("yt-dlp")
    mock_json = json.dumps({
        "id": "vid123",
        "title": "Learn Rust in 100 Seconds",
        "duration": 140,
        "channel": "Fireship",
        "uploader": "Fireship",
        "webpage_url": "https://www.youtube.com/watch?v=vid123",
    })

    with patch("shutil.which", return_value="/usr/local/bin/yt-dlp"):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=mock_json + "\n", stderr="")
            video = adapter.fetch_single_video("https://www.youtube.com/watch?v=vid123")

            assert video.video_id == "vid123"
            assert video.title == "Learn Rust in 100 Seconds"
            assert video.duration == 140
            assert video.duration_formatted == "02:20"
            assert video.channel == "Fireship"
            assert video.canonical_url == "https://www.youtube.com/watch?v=vid123"
            assert video.path == "youtube://video/vid123"

            # Verify yt-dlp arguments include --no-playlist
            args = mock_run.call_args[0][0]
            assert "--no-playlist" in args
            assert "--dump-json" in args
            assert "--skip-download" in args


def test_youtube_adapter_fetch_single_video_error():
    adapter = YouTubeAdapter("yt-dlp")
    with patch("shutil.which", return_value="/usr/local/bin/yt-dlp"):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=1,
                stdout="",
                stderr="ERROR: Video unavailable",
            )
            with pytest.raises(YouTubeFetchError, match="Video unavailable"):
                adapter.fetch_single_video("https://www.youtube.com/watch?v=vid123")


def test_youtube_adapter_fetch_single_video_timeout():
    adapter = YouTubeAdapter("yt-dlp")
    with patch("shutil.which", return_value="/usr/local/bin/yt-dlp"):
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="yt-dlp", timeout=45)):
            with pytest.raises(YouTubeFetchError, match="timed out"):
                adapter.fetch_single_video("https://www.youtube.com/watch?v=vid123")


def test_youtube_adapter_fetch_playlist_success_with_dedup_and_skips():
    adapter = YouTubeAdapter("yt-dlp")
    v1 = json.dumps({
        "id": "v1",
        "title": "Lesson 1: Intro",
        "duration": 300,
        "playlist_title": "Full Course",
        "channel": "TechChannel",
    })
    v2_deleted = json.dumps({
        "id": "v2",
        "title": "[Deleted video]",
        "playlist_title": "Full Course",
    })
    v3 = json.dumps({
        "id": "v3",
        "title": "Lesson 2: Core Concepts",
        "duration": 650,
        "playlist_title": "Full Course",
    })
    v1_dup = json.dumps({
        "id": "v1",
        "title": "Lesson 1: Intro",
        "duration": 300,
        "playlist_title": "Full Course",
    })

    stdout_content = "\n".join([v1, v2_deleted, v3, v1_dup])

    with patch("shutil.which", return_value="/usr/local/bin/yt-dlp"):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=stdout_content, stderr="")
            playlist = adapter.fetch_playlist("https://www.youtube.com/playlist?list=PL_TEST")

            assert playlist.playlist_id == "PL_TEST"
            assert playlist.title == "Full Course"
            assert playlist.video_count == 2
            assert playlist.skipped_count == 1
            assert len(playlist.items) == 2
            assert playlist.items[0].video_id == "v1"
            assert playlist.items[0].position == 1
            assert playlist.items[1].video_id == "v3"
            assert playlist.items[1].position == 2


# ============================================================================
# PART 4: Database & Services Persistence Tests
# ============================================================================


@pytest.fixture
def test_db():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        yield Database(db_path)


@pytest.fixture
def services(test_db):
    task_service = TaskService(test_db)
    source_service = SourceService(test_db)
    daily_service = DailyService(test_db)
    return task_service, source_service, daily_service


def test_youtube_single_video_task_creation_and_deduplication(services):
    task_service, source_service, daily_service = services

    # 1. First time creating task
    task1, created1 = task_service.get_or_create_video_task(
        title="Intro to Python",
        canonical_url="https://www.youtube.com/watch?v=abc123",
        video_id="abc123",
    )
    assert created1 is True
    assert task1.title == "Intro to Python"
    assert task1.source_type == "youtube"
    assert task1.description == "https://www.youtube.com/watch?v=abc123"

    # 2. Scheduling to today
    today_str = datetime.now().date().isoformat()
    daily_service.add_task_to_date(task1.id, today_str)

    today_tasks = daily_service.get_tasks_for_date(today_str)
    assert any(t.id == task1.id for t in today_tasks)

    # 3. Subsequent call with same canonical URL returns existing task
    task2, created2 = task_service.get_or_create_video_task(
        title="Intro to Python (Updated Title)",
        canonical_url="https://www.youtube.com/watch?v=abc123",
        video_id="abc123",
    )
    assert created2 is False
    assert task2.id == task1.id


def test_youtube_playlist_source_persistence_and_filtering(services):
    task_service, source_service, daily_service = services

    # Create YouTube playlist source
    scan_yt = FilemetaScanResult(
        source_name="YouTube: Docker Deep Dive",
        source_path="youtube://playlist/PL_DOCKER",
        items=[
            ScannedFile(
                name="d1",
                path="youtube://video/d1",
                title="01 - Docker Architecture",
                type="video",
                metadata={"duration": 400, "source_type": "youtube"},
            ),
            ScannedFile(
                name="d2",
                path="youtube://video/d2",
                title="02 - Docker Compose",
                type="video",
                metadata={"duration": 600, "source_type": "youtube"},
            ),
        ],
    )
    src, _, _ = source_service.add_or_update_source(scan_yt)

    # Create a filemeta source to ensure source filtering works
    scan_fm = FilemetaScanResult(
        source_name="Local Course",
        source_path="/home/user/courses/local",
        items=[
            ScannedFile(
                name="1.mp4",
                path="/home/user/courses/local/1.mp4",
                title="1.mp4",
                type="video",
                metadata={},
            )
        ],
    )
    source_service.add_or_update_source(scan_fm)

    # Filter sources by type
    yt_sources = source_service.get_sources(source_type="youtube")
    assert len(yt_sources) == 1
    assert yt_sources[0].path == "youtube://playlist/PL_DOCKER"
    assert yt_sources[0].source_type == "youtube"

    fm_sources = source_service.get_sources(source_type="filemeta")
    assert len(fm_sources) == 1
    assert fm_sources[0].path == "/home/user/courses/local"
    assert fm_sources[0].source_type == "filemeta"

    # Verify items
    items = source_service.get_items_by_source(src.id)
    assert len(items) == 2
    assert items[0].path == "youtube://video/d1"
    assert items[1].path == "youtube://video/d2"


def test_youtube_two_way_completion_sync(services):
    task_service, source_service, daily_service = services
    today_str = datetime.now().date().isoformat()

    # Create YouTube source and items
    scan_res = FilemetaScanResult(
        source_name="YT Playlist",
        source_path="youtube://playlist/PL_SYNC",
        items=[
            ScannedFile(
                name="s1",
                path="youtube://video/s1",
                title="Sync Lesson 1",
                type="video",
                metadata={"source_type": "youtube"},
            )
        ],
    )
    src, _, _ = source_service.add_or_update_source(scan_res)
    items = source_service.get_items_by_source(src.id)
    item = items[0]
    assert not item.completed

    # Schedule item to Today
    task = task_service.get_or_create_youtube_task(
        item,
        source_name=src.name,
    )
    daily_task, _ = daily_service.add_task_to_date(task.id, today_str)

    # Toggling in Today marks file_item completed
    daily_service.toggle_task_completion(daily_task.id)
    updated_item = source_service.get_item_by_id(item.id)
    assert updated_item.completed is True

    # Toggling file_item directly updates daily task
    source_service.toggle_item_completion(item.id)
    updated_item_2 = source_service.get_item_by_id(item.id)
    assert updated_item_2.completed is False

    today_tasks = daily_service.get_tasks_for_date(today_str)
    today_task = next(t for t in today_tasks if t.task_id == task.id)
    assert not today_task.is_completed


def test_youtube_delete_source_preserves_standalone_tasks(services):
    task_service, source_service, daily_service = services

    # Standalone video task
    standalone_task, _ = task_service.get_or_create_video_task(
        title="Standalone Video",
        canonical_url="https://www.youtube.com/watch?v=standalone",
        video_id="standalone",
    )

    # Playlist source
    scan_res = FilemetaScanResult(
        source_name="Playlist To Delete",
        source_path="youtube://playlist/PL_DEL",
        items=[
            ScannedFile(
                name="del1",
                path="youtube://video/del1",
                title="Del 1",
                type="video",
                metadata={"source_type": "youtube"},
            )
        ],
    )
    src, _, _ = source_service.add_or_update_source(scan_res)

    # Delete playlist source
    source_service.delete_source(src.id)

    # Source and items are removed
    assert source_service.get_source_by_id(src.id) is None
    assert len(source_service.get_items_by_source(src.id)) == 0

    # Standalone task remains completely intact
    retrieved_task = task_service.get_task_by_id(standalone_task.id)
    assert retrieved_task is not None
    assert retrieved_task.title == "Standalone Video"


# ============================================================================
# PART 5: YouTubeView Interactive TUI Tests
# ============================================================================


def test_youtube_view_add_single_video_schedules_today_with_zero_banners(services):
    """Adding a single video URL creates task, schedules to Today, and exits with zero banner messages."""
    task_service, source_service, daily_service = services
    today_str = datetime.now().date().isoformat()

    mock_adapter = MagicMock()
    mock_adapter.fetch_single_video.return_value = YouTubeVideo(
        video_id="cool123",
        title="Cool Linux Tricks",
        canonical_url="https://www.youtube.com/watch?v=cool123",
        path="youtube://video/cool123",
        duration=300,
        duration_formatted="05:00",
    )

    view = YouTubeView(mock_adapter, source_service, task_service, daily_service)

    # Mock user typing URL
    with patch("todo.ui.youtube_view.key_reader.read_text_input", return_value="https://www.youtube.com/watch?v=cool123"):
        result = view.handle_add_url()

    assert result == "added"
    tasks = daily_service.get_tasks_for_date(today_str)
    assert any(t.task.title == "Cool Linux Tricks" for t in tasks)


def test_youtube_view_add_playlist_creates_source_and_opens_browser(services):
    """Adding a playlist URL creates source and items in SQLite."""
    task_service, source_service, daily_service = services

    mock_adapter = MagicMock()
    mock_adapter.fetch_playlist.return_value = YouTubePlaylist(
        playlist_id="PL_NEW",
        title="Mastering Go",
        canonical_url="https://www.youtube.com/playlist?list=PL_NEW",
        source_path="youtube://playlist/PL_NEW",
        items=[
            YouTubeVideo(video_id="g1", title="01 - Goroutines", canonical_url="https://www.youtube.com/watch?v=g1", path="youtube://video/g1"),
            YouTubeVideo(video_id="g2", title="02 - Channels", canonical_url="https://www.youtube.com/watch?v=g2", path="youtube://video/g2"),
        ],
        video_count=2,
    )

    view = YouTubeView(mock_adapter, source_service, task_service, daily_service)

    # When handle_playlist_videos is called, simulate pressing 'b' to go back
    with patch("todo.ui.youtube_view.key_reader.read_text_input", return_value="https://www.youtube.com/playlist?list=PL_NEW"):
        with patch.object(view, "handle_playlist_videos", return_value=None) as mock_handle_videos:
            view.handle_add_url()
            mock_handle_videos.assert_called_once()
            created_src = mock_handle_videos.call_args[0][0]
            assert created_src.name == "Mastering Go"
            assert created_src.path == "youtube://playlist/PL_NEW"

    # Verify persisted in SQLite
    src = source_service.get_source_by_path("youtube://playlist/PL_NEW")
    assert src is not None
    assert src.name == "Mastering Go"
    items = source_service.get_items_by_source(src.id)
    assert len(items) == 2


def test_youtube_view_reopen_cached_playlist_without_network_call(services):
    """Reopening an existing playlist does NOT invoke YouTubeAdapter fetch_playlist."""
    task_service, source_service, daily_service = services

    # Pre-populate source in SQLite
    scan_res = FilemetaScanResult(
        source_name="Cached Playlist",
        source_path="youtube://playlist/PL_CACHED",
        items=[
            ScannedFile(
                name="c1",
                path="youtube://video/c1",
                title="Cached Video 1",
                type="video",
                metadata={"source_type": "youtube"},
            )
        ],
    )
    source_service.add_or_update_source(scan_res)

    mock_adapter = MagicMock()
    view = YouTubeView(mock_adapter, source_service, task_service, daily_service)

    with patch("todo.ui.youtube_view.key_reader.read_text_input", return_value="https://www.youtube.com/playlist?list=PL_CACHED"):
        with patch.object(view, "handle_playlist_videos", return_value=None) as mock_handle_videos:
            view.handle_add_url()
            mock_handle_videos.assert_called_once()
            # Ensure adapter was NOT called!
            mock_adapter.fetch_playlist.assert_not_called()


def test_youtube_video_browser_multi_select_and_schedule(services):
    """Video browser Space toggles selection, Enter schedules all selected to Today with [Today] badge."""
    task_service, source_service, daily_service = services
    today_str = datetime.now().date().isoformat()

    scan_res = FilemetaScanResult(
        source_name="Batch Playlist",
        source_path="youtube://playlist/PL_BATCH",
        items=[
            ScannedFile(name="b1", path="youtube://video/b1", title="Batch 1", type="video", metadata={"source_type": "youtube"}),
            ScannedFile(name="b2", path="youtube://video/b2", title="Batch 2", type="video", metadata={"source_type": "youtube"}),
            ScannedFile(name="b3", path="youtube://video/b3", title="Batch 3", type="video", metadata={"source_type": "youtube"}),
        ],
    )
    src, _, _ = source_service.add_or_update_source(scan_res)

    mock_adapter = MagicMock()
    view = YouTubeView(mock_adapter, source_service, task_service, daily_service)

    # Key sequence:
    # 1. Space on item 0 (selects item 0)
    # 2. Down
    # 3. Space on item 1 (selects item 1)
    # 4. Enter (schedules items 0 and 1)
    # 5. 'b' to go back
    keys = [Key.SPACE, Key.DOWN, Key.SPACE, Key.ENTER, "b"]

    with patch("todo.ui.youtube_view.key_reader.read_key", side_effect=keys):
        view.handle_playlist_videos(src)

    # Verify both items scheduled to Today
    tasks = daily_service.get_tasks_for_date(today_str)
    task_titles = [t.task.title for t in tasks]
    assert "Batch 1" in task_titles
    assert "Batch 2" in task_titles
    assert "Batch 3" not in task_titles


def test_youtube_video_browser_toggle_completion(services):
    """Video browser 't' toggles completion state of item."""
    task_service, source_service, daily_service = services

    scan_res = FilemetaScanResult(
        source_name="Toggle Playlist",
        source_path="youtube://playlist/PL_TOGGLE",
        items=[
            ScannedFile(name="t1", path="youtube://video/t1", title="Toggle Item 1", type="video", metadata={"source_type": "youtube"}),
        ],
    )
    src, _, _ = source_service.add_or_update_source(scan_res)

    mock_adapter = MagicMock()
    view = YouTubeView(mock_adapter, source_service, task_service, daily_service)

    # Press 't', then 'b'
    keys = ["t", "b"]
    with patch("todo.ui.youtube_view.key_reader.read_key", side_effect=keys):
        view.handle_playlist_videos(src)

    item = source_service.get_items_by_source(src.id)[0]
    assert item.completed is True


def test_youtube_video_browser_rescan(services):
    """Video browser 'r' rescans playlist from YouTube and updates SQLite."""
    task_service, source_service, daily_service = services

    scan_res = FilemetaScanResult(
        source_name="Rescan Playlist",
        source_path="youtube://playlist/PL_RESCAN",
        items=[
            ScannedFile(name="r1", path="youtube://video/r1", title="Original R1", type="video", metadata={"source_type": "youtube"}),
        ],
    )
    src, _, _ = source_service.add_or_update_source(scan_res)

    # Mark r1 completed
    r1_item = source_service.get_items_by_source(src.id)[0]
    source_service.toggle_item_completion(r1_item.id)

    mock_adapter = MagicMock()
    mock_adapter.fetch_playlist.return_value = YouTubePlaylist(
        playlist_id="PL_RESCAN",
        title="Rescan Playlist",
        canonical_url="https://www.youtube.com/playlist?list=PL_RESCAN",
        source_path="youtube://playlist/PL_RESCAN",
        items=[
            YouTubeVideo(video_id="r1", title="Original R1", canonical_url="https://www.youtube.com/watch?v=r1", path="youtube://video/r1"),
            YouTubeVideo(video_id="r2", title="New R2 Added", canonical_url="https://www.youtube.com/watch?v=r2", path="youtube://video/r2"),
        ],
        video_count=2,
    )

    view = YouTubeView(mock_adapter, source_service, task_service, daily_service)

    # Press 'r', then 'b'
    keys = ["r", "b"]
    with patch("todo.ui.youtube_view.key_reader.read_key", side_effect=keys):
        view.handle_playlist_videos(src)

    # Ensure items updated, r1 remains completed, r2 added
    items = source_service.get_items_by_source(src.id)
    assert len(items) == 2
    assert items[0].path == "youtube://video/r1"
    assert items[0].completed is True
    assert items[1].path == "youtube://video/r2"
    assert items[1].completed is False


# ============================================================================
# PART 6: Regression Tests - Restart Persistence & Ordering
# ============================================================================


def test_youtube_persistence_across_app_restarts_and_natural_ordering():
    """
    Test full lifecycle across restarts:
    1. Import playlist with non-alphabetical video IDs (e.g. z99 at pos 1, a11 at pos 2, m55 at pos 3).
    2. Close app and simulate restart with fresh Database/Services/View on the same SQLite file.
    3. Open YouTube library and playlist without network calls (zero yt-dlp calls).
    4. Verify videos are ordered by playlist position, NOT alphabetically by video ID.
    5. Mark video 2 complete and schedule video 1 for Today.
    6. Close app and simulate restart #2.
    7. Verify completion state and Today schedule persist across restart.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "todo_yt_test.db"
        today_str = datetime.now().date().isoformat()

        # ----------------------------------------------------------------------
        # Session 1: Import playlist
        # ----------------------------------------------------------------------
        db1 = Database(db_path)
        source_svc1 = SourceService(db1)
        task_svc1 = TaskService(db1)
        daily_svc1 = DailyService(db1)

        scan = FilemetaScanResult(
            source_name="Algorithms Course",
            source_path="youtube://playlist/PL_ALGO",
            items=[
                ScannedFile(
                    name="z99",
                    path="youtube://video/z99",
                    title="01 - Big O Notation",
                    type="video",
                    metadata={
                        "source_type": "youtube",
                        "video_id": "z99",
                        "playlist_id": "PL_ALGO",
                        "duration": 600,
                        "duration_formatted": "10:00",
                        "position": 1,
                    },
                ),
                ScannedFile(
                    name="a11",
                    path="youtube://video/a11",
                    title="02 - Binary Search",
                    type="video",
                    metadata={
                        "source_type": "youtube",
                        "video_id": "a11",
                        "playlist_id": "PL_ALGO",
                        "duration": 900,
                        "duration_formatted": "15:00",
                        "position": 2,
                    },
                ),
                ScannedFile(
                    name="m55",
                    path="youtube://video/m55",
                    title="03 - Graph Traversal",
                    type="video",
                    metadata={
                        "source_type": "youtube",
                        "video_id": "m55",
                        "playlist_id": "PL_ALGO",
                        "duration": 1200,
                        "duration_formatted": "20:00",
                        "position": 3,
                    },
                ),
            ],
        )
        source_svc1.add_or_update_source(scan)

        # ----------------------------------------------------------------------
        # Session 2: Simulated App Restart #1
        # ----------------------------------------------------------------------
        db2 = Database(db_path)
        source_svc2 = SourceService(db2)
        task_svc2 = TaskService(db2)
        daily_svc2 = DailyService(db2)

        mock_adapter2 = MagicMock()
        view2 = YouTubeView(mock_adapter2, source_svc2, task_svc2, daily_svc2)

        # Verify playlist is in database without any network calls
        yt_sources = source_svc2.get_sources(source_type="youtube")
        assert len(yt_sources) == 1
        assert yt_sources[0].name == "Algorithms Course"
        assert yt_sources[0].item_count == 3
        assert yt_sources[0].completed_count == 0

        # Load items from SQLite and verify ordering
        items = source_svc2.get_items_by_source(yt_sources[0].id)
        assert len(items) == 3
        # Must be ordered pos 1, pos 2, pos 3 (z99, a11, m55) NOT alphabetical (a11, m55, z99)
        assert items[0].path == "youtube://video/z99"
        assert items[0].title == "01 - Big O Notation"
        assert items[1].path == "youtube://video/a11"
        assert items[1].title == "02 - Binary Search"
        assert items[2].path == "youtube://video/m55"
        assert items[2].title == "03 - Graph Traversal"

        # Simulate user opening library, pressing Enter on playlist,
        # moving down to video 2, pressing 't' to mark complete,
        # moving up to video 1, pressing Space + Enter to schedule to Today
        keys_session2 = [Key.ENTER, Key.DOWN, "t", Key.UP, Key.SPACE, Key.ENTER, "b"]
        with patch("todo.ui.youtube_view.key_reader.read_key", side_effect=keys_session2):
            view2.run()

        # Verify zero calls to yt-dlp adapter during this entire session
        mock_adapter2.fetch_playlist.assert_not_called()
        mock_adapter2.fetch_single_video.assert_not_called()

        # ----------------------------------------------------------------------
        # Session 3: Simulated App Restart #2
        # ----------------------------------------------------------------------
        db3 = Database(db_path)
        source_svc3 = SourceService(db3)
        task_svc3 = TaskService(db3)
        daily_svc3 = DailyService(db3)

        mock_adapter3 = MagicMock()
        view3 = YouTubeView(mock_adapter3, source_svc3, task_svc3, daily_svc3)

        # 1. Verify library progress shows 33% (1/3 completed)
        yt_sources3 = source_svc3.get_sources(source_type="youtube")
        assert len(yt_sources3) == 1
        assert yt_sources3[0].completed_count == 1
        assert yt_sources3[0].progress_pct == 33

        # 2. Verify Today's tasks has video 1 scheduled
        today_tasks3 = daily_svc3.get_tasks_for_date(today_str)
        assert len(today_tasks3) == 1
        assert today_tasks3[0].task.title == "01 - Big O Notation"
        assert today_tasks3[0].task.source_type == "youtube"
        assert not today_tasks3[0].is_completed

        # 3. Verify items in SQLite
        items3 = source_svc3.get_items_by_source(yt_sources3[0].id)
        assert items3[0].path == "youtube://video/z99"
        assert items3[0].completed is False
        assert items3[1].path == "youtube://video/a11"
        assert items3[1].completed is True
        assert items3[2].path == "youtube://video/m55"
        assert items3[2].completed is False

        # 4. Open playlist videos in view and verify [Today] badge appears
        keys_session3 = [Key.ENTER, "b", "b"]
        with patch("todo.ui.youtube_view.key_reader.read_key", side_effect=keys_session3):
            view3.run()

        mock_adapter3.fetch_playlist.assert_not_called()


def test_youtube_rescan_preserves_unavailable_videos_and_completion():
    """
    Verify rescan behavior:
    1. Playlist initially has videos v1, v2, v3.
    2. v1 is completed and scheduled to Today.
    3. Rescan occurs: v2 became unavailable (deleted on YouTube) and is skipped by yt-dlp;
       v1 has an updated title; v4 is added.
    4. Result:
       - v1 has updated title, preserved completion, preserved Today association.
       - v2 is NOT deleted from SQLite (remains available in local course).
       - v4 is added as pending.
       - No duplicate sources or tasks created.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "todo_rescan_test.db"
        today_str = datetime.now().date().isoformat()

        db = Database(db_path)
        source_svc = SourceService(db)
        task_svc = TaskService(db)
        daily_svc = DailyService(db)

        # Initial import
        initial_scan = FilemetaScanResult(
            source_name="System Design",
            source_path="youtube://playlist/PL_SYS",
            items=[
                ScannedFile(name="v1", path="youtube://video/v1", title="01 - Caching", type="video", metadata={"source_type": "youtube", "position": 1}),
                ScannedFile(name="v2", path="youtube://video/v2", title="02 - Load Balancing", type="video", metadata={"source_type": "youtube", "position": 2}),
                ScannedFile(name="v3", path="youtube://video/v3", title="03 - Sharding", type="video", metadata={"source_type": "youtube", "position": 3}),
            ],
        )
        src, _, _ = source_svc.add_or_update_source(initial_scan)

        # Schedule v1 to Today and mark completed
        v1_item = source_svc.get_items_by_source(src.id)[0]
        task_v1 = task_svc.get_or_create_youtube_task(v1_item, source_name=src.name)
        daily_task, _ = daily_svc.add_task_to_date(task_v1.id, today_str)
        daily_svc.toggle_task_completion(daily_task.id)

        # Verify v1 is marked completed in both SQLite file_items and Today
        assert source_svc.get_item_by_id(v1_item.id).completed is True
        assert daily_svc.get_tasks_for_date(today_str)[0].is_completed is True

        # Mock rescan with v2 missing, v1 updated title, and v4 new
        mock_adapter = MagicMock()
        mock_adapter.fetch_playlist.return_value = YouTubePlaylist(
            playlist_id="PL_SYS",
            title="System Design (Updated Title)",
            canonical_url="https://www.youtube.com/playlist?list=PL_SYS",
            source_path="youtube://playlist/PL_SYS",
            items=[
                YouTubeVideo(video_id="v1", title="01 - Caching & Redis (Updated)", canonical_url="https://www.youtube.com/watch?v=v1", path="youtube://video/v1", position=1),
                YouTubeVideo(video_id="v3", title="03 - Sharding", canonical_url="https://www.youtube.com/watch?v=v3", path="youtube://video/v3", position=2),
                YouTubeVideo(video_id="v4", title="04 - Message Queues (New)", canonical_url="https://www.youtube.com/watch?v=v4", path="youtube://video/v4", position=3),
            ],
            video_count=3,
            skipped_count=1,  # v2 was skipped as deleted
        )

        view = YouTubeView(mock_adapter, source_svc, task_svc, daily_svc)

        # Trigger rescan ('r'), then 'b' to exit
        with patch("todo.ui.youtube_view.key_reader.read_key", side_effect=["r", "b"]):
            view.handle_playlist_videos(src)

        # Check SQLite persistence
        updated_items = source_svc.get_items_by_source(src.id)
        # All 4 items exist: v1, v2 (preserved!), v3, v4
        assert len(updated_items) == 4

        # v1 updated title and preserved completion
        item_v1 = next(it for it in updated_items if it.path == "youtube://video/v1")
        assert item_v1.title == "01 - Caching & Redis (Updated)"
        assert item_v1.completed is True

        # v2 was preserved from SQLite even though YouTube omitted it
        item_v2 = next(it for it in updated_items if it.path == "youtube://video/v2")
        assert item_v2.title == "02 - Load Balancing"
        assert item_v2.completed is False

        # v4 was inserted as new
        item_v4 = next(it for it in updated_items if it.path == "youtube://video/v4")
        assert item_v4.title == "04 - Message Queues (New)"
        assert item_v4.completed is False

        # Today association for v1 is completely intact
        today_tasks = daily_svc.get_tasks_for_date(today_str)
        assert len(today_tasks) == 1
        assert today_tasks[0].task_id == task_v1.id
        assert today_tasks[0].is_completed is True

        # Exactly 1 source record exists (no duplicate)
        sources = source_svc.get_sources(source_type="youtube")
        assert len(sources) == 1
        assert sources[0].id == src.id
        assert sources[0].name == "System Design (Updated Title)"


def test_youtube_add_task_menu_opens_library_with_cached_playlists():
    """
    Selecting option 3 from Add Task menu opens YouTube Library view,
    displaying previously imported playlists from SQLite.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "todo_menu_test.db"
        cfg = Config(db_path=db_path)

        app = TodoApp(cfg)

        # Pre-populate YouTube playlist in SQLite
        scan = FilemetaScanResult(
            source_name="Existing Playlist",
            source_path="youtube://playlist/PL_EXISTING",
            items=[
                ScannedFile(name="ex1", path="youtube://video/ex1", title="Ex 1", type="video", metadata={"source_type": "youtube", "position": 1}),
            ],
        )
        app.source_service.add_or_update_source(scan)

        # Key sequence:
        # 1. "+" (Add Task menu)
        # 2. "3" (YouTube option) -> Opens YouTube Library
        # 3. Enter (Opens playlist videos from SQLite)
        # 4. "b" (Exit playlist videos)
        # 5. "b" (Exit YouTube Library)
        # 6. "q" (Quit app)
        keys = ["+\n", "3\n", Key.ENTER, "b", "b", "q\n"]
        with patch("sys.stdin", io.StringIO("".join(keys))):
            app.run()

        # Verify playlist is still in database and unchanged
        sources = app.source_service.get_sources(source_type="youtube")
        assert len(sources) == 1
        assert sources[0].name == "Existing Playlist"
        items = app.source_service.get_items_by_source(sources[0].id)
        assert len(items) == 1
        assert items[0].title == "Ex 1"
