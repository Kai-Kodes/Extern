"""Tests for native Telegram course integration in Todo CLI.
All Telegram network calls and Telethon interactions are strictly mocked.
Real sessions or live accounts are never accessed during test execution.
"""

import asyncio
from datetime import datetime
from pathlib import Path
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from todo.config import Config
from todo.database import Database
from todo.integrations.filemeta import FilemetaScanResult, ScannedFile
from todo.integrations.telegram import (
    TelegramAdapter,
    TelegramChannel,
    TelegramChannelNotFoundError,
    TelegramConnectionError,
    TelegramIntegrationError,
    TelegramNotConfiguredError,
    TelegramSessionNotFoundError,
    TelegramUnauthorizedError,
    clean_video_title,
    format_duration,
)
from todo.models import FileItem
from todo.services.daily_service import DailyService
from todo.services.source_service import SourceService
from todo.services.task_service import TaskService
from todo.ui.key_reader import Key
from todo.ui.telegram_view import TelegramView


def test_telegram_helpers():
    """Verify duration formatting and video title cleanup."""
    # format_duration
    assert format_duration(None) == ""
    assert format_duration(0) == ""
    assert format_duration(-10) == ""
    assert format_duration(45) == "00:45"
    assert format_duration(125) == "02:05"
    assert format_duration(3665) == "1h 01m 05s"

    # clean_video_title
    assert clean_video_title("01 - Intro to Networking.mp4") == "01 - Intro to Networking"
    assert clean_video_title(None, caption="Lesson 1: Basics\nAdditional text") == "Lesson 1: Basics"
    assert clean_video_title(None, caption=None, msg_id=42) == "Video #42"
    assert clean_video_title(None, caption=None, msg_id=0) == "Untitled Video"


def test_telegram_adapter_import_safety():
    """Instantiating TelegramAdapter must not trigger network calls or login."""
    with patch("telethon.TelegramClient") as mock_client:
        adapter = TelegramAdapter(session_path="/tmp/fake.session", api_id=12345, api_hash="abcde")
        assert adapter._client is None
        mock_client.assert_not_called()


def test_telegram_adapter_check_availability():
    """Check availability reports accurate status without throwing."""
    # Missing credentials
    adapter_no_creds = TelegramAdapter(session_path="/tmp/fake.session")
    ok, err = adapter_no_creds.check_availability()
    assert not ok
    assert "credentials" in err.lower()

    # Missing session path
    adapter_no_session = TelegramAdapter(api_id=123, api_hash="abc")
    ok, err = adapter_no_session.check_availability()
    assert not ok
    assert "session path" in err.lower()

    # Session file does not exist
    adapter_nonexistent = TelegramAdapter(
        session_path="/tmp/nonexistent_session_123456.session",
        api_id=123,
        api_hash="abc",
    )
    ok, err = adapter_nonexistent.check_availability()
    assert not ok
    assert "not found" in err.lower()

    # Valid config with existing session file
    with tempfile.NamedTemporaryFile(suffix=".session") as tmp:
        adapter_valid = TelegramAdapter(
            session_path=tmp.name,
            api_id=123,
            api_hash="abc",
        )
        ok, err = adapter_valid.check_availability()
        assert ok
        assert err is None


def test_telegram_adapter_connect_unauthorized():
    """Connecting an unauthorized session raises TelegramUnauthorizedError."""
    with tempfile.NamedTemporaryFile(suffix=".session") as tmp:
        adapter = TelegramAdapter(
            session_path=tmp.name,
            api_id=12345,
            api_hash="abcdef",
        )

        mock_client = MagicMock()
        mock_client.is_connected.return_value = False
        mock_client.connect = AsyncMock()
        mock_client.is_user_authorized = AsyncMock(return_value=False)

        with patch.object(adapter, "_get_client", return_value=mock_client):
            with pytest.raises(TelegramUnauthorizedError):
                adapter.connect()


