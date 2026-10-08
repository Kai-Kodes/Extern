"""Tests for the FilemetaAdapter integration."""

import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from todo.integrations.filemeta import (
    FilemetaAdapter,
    FilemetaError,
    FilemetaExecutionError,
    FilemetaNotFoundError,
    FilemetaParseError,
    FolderNotFoundError,
    InvalidFolderError,
    clean_file_title,
    determine_file_type,
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
