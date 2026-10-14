"""Integration tests for Filemeta device integration (KDE Connect, Wired Devices, Persistence, Disconnection)."""

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from todo.config import Config
from todo.integrations.devices import ConnectedDevice, DeviceManager, DeviceType
from todo.integrations.filemeta import FilemetaScanResult, FilemetaTimeoutError, ScannedFile
from todo.models import FileItem, Source
from todo.services.daily_service import DailyService
from todo.services.source_service import SourceService
from todo.services.task_service import TaskService
from todo.ui.app import TodoApp
from todo.ui.filemeta_view import FilemetaView
from todo.ui.key_reader import Key


@pytest.fixture
def test_setup():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_devices.db"
        cfg = Config(db_path=db_path)
        app = TodoApp(cfg)
        yield {
            "cfg": cfg,
            "app": app,
            "db": app.db,
            "daily_svc": app.daily_service,
            "task_svc": app.task_service,
            "source_svc": app.source_service,
            "adapter": app.filemeta_adapter,
            "device_mgr": app.device_manager,
        }


def test_device_source_persistent_import_and_restart(test_setup):
    """
    Imported device sources must persist across app restarts and remain
    accessible from SQLite without requiring the device to be connected.
    """
    cfg = test_setup["cfg"]
    source_svc = test_setup["source_svc"]
    device_path = "/run/user/1000/1663dac047b6436fae66e27dfca1ed31/storage/emulated/0/PythonCourse"

    scan_res = FilemetaScanResult(
        source_name="Python Course",
        source_path=device_path,
        items=[
            ScannedFile(
                name="01 - Intro.mp4",
                path=f"{device_path}/01 - Intro.mp4",
                title="01 - Intro",
                type="video",
                metadata={"duration_formatted": "12:30"},
            ),
            ScannedFile(
                name="02 - Basics.mp4",
                path=f"{device_path}/02 - Basics.mp4",
                title="02 - Basics",
                type="video",
                metadata={"duration_formatted": "18:45"},
            ),
        ],
    )

    src, added, _ = source_svc.add_or_update_source(scan_res)
    assert added == 2
    assert src.name == "Python Course"
    assert src.path == device_path

    # Simulate app restart with a fresh TodoApp instance
    fresh_app = TodoApp(cfg)
    fresh_sources = fresh_app.source_service.get_sources(source_type="filemeta")
    assert len(fresh_sources) == 1
    loaded_src = fresh_sources[0]
    assert loaded_src.name == "Python Course"
    assert loaded_src.path == device_path
    assert loaded_src.item_count == 2
    assert loaded_src.completed_count == 0

    # Load items directly from SQLite (device disconnected / not touching disk)
    loaded_items = fresh_app.source_service.get_items_by_source(loaded_src.id)
    assert len(loaded_items) == 2
    assert loaded_items[0].title == "01 - Intro"
    assert loaded_items[0].metadata["duration_formatted"] == "12:30"
    assert loaded_items[1].title == "02 - Basics"


def test_disconnected_device_viewing_and_scheduling(test_setup):
    """
    When device is disconnected, items remain viewable, completion can be toggled,
    and items can be scheduled to Today without error.
    """
    app = test_setup["app"]
    source_svc = test_setup["source_svc"]
    daily_svc = test_setup["daily_svc"]
    task_svc = test_setup["task_svc"]
    device_path = "/media/user/USB_DRIVE/DesignCourse"

    scan_res = FilemetaScanResult(
        source_name="Design Course",
        source_path=device_path,
        items=[
            ScannedFile(
                name="01 - Typography.mp4",
                path=f"{device_path}/01 - Typography.mp4",
                title="01 - Typography",
                type="video",
                metadata={"duration_formatted": "15:00"},
            )
        ],
    )
    src, _, _ = source_svc.add_or_update_source(scan_res)

    # Mock device as disconnected
    mock_dev_mgr = MagicMock(spec=DeviceManager)
    mock_dev_mgr.is_source_accessible.return_value = False

    view = FilemetaView(
        source_svc, task_svc, daily_svc, app.filemeta_adapter, device_manager=mock_dev_mgr
    )

    # In items view: press Enter (add to Today) -> silent return "added"
    with patch("todo.ui.filemeta_view.key_reader.read_key", side_effect=[Key.ENTER]):
        ret = view.handle_source_items(src)

    assert ret == "added"
    today_str = daily_svc.get_today_str()
    tasks_today = daily_svc.get_tasks_for_date(today_str)
    assert len(tasks_today) == 1
    assert "01 - Typography" in tasks_today[0].task.title

    # Toggle completion from Filemeta items view: 't' then 'b' to exit
    with patch("todo.ui.filemeta_view.key_reader.read_key", side_effect=["t", "b"]):
        view.handle_source_items(src)

    completed, total, pct = source_svc.get_source_progress(src.id)
    assert completed == 1
    assert pct == 100

    # Associated daily task must be synced to completed
    tasks_today_after = daily_svc.get_tasks_for_date(today_str)
    assert tasks_today_after[0].is_completed is True


