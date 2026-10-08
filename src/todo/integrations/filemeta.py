"""Filemeta integration adapter for scanning folders and extracting metadata."""

import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


class FilemetaError(Exception):
    """Base exception for filemeta adapter errors."""
    pass


class FilemetaNotFoundError(FilemetaError):
    """Raised when the filemeta executable is not found on the system."""
    pass


class FolderNotFoundError(FilemetaError):
    """Raised when the target folder does not exist."""
    pass


class InvalidFolderError(FilemetaError):
    """Raised when the target path is not a directory."""
    pass


class PermissionDeniedError(FilemetaError):
    """Raised when access to the folder or files is denied."""
    pass


class FilemetaExecutionError(FilemetaError):
    """Raised when filemeta execution fails or exits with non-zero code."""
    pass


class FilemetaParseError(FilemetaError):
    """Raised when filemeta output cannot be parsed as JSON."""
    pass


VIDEO_EXTENSIONS = {
    ".mp4",
    ".mkv",
    ".avi",
    ".mov",
    ".webm",
    ".flv",
    ".wmv",
}

AUDIO_EXTENSIONS = {
    ".mp3",
    ".wav",
    ".flac",
    ".aac",
    ".ogg",
    ".m4a",
}

DOCUMENT_EXTENSIONS = {
    ".pdf",
    ".doc",
    ".docx",
    ".odt",
    ".txt",
    ".md",
}


def clean_file_title(filename: str) -> str:
    """
    Clean filename to produce a friendly display/task title.
    Strips the file extension without modifying the original path or filename.
    """
    path = Path(filename)
    return path.stem


def determine_file_type(filename: str) -> str:
    """Determine high-level item type based on file extension."""
    ext = Path(filename).suffix.lower()
    if ext in VIDEO_EXTENSIONS:
        return "video"
    if ext in AUDIO_EXTENSIONS:
        return "audio"
    if ext in DOCUMENT_EXTENSIONS:
        return "document"
    return "file"


@dataclass
class ScannedFile:
    name: str
    path: str
    title: str
    type: str
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class FilemetaScanResult:
    source_name: str
    source_path: str
    items: List[ScannedFile] = field(default_factory=list)


class FilemetaAdapter:
    """Adapter for invoking the external `filemeta` CLI and normalizing output."""

    def __init__(self, command: str = "filemeta"):
        self.command = command

    def check_availability(self) -> bool:
        """Check if the configured filemeta command is accessible in PATH."""
        return shutil.which(self.command) is not None

    def scan_folder(self, folder_path: str | Path) -> FilemetaScanResult:
        """
        Scan a folder using filemeta -j and parse the resulting metadata tree.

        Raises:
            FilemetaNotFoundError
            FolderNotFoundError
            InvalidFolderError
            PermissionDeniedError
            FilemetaExecutionError
            FilemetaParseError
        """
        if not self.check_availability():
            raise FilemetaNotFoundError(
                f"Command '{self.command}' not found. "
                "Please verify that filemeta is installed and in your PATH."
            )

        # Expand and resolve target directory
        raw_path = Path(folder_path).expanduser()
        try:
            resolved_path = raw_path.resolve()
        except Exception as e:
            raise FolderNotFoundError(f"Cannot resolve path '{folder_path}': {e}")

        if not resolved_path.exists():
            raise FolderNotFoundError(f"Folder '{folder_path}' does not exist.")

        if not resolved_path.is_dir():
            raise InvalidFolderError(f"Path '{folder_path}' is not a directory.")

        if not os.access(resolved_path, os.R_OK):
            raise PermissionDeniedError(f"Permission denied accessing folder '{folder_path}'.")

        # Run filemeta with -j (JSON) flag
        try:
            proc = subprocess.run(
                [self.command, "-j", str(resolved_path)],
                capture_output=True,
                text=True,
                timeout=60,
            )
        except subprocess.TimeoutExpired:
            raise FilemetaExecutionError(f"filemeta timed out while scanning '{folder_path}'.")
        except OSError as e:
            raise FilemetaExecutionError(f"Failed to execute '{self.command}': {e}")

        if proc.returncode != 0:
            stderr_msg = proc.stderr.strip() or f"exit code {proc.returncode}"
            raise FilemetaExecutionError(f"filemeta failed: {stderr_msg}")

        # Parse JSON output
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError as e:
            raise FilemetaParseError(f"Invalid JSON returned by filemeta: {e}")

        if not isinstance(data, dict):
            raise FilemetaParseError("Unexpected JSON output from filemeta (expected object).")

        source_name = data.get("name") or resolved_path.name
        source_path = str(resolved_path)

        # Recursively collect files from JSON tree
        items: List[ScannedFile] = []
        self._collect_files(data, items)

        return FilemetaScanResult(
            source_name=source_name,
            source_path=source_path,
            items=items,
        )

    def _collect_files(self, node: Dict[str, Any], items: List[ScannedFile]) -> None:
        """Recursively collect file items from node children."""
        node_type = node.get("type", "file")
        if node_type == "file":
            name = node.get("name", "")
            path = node.get("path", "")
            if name and path:
                title = clean_file_title(name)
                item_type = determine_file_type(name)
                # Filter out pure container fields from metadata
                meta = {
                    k: v
                    for k, v in node.items()
                    if k not in ("name", "path", "type", "children")
                }
                items.append(
                    ScannedFile(
                        name=name,
                        path=path,
                        title=title,
                        type=item_type,
                        metadata=meta,
                    )
                )

        children = node.get("children", [])
        if isinstance(children, list):
            for child in children:
                if isinstance(child, dict):
                    self._collect_files(child, items)