def test_telegram_adapter_connect_network_failure():
    """Network connection failure raises TelegramConnectionError."""
    with tempfile.NamedTemporaryFile(suffix=".session") as tmp:
        adapter = TelegramAdapter(
            session_path=tmp.name,
            api_id=12345,
            api_hash="abcdef",
        )

        mock_client = MagicMock()
        mock_client.is_connected.return_value = False
        mock_client.connect = AsyncMock(side_effect=ConnectionResetError("Connection lost"))

        with patch.object(adapter, "_get_client", return_value=mock_client):
            with pytest.raises(TelegramConnectionError):
                adapter.connect()


def test_telegram_adapter_discover_channels():
    """Discover channels retrieves and filters channels and groups properly without truncation."""
    with tempfile.NamedTemporaryFile(suffix=".session") as tmp:
        adapter = TelegramAdapter(
            session_path=tmp.name,
            api_id=12345,
            api_hash="abcdef",
        )

        # Mock dialogs
        # 1. Public broadcast channel
        d1 = MagicMock()
        d1.id = -100111
        d1.name = "Linux Kernel Networking"
        d1.is_channel = True
        d1.is_group = False
        d1.archived = False
        d1.entity = MagicMock(username="linux_net")

        # 2. Private broadcast channel (no username)
        d2 = MagicMock()
        d2.id = -100222
        d2.name = "Private Paid Course Channel"
        d2.is_channel = True
        d2.is_group = False
        d2.archived = False
        d2.entity = MagicMock(username=None)

        # 3. Private supergroup / megagroup (no username)
        d3 = MagicMock()
        d3.id = -100333
        d3.name = "Private Discussion Supergroup"
        d3.is_channel = True
        d3.is_group = True
        d3.archived = False
        d3.entity = MagicMock(username=None)

        # 4. Basic group (Chat)
        d4 = MagicMock()
        d4.id = -444
        d4.name = "Study Group Basic Chat"
        d4.is_channel = False
        d4.is_group = True
        d4.archived = False
        d4.entity = MagicMock(username=None)

        # 5. Archived channel
        d5 = MagicMock()
        d5.id = -100555
        d5.name = "Archived Course Archive"
        d5.is_channel = True
        d5.is_group = False
        d5.archived = True
        d5.entity = MagicMock(username="archived_course")

        # 6. Direct 1-on-1 user chat (must be filtered out)
        d6 = MagicMock()
        d6.id = 666
        d6.name = "John Doe (Direct Chat)"
        d6.is_channel = False
        d6.is_group = False
        d6.archived = False
        d6.entity = MagicMock(username="johndoe")

        # 7. Bot chat (must be filtered out)
        d7 = MagicMock()
        d7.id = 777
        d7.name = "Helper Bot"
        d7.is_channel = False
        d7.is_group = False
        d7.archived = False
        d7.entity = MagicMock(username="helperbot")

        mock_client = MagicMock()
        mock_client.is_connected.return_value = True
        mock_client.is_user_authorized = AsyncMock(return_value=True)
        mock_client.get_dialogs = AsyncMock(return_value=[d1, d2, d3, d4, d5, d6, d7])

        with patch.object(adapter, "_get_client", return_value=mock_client):
            channels = adapter.discover_channels()

            # Must call get_dialogs with limit=None so no channels are truncated
            mock_client.get_dialogs.assert_called_with(limit=None)

            # Direct chat (d6) and bot (d7) must be filtered out; 5 channels/groups retained
            assert len(channels) == 5

            titles = [c.title for c in channels]
            assert "Linux Kernel Networking" in titles
            assert "Private Paid Course Channel" in titles
            assert "Private Discussion Supergroup" in titles
            assert "Study Group Basic Chat" in titles
            assert "Archived Course Archive" in titles

            # Verify alphabetical sorting
            assert titles == sorted(titles, key=lambda s: s.lower())