def test_device_rescan_disconnected_rejected_without_data_loss(test_setup):
    """
    Attempting to rescan when a device is disconnected must not delete or alter
    existing cached SQLite records, and must report a helpful warning.
    """
    app = test_setup["app"]
    source_svc = test_setup["source_svc"]
    daily_svc = test_setup["daily_svc"]
    task_svc = test_setup["task_svc"]
    device_path = "/run/user/1000/gvfs/mtp:host=realme_123/Course"

    scan_res = FilemetaScanResult(
        source_name="MTP Course",
        source_path=device_path,
        items=[
            ScannedFile(
                name="01.mp4",
                path=f"{device_path}/01.mp4",
                title="Lesson 1",
                type="video",
            )
        ],
    )
    src, _, _ = source_svc.add_or_update_source(scan_res)
    # Mark item 1 completed
    items = source_svc.get_items_by_source(src.id)
    source_svc.toggle_item_completion(items[0].id)

    mock_dev_mgr = MagicMock(spec=DeviceManager)
    mock_dev_mgr.is_source_accessible.return_value = False

    adapter = MagicMock()
    view = FilemetaView(
        source_svc, task_svc, daily_svc, adapter, device_manager=mock_dev_mgr
    )

    # Press 'r' (rescan) while disconnected, then 'b' to exit
    with patch("todo.ui.filemeta_view.key_reader.read_key", side_effect=["r", "b"]):
        view.handle_source_items(src)

    # Adapter's scan_folder MUST NOT be called!
    adapter.scan_folder.assert_not_called()

    # Cached items must still exist and retain completion status!
    items_after = source_svc.get_items_by_source(src.id)
    assert len(items_after) == 1
    assert items_after[0].title == "Lesson 1"
    assert items_after[0].completed is True


def test_device_rescan_reconnected_preserves_completion_state(test_setup):
    """
    When device is reconnected, rescanning refreshes metadata and preserves
    completion status and existing tasks without duplicating records.
    """
    app = test_setup["app"]
    source_svc = test_setup["source_svc"]
    daily_svc = test_setup["daily_svc"]
    task_svc = test_setup["task_svc"]
    device_path = "/media/user/USB/Course"

    initial_scan = FilemetaScanResult(
        source_name="USB Course",
        source_path=device_path,
        items=[
            ScannedFile(
                name="01.mp4",
                path=f"{device_path}/01.mp4",
                title="Lesson 1",
                type="video",
            )
        ],
    )
    src, _, _ = source_svc.add_or_update_source(initial_scan)
    items = source_svc.get_items_by_source(src.id)
    source_svc.toggle_item_completion(items[0].id)

    # Rescan result with updated title and an added lesson 2
    updated_scan = FilemetaScanResult(
        source_name="USB Course",
        source_path=device_path,
        items=[
            ScannedFile(
                name="01.mp4",
                path=f"{device_path}/01.mp4",
                title="Lesson 1 (Updated)",
                type="video",
            ),
            ScannedFile(
                name="02.mp4",
                path=f"{device_path}/02.mp4",
                title="Lesson 2",
                type="video",
            ),
        ],
    )

    mock_dev_mgr = MagicMock(spec=DeviceManager)
    mock_dev_mgr.is_source_accessible.return_value = True

    adapter = MagicMock()
    adapter.scan_folder.return_value = updated_scan

    view = FilemetaView(
        source_svc, task_svc, daily_svc, adapter, device_manager=mock_dev_mgr
    )

    # Press 'r' (rescan) then 'b'
    with patch("todo.ui.filemeta_view.key_reader.read_key", side_effect=["r", "b"]):
        view.handle_source_items(src)

    adapter.scan_folder.assert_called_once_with(device_path)

    items_after = source_svc.get_items_by_source(src.id)
    assert len(items_after) == 2
    # Lesson 1 title updated, but completion PRESERVED!
    assert items_after[0].title == "Lesson 1 (Updated)"
    assert items_after[0].completed is True
    assert items_after[1].title == "Lesson 2"
    assert items_after[1].completed is False


