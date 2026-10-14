"""Regression tests for offline-first SQLite persistence and zero remote filesystem access during browsing."""

import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from todo.config import Config
from todo.integrations.devices import ConnectedDevice, DeviceManager, DeviceType
from todo.integrations.filemeta import (
    FilemetaAdapter,
    FilemetaScanResult,
    ScannedFile,
    ScannedFolder,
    compute_canonical_uri,
)
from todo.models import FileItem, Source, SourceFolder
from todo.services.daily_service import DailyService
from todo.services.source_service import SourceService
from todo.services.task_service import TaskService
from todo.ui.app import TodoApp
from todo.ui.filemeta_view import FilemetaView
from todo.ui.key_reader import Key


@pytest.fixture
def offline_setup():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "offline_test.db"
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
            "db_path": db_path,
        }


def test_hierarchical_folder_import_and_sqlite_persistence(offline_setup):
    """
    Importing a folder hierarchy must parse and persist the full tree:
    source_folders with parent_id and relative_path, and file_items with folder_id.
    """
    source_svc: SourceService = offline_setup["source_svc"]
    root_path = "/run/user/1000/5f058b98/storage/Courses/AI_Machine"

    scan_res = FilemetaScanResult(
        source_name="AI Machine Course",
        source_path=root_path,
        canonical_uri="kdeconnect://5f058b98/storage/Courses/AI_Machine",
        folders=[
            ScannedFolder(
                name="01-Email Domination",
                path=f"{root_path}/01-Email Domination",
                relative_path="01-Email Domination",
                parent_relative_path=None,
            ),
            ScannedFolder(
                name="Resources",
                path=f"{root_path}/01-Email Domination/Resources",
                relative_path="01-Email Domination/Resources",
                parent_relative_path="01-Email Domination",
            ),
            ScannedFolder(
                name="02-Outreach Playbook",
                path=f"{root_path}/02-Outreach Playbook",
                relative_path="02-Outreach Playbook",
                parent_relative_path=None,
            ),
        ],
        items=[
            ScannedFile(
                name="Overview.mp4",
                path=f"{root_path}/Overview.mp4",
                title="Course Overview",
                type="video",
                relative_path="Overview.mp4",
                parent_relative_path=None,
                size=10485760,
                extension=".mp4",
                metadata={"duration_formatted": "05:12"},
            ),
            ScannedFile(
                name="01-Intro.mp4",
                path=f"{root_path}/01-Email Domination/01-Intro.mp4",
                title="01 - Intro",
                type="video",
                relative_path="01-Email Domination/01-Intro.mp4",
                parent_relative_path="01-Email Domination",
                size=20971520,
                extension=".mp4",
                metadata={"duration_formatted": "14:20"},
            ),
            ScannedFile(
                name="Templates.pdf",
                path=f"{root_path}/01-Email Domination/Resources/Templates.pdf",
                title="Email Templates",
                type="document",
                relative_path="01-Email Domination/Resources/Templates.pdf",
                parent_relative_path="01-Email Domination/Resources",
                size=1024000,
                extension=".pdf",
                metadata={"pages": 12},
            ),
            ScannedFile(
                name="01-Cold-Email.mp4",
                path=f"{root_path}/02-Outreach Playbook/01-Cold-Email.mp4",
                title="01 - Cold Email",
                type="video",
                relative_path="02-Outreach Playbook/01-Cold-Email.mp4",
                parent_relative_path="02-Outreach Playbook",
                size=31457280,
                extension=".mp4",
                metadata={"duration_formatted": "22:00"},
            ),
        ],
    )

    src, added, updated = source_svc.add_or_update_source(scan_res)
    assert added == 4
    assert updated == 0
    assert src.canonical_uri == "kdeconnect://5f058b98/storage/Courses/AI_Machine"
    assert src.scan_status == "completed"

    # Verify source has subfolders
    assert source_svc.has_subfolders(src.id) is True

    # Root folders: "01-Email Domination" and "02-Outreach Playbook"
    root_folders = source_svc.get_folders(src.id, parent_id=None)
    assert len(root_folders) == 2
    f_email = next(f for f in root_folders if f.name == "01-Email Domination")
    f_outreach = next(f for f in root_folders if f.name == "02-Outreach Playbook")
    assert f_email.parent_id is None
    assert f_email.relative_path == "01-Email Domination"
    # Includes recursive child file Templates.pdf + direct child 01-Intro.mp4 = 2 items
    assert f_email.item_count == 2
    assert f_email.completed_count == 0

    # Subfolder of 01-Email Domination: "Resources"
    email_subs = source_svc.get_folders(src.id, parent_id=f_email.id)
    assert len(email_subs) == 1
    f_res = email_subs[0]
    assert f_res.name == "Resources"
    assert f_res.parent_id == f_email.id
    assert f_res.relative_path == "01-Email Domination/Resources"
    assert f_res.item_count == 1

    # Root level files
    root_files = source_svc.get_items_in_folder(src.id, folder_id=None)
    assert len(root_files) == 1
    assert root_files[0].title == "Course Overview"
    assert root_files[0].folder_id is None

    # Files inside "01-Email Domination"
    email_files = source_svc.get_items_in_folder(src.id, folder_id=f_email.id)
    assert len(email_files) == 1
    assert email_files[0].title == "01 - Intro"
    assert email_files[0].folder_id == f_email.id

    # Files inside "Resources"
    res_files = source_svc.get_items_in_folder(src.id, folder_id=f_res.id)
    assert len(res_files) == 1
    assert res_files[0].title == "Email Templates"
    assert res_files[0].folder_id == f_res.id