def test_telegram_adapter_scan_channel():
    """Scan channel extracts video messages with duration, fallback titles, and chronological order."""
    with tempfile.NamedTemporaryFile(suffix=".session") as tmp:
        adapter = TelegramAdapter(
            session_path=tmp.name,
            api_id=12345,
            api_hash="abcdef",
        )

        # Mock entity
        mock_entity = MagicMock()
        mock_entity.title = "Distributed Systems Course"

        # Mock messages
        # msg 1: video message with duration attribute and file_name
        attr_dur = type("AttrDur", (), {"duration": 1800})()
        attr_file = type("AttrFile", (), {"file_name": "01_Consensus_Intro.mp4"})()
        msg1 = MagicMock()
        msg1.id = 10
        msg1.video = MagicMock(attributes=[attr_dur, attr_file], size=104857600)
        msg1.document = None
        msg1.message = "Lesson 1: Intro to Consensus"
        msg1.date = datetime(2026, 10, 1, 10, 0, 0)

        # msg 2: document with video/mp4 mime type, no filename, caption provided
        attr_dur2 = type("AttrDur2", (), {"duration": 720})()
        msg2 = MagicMock()
        msg2.id = 15
        msg2.video = None
        msg2.document = MagicMock(
            mime_type="video/mp4",
            attributes=[attr_dur2],
            size=52428800,
        )
        msg2.file = None
        msg2.message = "02 - Raft Protocol Deep Dive\nDetails on leader election"
        msg2.date = datetime(2026, 10, 2, 10, 0, 0)

        # msg 3: text message only (must be ignored)
        msg3 = MagicMock()
        msg3.id = 12
        msg3.video = None
        msg3.document = None
        msg3.message = "Hello class, homework is due Friday"
        msg3.date = datetime(2026, 10, 1, 15, 0, 0)

        async def mock_iter_messages(entity, limit=None):
            # Simulate Telethon yielding in reverse chronological order (newest first)
            for m in [msg2, msg3, msg1]:
                yield m

        mock_client = MagicMock()
        mock_client.is_connected.return_value = True
        mock_client.is_user_authorized = AsyncMock(return_value=True)
        mock_client.get_entity = AsyncMock(return_value=mock_entity)
        mock_client.iter_messages = mock_iter_messages

        with patch.object(adapter, "_get_client", return_value=mock_client):
            scan_result = adapter.scan_channel(-100999)

            assert scan_result.source_name == "Distributed Systems Course"
            assert scan_result.source_path == "telegram://-100999"
            assert len(scan_result.items) == 2

            # Chronologically sorted by message ID (msg1 id=10, msg2 id=15)
            item1 = scan_result.items[0]
            assert item1.title == "01_Consensus_Intro"
            assert item1.path == "telegram://-100999/10"
            assert item1.metadata["duration"] == 1800
            assert item1.metadata["duration_formatted"] == "30:00"

            item2 = scan_result.items[1]
            assert item2.title == "02 - Raft Protocol Deep Dive"
            assert item2.path == "telegram://-100999/15"
            assert item2.metadata["duration"] == 720
            assert item2.metadata["duration_formatted"] == "12:00"