def test_device_sources_bulk_deletion(test_setup):
    """
    Bulk deleting device sources removes them from SQLite and unlinks tasks,
    while physical files on the device are never deleted.
    """
    app = test_setup["app"]
    source_svc = test_setup["source_svc"]
    daily_svc = test_setup["daily_svc"]
    task_svc = test_setup["task_svc"]
    path1 = "/media/user/USB/CourseA"
    path2 = "/run/user/1000/1663dac047b6436fae66e27dfca1ed31/storage/emulated/0/CourseB"

    scan1 = FilemetaScanResult(
        source_name="Course A",
        source_path=path1,
        items=[ScannedFile(name="a.mp4", path=f"{path1}/a.mp4", title="A", type="video")],
    )
    scan2 = FilemetaScanResult(
        source_name="Course B",
        source_path=path2,
        items=[ScannedFile(name="b.mp4", path=f"{path2}/b.mp4", title="B", type="video")],
    )

    src1, _, _ = source_svc.add_or_update_source(scan1)
    src2, _, _ = source_svc.add_or_update_source(scan2)

    # Schedule item from src1 to today
    it1 = source_svc.get_items_by_source(src1.id)[0]
    task1 = task_svc.get_or_create_filemeta_task(it1, source_name="Course A")
    daily_svc.add_task_to_date(task1.id, daily_svc.get_today_str())

    view = FilemetaView(
        source_svc, task_svc, daily_svc, app.filemeta_adapter, device_manager=app.device_manager
    )

    # In sources list:
    # 'd' (enter delete mode)
    # Space (select source 1)
    # Down, Space (select source 2)
    # Enter (confirm selection)
    # 'y' (confirm delete)
    # 'b' (exit)
    keys = ["d", Key.SPACE, Key.DOWN, Key.SPACE, Key.ENTER, "y", "b"]
    with patch("todo.ui.filemeta_view.key_reader.read_key", side_effect=keys):
        view.run()

    # Sources deleted from SQLite
    sources_after = source_svc.get_sources(source_type="filemeta")
    assert len(sources_after) == 0

    # Task in Today is unlinked (source_id set to NULL), not deleted!
    tasks_today = daily_svc.get_tasks_for_date(daily_svc.get_today_str())
    assert len(tasks_today) == 1
    assert tasks_today[0].task.source_id is None


