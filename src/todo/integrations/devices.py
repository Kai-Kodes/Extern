"""Device discovery and management for wired storage, MTP, and KDE Connect devices."""

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple


class DeviceType(str, Enum):
    LOCAL = "local"
    WIRED_STORAGE = "wired_storage"
    WIRED_MTP = "wired_mtp"
    KDECONNECT = "kdeconnect"


@dataclass
class ConnectedDevice:
    id: str  # Unique identifier (KDE Connect device ID, block dev name, or MTP serial/ID)
    name: str  # Human-friendly display name (e.g. "SheroninRedmi", "SanDisk Ultra USB")
    device_type: DeviceType
    is_connected: bool = True
    is_mounted: bool = False
    mount_paths: List[str] = field(default_factory=list)
    storage_labels: Dict[str, str] = field(default_factory=dict)  # path -> friendly storage label
    status_detail: str = ""
    activation_root: Optional[str] = None  # URI used for mounting (e.g. mtp://...)
    device_uri: Optional[str] = None


class DeviceManager:
    """Manages discovery, mount state, and browsing for wired and KDE Connect devices."""

    def __init__(
        self,
        command_runner: Optional[Callable[..., subprocess.CompletedProcess]] = None,
    ):
        self._runner = command_runner or subprocess.run

    def check_dependencies(self) -> Dict[str, bool]:
        """Check presence of external tools used for device discovery and mounting."""
        return {
            "kdeconnect_cli": shutil.which("kdeconnect-cli") is not None,
            "gdbus": shutil.which("gdbus") is not None,
            "gio": shutil.which("gio") is not None,
            "lsblk": shutil.which("lsblk") is not None,
            "findmnt": shutil.which("findmnt") is not None,
        }

    # =========================================================================
    # KDE Connect Discovery & Mounting
    # =========================================================================

    def discover_kdeconnect_devices(self) -> List[ConnectedDevice]:
        """
        Discover paired and reachable KDE Connect devices.
        Uses `kdeconnect-cli -l` and D-Bus facilities via `gdbus`.
        """
        devices: List[ConnectedDevice] = []

        if not shutil.which("kdeconnect-cli"):
            return devices

        try:
            proc = self._runner(
                ["kdeconnect-cli", "-l"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            raw_output = proc.stdout or ""
        except Exception:
            return devices

        # Parse kdeconnect-cli -l output:
        # - SheroninRedmi: 1663dac047b6436fae66e27dfca1ed31 (paired and reachable)
        # - DeviceName: <id> (paired and unreachable)
        pattern = re.compile(
            r"^-\s+(?P<name>.+?):\s+(?P<id>[0-9a-fA-F_]+)(?:\s+\((?P<status>[^)]+)\))?$",
            re.MULTILINE,
        )

        for match in pattern.finditer(raw_output):
            dev_name = match.group("name").strip()
            dev_id = match.group("id").strip()
            status_str = (match.group("status") or "").lower()

            is_reachable = "reachable" in status_str and "unreachable" not in status_str
            is_paired = "paired" in status_str

            device = ConnectedDevice(
                id=dev_id,
                name=dev_name,
                device_type=DeviceType.KDECONNECT,
                is_connected=is_reachable,
                is_mounted=False,
                status_detail=status_str if status_str else ("reachable" if is_reachable else "offline"),
                device_uri=f"kdeconnect://{dev_id}",
            )

            # Check SFTP mount status via gdbus if reachable
            if is_reachable and shutil.which("gdbus"):
                self._enrich_kdeconnect_sftp(device)

            devices.append(device)

        return devices

    def _enrich_kdeconnect_sftp(self, device: ConnectedDevice) -> None:
        """Inspect KDE Connect SFTP plugin mount status and storage directories."""
        sftp_path = f"/modules/kdeconnect/devices/{device.id}/sftp"

        # 1. Check isMounted
        try:
            res_mount = self._runner(
                [
                    "gdbus",
                    "call",
                    "--session",
                    "--dest",
                    "org.kde.kdeconnect",
                    "--object-path",
                    sftp_path,
                    "--method",
                    "org.kde.kdeconnect.device.sftp.isMounted",
                ],
                capture_output=True,
                text=True,
                timeout=4,
            )
            is_mounted = "(true,)" in (res_mount.stdout or "")
            device.is_mounted = is_mounted
        except Exception:
            device.is_mounted = False
            return

        if not device.is_mounted:
            return

        # 2. Get getDirectories
        try:
            res_dirs = self._runner(
                [
                    "gdbus",
                    "call",
                    "--session",
                    "--dest",
                    "org.kde.kdeconnect",
                    "--object-path",
                    sftp_path,
                    "--method",
                    "org.kde.kdeconnect.device.sftp.getDirectories",
                ],
                capture_output=True,
                text=True,
                timeout=4,
            )
            raw_dirs = res_dirs.stdout or ""
            # Parse dict items: 'path': <'name'>
            matches = re.findall(r"'([^']+)':\s*<'([^']*)'>", raw_dirs)
            if matches:
                for path_str, label_str in matches:
                    device.mount_paths.append(path_str)
                    device.storage_labels[path_str] = label_str
            else:
                # Fallback to mountPoint
                res_point = self._runner(
                    [
                        "gdbus",
                        "call",
                        "--session",
                        "--dest",
                        "org.kde.kdeconnect",
                        "--object-path",
                        sftp_path,
                        "--method",
                        "org.kde.kdeconnect.device.sftp.mountPoint",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=3,
                )
                m = re.search(r"\('([^']+)',\)", res_point.stdout or "")
                if m:
                    mount_p = m.group(1)
                    device.mount_paths.append(mount_p)
                    device.storage_labels[mount_p] = "Device Storage"
        except Exception:
            pass

    def mount_kdeconnect_device(self, device_id: str) -> Tuple[bool, Optional[str]]:
        """
        Attempt to mount the SFTP filesystem for a KDE Connect device.
        Returns: (success, error_message)
        """
        if not shutil.which("gdbus"):
            return False, "Tool 'gdbus' is required to mount KDE Connect devices."

        sftp_path = f"/modules/kdeconnect/devices/{device_id}/sftp"

        try:
            proc = self._runner(
                [
                    "gdbus",
                    "call",
                    "--session",
                    "--dest",
                    "org.kde.kdeconnect",
                    "--object-path",
                    sftp_path,
                    "--method",
                    "org.kde.kdeconnect.device.sftp.mountAndWait",
                ],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if "(true,)" in (proc.stdout or ""):
                return True, None

            # Check getMountError if mount failed
            err_proc = self._runner(
                [
                    "gdbus",
                    "call",
                    "--session",
                    "--dest",
                    "org.kde.kdeconnect",
                    "--object-path",
                    sftp_path,
                    "--method",
                    "org.kde.kdeconnect.device.sftp.getMountError",
                ],
                capture_output=True,
                text=True,
                timeout=3,
            )
            m = re.search(r"\('([^']*)',\)", err_proc.stdout or "")
            err_msg = m.group(1) if m else ""
            if not err_msg:
                err_msg = "Device is unreachable or storage permission was denied on the device."
            return False, err_msg
        except subprocess.TimeoutExpired:
            return False, "KDE Connect mount request timed out."
        except Exception as e:
            return False, str(e)

    # =========================================================================
    # Wired Devices Discovery (Removable Storage & MTP)
    # =========================================================================

    def discover_wired_devices(self) -> List[ConnectedDevice]:
        """
        Discover accessible mounted and unmounted removable storage and Android MTP devices.
        Uses `lsblk` and `gio mount -l -i`.
        """
        devices: List[ConnectedDevice] = []

        # 1. Discover removable block devices via lsblk
        devices.extend(self._discover_removable_block_devices())

        # 2. Discover MTP devices via gio
        devices.extend(self._discover_mtp_devices())

        return devices

    def _discover_removable_block_devices(self) -> List[ConnectedDevice]:
        """Discover removable external USB drives, flash drives, and external SSDs."""
        devices: List[ConnectedDevice] = []
        if not shutil.which("lsblk"):
            return devices

        try:
            proc = self._runner(
                [
                    "lsblk",
                    "-J",
                    "-o",
                    "NAME,MODEL,RM,SIZE,TYPE,MOUNTPOINTS,HOTPLUG,LABEL",
                ],
                capture_output=True,
                text=True,
                timeout=5,
            )
            data = json.loads(proc.stdout)
        except Exception:
            return devices

        block_devs = data.get("blockdevices", [])

        def process_node(node: dict, parent_model: Optional[str] = None):
            is_removable = bool(node.get("rm") or node.get("hotplug"))
            model = node.get("model") or parent_model or ""
            label = node.get("label") or ""
            name = node.get("name") or ""
            mountpoints = [
                m for m in (node.get("mountpoints") or []) if m and m != "[SWAP]"
            ]

            # Also check if mounted under /media or /run/media
            media_mount = any(
                m.startswith("/media/") or m.startswith("/run/media/")
                for m in mountpoints
            )

            if is_removable or media_mount:
                display_name = label or model or name
                size_str = node.get("size", "")
                if size_str:
                    display_name += f" ({size_str})"

                is_mounted = len(mountpoints) > 0
                status = (
                    f"Mounted at {mountpoints[0]}"
                    if is_mounted
                    else "Unmounted"
                )

                storage_labels = {m: label or f"Drive {name}" for m in mountpoints}

                dev = ConnectedDevice(
                    id=name,
                    name=display_name,
                    device_type=DeviceType.WIRED_STORAGE,
                    is_connected=True,
                    is_mounted=is_mounted,
                    mount_paths=mountpoints,
                    storage_labels=storage_labels,
                    status_detail=status,
                    device_uri=f"file://{mountpoints[0]}" if is_mounted else None,
                )
                devices.append(dev)

            # Process children (partitions)
            for child in node.get("children", []):
                process_node(child, parent_model=model)

        for root_node in block_devs:
            process_node(root_node)

        # De-duplicate: if parent and child are added with identical mount paths, keep partitions
        seen_paths = set()
        deduped = []
        for d in devices:
            key = tuple(sorted(d.mount_paths)) if d.mount_paths else (d.id,)
            if key not in seen_paths:
                seen_paths.add(key)
                deduped.append(d)

        return deduped

    def _discover_mtp_devices(self) -> List[ConnectedDevice]:
        """Discover Android devices connected via MTP using GVfs / gio."""
        devices: List[ConnectedDevice] = []
        if not shutil.which("gio"):
            return devices

        try:
            proc = self._runner(
                ["gio", "mount", "-l", "-i"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            raw_output = proc.stdout or ""
        except Exception:
            return devices

        # Parse Volume / Mount blocks in gio output
        lines = raw_output.splitlines()
        current_block: Dict[str, str] = {}
        blocks: List[Dict[str, str]] = []

        for line in lines:
            line_str = line.strip()
            if line_str.startswith("Volume(") or line_str.startswith("Mount("):
                if current_block:
                    blocks.append(current_block)
                current_block = {"title": line_str}
            elif current_block:
                if line_str.startswith("Type:"):
                    current_block["type"] = line_str.split(":", 1)[1].strip()
                elif line_str.startswith("activation_root="):
                    current_block["activation_root"] = line_str.split("=", 1)[1].strip()
                elif line_str.startswith("default_location="):
                    current_block["default_location"] = line_str.split("=", 1)[1].strip()
                elif "unix-device:" in line_str:
                    current_block["unix_device"] = line_str.split(":", 1)[1].strip().strip("'")

        if current_block:
            blocks.append(current_block)

        # Look for GVfs mount directory for MTP
        uid = os.getuid()
        gvfs_dir = Path(f"/run/user/{uid}/gvfs")
        mounted_mtp_dirs: List[Path] = []
        if gvfs_dir.exists():
            try:
                mounted_mtp_dirs = [
                    p for p in gvfs_dir.iterdir() if p.name.startswith("mtp:")
                ]
            except Exception:
                pass

        for b in blocks:
            b_type = b.get("type", "")
            title = b.get("title", "")
            if "MTP" in b_type or "mtp://" in b.get("activation_root", "") or "mtp://" in b.get("default_location", ""):
                # Extract clean name
                clean_name = title
                m = re.search(r"(?:Volume|Mount)\(\d+\):\s*(.+)", title)
                if m:
                    clean_name = m.group(1).split("->")[0].strip()

                act_root = b.get("activation_root") or b.get("default_location")
                dev_id = b.get("unix_device") or clean_name

                # Check if mounted
                is_mounted = False
                mount_paths = []
                storage_labels = {}

                for m_dir in mounted_mtp_dirs:
                    is_mounted = True
                    m_path_str = str(m_dir)
                    mount_paths.append(m_path_str)
                    storage_labels[m_path_str] = clean_name
                    # Look inside for storage directories (Internal storage, SD card)
                    try:
                        for sub in m_dir.iterdir():
                            if sub.is_dir():
                                sub_str = str(sub)
                                mount_paths.append(sub_str)
                                storage_labels[sub_str] = sub.name
                    except Exception:
                        pass

                dev = ConnectedDevice(
                    id=dev_id,
                    name=f"{clean_name} (MTP)",
                    device_type=DeviceType.WIRED_MTP,
                    is_connected=True,
                    is_mounted=is_mounted,
                    mount_paths=mount_paths,
                    storage_labels=storage_labels,
                    status_detail="Mounted" if is_mounted else "Connected (Not Mounted)",
                    activation_root=act_root,
                    device_uri=act_root,
                )
                devices.append(dev)

        return devices

    def mount_mtp_device(
        self, activation_root: str
    ) -> Tuple[bool, Optional[str], Optional[str]]:
        """
        Attempt to mount an MTP device using gio mount.
        Returns: (success, mount_path, error_message)
        """
        if not shutil.which("gio"):
            return (
                False,
                None,
                "The 'gio' tool is required for MTP support on Debian. Please install gvfs-backends.",
            )

        try:
            proc = self._runner(
                ["gio", "mount", activation_root],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if proc.returncode == 0:
                # Find the mount path in /run/user/<uid>/gvfs/
                uid = os.getuid()
                gvfs_dir = Path(f"/run/user/{uid}/gvfs")
                if gvfs_dir.exists():
                    for p in gvfs_dir.iterdir():
                        if p.name.startswith("mtp:"):
                            return True, str(p), None
                return True, None, None

            stderr_msg = proc.stderr.strip()
            if "Unable to open MTP device" in stderr_msg:
                error_explanation = (
                    "Unable to open MTP device. Ensure the Android device is unlocked, "
                    "and USB mode is set to 'File Transfer / MTP' (not charging only)."
                )
            else:
                error_explanation = f"MTP mount failed: {stderr_msg}"
            return False, None, error_explanation
        except subprocess.TimeoutExpired:
            return (
                False,
                None,
                "MTP mount timed out. Please check your phone connection and USB mode.",
            )
        except Exception as e:
            return False, None, str(e)

    # =========================================================================
    # Path & Source Accessibility and Directory Listing
    # =========================================================================

    def is_source_accessible(
        self, source_path: str, attempt_reconnect: bool = True
    ) -> bool:
        """
        Check if the path of an imported source is currently reachable and readable.
        If attempt_reconnect is True and it belongs to a known device that is currently
        unmounted but reachable, attempts to reconnect/mount it.
        """
        path_obj = Path(source_path)
        try:
            if path_obj.exists() and os.access(path_obj, os.R_OK):
                return True
        except Exception:
            pass

        if not attempt_reconnect:
            return False

        # If not directly accessible and reconnection requested, check if it's a KDE Connect path
        # KDE Connect paths: /run/user/<uid>/<hex_device_id>/...
        m_kde = re.match(r"^/run/user/\d+/([0-9a-fA-F_]+)/", source_path)
        if m_kde:
            device_id = m_kde.group(1)
            # Check if this device is reachable
            kde_devs = self.discover_kdeconnect_devices()
            for dev in kde_devs:
                if dev.id == device_id and dev.is_connected:
                    ok, _ = self.mount_kdeconnect_device(device_id)
                    if ok:
                        try:
                            return path_obj.exists() and os.access(path_obj, os.R_OK)
                        except Exception:
                            pass

        return False

    def resolve_canonical_uri(self, canonical_uri: str) -> Tuple[bool, Optional[str]]:
        """
        Re-resolve a canonical URI (kdeconnect://, mtp://, media://, file://)
        to a physical filesystem path, mounting the device if reachable.
        """
        if not canonical_uri:
            return False, None

        if canonical_uri.startswith("kdeconnect://"):
            rest = canonical_uri[len("kdeconnect://"):]
            parts = rest.split("/", 1)
            device_id = parts[0]
            subpath = f"/{parts[1]}" if len(parts) > 1 else ""

            # Check/mount device
            ok, _ = self.mount_kdeconnect_device(device_id)
            uid = os.getuid()
            base_dir = Path(f"/run/user/{uid}/{device_id}")
            if base_dir.exists():
                resolved = str(base_dir) + subpath
                if Path(resolved).exists():
                    return True, resolved
            return False, None

        if canonical_uri.startswith("mtp://"):
            rest = canonical_uri[len("mtp://"):]
            parts = rest.split("/", 1)
            host = parts[0]
            subpath = f"/{parts[1]}" if len(parts) > 1 else ""
            uid = os.getuid()
            gvfs_dir = Path(f"/run/user/{uid}/gvfs")
            if gvfs_dir.exists():
                for p in gvfs_dir.iterdir():
                    if f"mtp:host={host}" in p.name:
                        resolved = str(p) + subpath
                        if Path(resolved).exists():
                            return True, resolved
            return False, None

        if canonical_uri.startswith("media://"):
            rest = canonical_uri[len("media://"):]
            parts = rest.split("/", 1)
            label = parts[0]
            subpath = f"/{parts[1]}" if len(parts) > 1 else ""
            user = os.environ.get("USER", "")
            candidates = [
                Path(f"/run/media/{user}/{label}") if user else None,
                Path(f"/media/{user}/{label}") if user else None,
                Path(f"/media/{label}"),
            ]
            for cand in candidates:
                if cand and cand.exists():
                    resolved = str(cand) + subpath
                    if Path(resolved).exists():
                        return True, resolved
            return False, None

        if canonical_uri.startswith("file://"):
            p_str = canonical_uri[len("file://"):]
            if Path(p_str).exists():
                return True, p_str
            return False, None

        return False, None

    def resolve_and_mount_path(
        self, raw_path: str
    ) -> Tuple[bool, Optional[str], Optional[str]]:
        """
        Resolve an entered folder location (local path, removable drive, MTP, or KDE Connect).
        If the path refers to a recognized device that is currently unmounted but reachable,
        attempts mounting and verifies existence.

        Returns:
            (success: bool, resolved_path: Optional[str], error_message: Optional[str])
        """
        if not raw_path:
            return False, None, "Folder path cannot be empty."

        clean_path = raw_path.strip().strip("'\"")
        if not clean_path:
            return False, None, "Folder path cannot be empty."

        # ---------------------------------------------------------------------
        # 1. Check KDE Connect URI scheme: kdeconnect://<device_id_or_name>/<subpath>
        # ---------------------------------------------------------------------
        if clean_path.startswith("kdeconnect://"):
            remainder = clean_path[len("kdeconnect://") :]
            parts = remainder.split("/", 1)
            target_dev = parts[0]
            subpath = "/" + parts[1] if len(parts) > 1 else ""

            kde_devs = self.discover_kdeconnect_devices()
            matched_dev = None
            for d in kde_devs:
                if d.id.lower() == target_dev.lower() or d.name.lower() == target_dev.lower():
                    matched_dev = d
                    break

            if not matched_dev:
                return (
                    False,
                    None,
                    f"KDE Connect device '{target_dev}' not found. Ensure device is paired.",
                )

            if not matched_dev.is_connected:
                return (
                    False,
                    None,
                    f"KDE Connect device '{matched_dev.name}' is offline or not reachable.",
                )

            if not matched_dev.is_mounted:
                ok, err = self.mount_kdeconnect_device(matched_dev.id)
                if not ok:
                    return False, None, f"Could not mount KDE Connect device '{matched_dev.name}': {err}"

            uid = os.getuid()
            candidate = Path(f"/run/user/{uid}/{matched_dev.id}{subpath}")
            if not candidate.exists():
                return (
                    False,
                    None,
                    f"Directory '{subpath or '/'}' does not exist on KDE Connect device '{matched_dev.name}'.",
                )
            if not candidate.is_dir():
                return (
                    False,
                    None,
                    f"Path '{candidate}' is a file, not a directory.",
                )
            if not os.access(candidate, os.R_OK):
                return (
                    False,
                    None,
                    f"Permission denied accessing '{candidate}'. Ensure storage permissions are granted on '{matched_dev.name}'.",
                )
            return True, str(candidate.resolve()), None

        # ---------------------------------------------------------------------
        # 2. Check MTP URI scheme: mtp://<host_or_name>/<subpath>
        # ---------------------------------------------------------------------
        if clean_path.startswith("mtp://"):
            remainder = clean_path[len("mtp://") :]
            parts = remainder.split("/", 1)
            target_dev = parts[0]
            subpath = "/" + parts[1] if len(parts) > 1 else ""

            mtp_devs = self._discover_mtp_devices()
            if not mtp_devs:
                return (
                    False,
                    None,
                    "No Android MTP devices connected. Please connect the device via USB.",
                )

            matched_dev = None
            for d in mtp_devs:
                if (
                    target_dev.lower() in d.name.lower()
                    or target_dev.lower() in d.id.lower()
                    or (d.activation_root and target_dev in d.activation_root)
                ):
                    matched_dev = d
                    break
            if not matched_dev:
                if len(mtp_devs) == 1:
                    matched_dev = mtp_devs[0]
                else:
                    return (
                        False,
                        None,
                        f"MTP device '{target_dev}' not found among connected devices.",
                    )

            if not matched_dev.is_mounted and matched_dev.activation_root:
                ok, m_path, err = self.mount_mtp_device(matched_dev.activation_root)
                if not ok:
                    return False, None, err
            elif matched_dev.is_mounted and matched_dev.mount_paths:
                m_path = matched_dev.mount_paths[0]
            else:
                uid = os.getuid()
                gvfs_dir = Path(f"/run/user/{uid}/gvfs")
                mtp_dirs = (
                    [p for p in gvfs_dir.iterdir() if p.name.startswith("mtp:")]
                    if gvfs_dir.exists()
                    else []
                )
                m_path = str(mtp_dirs[0]) if mtp_dirs else None

            if not m_path:
                return (
                    False,
                    None,
                    "MTP device is connected but could not be mounted in GVfs.",
                )

            candidate = Path(f"{m_path}{subpath}")
            if not candidate.exists():
                return (
                    False,
                    None,
                    f"Directory '{subpath or '/'}' does not exist on MTP device.",
                )
            if not candidate.is_dir():
                return (
                    False,
                    None,
                    f"Path '{candidate}' is a file, not a directory.",
                )
            return True, str(candidate.resolve()), None

        # ---------------------------------------------------------------------
        # 3. Standard filesystem path
        # ---------------------------------------------------------------------
        expanded = Path(clean_path).expanduser()

        # If already exists and accessible:
        if expanded.exists():
            if not expanded.is_dir():
                return False, None, f"Path '{clean_path}' is a file, not a directory."
            if not os.access(expanded, os.R_OK):
                return (
                    False,
                    None,
                    f"Permission denied accessing '{clean_path}'. Check filesystem permissions.",
                )
            return True, str(expanded.resolve()), None

        # If it does NOT exist, determine if it belongs to a recognized device namespace:
        # Case A: KDE Connect FUSE mount path (/run/user/<uid>/<hex_id>/...)
        m_kde = re.match(r"^/run/user/\d+/([0-9a-fA-F_]+)(/.*)?$", str(expanded))
        if m_kde:
            dev_id = m_kde.group(1)
            kde_devs = self.discover_kdeconnect_devices()
            matched_dev = next((d for d in kde_devs if d.id == dev_id), None)
            if not matched_dev:
                return (
                    False,
                    None,
                    f"KDE Connect device ({dev_id[:8]}...) is disconnected or not paired.",
                )
            if not matched_dev.is_connected:
                return (
                    False,
                    None,
                    f"KDE Connect device '{matched_dev.name}' is offline or unreachable.",
                )

            ok, err = self.mount_kdeconnect_device(dev_id)
            if not ok:
                return (
                    False,
                    None,
                    f"Could not mount KDE Connect device '{matched_dev.name}': {err}",
                )

            if expanded.exists():
                if not expanded.is_dir():
                    return False, None, f"Path '{clean_path}' is a file, not a directory."
                if not os.access(expanded, os.R_OK):
                    return (
                        False,
                        None,
                        f"Permission denied accessing '{clean_path}'. Ensure storage permissions are granted on '{matched_dev.name}'.",
                    )
                return True, str(expanded.resolve()), None
            else:
                return (
                    False,
                    None,
                    f"Device '{matched_dev.name}' is connected, but folder '{clean_path}' does not exist on the device.",
                )

        # Case B: GVfs MTP path (/run/user/<uid>/gvfs/mtp:host=...)
        m_mtp = re.match(r"^/run/user/\d+/gvfs/mtp:(.*)$", str(expanded))
        if m_mtp:
            mtp_devs = self._discover_mtp_devices()
            if not mtp_devs:
                return (
                    False,
                    None,
                    "Android MTP device is disconnected. Please connect the device via USB.",
                )
            for dev in mtp_devs:
                if not dev.is_mounted and dev.activation_root:
                    self.mount_mtp_device(dev.activation_root)

            if expanded.exists():
                if not expanded.is_dir():
                    return False, None, f"Path '{clean_path}' is a file, not a directory."
                return True, str(expanded.resolve()), None
            else:
                return (
                    False,
                    None,
                    f"MTP device is connected, but folder '{clean_path}' does not exist on the device.",
                )

        # Case C: Removable drive path (/media/<user>/<label>/... or /run/media/<user>/<label>/...)
        m_media = re.match(r"^/(?:run/)?media/[^/]+/([^/]+)(/.*)?$", str(expanded))
        if m_media:
            label = m_media.group(1)
            removable_devs = self._discover_removable_block_devices()
            matched_dev = next(
                (d for d in removable_devs if label.lower() in d.name.lower() or label in d.id),
                None,
            )
            if matched_dev:
                if not matched_dev.is_mounted:
                    return (
                        False,
                        None,
                        f"Removable drive '{label}' is not mounted. Please mount the drive via your file manager or 'udisksctl mount' before importing.",
                    )
            return (
                False,
                None,
                f"Removable drive '{label}' is disconnected or not found.",
            )

        # Default fallback for regular nonexistent local path
        return False, None, f"Folder '{clean_path}' does not exist."

    def list_directories(self, directory_path: str) -> List[Tuple[str, str]]:
        """
        List immediate subdirectories in the specified path.
        Returns a sorted list of (subfolder_name, full_path).
        """
        entries: List[Tuple[str, str]] = []
        path_obj = Path(directory_path)

        if not path_obj.exists():
            raise FileNotFoundError(f"Directory '{directory_path}' does not exist.")

        if not path_obj.is_dir():
            raise NotADirectoryError(f"Path '{directory_path}' is not a directory.")

        with os.scandir(directory_path) as it:
            for entry in it:
                try:
                    if entry.is_dir():
                        entries.append((entry.name, entry.path))
                except OSError:
                    # Skip unreadable or permission-denied entries
                    continue

        entries.sort(key=lambda x: x[0].lower())
        return entries