def test_telegram_persistence_and_rescanning():
    """Rescanning Telegram channels preserves completion state and Today schedule."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        db = Database(db_path)

        source_svc = SourceService(db)
        task_svc = TaskService(db)
        daily_svc = DailyService(db)

        # 1. Initial scan
        initial_scan = FilemetaScanResult(
            source_name="DevOps Course",
            source_path="telegram://-100123",
            items=[
                ScannedFile(
                    name="01_Docker.mp4",
                    path="telegram://-100123/1",
                    title="01 - Docker Basics",
                    type="video",
                    metadata={"source_type": "telegram", "channel_id": -100123, "message_id": 1},
                ),
                ScannedFile(
                    name="02_K8s.mp4",
                    path="telegram://-100123/2",
                    title="02 - Kubernetes Intro",
                    type="video",
                    metadata={"source_type": "telegram", "channel_id": -100123, "message_id": 2},
                ),
            ],
        )

        source, added, _ = source_svc.add_or_update_source(initial_scan)
        assert source.source_type == "telegram"
        assert added == 2

        items = source_svc.get_items_by_source(source.id)
        assert len(items) == 2

        # 2. Complete item 1 and schedule item 2 to Today
        source_svc.toggle_item_completion(items[0].id)
        assert source_svc.get_items_by_source(source.id)[0].completed is True

        today_str = datetime.now().date().isoformat()
        task2 = task_svc.get_or_create_telegram_task(items[1], source_name=source.name)
        daily_svc.add_task_to_date(task2.id, today_str)

        assert len(daily_svc.get_tasks_for_date(today_str)) == 1

        # 3. Rescan with an additional item
        rescan = FilemetaScanResult(
            source_name="DevOps Course",
            source_path="telegram://-100123",
            items=[
                ScannedFile(
                    name="01_Docker.mp4",
                    path="telegram://-100123/1",
                    title="01 - Docker Basics",
                    type="video",
                    metadata={"source_type": "telegram", "channel_id": -100123, "message_id": 1},
                ),
                ScannedFile(
                    name="02_K8s.mp4",
                    path="telegram://-100123/2",
                    title="02 - Kubernetes Intro",
                    type="video",
                    metadata={"source_type": "telegram", "channel_id": -100123, "message_id": 2},
                ),
                ScannedFile(
                    name="03_CI_CD.mp4",
                    path="telegram://-100123/3",
                    title="03 - CI/CD Pipelines",
                    type="video",
                    metadata={"source_type": "telegram", "channel_id": -100123, "message_id": 3},
                ),
            ],
        )

        source_updated, newly_added, updated = source_svc.add_or_update_source(rescan)
        assert newly_added == 1

        items_after = source_svc.get_items_by_source(source.id)
        assert len(items_after) == 3
        # Item 1 MUST retain completion state
        assert items_after[0].completed is True
        # Item 2 Today schedule must remain intact
        assert len(daily_svc.get_tasks_for_date(today_str)) == 1


def test_telegram_two_way_completion_sync():
    """Toggling Telegram task completion in Today syncs with FileItem completion."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        db = Database(db_path)

        source_svc = SourceService(db)
        task_svc = TaskService(db)
        daily_svc = DailyService(db)

        scan = FilemetaScanResult(
            source_name="Python Advanced",
            source_path="telegram://-100777",
            items=[
                ScannedFile(
                    name="01_Asyncio.mp4",
                    path="telegram://-100777/101",
                    title="01 - Asyncio Internals",
                    type="video",
                    metadata={"source_type": "telegram", "channel_id": -100777, "message_id": 101},
                )
            ],
        )
        source, _, _ = source_svc.add_or_update_source(scan)
        file_item = source_svc.get_items_by_source(source.id)[0]
        assert not file_item.completed

        # Create task and schedule to today
        task = task_svc.get_or_create_telegram_task(file_item, source_name=source.name)
        today_str = datetime.now().date().isoformat()
        daily_task, _ = daily_svc.add_task_to_date(task.id, today_str)

        # Toggle Today task
        daily_svc.toggle_task_completion(daily_task.id)

        # File item must now be completed!
        item_reloaded = source_svc.get_items_by_source(source.id)[0]
        assert item_reloaded.completed is True

        # Toggle back from SourceService
        source_svc.toggle_item_completion(item_reloaded.id)
        task_reloaded = task_svc.get_task_by_id(task.id)
        assert task_reloaded.status == "pending"