def test_handle_browse_device_navigation_and_import(test_setup):
    """
    Test interactive directory browser on a device: navigating subfolders,
    parent folder, and selecting [Import this folder].
    """
    app = test_setup["app"]
    source_svc = test_setup["source_svc"]
    daily_svc = test_setup["daily_svc"]
    task_svc = test_setup["task_svc"]

    mock_dev_mgr = MagicMock(spec=DeviceManager)
    def fake_list_dirs(path):
        if path == "/media/usb":
            return [("Books", "/media/usb/Books"), ("Podcasts", "/media/usb/Podcasts")]
        elif path == "/media/usb/Books":
            return [("Chapter1", "/media/usb/Books/Chapter1")]
        return []
    mock_dev_mgr.list_directories.side_effect = fake_list_dirs

    adapter = MagicMock()
    adapter.scan_folder.return_value = FilemetaScanResult(
        source_name="Books",
        source_path="/media/usb/Books",
        items=[
            ScannedFile(
                name="audio.mp3",
                path="/media/usb/Books/audio.mp3",
                title="audio",
                type="audio",
            )
        ],
    )

    view = FilemetaView(
        source_svc, task_svc, daily_svc, adapter, device_manager=mock_dev_mgr
    )

    device = ConnectedDevice(
        id="usb1",
        name="USB Drive",
        device_type=DeviceType.WIRED_STORAGE,
        is_mounted=True,
        mount_paths=["/media/usb"],
    )

    # 1. At /media/usb: entries are:
    # 0: [Import this folder: usb]
    # 1: 📁 Books/
    # 2: 📁 Podcasts/
    # Key 'down' moves cursor to index 1 (Books/)
    # Key Enter enters Books/ (/media/usb/Books)
    # 2. At /media/usb/Books: entries are:
    # 0: [Import this folder: Books]
    # 1: 📁 .. (Parent folder)
    # 2: 📁 Chapter1/
    # Key Enter selects index 0 -> imports /media/usb/Books!
    keys = [Key.DOWN, Key.ENTER, Key.ENTER]
    with patch("todo.ui.filemeta_view.key_reader.read_key", side_effect=keys):
        err, src = view.handle_browse_device(device, "/media/usb")

    assert err is None
    assert src is not None
    assert src.name == "Books"
    assert src.path == "/media/usb/Books"
    adapter.scan_folder.assert_called_once_with("/media/usb/Books")


def test_device_access_failure_workflows(test_setup):
    """
    Verify clear reporting when:
    - Wired removable drive is unmounted
    - MTP device fails mounting (e.g. locked phone)
    - KDE Connect SFTP fails mounting (storage permission denied)
    """
    app = test_setup["app"]
    mock_dev_mgr = MagicMock(spec=DeviceManager)

    # 1. Unmounted wired drive
    unmounted_drive = ConnectedDevice(
        id="sdb1",
        name="Kingston USB",
        device_type=DeviceType.WIRED_STORAGE,
        is_mounted=False,
        mount_paths=[],
    )
    mock_dev_mgr.discover_wired_devices.return_value = [unmounted_drive]

    view = FilemetaView(
        app.source_service,
        app.task_service,
        app.daily_service,
        app.filemeta_adapter,
        device_manager=mock_dev_mgr,
    )

    # Select unmounted drive -> error message shown, then 'b' to exit
    with patch("todo.ui.filemeta_view.key_reader.read_key", side_effect=[Key.ENTER, "b"]):
        err, src = view.handle_wired_devices()
        assert src is None

    # 2. MTP mount failure
    mtp_dev = ConnectedDevice(
        id="mtp1",
        name="Android Phone (MTP)",
        device_type=DeviceType.WIRED_MTP,
        is_mounted=False,
        activation_root="mtp://host/",
    )
    mock_dev_mgr.discover_wired_devices.return_value = [mtp_dev]
    mock_dev_mgr.mount_mtp_device.return_value = (
        False,
        None,
        "Ensure the Android device is unlocked and USB mode is set to 'File Transfer / MTP'.",
    )

    with patch("todo.ui.filemeta_view.key_reader.read_key", side_effect=[Key.ENTER, "b"]):
        err, src = view.handle_wired_devices()
        assert src is None

    # 3. KDE Connect SFTP failure
    kde_dev = ConnectedDevice(
        id="kde1",
        name="Phone",
        device_type=DeviceType.KDECONNECT,
        is_connected=True,
        is_mounted=False,
    )
    mock_dev_mgr.discover_kdeconnect_devices.return_value = [kde_dev]
    mock_dev_mgr.mount_kdeconnect_device.return_value = (
        False,
        "Ensure storage permissions are granted in the KDE Connect app.",
    )

    with patch("todo.ui.filemeta_view.key_reader.read_key", side_effect=[Key.ENTER, "b"]):
        err, src = view.handle_kdeconnect_devices()
        assert src is None