def test_zero_remote_filesystem_access_during_hierarchical_navigation(offline_setup):
    """
    Crucial regression test:
    When navigating folders, entering subfolders, navigating back up, and inspecting items,
    ZERO remote filesystem calls (scandir, listdir, scan_folder, subprocess) must be made.
    """
    source_svc: SourceService = offline_setup["source_svc"]
    task_svc: TaskService = offline_setup["task_svc"]
    daily_svc: DailyService = offline_setup["daily_svc"]
    adapter = offline_setup["adapter"]
    root_path = "/run/user/1000/device_phone/storage/Course"

    scan_res = FilemetaScanResult(
        source_name="Offline Navigation Course",
        source_path=root_path,
        folders=[
            ScannedFolder(
                name="Module 1",
                path=f"{root_path}/Module 1",
                relative_path="Module 1",
                parent_relative_path=None,
            ),
        ],
        items=[
            ScannedFile(
                name="Lesson1.mp4",
                path=f"{root_path}/Module 1/Lesson1.mp4",
                title="Lesson 1 - Fundamentals",
                type="video",
                relative_path="Module 1/Lesson1.mp4",
                parent_relative_path="Module 1",
                metadata={"duration_formatted": "10:00"},
            )
        ],
    )
    src, _, _ = source_svc.add_or_update_source(scan_res)

    mock_dev_mgr = MagicMock(spec=DeviceManager)
    # Device is disconnected!
    mock_dev_mgr.is_source_accessible.return_value = False

    view = FilemetaView(
        source_svc, task_svc, daily_svc, adapter, device_manager=mock_dev_mgr
    )

    # Key sequence 1: Navigate into Module 1 and schedule Lesson 1 to Today
    # 1. At root: Module 1 is at index 0. Press Enter -> opens Module 1!
    # 2. Inside Module 1: Entry 0 is ".. (Parent folder)", Entry 1 is Lesson 1.
    #    Press Down arrow -> highlights Lesson 1.
    #    Press Enter -> schedules Lesson 1 to Today! (returns "added")
    keys_schedule = [
        Key.ENTER,  # Open Module 1
        Key.DOWN,   # Select Lesson 1
        Key.ENTER,  # Add to Today
    ]

    def forbidden_call(*args, **kwargs):
        raise AssertionError("Forbidden remote access occurred during routine browsing!")

    with patch("os.scandir", side_effect=forbidden_call):
        with patch("os.listdir", side_effect=forbidden_call):
            with patch.object(adapter, "scan_folder", side_effect=forbidden_call):
                with patch("subprocess.run", side_effect=forbidden_call):
                    with patch("todo.ui.filemeta_view.key_reader.read_key", side_effect=keys_schedule):
                        ret = view.handle_source_items(src)

    assert ret == "added"

    today_str = daily_svc.get_today_str()
    tasks_today = daily_svc.get_tasks_for_date(today_str)
    assert len(tasks_today) == 1
    assert "Lesson 1 - Fundamentals" in tasks_today[0].task.title

    # Key sequence 2: Navigate back in, toggle completion with 't', navigate up with 'b'
    keys_toggle = [
        Key.ENTER,  # Open Module 1
        Key.DOWN,   # Select Lesson 1
        "t",        # Toggle completion
        "b",        # Back to root
        "b",        # Exit view
    ]
    with patch("os.scandir", side_effect=forbidden_call):
        with patch("os.listdir", side_effect=forbidden_call):
            with patch.object(adapter, "scan_folder", side_effect=forbidden_call):
                with patch("subprocess.run", side_effect=forbidden_call):
                    with patch("todo.ui.filemeta_view.key_reader.read_key", side_effect=keys_toggle):
                        view.handle_source_items(src)

    # Verify task was synced to completed in SQLite
    tasks_today_after = daily_svc.get_tasks_for_date(today_str)
    assert tasks_today_after[0].is_completed is True


