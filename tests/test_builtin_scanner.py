"""Unit tests for the native built-in pure-Python directory scanner and tinytag integration."""

import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from todo.integrations.filemeta import (
    FilemetaAdapter,
    FolderNotFoundError,
    InvalidFolderError,
    PermissionDeniedError,
    clean_file_title,
    determine_file_type,
    natural_sort_key,
    scan_folder_builtin,
)


def test_natural_sort_key():
    items = [
        "Lesson 10.mp4",
        "Lesson 2.mp4",
        "Lesson 1.mp4",
        "Module 12",
        "Module 2",
        "Module 1",
    ]
    sorted_items = sorted(items, key=natural_sort_key)
    assert sorted_items == [
        "Lesson 1.mp4",
        "Lesson 2.mp4",
        "Lesson 10.mp4",
        "Module 1",
        "Module 2",
        "Module 12",
    ]


def test_scan_folder_builtin_hierarchy():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir) / "Full Course"
        root.mkdir()

        # Hidden directory should be ignored
        (root / ".git").mkdir()
        (root / ".git" / "HEAD").write_text("ref: refs/heads/master")

        # Hidden file should be ignored
        (root / ".DS_Store").write_text("meta")

        # Top-level lesson
        (root / "00 - Overview.mp4").write_text("video-data")

        # Module 1 with lessons and resources
        m1 = root / "Module 1 - Basics"
        m1.mkdir()
        (m1 / "01 - Intro.mp4").write_text("video-data")
        (m1 / "02 - Getting Started.mp4").write_text("video-data")
        (m1 / "Cheatsheet.pdf").write_text("doc-data")

        # Subdirectory inside Module 1
        m1_res = m1 / "Extra Resources"
        m1_res.mkdir()
        (m1_res / "Deep Dive.mp3").write_text("audio-data")

        # Module 2
        m2 = root / "Module 2 - Advanced"
        m2.mkdir()
        (m2 / "01 - Advanced Patterns.mkv").write_text("video-data")

        res = scan_folder_builtin(root)

        assert res.source_name == "Full Course"
        assert res.scan_status == "completed"

        # Check folders
        folder_names = [f.name for f in res.folders]
        assert "Module 1 - Basics" in folder_names
        assert "Extra Resources" in folder_names
        assert "Module 2 - Advanced" in folder_names
        assert ".git" not in folder_names

        # Check subfolder parent_relative_path
        extra_res_folder = next(f for f in res.folders if f.name == "Extra Resources")
        assert extra_res_folder.parent_relative_path == "Module 1 - Basics"
        assert extra_res_folder.relative_path == "Module 1 - Basics/Extra Resources"

        # Check files
        file_titles = [i.title for i in res.items]
        assert "00 - Overview" in file_titles
        assert "01 - Intro" in file_titles
        assert "02 - Getting Started" in file_titles
        assert "Cheatsheet" in file_titles
        assert "Deep Dive" in file_titles
        assert "01 - Advanced Patterns" in file_titles
        assert ".DS_Store" not in [i.name for i in res.items]

        # Check types
        cheatsheet = next(i for i in res.items if i.title == "Cheatsheet")
        assert cheatsheet.type == "document"

        deep_dive = next(i for i in res.items if i.title == "Deep Dive")
        assert deep_dive.type == "audio"
        assert deep_dive.parent_relative_path == "Module 1 - Basics/Extra Resources"


def test_scan_folder_builtin_tinytag_duration():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir) / "Media Course"
        root.mkdir()
        video_file = root / "01 - Lesson.mp4"
        video_file.write_bytes(b"\x00\x00\x00\x20ftypisom")

        mock_tag = MagicMock()
        mock_tag.duration = 1845.5

        with patch("tinytag.TinyTag.get", return_value=mock_tag):
            res = scan_folder_builtin(root)
            assert len(res.items) == 1
            item = res.items[0]
            assert item.title == "01 - Lesson"
            assert item.type == "video"
            assert item.metadata.get("duration") == 1845.5


def test_scan_folder_builtin_tinytag_unreadable_fallback():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir) / "Corrupted Course"
        root.mkdir()
        video_file = root / "Corrupted.mp4"
        video_file.write_bytes(b"corrupted")

        # TinyTag raises an exception on unreadable or unsupported media
        with patch("tinytag.TinyTag.get", side_effect=Exception("Unreadable")):
            res = scan_folder_builtin(root)
            assert len(res.items) == 1
            item = res.items[0]
            assert item.title == "Corrupted"
            # Gracefully defaults to no duration / 0 without crashing
            assert "duration" not in item.metadata or item.metadata.get("duration") == 0


def test_scan_folder_builtin_errors():
    adapter = FilemetaAdapter(allow_builtin_fallback=True)

    with pytest.raises(FolderNotFoundError):
        adapter.scan_folder("/non/existent/path/for/extern/test")

    with tempfile.NamedTemporaryFile() as tmpf:
        with pytest.raises(InvalidFolderError):
            adapter.scan_folder(tmpf.name)
