"""Tests for device discovery, mounting, and management (KDE Connect, Wired Storage, MTP)."""

import json
import os
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from todo.integrations.devices import ConnectedDevice, DeviceManager, DeviceType


@pytest.fixture
def mock_runner():
    return MagicMock()


def test_check_dependencies():
    dm = DeviceManager()
    deps = dm.check_dependencies()
    assert isinstance(deps, dict)
    assert "kdeconnect_cli" in deps
    assert "gdbus" in deps
    assert "gio" in deps
    assert "lsblk" in deps
    assert "findmnt" in deps


def test_discover_kdeconnect_devices_parsing(mock_runner):
    raw_kde_cli = """- SheroninRedmi: 1663dac047b6436fae66e27dfca1ed31 (paired and reachable)
- Sheroninrealme: 5f058b98d20d4a9db6be5533fed8ed05 (paired and reachable)
- OfflineTablet: 11223344556677889900aabbccddeeff (paired and unreachable)
3 devices found
"""
    raw_is_mounted = "(true,)"
    raw_dirs = "({'/run/user/1000/1663dac047b6436fae66e27dfca1ed31/storage/emulated/0': <'Internal shared storage'>, '/run/user/1000/1663dac047b6436fae66e27dfca1ed31/storage/87D6-19FE': <'SanDisk SD card'>},)"

    def fake_run(cmd, *args, **kwargs):
        if cmd[0] == "kdeconnect-cli":
            return MagicMock(returncode=0, stdout=raw_kde_cli)
        if "isMounted" in cmd[-1]:
            return MagicMock(returncode=0, stdout=raw_is_mounted)
        if "getDirectories" in cmd[-1]:
            return MagicMock(returncode=0, stdout=raw_dirs)
        return MagicMock(returncode=0, stdout="")

    mock_runner.side_effect = fake_run
    dm = DeviceManager(command_runner=mock_runner)

    with patch("shutil.which", return_value="/usr/bin/kdeconnect-cli"):
        devs = dm.discover_kdeconnect_devices()

    assert len(devs) == 3
    redmi = devs[0]
    assert redmi.name == "SheroninRedmi"
    assert redmi.id == "1663dac047b6436fae66e27dfca1ed31"
    assert redmi.is_connected is True
    assert redmi.is_mounted is True
    assert len(redmi.mount_paths) == 2
    assert "/run/user/1000/1663dac047b6436fae66e27dfca1ed31/storage/emulated/0" in redmi.mount_paths
    assert redmi.storage_labels["/run/user/1000/1663dac047b6436fae66e27dfca1ed31/storage/emulated/0"] == "Internal shared storage"

    offline = devs[2]
    assert offline.name == "OfflineTablet"
    assert offline.is_connected is False


def test_mount_kdeconnect_device_success(mock_runner):
    mock_runner.return_value = MagicMock(returncode=0, stdout="(true,)")
    dm = DeviceManager(command_runner=mock_runner)

    with patch("shutil.which", return_value="/usr/bin/gdbus"):
        ok, err = dm.mount_kdeconnect_device("1663dac047b6436fae66e27dfca1ed31")

    assert ok is True
    assert err is None


def test_mount_kdeconnect_device_failure(mock_runner):
    def fake_run(cmd, *args, **kwargs):
        if "mountAndWait" in cmd[-1]:
            return MagicMock(returncode=0, stdout="(false,)")
        if "getMountError" in cmd[-1]:
            return MagicMock(returncode=0, stdout="('Storage permission denied on phone',)")
        return MagicMock(returncode=0, stdout="")

    mock_runner.side_effect = fake_run
    dm = DeviceManager(command_runner=mock_runner)

    with patch("shutil.which", return_value="/usr/bin/gdbus"):
        ok, err = dm.mount_kdeconnect_device("1663dac047b6436fae66e27dfca1ed31")

    assert ok is False
    assert "Storage permission denied" in err