def test_telegram_today_delete_semantics():
    """Deleting a Telegram task from Today removes it from Today without deleting the source item."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        db = Database(db_path)

        source_svc = SourceService(db)
        task_svc = TaskService(db)
        daily_svc = DailyService(db)

        scan = FilemetaScanResult(
            source_name="Rust Systems",
            source_path="telegram://-100888",
            items=[
                ScannedFile(
                    name="01_Ownership.mp4",
                    path="telegram://-100888/201",
                    title="01 - Ownership and Lifetimes",
                    type="video",
                    metadata={"source_type": "telegram", "channel_id": -100888, "message_id": 201},
                )
            ],
        )
        source, _, _ = source_svc.add_or_update_source(scan)
        file_item = source_svc.get_items_by_source(source.id)[0]

        task = task_svc.get_or_create_telegram_task(file_item, source_name=source.name)
        today_str = datetime.now().date().isoformat()
        daily_task, _ = daily_svc.add_task_to_date(task.id, today_str)

        assert len(daily_svc.get_tasks_for_date(today_str)) == 1

        # Delete daily task
        daily_svc.remove_daily_task(daily_task.id)

        # Removed from Today
        assert len(daily_svc.get_tasks_for_date(today_str)) == 0
        # Source item still exists in SQLite
        assert len(source_svc.get_items_by_source(source.id)) == 1


def test_telegram_multi_select_batch_scheduling():
    """Batch selecting Telegram lessons with Space and scheduling with Enter."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        db = Database(db_path)

        source_svc = SourceService(db)
        task_svc = TaskService(db)
        daily_svc = DailyService(db)

        # Seed channel source in SQLite
        scan = FilemetaScanResult(
            source_name="Go Concurrency",
            source_path="telegram://-100333",
            items=[
                ScannedFile(
                    name="01_Goroutines.mp4",
                    path="telegram://-100333/1",
                    title="01 - Goroutines",
                    type="video",
                    metadata={"source_type": "telegram", "channel_id": -100333, "message_id": 1},
                ),
                ScannedFile(
                    name="02_Channels.mp4",
                    path="telegram://-100333/2",
                    title="02 - Channels",
                    type="video",
                    metadata={"source_type": "telegram", "channel_id": -100333, "message_id": 2},
                ),
                ScannedFile(
                    name="03_Select.mp4",
                    path="telegram://-100333/3",
                    title="03 - Select Statement",
                    type="video",
                    metadata={"source_type": "telegram", "channel_id": -100333, "message_id": 3},
                ),
            ],
        )
        source_svc.add_or_update_source(scan)

        adapter = MagicMock()
        view = TelegramView(adapter, source_svc, task_svc, daily_svc)

        channel = TelegramChannel(id=-100333, title="Go Concurrency")

        # Simulate user actions in handle_channel_lessons:
        # Space (select item 1), Down, Space (select item 2), Enter (schedule both to Today)
        inputs = [
            Key.SPACE,
            Key.DOWN,
            Key.SPACE,
            Key.ENTER,
        ]

        with patch("todo.ui.telegram_view.key_reader.read_key", side_effect=inputs):
            res = view.handle_channel_lessons(channel)
            assert res == "added"

        today_str = datetime.now().date().isoformat()
        today_tasks = daily_svc.get_tasks_for_date(today_str)
        assert len(today_tasks) == 2
        titles = [dt.task.title for dt in today_tasks]
        assert "01 - Goroutines" in titles
        assert "02 - Channels" in titles
        assert "03 - Select Statement" not in titles


def test_telegram_view_unauthorized_error_screen():
    """TelegramView displays structured error screen on unauthorized session without crashing."""
    adapter = MagicMock()
    adapter.check_availability.return_value = (True, None)
    adapter.connect.side_effect = TelegramUnauthorizedError("Session is expired.")

    source_svc = MagicMock()
    task_svc = MagicMock()
    daily_svc = MagicMock()

    view = TelegramView(adapter, source_svc, task_svc, daily_svc)

    with patch("todo.ui.telegram_view.key_reader.read_key", return_value="b"):
        result = view.run()
        assert result is None


