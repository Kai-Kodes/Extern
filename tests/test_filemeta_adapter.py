"""Tests for the FilemetaAdapter integration."""

import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import subprocess

from todo.config import Config
from todo.integrations.filemeta import (
    FilemetaAdapter,
    FilemetaError,
    FilemetaExecutionError,
    FilemetaNotFoundError,
    FilemetaParseError,
    FilemetaTimeoutError,
    FolderNotFoundError,
    InvalidFolderError,
    clean_file_title,
    determine_file_type,
    is_device_path,
)


def test_clean_file_title():
    assert clean_file_title("Understanding Desire.mp4") == "Understanding Desire"
    assert clean_file_title("01 - Introduction.mkv") == "01 - Introduction"
    assert clean_file_title("Lecture Notes.pdf") == "Lecture Notes"
    assert clean_file_title("plain_file") == "plain_file"


def test_determine_file_type():
    assert determine_file_type("video.mp4") == "video"
    assert determine_file_type("audio.mp3") == "audio"
    assert determine_file_type("doc.pdf") == "document"
    assert determine_file_type("archive.tar.gz") == "file"


def test_scan_folder_real():
    """Test actual scan using the installed filemeta executable."""
    adapter = FilemetaAdapter("filemeta")
    if not adapter.check_availability():
        pytest.skip("filemeta executable not found in PATH")

    with tempfile.TemporaryDirectory() as tmpdir:
        course_dir = Path(tmpdir) / "Test Course"
        course_dir.mkdir()
        (course_dir / "01 - Introduction.mp4").write_text("dummy")
        (course_dir / "02 - Strategy.mp4").write_text("dummy")
        (course_dir / "Syllabus.pdf").write_text("dummy")

        result = adapter.scan_folder(course_dir)

        assert result.source_name == "Test Course"
        assert len(result.items) == 3
        titles = {it.title for it in result.items}
        assert "01 - Introduction" in titles
        assert "02 - Strategy" in titles
        assert "Syllabus" in titles

        video_items = [it for it in result.items if it.type == "video"]
        assert len(video_items) == 2


def test_scan_folder_nonexistent():
    adapter = FilemetaAdapter("filemeta")
    with pytest.raises(FolderNotFoundError):
        adapter.scan_folder("/path/that/definitely/does/not/exist/12345")


def test_scan_folder_not_a_directory():
    adapter = FilemetaAdapter("filemeta")
    with tempfile.NamedTemporaryFile() as tmpfile:
        with pytest.raises(InvalidFolderError):
            adapter.scan_folder(tmpfile.name)


def test_scan_folder_command_not_found():
    adapter = FilemetaAdapter("nonexistent_command_xyz_123")
    with pytest.raises(FilemetaNotFoundError):
        adapter.scan_folder("/tmp")


def test_scan_folder_malformed_json():
    adapter = FilemetaAdapter("filemeta")
    with tempfile.TemporaryDirectory() as tmpdir:
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="not valid json")
            with pytest.raises(FilemetaParseError):
                adapter.scan_folder(tmpdir)


def test_scan_folder_nonzero_exit():
    adapter = FilemetaAdapter("filemeta")
    with tempfile.TemporaryDirectory() as tmpdir:
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stderr="Something broke")
            with pytest.raises(FilemetaExecutionError):
                adapter.scan_folder(tmpdir)


def test_is_device_path_detection():
    # KDE Connect SFTP mount
    kde_path = "/run/user/1000/5f058b98d20d4a9db6be5533fed8ed05/storage/3533-3033/Course"
    assert is_device_path(kde_path) is True
    assert FilemetaAdapter.is_device_path(kde_path) is True

    # Android MTP GVfs mount
    mtp_path = "/run/user/1000/gvfs/mtp:host=Xiaomi_Redmi/Internal storage/Books"
    assert is_device_path(mtp_path) is True

    # Removable USB media
    usb_path = "/media/redsheronin/USB_DRIVE/Lectures"
    assert is_device_path(usb_path) is True

    run_media_path = "/run/media/redsheronin/PORTABLE/Music"
    assert is_device_path(run_media_path) is True

    # URI schemes
    assert is_device_path("kdeconnect://device_id/folder") is True
    assert is_device_path("mtp://device_id/folder") is True

    # Standard local filesystem paths
    assert is_device_path("/home/redsheronin/todo") is False
    assert is_device_path("/tmp/local_dir") is False