def test_discover_wired_devices_lsblk_parsing(mock_runner):
    lsblk_data = {
        "blockdevices": [
            {
                "name": "nvme0n1",
                "model": "Internal SSD",
                "rm": False,
                "hotplug": False,
                "size": "500G",
                "mountpoints": ["/"],
                "children": [],
            },
            {
                "name": "sdb",
                "model": "SanDisk Cruzer",
                "rm": True,
                "hotplug": True,
                "size": "64G",
                "mountpoints": [],
                "children": [
                    {
                        "name": "sdb1",
                        "model": None,
                        "label": "COURSES_USB",
                        "rm": True,
                        "hotplug": True,
                        "size": "64G",
                        "mountpoints": ["/media/user/COURSES_USB"],
                    }
                ],
            },
            {
                "name": "sdc",
                "model": "Kingston DataTraveler",
                "rm": True,
                "hotplug": True,
                "size": "32G",
                "mountpoints": [],
                "children": [],
            },
        ]
    }

    def fake_run(cmd, *args, **kwargs):
        if cmd[0] == "lsblk":
            return MagicMock(returncode=0, stdout=json.dumps(lsblk_data))
        if cmd[0] == "gio":
            return MagicMock(returncode=0, stdout="")
        return MagicMock(returncode=0, stdout="")

    mock_runner.side_effect = fake_run
    dm = DeviceManager(command_runner=mock_runner)

    with patch("shutil.which", return_value="/usr/bin/lsblk"):
        devs = dm.discover_wired_devices()

    # nvme internal SSD ignored; sdb1 mounted removable detected; sdc unmounted removable detected
    assert len(devs) >= 2
    names = [d.name for d in devs]
    assert any("COURSES_USB" in n for n in names)
    assert any("Kingston" in n for n in names)

    mounted_dev = next(d for d in devs if "COURSES_USB" in d.name)
    assert mounted_dev.is_mounted is True
    assert "/media/user/COURSES_USB" in mounted_dev.mount_paths

    unmounted_dev = next(d for d in devs if "Kingston" in d.name)
    assert unmounted_dev.is_mounted is False


def test_discover_wired_devices_mtp_parsing(mock_runner):
    gio_output = """Drive(0): Internal SSD
  Type: GProxyDrive (GProxyVolumeMonitorUDisks2)
Volume(0): RMP2204
  Type: GProxyVolume (GProxyVolumeMonitorMTP)
  ids:
   unix-device: '/dev/bus/usb/001/015'
  activation_root=mtp://realme_RMP2204_VK9PU84H5PGUF6EM/
  themed icons:  [phone]
  can_mount=1
"""

    def fake_run(cmd, *args, **kwargs):
        if cmd[0] == "lsblk":
            return MagicMock(returncode=0, stdout='{"blockdevices": []}')
        if cmd[0] == "gio":
            return MagicMock(returncode=0, stdout=gio_output)
        return MagicMock(returncode=0, stdout="")

    mock_runner.side_effect = fake_run
    dm = DeviceManager(command_runner=mock_runner)

    with patch("shutil.which", return_value="/usr/bin/gio"):
        devs = dm.discover_wired_devices()

    assert len(devs) == 1
    mtp_dev = devs[0]
    assert mtp_dev.device_type == DeviceType.WIRED_MTP
    assert "RMP2204" in mtp_dev.name
    assert mtp_dev.activation_root == "mtp://realme_RMP2204_VK9PU84H5PGUF6EM/"
    assert mtp_dev.is_mounted is False


def test_mount_mtp_device_success(mock_runner):
    mock_runner.return_value = MagicMock(returncode=0, stdout="", stderr="")
    dm = DeviceManager(command_runner=mock_runner)

    with patch("shutil.which", return_value="/usr/bin/gio"):
        with patch("pathlib.Path.exists", return_value=True):
            with patch(
                "pathlib.Path.iterdir",
                return_value=[Path("/run/user/1000/gvfs/mtp:host=realme_123")],
            ):
                ok, path, err = dm.mount_mtp_device("mtp://realme_123/")

    assert ok is True
    assert err is None
    assert path == "/run/user/1000/gvfs/mtp:host=realme_123"


def test_mount_mtp_device_locked_error(mock_runner):
    mock_runner.return_value = MagicMock(
        returncode=2, stdout="", stderr="Unable to open MTP device “001,015”"
    )
    dm = DeviceManager(command_runner=mock_runner)

    with patch("shutil.which", return_value="/usr/bin/gio"):
        ok, path, err = dm.mount_mtp_device("mtp://realme_123/")

    assert ok is False
    assert path is None
    assert "Ensure the Android device is unlocked" in err


def test_list_directories():
    dm = DeviceManager()
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_p = Path(tmpdir)
        (tmp_p / "Folder B").mkdir()
        (tmp_p / "Folder A").mkdir()
        (tmp_p / "file.txt").write_text("hello")

        subdirs = dm.list_directories(tmpdir)
        assert len(subdirs) == 2
        # Sorted alphabetically
        assert subdirs[0][0] == "Folder A"
        assert subdirs[1][0] == "Folder B"
        assert subdirs[0][1] == str(tmp_p / "Folder A")