def test_telegram_duplicate_scheduling_protection():
    """Scheduling the same Telegram lesson multiple times avoids duplicate daily tasks."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        db = Database(db_path)

        source_svc = SourceService(db)
        task_svc = TaskService(db)
        daily_svc = DailyService(db)

        scan = FilemetaScanResult(
            source_name="Algorithms",
            source_path="telegram://-100444",
            items=[
                ScannedFile(
                    name="01_Sorting.mp4",
                    path="telegram://-100444/1",
                    title="01 - Sorting Algorithms",
                    type="video",
                    metadata={"source_type": "telegram", "channel_id": -100444, "message_id": 1},
                )
            ],
        )
        source_svc.add_or_update_source(scan)

        adapter = MagicMock()
        view = TelegramView(adapter, source_svc, task_svc, daily_svc)
        channel = TelegramChannel(id=-100444, title="Algorithms")

        # First add
        with patch("todo.ui.telegram_view.key_reader.read_key", side_effect=[Key.ENTER]):
            view.handle_channel_lessons(channel)

        today_str = datetime.now().date().isoformat()
        assert len(daily_svc.get_tasks_for_date(today_str)) == 1

        # Second add of the same item
        with patch("todo.ui.telegram_view.key_reader.read_key", side_effect=[Key.ENTER]):
            view.handle_channel_lessons(channel)

        # Still exactly 1 daily task
        assert len(daily_svc.get_tasks_for_date(today_str)) == 1


def test_telegram_next_unfinished_navigation():
    """Pressing 'n' jumps to the next unfinished lesson in the channel."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        db = Database(db_path)

        source_svc = SourceService(db)
        task_svc = TaskService(db)
        daily_svc = DailyService(db)

        scan = FilemetaScanResult(
            source_name="Databases",
            source_path="telegram://-100555",
            items=[
                ScannedFile(
                    name="01_BTree.mp4",
                    path="telegram://-100555/1",
                    title="01 - B-Trees",
                    type="video",
                    metadata={"source_type": "telegram", "channel_id": -100555, "message_id": 1},
                ),
                ScannedFile(
                    name="02_LSM.mp4",
                    path="telegram://-100555/2",
                    title="02 - LSM Trees",
                    type="video",
                    metadata={"source_type": "telegram", "channel_id": -100555, "message_id": 2},
                ),
            ],
        )
        source, _, _ = source_svc.add_or_update_source(scan)
        items = source_svc.get_items_by_source(source.id)

        # Mark item 1 completed
        source_svc.set_item_completion(items[0].id, True)

        adapter = MagicMock()
        view = TelegramView(adapter, source_svc, task_svc, daily_svc)
        channel = TelegramChannel(id=-100555, title="Databases")

        # User presses 'n' (jump to item 2), then 'b' to exit
        with patch("todo.ui.telegram_view.key_reader.read_key", side_effect=["n", "b"]):
            view.handle_channel_lessons(channel)