def test_direct_path_import_and_duplicate_handling(test_setup):
    """
    Test entering or pasting a folder path directly at the primary prompt.
    Verifies automatic path resolution, metadata scanning, and safe re-import
    without duplicate sources or loss of item completion state.
    """
    app = test_setup["app"]
    source_svc = test_setup["source_svc"]
    daily_svc = test_setup["daily_svc"]
    task_svc = test_setup["task_svc"]

    with tempfile.TemporaryDirectory() as tmpdir:
        course_dir = Path(tmpdir) / "Advanced Rust 2026"
        course_dir.mkdir()
        (course_dir / "01 - Intro.mp4").write_text("dummy")

        adapter = MagicMock()
        adapter.scan_folder.return_value = FilemetaScanResult(
            source_name="Advanced Rust 2026",
            source_path=str(course_dir),
            items=[
                ScannedFile(
                    name="01 - Intro.mp4",
                    path=str(course_dir / "01 - Intro.mp4"),
                    title="01 - Intro",
                    type="video",
                )
            ],
        )

        view = FilemetaView(
            source_svc, task_svc, daily_svc, adapter, device_manager=app.device_manager
        )

        # 1. Primary prompt: directly enter path (with surrounding quotes and spaces)
        with patch("todo.ui.filemeta_view.key_reader.read_text_input", return_value=f'"{course_dir}"'):
            err, src = view.handle_add_folder()

        assert err is None
        assert src is not None
        assert src.name == "Advanced Rust 2026"
        assert src.path == str(course_dir)
        assert src.item_count == 1

        # Mark item as completed
        items = source_svc.get_items_by_source(src.id)
        source_svc.toggle_item_completion(items[0].id)

        # 2. Re-import the exact same folder (e.g. rescan / re-import)
        with patch("todo.ui.filemeta_view.key_reader.read_text_input", return_value=str(course_dir)):
            err2, src2 = view.handle_add_folder()

        assert err2 is None
        assert src2 is not None
        assert src2.id == src.id  # Same source ID, no duplicate source!

        # Items count still 1, and completion status PRESERVED!
        sources = source_svc.get_sources(source_type="filemeta")
        assert len(sources) == 1
        items_after = source_svc.get_items_by_source(src2.id)
        assert len(items_after) == 1
        assert items_after[0].completed is True


def test_direct_path_optional_browse_fallback(test_setup):
    """
    Entering '?' or 'd' at the folder path prompt triggers the optional
    device discovery and browsing screen.
    """
    app = test_setup["app"]
    source_svc = test_setup["source_svc"]
    daily_svc = test_setup["daily_svc"]
    task_svc = test_setup["task_svc"]

    mock_dev_mgr = MagicMock(spec=DeviceManager)
    # Discover 1 wired device
    wired_dev = ConnectedDevice(
        id="usb1",
        name="SanDisk USB",
        device_type=DeviceType.WIRED_STORAGE,
        is_mounted=True,
        mount_paths=["/media/usb"],
    )
    mock_dev_mgr.discover_wired_devices.return_value = [wired_dev]
    mock_dev_mgr.list_directories.return_value = [("Lectures", "/media/usb/Lectures")]

    adapter = MagicMock()
    adapter.scan_folder.return_value = FilemetaScanResult(
        source_name="Lectures",
        source_path="/media/usb/Lectures",
        items=[ScannedFile(name="01.mp4", path="/media/usb/Lectures/01.mp4", title="01", type="video")],
    )

    view = FilemetaView(
        source_svc, task_svc, daily_svc, adapter, device_manager=mock_dev_mgr
    )

    # 1. At prompt, type '?' to trigger browse devices
    # 2. In BROWSE CONNECTED DEVICES: '1' selects Wired Devices
    # 3. In WIRED DEVICES: Enter selects SanDisk USB
    # 4. In BROWSE FOLDERS: Enter on [Import this folder: usb] imports it!
    keys = ["1", Key.ENTER, Key.ENTER]
    with patch("todo.ui.filemeta_view.key_reader.read_text_input", return_value="?"):
        with patch("todo.ui.filemeta_view.key_reader.read_key", side_effect=keys):
            err, src = view.handle_add_folder()

    assert err is None
    assert src is not None


