"""Filemeta integration adapter for scanning folders and extracting metadata."""

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional


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


class FilemetaTimeoutError(FilemetaExecutionError):
    """Raised when filemeta execution times out."""

    def __init__(self, message: str, timeout: int, path: str):
        super().__init__(message)
        self.timeout = timeout
        self.path = path


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


def is_device_path(folder_path: str | Path) -> bool:
    """
    Determine whether a given folder path is backed by an external or network device
    (e.g., KDE Connect SFTP, Android MTP/GVfs mounts, removable USB/SD drives, or FUSE mounts).
    """
    str_path = str(folder_path)

    # KDE Connect SFTP mount path: /run/user/<uid>/<hex_id>/...
    if re.match(r"^/run/user/\d+/[0-9a-fA-F_]+(/.*)?$", str_path):
        return True

    # Android MTP / GVfs mounts: /run/user/<uid>/gvfs(/.*)?
    if re.match(r"^/run/user/\d+/gvfs(/.*)?$", str_path):
        return True

    # Removable media mounts (/media/<user>/..., /run/media/<user>/...)
    if re.match(r"^/(?:run/)?media/[^/]+(/.*)?$", str_path):
        return True

    # URI schemes
    if str_path.startswith(("kdeconnect://", "mtp://")):
        return True

    # Inspect /proc/mounts on Linux for slow/network/FUSE filesystem types
    try:
        if os.path.exists("/proc/mounts"):
            resolved = str(Path(folder_path).expanduser().resolve())
            with open("/proc/mounts", "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    parts = line.strip().split()
                    if len(parts) >= 3:
                        mount_point = parts[1]
                        fstype = parts[2].lower()
                        if resolved == mount_point or resolved.startswith(mount_point.rstrip("/") + "/"):
                            if fstype in (
                                "fuse.sshfs",
                                "fuse.gvfsd-fuse",
                                "fuse",
                                "cifs",
                                "smbfs",
                                "nfs",
                                "nfs4",
                                "sshfs",
                                "vfat",
                                "exfat",
                                "ntfs",
                            ):
                                return True
    except Exception:
        pass

    return False


def compute_canonical_uri(folder_path: str | Path) -> str:
    """Compute a stable canonical URI for a folder path to survive mount changes across sessions."""
    str_path = str(folder_path)

    # KDE Connect SFTP: /run/user/<uid>/<hex_id>(/.*)?
    m_kde = re.match(r"^/run/user/\d+/([0-9a-fA-F_]+)(/.*)?$", str_path)
    if m_kde:
        dev_id = m_kde.group(1)
        subpath = m_kde.group(2) or ""
        return f"kdeconnect://{dev_id}{subpath}"

    # GVfs MTP: /run/user/<uid>/gvfs/mtp:host=([^/]+)(/.*)?
    m_mtp = re.match(r"^/run/user/\d+/gvfs/mtp:host=([^/]+)(/.*)?$", str_path)
    if m_mtp:
        host = m_mtp.group(1)
        subpath = m_mtp.group(2) or ""
        return f"mtp://{host}{subpath}"

    # Removable media: /(run/)?media/<user>/<label>(/.*)?
    m_media = re.match(r"^/(?:run/)?media/[^/]+/([^/]+)(/.*)?$", str_path)
    if m_media:
        label = m_media.group(1)
        subpath = m_media.group(2) or ""
        return f"media://{label}{subpath}"

    if str_path.startswith(("kdeconnect://", "mtp://", "media://")):
        return str_path

    return f"file://{Path(folder_path).expanduser().resolve()}"


@dataclass
class ScannedFolder:
    name: str
    path: str
    relative_path: str
    parent_relative_path: Optional[str] = None


@dataclass
class ScannedFile:
    name: str
    path: str
    title: str
    type: str
    relative_path: str = ""
    parent_relative_path: Optional[str] = None
    size: int = 0
    extension: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class FilemetaScanResult:
    source_name: str
    source_path: str
    canonical_uri: Optional[str] = None
    folders: List[ScannedFolder] = field(default_factory=list)
    items: List[ScannedFile] = field(default_factory=list)
    scan_status: str = "completed"


class FilemetaAdapter:
    """Adapter for invoking the external `filemeta` CLI and normalizing output."""

    def __init__(
        self,
        command: str = "filemeta",
        timeout: int = 60,
        device_timeout: int = 300,
        command_runner: Optional[Callable[..., subprocess.CompletedProcess]] = None,
    ):
        self.command = command
        self.default_timeout = timeout
        self.device_timeout = device_timeout
        self._runner = command_runner

    @staticmethod
    def is_device_path(folder_path: str | Path) -> bool:
        """Check if path is backed by an external device or network/FUSE mount."""
        return is_device_path(folder_path)

    def get_timeout_for_path(self, folder_path: str | Path) -> int:
        """Get the appropriate timeout for the given path based on device detection."""
        if self.is_device_path(folder_path):
            return self.device_timeout
        return self.default_timeout

    def check_availability(self) -> bool:
        """Check if the configured filemeta command is accessible in PATH."""
        return shutil.which(self.command) is not None

    def scan_folder(
        self,
        folder_path: str | Path,
        timeout: Optional[int] = None,
    ) -> FilemetaScanResult:
        """
        Scan a folder using filemeta -j and parse the resulting metadata tree.

        Raises:
            FilemetaNotFoundError
            FolderNotFoundError
            InvalidFolderError
            PermissionDeniedError
            FilemetaTimeoutError
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
        except OSError as e:
            if self.is_device_path(raw_path):
                raise FolderNotFoundError(
                    f"Cannot access device folder '{folder_path}' (device may be disconnected or unreachable): {e}"
                ) from e
            raise FolderNotFoundError(f"Cannot resolve path '{folder_path}': {e}") from e
        except Exception as e:
            raise FolderNotFoundError(f"Cannot resolve path '{folder_path}': {e}") from e

        try:
            exists = resolved_path.exists()
        except OSError as e:
            if self.is_device_path(resolved_path) or self.is_device_path(raw_path):
                raise FolderNotFoundError(
                    f"Device folder '{folder_path}' is inaccessible (device may have disconnected or unmounted): {e}"
                ) from e
            raise FolderNotFoundError(f"Folder '{folder_path}' is inaccessible: {e}") from e

        if not exists:
            if self.is_device_path(resolved_path) or self.is_device_path(raw_path):
                raise FolderNotFoundError(
                    f"Folder '{folder_path}' does not exist or device is disconnected."
                )
            raise FolderNotFoundError(f"Folder '{folder_path}' does not exist.")

        try:
            is_dir = resolved_path.is_dir()
        except OSError as e:
            raise FolderNotFoundError(f"Folder '{folder_path}' is inaccessible: {e}") from e

        if not is_dir:
            raise InvalidFolderError(f"Path '{folder_path}' is not a directory.")

        try:
            is_readable = os.access(resolved_path, os.R_OK)
        except OSError as e:
            raise PermissionDeniedError(f"Cannot access folder '{folder_path}': {e}") from e

        if not is_readable:
            raise PermissionDeniedError(f"Permission denied accessing folder '{folder_path}'.")

        # Determine scan timeout: explicit > device detection > default
        effective_timeout = (
            timeout
            if timeout is not None
            else self.get_timeout_for_path(resolved_path)
        )

        # Run filemeta with -j (JSON) flag
        runner = self._runner or subprocess.run
        try:
            proc = runner(
                [self.command, "-j", str(resolved_path)],
                capture_output=True,
                text=True,
                timeout=effective_timeout,
            )
        except subprocess.TimeoutExpired as e:
            raise FilemetaTimeoutError(
                f"filemeta timed out after {effective_timeout}s while scanning '{folder_path}'. "
                "Remote or device storage may be slow or unresponsive.",
                timeout=effective_timeout,
                path=str(folder_path),
            ) from e
        except OSError as e:
            raise FilemetaExecutionError(f"Failed to execute '{self.command}': {e}") from e

        if proc.returncode != 0:
            stderr_msg = proc.stderr.strip() or f"exit code {proc.returncode}"
            if any(
                term in stderr_msg.lower()
                for term in ("transport endpoint", "input/output error", "device", "disconnected")
            ):
                raise FilemetaExecutionError(
                    f"filemeta failed (storage device may have disconnected or unmounted): {stderr_msg}"
                )
            raise FilemetaExecutionError(f"filemeta failed: {stderr_msg}")

        # Parse JSON output
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError as e:
            raise FilemetaParseError(f"Invalid JSON returned by filemeta: {e}") from e

        if not isinstance(data, dict):
            raise FilemetaParseError("Unexpected JSON output from filemeta (expected object).")

        source_name = data.get("name") or resolved_path.name
        source_path = str(resolved_path)
        canonical_uri = compute_canonical_uri(resolved_path)

        # Recursively collect folders and files from JSON tree
        folders: List[ScannedFolder] = []
        items: List[ScannedFile] = []
        self._collect_tree(data, resolved_path, folders, items, parent_rel="")

        return FilemetaScanResult(
            source_name=source_name,
            source_path=source_path,
            canonical_uri=canonical_uri,
            folders=folders,
            items=items,
            scan_status="completed",
        )

    def _collect_tree(
        self,
        node: Dict[str, Any],
        root_path: Path,
        folders: List[ScannedFolder],
        items: List[ScannedFile],
        parent_rel: Optional[str] = "",
    ) -> None:
        """Recursively collect folders and files from JSON tree preserving hierarchy and relative paths."""
        node_type = node.get("type", "file")
        node_name = node.get("name", "")
        node_path_str = node.get("path", "")

        if not node_name:
            return

        if not node_path_str:
            sub = f"{parent_rel}/{node_name}".lstrip("/") if parent_rel else node_name
            node_path_str = str(root_path / sub)

        node_path = Path(node_path_str)
        try:
            rel_path = str(node_path.relative_to(root_path))
        except ValueError:
            rel_path = f"{parent_rel}/{node_name}".lstrip("/") if parent_rel else node_name

        if node_type == "directory":
            if rel_path and rel_path != ".":
                folders.append(
                    ScannedFolder(
                        name=node_name,
                        path=node_path_str,
                        relative_path=rel_path,
                        parent_relative_path=parent_rel or None,
                    )
                )
                current_parent_rel = rel_path
            else:
                current_parent_rel = ""

            children = node.get("children", [])
            if isinstance(children, list):
                for child in children:
                    if isinstance(child, dict):
                        self._collect_tree(
                            child,
                            root_path=root_path,
                            folders=folders,
                            items=items,
                            parent_rel=current_parent_rel,
                        )
        elif node_type == "file":
            title = clean_file_title(node_name)
            item_type = determine_file_type(node_name)
            ext = Path(node_name).suffix.lower()
            size = node.get("size", 0) or 0
            meta = {
                k: v
                for k, v in node.items()
                if k not in ("name", "path", "type", "children")
            }
            items.append(
                ScannedFile(
                    name=node_name,
                    path=node_path_str,
                    title=title,
                    type=item_type,
                    relative_path=rel_path if rel_path and rel_path != "." else node_name,
                    parent_relative_path=parent_rel or None,
                    size=size,
                    extension=ext,
                    metadata=meta,
                )
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