def test_get_timeout_for_path():
    adapter = FilemetaAdapter("filemeta", timeout=45, device_timeout=240)
    kde_path = "/run/user/1000/5f058b98d20d4a9db6be5533fed8ed05/storage/Videos"
    local_path = "/home/redsheronin/Course"

    assert adapter.get_timeout_for_path(kde_path) == 240
    assert adapter.get_timeout_for_path(local_path) == 45


def test_scan_folder_timeout_raises_filemeta_timeout_error():
    adapter = FilemetaAdapter("filemeta", timeout=60, device_timeout=300)
    with tempfile.TemporaryDirectory() as tmpdir:
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(
                cmd=["filemeta", "-j", str(tmpdir)],
                timeout=60,
            )
            with pytest.raises(FilemetaTimeoutError) as exc_info:
                adapter.scan_folder(tmpdir)

            exc = exc_info.value
            assert isinstance(exc, FilemetaTimeoutError)
            assert isinstance(exc, FilemetaExecutionError)  # Subclass relationship preserved
            assert exc.timeout == 60
            assert str(tmpdir) in exc.path
            assert "timed out after 60s" in str(exc)
            assert "slow or unresponsive" in str(exc)


def test_scan_folder_uses_device_timeout():
    adapter = FilemetaAdapter("filemeta", timeout=60, device_timeout=300)
    with tempfile.TemporaryDirectory() as tmpdir:
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout='{"name": "DeviceCourse", "children": []}',
            )
            # Patch is_device_path to report True for this tmpdir
            with patch.object(adapter, "is_device_path", return_value=True):
                result = adapter.scan_folder(tmpdir)
                assert result.source_name == "DeviceCourse"
                mock_run.assert_called_once()
                # Verify that the effective timeout passed to subprocess.run is device_timeout (300)
                call_kwargs = mock_run.call_args[1]
                assert call_kwargs["timeout"] == 300


def test_scan_folder_explicit_timeout_override():
    adapter = FilemetaAdapter("filemeta", timeout=60, device_timeout=300)
    with tempfile.TemporaryDirectory() as tmpdir:
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout='{"name": "Course", "children": []}',
            )
            adapter.scan_folder(tmpdir, timeout=120)
            call_kwargs = mock_run.call_args[1]
            assert call_kwargs["timeout"] == 120


def test_scan_folder_with_custom_runner():
    custom_runner = MagicMock()
    custom_runner.return_value = MagicMock(
        returncode=0,
        stdout='{"name": "CourseRunner", "children": []}',
    )
    adapter = FilemetaAdapter("filemeta", command_runner=custom_runner)
    with tempfile.TemporaryDirectory() as tmpdir:
        result = adapter.scan_folder(tmpdir)
        assert result.source_name == "CourseRunner"
        custom_runner.assert_called_once()


def test_scan_folder_device_disconnected_error():
    adapter = FilemetaAdapter("filemeta")
    fake_kde_path = "/run/user/1000/5f058b98d20d4a9db6be5533fed8ed05/nonexistent_folder_abc"

    with pytest.raises(FolderNotFoundError) as exc_info:
        adapter.scan_folder(fake_kde_path)
    assert "device is disconnected" in str(exc_info.value)

    # Disconnected in the middle of scan (FUSE error)
    with tempfile.TemporaryDirectory() as tmpdir:
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=1,
                stderr="fuse: Transport endpoint is not connected",
            )
            with pytest.raises(FilemetaExecutionError) as exc_info2:
                adapter.scan_folder(tmpdir)
            assert "device may have disconnected or unmounted" in str(exc_info2.value)


def test_config_filemeta_timeout_defaults_and_env():
    # Defaults
    cfg = Config(db_path=Path("/tmp/test.db"))
    assert cfg.filemeta_timeout == 60
    assert cfg.filemeta_device_timeout == 300

    # Environment overrides
    with patch.dict(os.environ, {"FILEMETA_TIMEOUT": "90", "FILEMETA_DEVICE_TIMEOUT": "450"}):
        loaded_cfg = Config.load()
        assert loaded_cfg.filemeta_timeout == 90
        assert loaded_cfg.filemeta_device_timeout == 450