def test_direct_path_inaccessible_error_reporting(test_setup):
    """
    Entering an inaccessible or disconnected path returns a clear actionable
    error message without creating a blank source or crashing.
    """
    app = test_setup["app"]
    source_svc = test_setup["source_svc"]
    daily_svc = test_setup["daily_svc"]
    task_svc = test_setup["task_svc"]

    mock_dev_mgr = MagicMock(spec=DeviceManager)
    mock_dev_mgr.resolve_and_mount_path.return_value = (
        False,
        None,
        "KDE Connect device (1663dac0...) is disconnected or unreachable.",
    )

    view = FilemetaView(
        source_svc, task_svc, daily_svc, app.filemeta_adapter, device_manager=mock_dev_mgr
    )

    path_input = "/run/user/1000/1663dac047b6436fae66e27dfca1ed31/storage/emulated/0/Books"
    with patch("todo.ui.filemeta_view.key_reader.read_text_input", return_value=path_input):
        err, src = view.handle_add_folder()

    assert src is None
    assert "disconnected or unreachable" in err
    # Database remains pristine
    assert len(source_svc.get_sources(source_type="filemeta")) == 0


def test_direct_path_timeout_error_reporting(test_setup):
    """
    When filemeta times out while scanning a remote/device path,
    a clear actionable timeout error is displayed and no partial data is imported.
    """
    source_svc = test_setup["source_svc"]
    daily_svc = test_setup["daily_svc"]
    task_svc = test_setup["task_svc"]

    kde_path = "/run/user/1000/5f058b98d20d4a9db6be5533fed8ed05/storage/3533-3033/Course"
    mock_dev_mgr = MagicMock(spec=DeviceManager)
    mock_dev_mgr.resolve_and_mount_path.return_value = (True, kde_path, None)

    adapter = MagicMock()
    adapter.scan_folder.side_effect = FilemetaTimeoutError(
        f"filemeta timed out after 300s while scanning '{kde_path}'",
        timeout=300,
        path=kde_path,
    )

    view = FilemetaView(
        source_svc, task_svc, daily_svc, adapter, device_manager=mock_dev_mgr
    )

    with patch("todo.ui.filemeta_view.key_reader.read_text_input", return_value=kde_path):
        err, src = view.handle_add_folder()

    assert src is None
    assert "Scan timed out after 300s" in err
    assert "Device storage may be slow or unresponsive" in err
    # Nothing was saved to SQLite database
    assert len(source_svc.get_sources(source_type="filemeta")) == 0


def test_rescan_timeout_preserves_existing_data(test_setup):
    """
    When an existing imported source times out during rescan ('r'),
    the cached offline items and completion state remain completely intact.
    """
    source_svc = test_setup["source_svc"]
    daily_svc = test_setup["daily_svc"]
    task_svc = test_setup["task_svc"]

    kde_path = "/run/user/1000/5f058b98d20d4a9db6be5533fed8ed05/storage/Videos"
    initial_scan = FilemetaScanResult(
        source_name="KDE Videos",
        source_path=kde_path,
        items=[
            ScannedFile(name="01.mp4", path=f"{kde_path}/01.mp4", title="01", type="video"),
            ScannedFile(name="02.mp4", path=f"{kde_path}/02.mp4", title="02", type="video"),
        ],
    )
    source, _, _ = source_svc.add_or_update_source(initial_scan)

    # Complete item 1
    items = source_svc.get_items_by_source(source.id)
    source_svc.toggle_item_completion(items[0].id)

    # Rescan raises timeout
    adapter = MagicMock()
    adapter.scan_folder.side_effect = FilemetaTimeoutError(
        f"filemeta timed out after 300s",
        timeout=300,
        path=kde_path,
    )

    mock_dev_mgr = MagicMock(spec=DeviceManager)
    mock_dev_mgr.is_source_accessible.return_value = True

    view = FilemetaView(
        source_svc, task_svc, daily_svc, adapter, device_manager=mock_dev_mgr
    )

    # Press 'r' then 'b'
    keys = ["r", "b"]
    with patch("todo.ui.filemeta_view.key_reader.read_key", side_effect=keys):
        view.handle_source_items(source)

    # Existing items and completion state preserved
    reloaded_items = source_svc.get_items_by_source(source.id)
    assert len(reloaded_items) == 2
    assert reloaded_items[0].completed is True
    assert reloaded_items[1].completed is False