def test_telegram_rescan_failure_preserves_database():
    """If a rescan fails due to connection error, existing lessons in SQLite are untouched."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        db = Database(db_path)

        source_svc = SourceService(db)
        task_svc = TaskService(db)
        daily_svc = DailyService(db)

        scan = FilemetaScanResult(
            source_name="Networks",
            source_path="telegram://-100666",
            items=[
                ScannedFile(
                    name="01_TCP.mp4",
                    path="telegram://-100666/1",
                    title="01 - TCP Handshake",
                    type="video",
                    metadata={"source_type": "telegram", "channel_id": -100666, "message_id": 1},
                )
            ],
        )
        source, _, _ = source_svc.add_or_update_source(scan)

        adapter = MagicMock()
        # Scan failure
        adapter.scan_channel.side_effect = TelegramConnectionError("Timeout while scanning")
        view = TelegramView(adapter, source_svc, task_svc, daily_svc)
        channel = TelegramChannel(id=-100666, title="Networks")

        # User presses 'r' (rescan), which fails, then 'b' (back)
        with patch("todo.ui.telegram_view.key_reader.read_key", side_effect=["r", "b"]):
            view.handle_channel_lessons(channel)

        # Items in SQLite are completely preserved
        items = source_svc.get_items_by_source(source.id)
        assert len(items) == 1
        assert items[0].title == "01 - TCP Handshake"


def test_telegram_adapter_lifecycle_same_event_loop():
    """
    Regression test for: 'The asyncio event loop must not change after connection'
    Verifies that connect(), discover_channels(), scan_channel(), and disconnect()
    all run on the exact same asyncio event loop instance.
    """
    with tempfile.NamedTemporaryFile(suffix=".session") as tmp:
        adapter = TelegramAdapter(
            session_path=tmp.name,
            api_id=12345,
            api_hash="abcdef",
        )

        observed_loops = []

        class StrictLoopMockClient:
            def __init__(self, session, api_id, api_hash, loop=None):
                self._loop = loop
                self._connected = False

            def is_connected(self):
                return self._connected

            async def connect(self):
                current_loop = asyncio.get_running_loop()
                observed_loops.append(("connect", current_loop))
                if self._loop is not None and self._loop != current_loop:
                    raise RuntimeError("The asyncio event loop must not change after connection")
                self._connected = True

            async def is_user_authorized(self):
                current_loop = asyncio.get_running_loop()
                observed_loops.append(("is_user_authorized", current_loop))
                if self._loop is not None and self._loop != current_loop:
                    raise RuntimeError("The asyncio event loop must not change after connection")
                return True

            async def get_dialogs(self, limit=150):
                current_loop = asyncio.get_running_loop()
                observed_loops.append(("get_dialogs", current_loop))
                if self._loop is not None and self._loop != current_loop:
                    raise RuntimeError("The asyncio event loop must not change after connection")
                d = MagicMock()
                d.id = -100111
                d.name = "Course Channel"
                d.is_channel = True
                d.is_group = False
                d.entity = MagicMock(username="course_channel")
                return [d]

            async def get_entity(self, channel_id):
                current_loop = asyncio.get_running_loop()
                observed_loops.append(("get_entity", current_loop))
                if self._loop is not None and self._loop != current_loop:
                    raise RuntimeError("The asyncio event loop must not change after connection")
                return MagicMock(title="Course Channel")

            async def iter_messages(self, entity, limit=None):
                current_loop = asyncio.get_running_loop()
                observed_loops.append(("iter_messages", current_loop))
                if self._loop is not None and self._loop != current_loop:
                    raise RuntimeError("The asyncio event loop must not change after connection")
                msg = MagicMock()
                msg.id = 1
                msg.video = MagicMock(attributes=[], size=100)
                msg.document = None
                msg.message = "Lesson 1"
                msg.date = datetime.now()
                yield msg

            async def disconnect(self):
                current_loop = asyncio.get_running_loop()
                observed_loops.append(("disconnect", current_loop))
                if self._loop is not None and self._loop != current_loop:
                    raise RuntimeError("The asyncio event loop must not change after connection")
                self._connected = False

        with patch("telethon.TelegramClient", side_effect=StrictLoopMockClient):
            # 1. Connect
            adapter.connect()
            assert adapter.is_authorized() is True

            # 2. Discover channels (previously raised RuntimeError here!)
            channels = adapter.discover_channels()
            assert len(channels) == 1
            assert channels[0].title == "Course Channel"

            # 3. Scan channel
            scan_res = adapter.scan_channel(-100111)
            assert scan_res.source_name == "Course Channel"

            # Verify all calls used the exact same event loop
            assert len(observed_loops) >= 5
            first_loop = observed_loops[0][1]
            for op, loop in observed_loops:
                assert loop is first_loop, f"Operation {op} ran on a different loop!"

            # 4. Disconnect safely and idempotently
            adapter.disconnect()
            adapter.disconnect()
            assert adapter._loop is None
            assert adapter._thread is None
            assert adapter._client is None