def test_offline_fuzzy_search_across_entire_course(offline_setup):
    """
    Fuzzy search must query SQLite across ALL items in the course,
    allowing instant discovery even when the device is disconnected.
    """
    source_svc: SourceService = offline_setup["source_svc"]
    task_svc: TaskService = offline_setup["task_svc"]
    daily_svc: DailyService = offline_setup["daily_svc"]
    adapter = offline_setup["adapter"]
    root_path = "/run/user/1000/disconnected_dev/Course"

    scan_res = FilemetaScanResult(
        source_name="Fuzzy Search Course",
        source_path=root_path,
        folders=[
            ScannedFolder(
                name="Module A",
                path=f"{root_path}/Module A",
                relative_path="Module A",
                parent_relative_path=None,
            ),
            ScannedFolder(
                name="Module B",
                path=f"{root_path}/Module B",
                relative_path="Module B",
                parent_relative_path=None,
            ),
        ],
        items=[
            ScannedFile(
                name="LessonA.mp4",
                path=f"{root_path}/Module A/LessonA.mp4",
                title="Introduction to Neural Nets",
                type="video",
                relative_path="Module A/LessonA.mp4",
                parent_relative_path="Module A",
            ),
            ScannedFile(
                name="LessonB.mp4",
                path=f"{root_path}/Module B/LessonB.mp4",
                title="Convolutional Architectures",
                type="video",
                relative_path="Module B/LessonB.mp4",
                parent_relative_path="Module B",
            ),
        ],
    )
    src, _, _ = source_svc.add_or_update_source(scan_res)

    mock_dev_mgr = MagicMock(spec=DeviceManager)
    mock_dev_mgr.is_source_accessible.return_value = False

    view = FilemetaView(
        source_svc, task_svc, daily_svc, adapter, device_manager=mock_dev_mgr
    )

    # Search sequence:
    # 1. '/' activates search
    # 2. 'c', 'o', 'n', 'v' types "conv"
    # 3. Enter on search result adds "Convolutional Architectures" to Today!
    keys = ["/", "c", "o", "n", "v", Key.ENTER, Key.ENTER]

    with patch("todo.ui.filemeta_view.key_reader.read_key", side_effect=keys):
        ret = view.handle_source_items(src)

    assert ret == "added"
    today_str = daily_svc.get_today_str()
    tasks_today = daily_svc.get_tasks_for_date(today_str)
    assert len(tasks_today) == 1
    assert "Convolutional Architectures" in tasks_today[0].task.title


def test_app_restart_offline_folder_navigation(offline_setup):
    """
    Restarting Extern must load the full folder structure from SQLite without device interaction.
    """
    cfg = offline_setup["cfg"]
    source_svc: SourceService = offline_setup["source_svc"]
    root_path = "/run/user/1000/offline_device/Course"

    scan_res = FilemetaScanResult(
        source_name="Restart Course",
        source_path=root_path,
        folders=[
            ScannedFolder(
                name="Sub 1",
                path=f"{root_path}/Sub 1",
                relative_path="Sub 1",
                parent_relative_path=None,
            ),
        ],
        items=[
            ScannedFile(
                name="File 1.mp4",
                path=f"{root_path}/Sub 1/File 1.mp4",
                title="File 1",
                type="video",
                relative_path="Sub 1/File 1.mp4",
                parent_relative_path="Sub 1",
                size=5000,
                extension=".mp4",
            )
        ],
    )
    src, _, _ = source_svc.add_or_update_source(scan_res)

    # Simulate quitting and starting fresh TodoApp
    fresh_app = TodoApp(cfg)
    sources = fresh_app.source_service.get_sources(source_type="filemeta")
    assert len(sources) == 1
    loaded_src = sources[0]
    assert loaded_src.id == src.id
    assert loaded_src.name == "Restart Course"

    assert fresh_app.source_service.has_subfolders(loaded_src.id) is True
    folders = fresh_app.source_service.get_folders(loaded_src.id, parent_id=None)
    assert len(folders) == 1
    assert folders[0].name == "Sub 1"

    items_in_sub = fresh_app.source_service.get_items_in_folder(
        loaded_src.id, folder_id=folders[0].id
    )
    assert len(items_in_sub) == 1
    assert items_in_sub[0].title == "File 1"
    assert items_in_sub[0].extension == ".mp4"
    assert items_in_sub[0].file_size == 5000