def test_is_source_accessible_and_kdeconnect_reconnect(mock_runner):
    dm = DeviceManager(command_runner=mock_runner)

    # 1. Existing local path
    with tempfile.TemporaryDirectory() as tmpdir:
        assert dm.is_source_accessible(tmpdir) is True

    # 2. Inaccessible local path
    assert dm.is_source_accessible("/nonexistent/path/9999") is False

    # 3. Inaccessible KDE Connect path with reachable device -> auto-mount
    kde_path = "/run/user/1000/1663dac047b6436fae66e27dfca1ed31/storage/emulated/0/Courses"

    raw_kde_cli = "- SheroninRedmi: 1663dac047b6436fae66e27dfca1ed31 (paired and reachable)\n"

    def fake_run(cmd, *args, **kwargs):
        if cmd[0] == "kdeconnect-cli":
            return MagicMock(returncode=0, stdout=raw_kde_cli)
        if "mountAndWait" in cmd[-1]:
            return MagicMock(returncode=0, stdout="(true,)")
        return MagicMock(returncode=0, stdout="")

    mock_runner.side_effect = fake_run

    with patch("shutil.which", return_value="/usr/bin/kdeconnect-cli"):
        with patch("pathlib.Path.exists", side_effect=[False, True]):
            with patch("os.access", return_value=True):
                accessible = dm.is_source_accessible(kde_path)
                assert accessible is True


def test_resolve_and_mount_path_empty_and_spaces():
    dm = DeviceManager()
    ok, path, err = dm.resolve_and_mount_path("")
    assert ok is False
    assert "empty" in err

    ok, path, err = dm.resolve_and_mount_path("   ")
    assert ok is False
    assert "empty" in err


def test_resolve_and_mount_path_local_success_with_spaces_and_quotes():
    dm = DeviceManager()
    with tempfile.TemporaryDirectory() as tmpdir:
        spaced_dir = Path(tmpdir) / "My Course 101"
        spaced_dir.mkdir()

        # Quoted input with spaces
        quoted_input = f'"{spaced_dir}"'
        ok, res_path, err = dm.resolve_and_mount_path(quoted_input)
        assert ok is True
        assert err is None
        assert res_path == str(spaced_dir.resolve())

        # Single quoted
        single_quoted = f"'{spaced_dir}'"
        ok, res_path, err = dm.resolve_and_mount_path(single_quoted)
        assert ok is True
        assert res_path == str(spaced_dir.resolve())


def test_resolve_and_mount_path_local_errors():
    dm = DeviceManager()
    with tempfile.TemporaryDirectory() as tmpdir:
        file_path = Path(tmpdir) / "notes.txt"
        file_path.write_text("notes")

        # Passing a file instead of directory
        ok, path, err = dm.resolve_and_mount_path(str(file_path))
        assert ok is False
        assert "not a directory" in err

        # Nonexistent local folder
        nonexistent = Path(tmpdir) / "nonexistent_subfolder_xyz"
        ok, path, err = dm.resolve_and_mount_path(str(nonexistent))
        assert ok is False
        assert "does not exist" in err

        # Permission denied
        with patch("os.access", return_value=False):
            ok, path, err = dm.resolve_and_mount_path(tmpdir)
            assert ok is False
            assert "Permission denied" in err


def test_resolve_and_mount_path_kdeconnect_uri(mock_runner):
    raw_kde_cli = "- SheroninRedmi: 1663dac047b6436fae66e27dfca1ed31 (paired and reachable)\n"

    def fake_run(cmd, *args, **kwargs):
        if cmd[0] == "kdeconnect-cli":
            return MagicMock(returncode=0, stdout=raw_kde_cli)
        if "isMounted" in cmd[-1]:
            return MagicMock(returncode=0, stdout="(true,)")
        return MagicMock(returncode=0, stdout="")

    mock_runner.side_effect = fake_run
    dm = DeviceManager(command_runner=mock_runner)

    uri = "kdeconnect://SheroninRedmi/storage/emulated/0/Books"
    with patch("shutil.which", return_value="/usr/bin/kdeconnect-cli"):
        with patch("pathlib.Path.exists", return_value=True):
            with patch("pathlib.Path.is_dir", return_value=True):
                with patch("os.access", return_value=True):
                    ok, res_path, err = dm.resolve_and_mount_path(uri)
                    assert ok is True
                    assert err is None
                    assert "1663dac047b6436fae66e27dfca1ed31/storage/emulated/0/Books" in res_path