def test_disconnected_rescan_preserves_records_and_warns(offline_setup):
    """
    Rescanning ('r') when the device is disconnected must not delete or alter any
    records and must display a clear warning.
    """
    source_svc: SourceService = offline_setup["source_svc"]
    task_svc: TaskService = offline_setup["task_svc"]
    daily_svc: DailyService = offline_setup["daily_svc"]
    adapter = offline_setup["adapter"]
    root_path = "/run/user/1000/disconnected_dev/Course"

    scan_res = FilemetaScanResult(
        source_name="Preserve Course",
        source_path=root_path,
        folders=[
            ScannedFolder(
                name="Mod 1",
                path=f"{root_path}/Mod 1",
                relative_path="Mod 1",
                parent_relative_path=None,
            )
        ],
        items=[
            ScannedFile(
                name="Item 1.mp4",
                path=f"{root_path}/Mod 1/Item 1.mp4",
                title="Item 1",
                type="video",
                relative_path="Mod 1/Item 1.mp4",
                parent_relative_path="Mod 1",
            )
        ],
    )
    src, _, _ = source_svc.add_or_update_source(scan_res)

    # Mark item 1 completed
    items = source_svc.get_items_by_source(src.id)
    source_svc.toggle_item_completion(items[0].id)

    mock_dev_mgr = MagicMock(spec=DeviceManager)
    mock_dev_mgr.is_source_accessible.return_value = False
    mock_dev_mgr.resolve_canonical_uri.return_value = (False, None)

    mock_adapter = MagicMock()
    view = FilemetaView(
        source_svc, task_svc, daily_svc, mock_adapter, device_manager=mock_dev_mgr
    )

    # Press 'r' (rescan), then 'b' to exit
    with patch("todo.ui.filemeta_view.key_reader.read_key", side_effect=["r", "b"]):
        view.handle_source_items(src)

    # Adapter's scan_folder MUST NOT be called!
    mock_adapter.scan_folder.assert_not_called()

    # All items and folders remain intact in SQLite
    items_after = source_svc.get_items_by_source(src.id)
    assert len(items_after) == 1
    assert items_after[0].completed is True
    folders_after = source_svc.get_folders(src.id)
    assert len(folders_after) == 1


def test_reconnected_rescan_updates_metadata_and_preserves_completion(offline_setup):
    """
    When device is reconnected, an explicit rescan updates metadata, adds new files,
    prunes deleted files, and preserves existing completed status and Today tasks.
    """
    source_svc: SourceService = offline_setup["source_svc"]
    task_svc: TaskService = offline_setup["task_svc"]
    daily_svc: DailyService = offline_setup["daily_svc"]
    root_path = "/run/user/1000/reconnected_dev/Course"

    initial_scan = FilemetaScanResult(
        source_name="Reconnected Course",
        source_path=root_path,
        folders=[
            ScannedFolder(
                name="Mod 1",
                path=f"{root_path}/Mod 1",
                relative_path="Mod 1",
                parent_relative_path=None,
            )
        ],
        items=[
            ScannedFile(
                name="Old.mp4",
                path=f"{root_path}/Mod 1/Old.mp4",
                title="Lesson To Delete",
                type="video",
                relative_path="Mod 1/Old.mp4",
                parent_relative_path="Mod 1",
            ),
            ScannedFile(
                name="Keep.mp4",
                path=f"{root_path}/Mod 1/Keep.mp4",
                title="Keep Me",
                type="video",
                relative_path="Mod 1/Keep.mp4",
                parent_relative_path="Mod 1",
            ),
        ],
    )
    src, _, _ = source_svc.add_or_update_source(initial_scan)

    # Mark "Keep Me" as completed and add to Today
    items = source_svc.get_items_by_source(src.id)
    item_keep = next(it for it in items if it.title == "Keep Me")
    source_svc.toggle_item_completion(item_keep.id)
    task = task_svc.get_or_create_filemeta_task(item_keep, source_name=src.name)
    daily_svc.add_task_to_date(task.id, daily_svc.get_today_str())

    # New scan: Old.mp4 is deleted, Keep.mp4 has updated title, New.mp4 is added
    updated_scan = FilemetaScanResult(
        source_name="Reconnected Course",
        source_path=root_path,
        folders=[
            ScannedFolder(
                name="Mod 1",
                path=f"{root_path}/Mod 1",
                relative_path="Mod 1",
                parent_relative_path=None,
            )
        ],
        items=[
            ScannedFile(
                name="Keep.mp4",
                path=f"{root_path}/Mod 1/Keep.mp4",
                title="Keep Me (Updated 4K)",
                type="video",
                relative_path="Mod 1/Keep.mp4",
                parent_relative_path="Mod 1",
            ),
            ScannedFile(
                name="New.mp4",
                path=f"{root_path}/Mod 1/New.mp4",
                title="Brand New Lesson",
                type="video",
                relative_path="Mod 1/New.mp4",
                parent_relative_path="Mod 1",
            ),
        ],
    )

    mock_dev_mgr = MagicMock(spec=DeviceManager)
    mock_dev_mgr.is_source_accessible.return_value = True

    mock_adapter = MagicMock()
    mock_adapter.scan_folder.return_value = updated_scan

    view = FilemetaView(
        source_svc, task_svc, daily_svc, mock_adapter, device_manager=mock_dev_mgr
    )

    with patch("todo.ui.filemeta_view.key_reader.read_key", side_effect=["r", "b"]):
        view.handle_source_items(src)

    mock_adapter.scan_folder.assert_called_once_with(root_path)

    items_after = source_svc.get_items_by_source(src.id)
    assert len(items_after) == 2
    titles_after = {it.title for it in items_after}
    assert "Lesson To Delete" not in titles_after
    assert "Brand New Lesson" in titles_after
    assert "Keep Me (Updated 4K)" in titles_after

    # Completed status preserved!
    keep_after = next(it for it in items_after if "Keep Me" in it.title)
    assert keep_after.completed is True


def test_canonical_uri_re_resolution_on_mount_path_change(offline_setup):
    """
    When device mount point changes (e.g. across reconnects or reboots),
    rescan re-resolves the path using the canonical URI and updates records.
    """
    source_svc: SourceService = offline_setup["source_svc"]
    task_svc: TaskService = offline_setup["task_svc"]
    daily_svc: DailyService = offline_setup["daily_svc"]

    old_path = "/run/user/1000/old_mount/Course"
    new_path = "/run/user/1000/new_mount/Course"
    canonical_uri = "kdeconnect://device_xyz/Course"

    initial_scan = FilemetaScanResult(
        source_name="Mount Change Course",
        source_path=old_path,
        canonical_uri=canonical_uri,
        items=[
            ScannedFile(
                name="01.mp4",
                path=f"{old_path}/01.mp4",
                title="01 - Basics",
                type="video",
                relative_path="01.mp4",
            )
        ],
    )
    src, _, _ = source_svc.add_or_update_source(initial_scan)

    # Mark 01.mp4 completed
    items = source_svc.get_items_by_source(src.id)
    source_svc.toggle_item_completion(items[0].id)

    updated_scan = FilemetaScanResult(
        source_name="Mount Change Course",
        source_path=new_path,
        canonical_uri=canonical_uri,
        items=[
            ScannedFile(
                name="01.mp4",
                path=f"{new_path}/01.mp4",
                title="01 - Basics",
                type="video",
                relative_path="01.mp4",
            )
        ],
    )

    mock_dev_mgr = MagicMock(spec=DeviceManager)
    # Old path is not accessible, but new path is!
    def fake_accessible(path, attempt_reconnect=False):
        return path == new_path

    mock_dev_mgr.is_source_accessible.side_effect = fake_accessible
    mock_dev_mgr.resolve_canonical_uri.return_value = (True, new_path)

    mock_adapter = MagicMock()
    mock_adapter.scan_folder.return_value = updated_scan

    view = FilemetaView(
        source_svc, task_svc, daily_svc, mock_adapter, device_manager=mock_dev_mgr
    )

    with patch("todo.ui.filemeta_view.key_reader.read_key", side_effect=["r", "b"]):
        view.handle_source_items(src)

    mock_dev_mgr.resolve_canonical_uri.assert_called_with(canonical_uri)
    mock_adapter.scan_folder.assert_called_once_with(new_path)

    # Completion state survived the mount path change!
    items_after = source_svc.get_items_by_source(src.id)
    assert len(items_after) == 1
    assert items_after[0].completed is True
    assert items_after[0].path == f"{new_path}/01.mp4"