def test_resolve_and_mount_path_kdeconnect_fuse_path_auto_mount(mock_runner):
    fuse_path = "/run/user/1000/1663dac047b6436fae66e27dfca1ed31/storage/emulated/0/Books"
    raw_kde_cli = "- SheroninRedmi: 1663dac047b6436fae66e27dfca1ed31 (paired and reachable)\n"

    def fake_run(cmd, *args, **kwargs):
        if cmd[0] == "kdeconnect-cli":
            return MagicMock(returncode=0, stdout=raw_kde_cli)
        if "mountAndWait" in cmd[-1]:
            return MagicMock(returncode=0, stdout="(true,)")
        return MagicMock(returncode=0, stdout="")

    mock_runner.side_effect = fake_run
    dm = DeviceManager(command_runner=mock_runner)

    with patch("shutil.which", return_value="/usr/bin/kdeconnect-cli"):
        # First exists check is False (unmounted), second exists check is True (mounted)
        with patch("pathlib.Path.exists", side_effect=[False, True]):
            with patch("pathlib.Path.is_dir", return_value=True):
                with patch("os.access", return_value=True):
                    ok, res_path, err = dm.resolve_and_mount_path(fuse_path)
                    assert ok is True
                    assert res_path == fuse_path


def test_resolve_and_mount_path_kdeconnect_disconnected(mock_runner):
    fuse_path = "/run/user/1000/1663dac047b6436fae66e27dfca1ed31/storage/emulated/0/Books"
    raw_kde_cli = "- SheroninRedmi: 1663dac047b6436fae66e27dfca1ed31 (paired and unreachable)\n"

    mock_runner.return_value = MagicMock(returncode=0, stdout=raw_kde_cli)
    dm = DeviceManager(command_runner=mock_runner)

    with patch("shutil.which", return_value="/usr/bin/kdeconnect-cli"):
        with patch("pathlib.Path.exists", return_value=False):
            ok, res_path, err = dm.resolve_and_mount_path(fuse_path)
            assert ok is False
            assert "offline or unreachable" in err


def test_resolve_and_mount_path_mtp_uri_and_mount(mock_runner):
    gio_output = """Volume(0): RMP2204
  Type: GProxyVolume (GProxyVolumeMonitorMTP)
  activation_root=mtp://realme_RMP2204_VK9PU84H5PGUF6EM/
"""
    def fake_run(cmd, *args, **kwargs):
        if cmd[0] == "gio" and "-l" in cmd:
            return MagicMock(returncode=0, stdout=gio_output)
        if cmd[0] == "gio" and "mount" in cmd:
            return MagicMock(returncode=0, stdout="", stderr="")
        return MagicMock(returncode=0, stdout="")

    mock_runner.side_effect = fake_run
    dm = DeviceManager(command_runner=mock_runner)

    uri = "mtp://realme_RMP2204_VK9PU84H5PGUF6EM/Internal shared storage/Podcasts"
    with patch("shutil.which", return_value="/usr/bin/gio"):
        with patch("pathlib.Path.exists", return_value=True):
            with patch(
                "pathlib.Path.iterdir",
                return_value=[Path("/run/user/1000/gvfs/mtp:host=realme_RMP2204_VK9PU84H5PGUF6EM")],
            ):
                with patch("pathlib.Path.is_dir", return_value=True):
                    with patch("os.access", return_value=True):
                        ok, res_path, err = dm.resolve_and_mount_path(uri)
                        assert ok is True
                        assert "Internal shared storage/Podcasts" in res_path


def test_resolve_and_mount_path_mtp_disconnected(mock_runner):
    fuse_path = "/run/user/1000/gvfs/mtp:host=realme_123/Course"
    mock_runner.return_value = MagicMock(returncode=0, stdout="")
    dm = DeviceManager(command_runner=mock_runner)

    with patch("shutil.which", return_value="/usr/bin/gio"):
        with patch("pathlib.Path.exists", return_value=False):
            ok, res_path, err = dm.resolve_and_mount_path(fuse_path)
            assert ok is False
            assert "Android MTP device is disconnected" in err


def test_resolve_and_mount_path_removable_drive(mock_runner):
    lsblk_data = {
        "blockdevices": [
            {
                "name": "sdb1",
                "label": "MY_USB",
                "rm": True,
                "hotplug": True,
                "mountpoints": [],
            }
        ]
    }
    mock_runner.return_value = MagicMock(returncode=0, stdout=json.dumps(lsblk_data))
    dm = DeviceManager(command_runner=mock_runner)

    unmounted_path = "/media/user/MY_USB/Course"
    with patch("shutil.which", return_value="/usr/bin/lsblk"):
        with patch("pathlib.Path.exists", return_value=False):
            ok, res_path, err = dm.resolve_and_mount_path(unmounted_path)
            assert ok is False
            assert "is not mounted" in err

    disconnected_path = "/media/user/DISCONNECTED_DRIVE/Course"
    with patch("shutil.which", return_value="/usr/bin/lsblk"):
        with patch("pathlib.Path.exists", return_value=False):
            ok, res_path, err = dm.resolve_and_mount_path(disconnected_path)
            assert ok is False
            assert "disconnected or not found" in err
