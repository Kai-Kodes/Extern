"""Filemeta workflow views with centralized keyboard input, fuzzy search, multi-select, and course progress."""

from datetime import datetime
from pathlib import Path
from typing import List, Optional, Set, Tuple

from rich.table import Table
from rich.text import Text

from todo.integrations.devices import ConnectedDevice, DeviceManager, DeviceType
from todo.integrations.filemeta import FilemetaTimeoutError
from todo.models import FileItem, Source, SourceFolder
from todo.search import LiveSearchController, fuzzy_search
from todo.services.daily_service import DailyService
from todo.services.source_service import SourceService
from todo.services.task_service import TaskService
from todo.ui.console import (
    clear_viewport,
    console,
    print_status_message,
    render_footer,
    render_header,
    render_today_header,
)
from todo.ui.key_reader import Key, key_reader


class FilemetaView:
    def __init__(
        self,
        source_service: SourceService,
        task_service: TaskService,
        daily_service: DailyService,
        filemeta_adapter,
        device_manager: Optional[DeviceManager] = None,
    ):
        self.source_service = source_service
        self.task_service = task_service
        self.daily_service = daily_service
        self.filemeta_adapter = filemeta_adapter
        self.device_manager = device_manager or DeviceManager()

    def run(self) -> Optional[str]:
        """Main loop for Filemeta sources in full-screen TUI."""
        status_msg: Optional[str] = None
        status_style: str = "red"
        selected_idx = 0
        search = LiveSearchController[Source]()
        selected_source_ids: Set[int] = set()
        delete_mode = False
        accessibility_cache: dict[str, bool] = {}

        while True:
            clear_viewport()
            sources = self.source_service.get_sources(source_type="filemeta")
            subtitle = f"DELETE MODE · {len(selected_source_ids)} selected" if delete_mode else None
            render_header("FILEMETA SOURCES", subtitle=subtitle)

            valid_ids = {s.id for s in sources}
            selected_source_ids.intersection_update(valid_ids)

            if status_msg:
                print_status_message(status_msg, style=status_style)
                status_msg = None
                status_style = "red"

            if search.is_active:
                console.print(f"  Search: [bold cyan]{search.query}[/bold cyan]█\n")
            elif search.query:
                console.print(f"  Search: [bold cyan]{search.query}[/bold cyan] [dim](filter active, Esc to clear)[/dim]\n")

            active_sources = search.filter(sources)

            if not active_sources:
                if search.query:
                    console.print("  [dim italic]No matching Filemeta sources found.[/dim italic]\n")
                else:
                    console.print(
                        "  [dim italic]No Filemeta sources imported yet.[/dim italic]\n"
                    )
                if search.is_active:
                    hints = [("Enter", "Done"), ("Esc", "Cancel / Clear")]
                elif delete_mode:
                    hints = [("Esc", "Cancel")]
                else:
                    hints = [("+", "Add folder"), ("/", "Search"), ("b", "Back")]
                render_footer(hints)
            else:
                if selected_idx >= len(active_sources):
                    selected_idx = max(0, len(active_sources) - 1)

                table = Table.grid(padding=(0, 1))
                table.add_column("cursor", justify="right", style="cyan bold", width=2)
                if delete_mode:
                    table.add_column("select", justify="center", width=4)
                table.add_column("num", justify="right", style="cyan bold", width=4)
                table.add_column("name", style="bold white", width=32)
                table.add_column("progress", style="cyan")

                for idx, src in enumerate(active_sources):
                    cursor = "▶" if idx == selected_idx else " "
                    pct_str = (
                        f"{src.progress_pct}% ({src.completed_count}/{src.item_count})"
                    )
                    row_items = [cursor]
                    if delete_mode:
                        is_sel = src.id in selected_source_ids
                        chk = Text("[x]", style="bold cyan") if is_sel else Text("[ ]", style="dim")
                        row_items.append(chk)
                    row_items.extend([
                        f"{idx + 1}.",
                        src.name,
                        Text(pct_str, style="dim cyan"),
                    ])
                    table.add_row(*row_items)
                    empty_cols = ["", ""] if not delete_mode else ["", "", ""]
                    if src.path not in accessibility_cache:
                        accessibility_cache[src.path] = self.device_manager.is_source_accessible(
                            src.path, attempt_reconnect=False
                        )
                    is_accessible = accessibility_cache[src.path]
                    path_text = Text(f"   {src.path}", style="dim")
                    if not is_accessible:
                        path_text.append("  [Cached / Disconnected]", style="dim yellow")
                    table.add_row(*empty_cols, path_text, "")

                console.print(table)
                console.print()

                if delete_mode:
                    hints = [
                        ("↑/↓", "Navigate"),
                        ("Space", "Select"),
                        ("Enter", f"Confirm delete ({len(selected_source_ids)})" if selected_source_ids else "Confirm delete"),
                        ("Esc", "Cancel"),
                    ]
                elif search.is_active:
                    hints = [
                        ("↑/↓", "Navigate"),
                        ("Enter", "Done"),
                        ("Esc", "Cancel / Clear"),
                    ]
                elif search.query:
                    hints = [
                        ("↑/↓", "Navigate"),
                        ("Enter", "Open course"),
                        ("+", "Add folder"),
                        ("d", "Delete"),
                        ("/", "Search"),
                        ("Esc", "Clear search"),
                        ("b", "Back"),
                    ]
                else:
                    hints = [
                        ("↑/↓", "Navigate"),
                        ("Enter", "Open course"),
                        ("+", "Add folder"),
                        ("d", "Delete"),
                        ("/", "Search"),
                        ("b", "Back"),
                    ]
                render_footer(hints)

            k = key_reader.read_key()

            if delete_mode:
                if k in (Key.ESCAPE, "escape", "cancel"):
                    selected_source_ids.clear()
                    delete_mode = False
                    continue

                if k == Key.UP and active_sources:
                    selected_idx = max(0, selected_idx - 1)
                    continue

                if k == Key.DOWN and active_sources:
                    selected_idx = min(len(active_sources) - 1, selected_idx + 1)
                    continue

                if k in (Key.SPACE, "space", " ") and active_sources:
                    target_src = active_sources[selected_idx]
                    if target_src.id in selected_source_ids:
                        selected_source_ids.remove(target_src.id)
                    else:
                        selected_source_ids.add(target_src.id)
                    continue

                if k in (Key.ENTER, "enter"):
                    if not selected_source_ids:
                        continue
                    clear_viewport()
                    render_header("CONFIRM BULK DELETE")
                    console.print(
                        f"Delete [bold white]{len(selected_source_ids)} Filemeta sources[/bold white]?\n"
                    )
                    console.print(
                        "[yellow]This will remove the imported sources and cached metadata from Todo.[/yellow]"
                    )
                    console.print(
                        "[bold green]It will NOT delete anything from the actual filesystem.[/bold green]\n"
                    )
                    render_footer([("y", "Yes, delete"), ("n", "Cancel")])
                    k_conf = key_reader.read_key()
                    if k_conf.lower() in ("y", "yes", Key.ENTER, "enter"):
                        self.source_service.delete_sources_bulk(list(selected_source_ids))
                    selected_source_ids.clear()
                    delete_mode = False
                    continue

                # Block all other keys in delete mode
                continue

            handled, selected_idx = search.handle_key(k, len(active_sources), selected_idx)
            if handled:
                continue

            if k in (Key.ESCAPE, "escape"):
                return None

            if k in ("b", "q", "cancel"):
                return None

            if k == Key.UP and active_sources:
                selected_idx = max(0, selected_idx - 1)
                continue

            if k == Key.DOWN and active_sources:
                selected_idx = min(len(active_sources) - 1, selected_idx + 1)
                continue

            if k in ("+", Key.PLUS, "add"):
                err_msg, res_src = self.handle_add_folder()
                if res_src:
                    item_ret = self.handle_source_items(res_src)
                    if item_ret:
                        return item_ret
                elif err_msg:
                    status_msg = err_msg
                    status_style = "red"
                continue

            if k in ("d", "delete") and active_sources:
                delete_mode = True
                selected_source_ids.clear()
                continue

            if (k in (Key.ENTER, "enter")) and active_sources:
                selected_source = active_sources[selected_idx]
                item_ret = self.handle_source_items(selected_source)
                if item_ret:
                    return item_ret
                continue

            if k.isdigit() and active_sources:
                idx = int(k)
                if 1 <= idx <= len(active_sources):
                    selected_source = active_sources[idx - 1]
                    item_ret = self.handle_source_items(selected_source)
                    if item_ret:
                        return item_ret
                else:
                    status_msg = f"Invalid selection: #{k}."
                    status_style = "red"

    def handle_add_folder(
        self, path_input: Optional[str] = None
    ) -> Tuple[Optional[str], Optional[Source]]:
        """
        Direct folder location prompt for Filemeta imports.
        Resolves local folders, USB drives, MTP devices, and KDE Connect paths automatically.
        Optional fallback: entering '?' or 'd' opens connected device discovery.
        """
        if not path_input:
            clear_viewport()
            render_header("ADD FILEMETA SOURCE")
            console.print("Enter folder path (or '?' to browse devices, 'b' to cancel):\n")
            path_input = key_reader.read_text_input("Folder: > ")

        if not path_input or path_input.strip().lower() in ("b", "back", "cancel"):
            return None, None

        # Check if user requested optional device discovery
        if path_input.strip() in ("?", "d", "browse"):
            return self.handle_browse_devices()

        # Resolve and auto-mount if needed
        with console.status("[bold cyan]Resolving folder location...[/bold cyan]", spinner="dots"):
            ok, resolved_path, err_msg = self.device_manager.resolve_and_mount_path(path_input)

        if not ok or not resolved_path:
            return err_msg, None

        timeout_val = getattr(self.filemeta_adapter, "get_timeout_for_path", lambda p: 60)(resolved_path)
        is_dev = getattr(self.filemeta_adapter, "is_device_path", lambda p: False)(resolved_path)
        status_text = (
            f"[bold cyan]Scanning device folder with filemeta (timeout {timeout_val}s)...[/bold cyan]"
            if is_dev
            else "[bold cyan]Scanning folder with filemeta...[/bold cyan]"
        )

        with console.status(status_text, spinner="dots"):
            try:
                scan_result = self.filemeta_adapter.scan_folder(resolved_path)
            except FilemetaTimeoutError as e:
                return (
                    f"Scan timed out after {e.timeout}s: '{resolved_path}'. "
                    "Device storage may be slow or unresponsive. Check connection or try a subfolder.",
                    None,
                )
            except Exception as e:
                return f"Error scanning folder: {e}", None

        source, _, _ = self.source_service.add_or_update_source(scan_result)
        return None, source

    def handle_browse_devices(self) -> Tuple[Optional[str], Optional[Source]]:
        """Optional device discovery fallback when the user does not know the exact path."""
        selected_idx = 0
        status_msg: Optional[str] = None

        options = [
            ("1.", "Wired Devices", "(Removable USB drives, flash drives & Android MTP)"),
            ("2.", "KDE Connect Devices", "(Paired phones and tablets)"),
        ]

        while True:
            clear_viewport()
            render_header("BROWSE CONNECTED DEVICES")
            if status_msg:
                print_status_message(status_msg, style="red")
                status_msg = None

            table = Table.grid(padding=(0, 1))
            table.add_column("cursor", justify="right", style="cyan bold", width=2)
            table.add_column("num", justify="right", style="cyan bold", width=4)
            table.add_column("name", style="bold white", width=24)
            table.add_column("desc", style="dim")

            for idx, (num, name, desc) in enumerate(options):
                cursor = "▶" if idx == selected_idx else " "
                table.add_row(cursor, num, name, desc)

            console.print(table)
            console.print()
            render_footer([("↑/↓", "Navigate"), ("Enter / 1-2", "Select"), ("b", "Back")])

            k = key_reader.read_key()
            if k in (Key.ESCAPE, "b", "back", "cancel"):
                return None, None

            if k == Key.UP:
                selected_idx = max(0, selected_idx - 1)
                continue
            if k == Key.DOWN:
                selected_idx = min(len(options) - 1, selected_idx + 1)
                continue

            choice = None
            if k in (Key.ENTER, "enter"):
                choice = str(selected_idx + 1)
            elif k in ("1", "2"):
                choice = k

            if choice == "1":
                err, src = self.handle_wired_devices()
                if src:
                    return None, src
                if err:
                    status_msg = err
                continue
            elif choice == "2":
                err, src = self.handle_kdeconnect_devices()
                if src:
                    return None, src
                if err:
                    status_msg = err
                continue

    def handle_wired_devices(self) -> Tuple[Optional[str], Optional[Source]]:
        """Discover and browse wired storage devices (removable drives and MTP)."""
        selected_idx = 0
        search = LiveSearchController[ConnectedDevice]()
        status_msg: Optional[str] = None

        while True:
            clear_viewport()
            render_header("WIRED DEVICES", subtitle="Removable Drives & Android MTP")
            if status_msg:
                print_status_message(status_msg, style="yellow")
                status_msg = None

            with console.status("[bold cyan]Scanning wired devices...[/bold cyan]", spinner="dots"):
                devices = self.device_manager.discover_wired_devices()

            if search.is_active:
                console.print(f"  Search: [bold cyan]{search.query}[/bold cyan]█\n")
            elif search.query:
                console.print(f"  Search: [bold cyan]{search.query}[/bold cyan] [dim](filter active)[/dim]\n")

            active_devs = search.filter(devices)

            if not active_devs:
                if search.query:
                    console.print("  [dim italic]No matching wired devices found.[/dim italic]\n")
                else:
                    console.print("  [dim italic]No wired storage devices detected.[/dim italic]\n")
                    console.print("  [dim](Connect a USB drive or plug in an Android device with MTP)[/dim]\n")
                render_footer([("r", "Refresh"), ("b", "Back")])
            else:
                if selected_idx >= len(active_devs):
                    selected_idx = max(0, len(active_devs) - 1)

                table = Table.grid(padding=(0, 1))
                table.add_column("cursor", justify="right", style="cyan bold", width=2)
                table.add_column("num", justify="right", style="cyan bold", width=4)
                table.add_column("name", style="bold white", width=32)
                table.add_column("status", style="dim")

                for idx, dev in enumerate(active_devs):
                    cursor = "▶" if idx == selected_idx else " "
                    table.add_row(
                        cursor,
                        f"{idx + 1}.",
                        dev.name,
                        Text(f"[{dev.status_detail}]", style="green" if dev.is_mounted else "dim yellow"),
                    )

                console.print(table)
                console.print()
                render_footer([("↑/↓", "Navigate"), ("Enter", "Browse device"), ("/", "Search"), ("r", "Refresh"), ("b", "Back")])

            k = key_reader.read_key()
            if k in (Key.ESCAPE, "b", "back", "cancel"):
                return None, None

            handled, selected_idx = search.handle_key(k, len(active_devs), selected_idx)
            if handled:
                continue

            if k == Key.UP and active_devs:
                selected_idx = max(0, selected_idx - 1)
                continue
            if k == Key.DOWN and active_devs:
                selected_idx = min(len(active_devs) - 1, selected_idx + 1)
                continue
            if k in ("r", "refresh"):
                continue

            if k in (Key.ENTER, "enter") and active_devs:
                target_dev = active_devs[selected_idx]
                if target_dev.device_type == DeviceType.WIRED_MTP and not target_dev.is_mounted:
                    if not target_dev.activation_root:
                        status_msg = "MTP device has no mountable activation root."
                        continue
                    with console.status("[bold cyan]Mounting MTP device...[/bold cyan]", spinner="dots"):
                        ok, m_path, err = self.device_manager.mount_mtp_device(target_dev.activation_root)
                    if not ok:
                        status_msg = err or "Could not mount MTP device. Ensure device is unlocked and MTP mode is enabled."
                        continue
                    target_dev.is_mounted = True
                    if m_path:
                        target_dev.mount_paths = [m_path]

                if not target_dev.is_mounted or not target_dev.mount_paths:
                    status_msg = f"Device '{target_dev.name}' is unmounted. Please mount it before scanning."
                    continue

                chosen_path = self._select_storage_path(target_dev)
                if not chosen_path:
                    continue

                err, src = self.handle_browse_device(target_dev, chosen_path)
                if src:
                    return None, src
                if err:
                    status_msg = err
                continue

    def handle_kdeconnect_devices(self) -> Tuple[Optional[str], Optional[Source]]:
        """Discover and browse paired KDE Connect devices."""
        selected_idx = 0
        search = LiveSearchController[ConnectedDevice]()
        status_msg: Optional[str] = None

        while True:
            clear_viewport()
            render_header("KDE CONNECT DEVICES", subtitle="Paired Phones & Tablets")
            if status_msg:
                print_status_message(status_msg, style="yellow")
                status_msg = None

            with console.status("[bold cyan]Querying KDE Connect devices...[/bold cyan]", spinner="dots"):
                devices = self.device_manager.discover_kdeconnect_devices()

            if search.is_active:
                console.print(f"  Search: [bold cyan]{search.query}[/bold cyan]█\n")
            elif search.query:
                console.print(f"  Search: [bold cyan]{search.query}[/bold cyan] [dim](filter active)[/dim]\n")

            active_devs = search.filter(devices)

            if not active_devs:
                if search.query:
                    console.print("  [dim italic]No matching KDE Connect devices found.[/dim italic]\n")
                else:
                    console.print("  [dim italic]No KDE Connect devices found.[/dim italic]\n")
                    console.print("  [dim](Ensure kdeconnectd is running and devices are paired)[/dim]\n")
                render_footer([("r", "Refresh"), ("b", "Back")])
            else:
                if selected_idx >= len(active_devs):
                    selected_idx = max(0, len(active_devs) - 1)

                table = Table.grid(padding=(0, 1))
                table.add_column("cursor", justify="right", style="cyan bold", width=2)
                table.add_column("num", justify="right", style="cyan bold", width=4)
                table.add_column("name", style="bold white", width=28)
                table.add_column("status", style="dim")

                for idx, dev in enumerate(active_devs):
                    cursor = "▶" if idx == selected_idx else " "
                    stat_style = "green" if (dev.is_connected and dev.is_mounted) else ("cyan" if dev.is_connected else "dim")
                    table.add_row(
                        cursor,
                        f"{idx + 1}.",
                        dev.name,
                        Text(f"[{dev.status_detail}]", style=stat_style),
                    )

                console.print(table)
                console.print()
                render_footer([("↑/↓", "Navigate"), ("Enter", "Select device"), ("/", "Search"), ("r", "Refresh"), ("b", "Back")])

            k = key_reader.read_key()
            if k in (Key.ESCAPE, "b", "back", "cancel"):
                return None, None

            handled, selected_idx = search.handle_key(k, len(active_devs), selected_idx)
            if handled:
                continue

            if k == Key.UP and active_devs:
                selected_idx = max(0, selected_idx - 1)
                continue
            if k == Key.DOWN and active_devs:
                selected_idx = min(len(active_devs) - 1, selected_idx + 1)
                continue
            if k in ("r", "refresh"):
                continue

            if k in (Key.ENTER, "enter") and active_devs:
                target_dev = active_devs[selected_idx]
                if not target_dev.is_connected:
                    status_msg = f"Device '{target_dev.name}' is unreachable on the network."
                    continue

                if not target_dev.is_mounted:
                    with console.status("[bold cyan]Mounting device filesystem via SFTP...[/bold cyan]", spinner="dots"):
                        ok, err = self.device_manager.mount_kdeconnect_device(target_dev.id)
                    if not ok:
                        status_msg = err or "Could not mount device SFTP. Ensure storage permissions are granted in the KDE Connect app."
                        continue
                    self.device_manager._enrich_kdeconnect_sftp(target_dev)

                if not target_dev.mount_paths:
                    status_msg = f"No storage directories exposed by '{target_dev.name}'. Ensure storage permissions are granted in KDE Connect."
                    continue

                chosen_path = self._select_storage_path(target_dev)
                if not chosen_path:
                    continue

                err, src = self.handle_browse_device(target_dev, chosen_path)
                if src:
                    return None, src
                if err:
                    status_msg = err
                continue

    def _select_storage_path(self, device: ConnectedDevice) -> Optional[str]:
        """If device exposes multiple storage locations (e.g. Internal vs SD card), prompt user."""
        if not device.mount_paths:
            return None
        if len(device.mount_paths) == 1:
            return device.mount_paths[0]

        selected_idx = 0
        while True:
            clear_viewport()
            render_header(f"{device.name}: SELECT STORAGE")
            console.print("Select storage location to browse:\n")

            table = Table.grid(padding=(0, 1))
            table.add_column("cursor", justify="right", style="cyan bold", width=2)
            table.add_column("num", justify="right", style="cyan bold", width=4)
            table.add_column("label", style="bold white", width=28)
            table.add_column("path", style="dim")

            for idx, p in enumerate(device.mount_paths):
                cursor = "▶" if idx == selected_idx else " "
                lbl = device.storage_labels.get(p) or Path(p).name or "Storage"
                table.add_row(cursor, f"{idx + 1}.", lbl, f"({p})")

            console.print(table)
            console.print()
            render_footer([("↑/↓", "Navigate"), ("Enter", "Select"), ("b", "Back")])

            k = key_reader.read_key()
            if k in (Key.ESCAPE, "b", "back", "cancel"):
                return None
            if k == Key.UP:
                selected_idx = max(0, selected_idx - 1)
            elif k == Key.DOWN:
                selected_idx = min(len(device.mount_paths) - 1, selected_idx + 1)
            elif k in (Key.ENTER, "enter"):
                return device.mount_paths[selected_idx]
            elif k.isdigit() and 1 <= int(k) <= len(device.mount_paths):
                return device.mount_paths[int(k) - 1]

    def handle_browse_device(
        self, device: ConnectedDevice, initial_path: str
    ) -> Tuple[Optional[str], Optional[Source]]:
        """Interactively browse folders on the device and select a folder to import."""
        current_path = initial_path
        selected_idx = 0
        search = LiveSearchController[Tuple[str, str]]()
        status_msg: Optional[str] = None

        while True:
            clear_viewport()
            render_header(f"BROWSE: {device.name}", subtitle=current_path)
            if status_msg:
                print_status_message(status_msg, style="red")
                status_msg = None

            try:
                subdirs = self.device_manager.list_directories(current_path)
            except Exception as e:
                subdirs = []
                status_msg = f"Cannot read directory: {e}"

            current_folder_name = Path(current_path).name or device.name
            entries: List[Tuple[str, str]] = [
                (f"[Import this folder: {current_folder_name}]", current_path),
            ]
            if current_path != initial_path:
                entries.append(("📁 .. (Parent folder)", str(Path(current_path).parent)))

            for sub_name, sub_path in subdirs:
                entries.append((f"📁 {sub_name}/", sub_path))

            if search.is_active:
                console.print(f"  Search: [bold cyan]{search.query}[/bold cyan]█\n")
            elif search.query:
                console.print(f"  Search: [bold cyan]{search.query}[/bold cyan] [dim](filter active)[/dim]\n")

            active_entries = search.filter(entries)

            if not active_entries:
                console.print("  [dim italic]No matching folders found.[/dim italic]\n")
                render_footer([("b", "Back"), ("Esc", "Cancel")])
            else:
                if selected_idx >= len(active_entries):
                    selected_idx = max(0, len(active_entries) - 1)

                table = Table.grid(padding=(0, 1))
                table.add_column("cursor", justify="right", style="cyan bold", width=2)
                table.add_column("name", style="bold white")

                for idx, (label, _) in enumerate(active_entries):
                    cursor = "▶" if idx == selected_idx else " "
                    style = "bold cyan" if label.startswith("[Import") else "white"
                    table.add_row(cursor, Text(label, style=style))

                console.print(table)
                console.print()
                render_footer([
                    ("↑/↓", "Navigate"),
                    ("Enter", "Select / Open"),
                    ("/", "Search"),
                    ("r", "Refresh"),
                    ("b", "Parent / Back"),
                    ("Esc", "Cancel"),
                ])

            k = key_reader.read_key()
            if k in (Key.ESCAPE, "escape"):
                return None, None

            handled, selected_idx = search.handle_key(k, len(active_entries), selected_idx)
            if handled:
                continue

            if k == Key.UP and active_entries:
                selected_idx = max(0, selected_idx - 1)
                continue
            if k == Key.DOWN and active_entries:
                selected_idx = min(len(active_entries) - 1, selected_idx + 1)
                continue
            if k in ("r", "refresh"):
                continue

            if k in ("b", "back"):
                if current_path != initial_path:
                    current_path = str(Path(current_path).parent)
                    selected_idx = 0
                    continue
                else:
                    return None, None

            if k in (Key.ENTER, "enter") and active_entries:
                sel_label, sel_path = active_entries[selected_idx]
                if sel_label.startswith("[Import"):
                    timeout_val = getattr(self.filemeta_adapter, "get_timeout_for_path", lambda p: 60)(sel_path)
                    is_dev = getattr(self.filemeta_adapter, "is_device_path", lambda p: False)(sel_path)
                    status_text = (
                        f"[bold cyan]Scanning device folder with filemeta (timeout {timeout_val}s)...[/bold cyan]"
                        if is_dev
                        else f"[bold cyan]Scanning '{sel_path}' with filemeta...[/bold cyan]"
                    )
                    with console.status(status_text, spinner="dots"):
                        try:
                            scan_res = self.filemeta_adapter.scan_folder(sel_path)
                        except FilemetaTimeoutError as e:
                            status_msg = f"Scan timed out after {e.timeout}s: device may be slow or unresponsive."
                            continue
                        except Exception as e:
                            status_msg = f"Scan failed: {e}"
                            continue

                    source, _, _ = self.source_service.add_or_update_source(scan_res)
                    return None, source
                elif sel_label.startswith("📁 .."):
                    current_path = sel_path
                    selected_idx = 0
                    continue
                else:
                    current_path = sel_path
                    selected_idx = 0
                    continue

    def handle_delete_source(
        self, sources: List[Source], current_idx: int = 0
    ) -> Optional[str]:
        """Prompt to delete an imported Filemeta source."""
        if not sources:
            return "No sources to delete."

        del_idx = current_idx
        while True:
            clear_viewport()
            render_header("DELETE FILEMETA SOURCE")
            console.print("Select source to delete:\n")

            table = Table.grid(padding=(0, 1))
            table.add_column("cursor", justify="right", style="cyan bold", width=2)
            table.add_column("num", justify="right", style="cyan bold", width=4)
            table.add_column("name", style="bold white")
            table.add_column("path", style="dim")

            for idx, src in enumerate(sources):
                cursor = "▶" if idx == del_idx else " "
                table.add_row(cursor, f"{idx + 1}.", src.name, f"({src.path})")

            console.print(table)
            console.print()
            render_footer(
                [
                    ("↑/↓", "Navigate"),
                    ("Enter", "Select"),
                    ("b", "Cancel"),
                ]
            )

            k = key_reader.read_key()
            if k in (Key.ESCAPE, "b", "q", "cancel"):
                return None
            elif k == Key.UP:
                del_idx = max(0, del_idx - 1)
            elif k == Key.DOWN:
                del_idx = min(len(sources) - 1, del_idx + 1)
            elif k in (Key.ENTER, "enter"):
                break
            elif k.isdigit() and 1 <= int(k) <= len(sources):
                del_idx = int(k) - 1
                break

        selected_source = sources[del_idx]

        clear_viewport()
        render_header("DELETE FILEMETA SOURCE", subtitle=selected_source.name)
        console.print(f"Source: [bold white]{selected_source.name}[/bold white]")
        console.print(f"Path:   [dim]{selected_source.path}[/dim]\n")
        console.print(
            "[yellow]This will remove the imported source and cached metadata from Todo.[/yellow]"
        )
        console.print(
            "[bold green]It will NOT delete anything from the actual filesystem.[/bold green]\n"
        )
        render_footer([("y", "Yes, delete"), ("n", "Cancel")])

        k_conf = key_reader.read_key()
        if k_conf.lower() in ("y", "yes", Key.ENTER):
            self.source_service.delete_source(selected_source.id)
            return None

        return None

    def handle_source_items(self, source: Source) -> Optional[str]:
        """Course details view: displays progress, hierarchical folder navigation, item status, search, and task addition."""
        # Check accessibility once at view entry (fast local check, no reconnect attempt)
        is_accessible = self.device_manager.is_source_accessible(
            source.path, attempt_reconnect=False
        )

        selected_idx = 0
        search = LiveSearchController[FileItem]()
        selected_item_ids: Set[int] = set()
        selected_delete_ids: Set[int] = set()
        delete_mode = False
        status_msg: Optional[str] = None
        status_style: str = "red"

        # Folder hierarchy state
        current_folder_id: Optional[int] = None
        folder_history: List[Tuple[Optional[int], str]] = []

        while True:
            clear_viewport()
            all_source_items = self.source_service.get_items_by_source(source.id)
            if not all_source_items:
                render_header(source.name, subtitle=source.path)
                print_status_message("No items found in this source.", "yellow")
                render_footer([("b", "Back")])
                key_reader.read_key()
                return None

            valid_item_ids = {it.id for it in all_source_items}
            selected_item_ids.intersection_update(valid_item_ids)
            selected_delete_ids.intersection_update(valid_item_ids)

            completed_cnt, total_cnt, pct = self.source_service.get_source_progress(
                source.id
            )

            header_title = source.name if is_accessible else f"{source.name} [Cached]"
            render_today_header(header_title, completed=completed_cnt, total=total_cnt)

            has_subfolders = self.source_service.has_subfolders(source.id)

            # Subtitle / Breadcrumb if in folder view and inside a subfolder
            if (
                has_subfolders
                and current_folder_id is not None
                and not search.is_active
                and not search.query
            ):
                crumb_parts = [source.name] + [name for _, name in folder_history]
                crumb_str = " / ".join(crumb_parts)
                console.print(f"  [bold cyan]Folder:[/bold cyan] [dim]{crumb_str}[/dim]\n")

            if delete_mode:
                print_status_message(
                    f"DELETE MODE · {len(selected_delete_ids)} selected", style="bold yellow"
                )
            elif status_msg:
                print_status_message(status_msg, style=status_style)
                status_msg = None
                status_style = "red"
            elif not is_accessible:
                print_status_message(
                    "Device disconnected · Viewing cached metadata", style="dim yellow"
                )

            # Determine visible entries
            if search.is_active:
                console.print(f"  Search: [bold cyan]{search.query}[/bold cyan]█\n")
            elif search.query:
                console.print(
                    f"  Search: [bold cyan]{search.query}[/bold cyan] [dim](filter active across course, Esc to return to folder)[/dim]\n"
                )

            if search.is_active or search.query:
                # Searching across ALL files in the course from SQLite
                search_results = search.filter(all_source_items)
                entries: List[Tuple[str, Any]] = [("file", it) for it in search_results]
            elif has_subfolders:
                child_folders = self.source_service.get_folders(
                    source.id, parent_id=current_folder_id
                )
                child_files = self.source_service.get_items_in_folder(
                    source.id, folder_id=current_folder_id
                )
                entries = []
                if current_folder_id is not None:
                    entries.append(("parent", None))
                for f in child_folders:
                    entries.append(("folder", f))
                for f_it in child_files:
                    entries.append(("file", f_it))
            else:
                entries = [("file", it) for it in all_source_items]

            if not entries:
                if search.is_active or search.query:
                    console.print("  [dim italic]No matching items found.[/dim italic]\n")
                else:
                    console.print("  [dim italic]This folder is empty.[/dim italic]\n")
            else:
                if selected_idx >= len(entries):
                    selected_idx = max(0, len(entries) - 1)

                term_height = console.size.height if console.is_terminal else 24
                window_size = max(6, term_height - 9)
                if len(entries) > window_size:
                    scroll_offset = max(
                        0,
                        min(
                            selected_idx - window_size // 2,
                            len(entries) - window_size,
                        ),
                    )
                    visible_slice = entries[
                        scroll_offset : scroll_offset + window_size
                    ]
                else:
                    scroll_offset = 0
                    visible_slice = entries

                show_checkbox = delete_mode or bool(selected_item_ids)

                table = Table.grid(padding=(0, 1))
                table.add_column("cursor", justify="right", style="cyan bold", width=2)
                if show_checkbox:
                    table.add_column("select", justify="center", width=4)
                table.add_column("icon", justify="center", width=2)
                table.add_column("num", justify="right", style="cyan bold", width=4)
                table.add_column("title")
                table.add_column("info", style="dim")

                today_str = self.daily_service.get_today_str()
                today_tasks = self.daily_service.get_tasks_for_date(today_str)
                active_today = self.daily_service.get_active_tasks_for_date(today_str)
                scheduled_item_ids = {
                    t.task.source_id
                    for t in (today_tasks + active_today)
                    if t.task.source_type in ("filemeta", "telegram", "youtube")
                    and t.task.source_id is not None
                }

                for i, (entry_type, entry_obj) in enumerate(visible_slice):
                    actual_i = scroll_offset + i
                    cursor = "▶" if actual_i == selected_idx else " "

                    if entry_type == "parent":
                        chk_text = Text("   ")
                        icon = Text("📁", style="cyan")
                        num_str = ".."
                        title_str = Text(".. (Parent folder)", style="dim")
                        info_text = Text()
                    elif entry_type == "folder":
                        folder: SourceFolder = entry_obj
                        chk_text = Text("   ")
                        icon = Text("📁", style="bold cyan")
                        num_str = f"{actual_i + 1}."
                        title_str = Text(f"{folder.name}/", style="bold cyan")
                        pct_text = f"{folder.progress_pct}% ({folder.completed_count}/{folder.item_count})"
                        info_text = Text(pct_text, style="dim cyan")
                    else:
                        file_item: FileItem = entry_obj
                        if delete_mode:
                            is_selected = file_item.id in selected_delete_ids
                        else:
                            is_selected = file_item.id in selected_item_ids
                        chk_text = (
                            Text("[x]", style="bold cyan")
                            if is_selected
                            else Text("[ ]", style="dim")
                        )
                        icon = (
                            Text("✓", style="bold green")
                            if file_item.completed
                            else Text("○", style="bright_yellow")
                        )
                        title_style = "green" if file_item.completed else "bold white"
                        title_str = Text(file_item.title, style=title_style)
                        num_str = f"{actual_i + 1}."

                        info_text = Text()
                        if not is_accessible:
                            info_text.append("[Cached] ", style="dim yellow")
                        if file_item.id in scheduled_item_ids:
                            info_text.append("[Today] ", style="bold cyan")
                        if (
                            "duration_formatted" in file_item.metadata
                            and file_item.metadata["duration_formatted"]
                        ):
                            info_text.append(
                                f"({file_item.metadata['duration_formatted']})",
                                style="dim green",
                            )
                        elif "pages" in file_item.metadata and file_item.metadata["pages"]:
                            info_text.append(
                                f"({file_item.metadata['pages']} pages)",
                                style="dim green",
                            )

                    row_parts = [cursor]
                    if show_checkbox:
                        row_parts.append(chk_text)
                    row_parts.extend([
                        icon,
                        num_str,
                        title_str,
                        info_text,
                    ])
                    table.add_row(*row_parts)

                console.print(table)
                console.print()

            if delete_mode:
                hints = [
                    ("↑/↓", "Navigate"),
                    ("Space", "Select"),
                    (
                        "Enter",
                        f"Confirm delete ({len(selected_delete_ids)})"
                        if selected_delete_ids
                        else "Confirm delete",
                    ),
                    ("Esc", "Cancel"),
                ]
            elif search.is_active:
                hints = [
                    ("↑/↓", "Navigate"),
                    ("Enter", "Done"),
                    ("Esc", "Cancel / Clear"),
                ]
            elif selected_item_ids:
                hints = [
                    ("↑/↓", "Navigate"),
                    ("Space", "Deselect"),
                    ("Enter", f"Add to Today ({len(selected_item_ids)})"),
                    ("d", "Delete"),
                    ("Esc", "Clear selection"),
                    ("b", "Back"),
                ]
            elif search.query:
                hints = [
                    ("↑/↓", "Navigate"),
                    ("Space", "Select"),
                    ("Enter", "Add to Today"),
                    ("/", "Search"),
                    ("Esc", "Folder view"),
                    ("t", "Toggle"),
                    ("d", "Delete"),
                    ("b", "Back"),
                ]
            else:
                hints = [
                    ("↑/↓", "Navigate"),
                    ("Enter", "Open / Add to Today"),
                    ("Space", "Select"),
                    ("/", "Search"),
                    ("n", "Next"),
                    ("t", "Toggle"),
                    ("r", "Rescan"),
                    ("d", "Delete"),
                    ("b", "Back"),
                ]
            render_footer(hints)

            k = key_reader.read_key()

            if delete_mode:
                if k in (Key.ESCAPE, "escape", "cancel"):
                    selected_delete_ids.clear()
                    delete_mode = False
                    continue

                if k == Key.UP and entries:
                    selected_idx = max(0, selected_idx - 1)
                    continue

                if k == Key.DOWN and entries:
                    selected_idx = min(len(entries) - 1, selected_idx + 1)
                    continue

                if k in (Key.SPACE, "space", " ") and entries:
                    entry_type, entry_obj = entries[selected_idx]
                    if entry_type == "file":
                        target_item = entry_obj
                        if target_item.id in selected_delete_ids:
                            selected_delete_ids.remove(target_item.id)
                        else:
                            selected_delete_ids.add(target_item.id)
                    continue

                if k in (Key.ENTER, "enter"):
                    if not selected_delete_ids:
                        continue
                    clear_viewport()
                    render_header("DELETE ITEMS", subtitle=source.name)
                    console.print(
                        f"Delete [bold white]{len(selected_delete_ids)} items[/bold white] from this course?\n"
                    )
                    console.print(
                        "[yellow]This will remove the items from Todo.[/yellow]"
                    )
                    console.print(
                        "[bold green]Real files on the filesystem remain untouched.[/bold green]\n"
                    )
                    render_footer([("y", "Yes, delete"), ("n", "Cancel")])
                    k_conf = key_reader.read_key()
                    if k_conf.lower() in ("y", "yes", Key.ENTER, "enter"):
                        self.source_service.delete_file_items_bulk(
                            list(selected_delete_ids)
                        )
                    selected_delete_ids.clear()
                    delete_mode = False
                    continue

                continue

            handled, selected_idx = search.handle_key(k, len(entries), selected_idx)
            if handled:
                continue

            if k in (Key.ESCAPE, "escape"):
                if selected_item_ids:
                    selected_item_ids.clear()
                    continue
                if current_folder_id is not None:
                    if folder_history:
                        current_folder_id, _ = folder_history.pop()
                    else:
                        current_folder_id = None
                    selected_idx = 0
                    continue
                return None

            if k in ("b", "cancel", "q", "quit", Key.LEFT):
                if current_folder_id is not None:
                    if folder_history:
                        current_folder_id, _ = folder_history.pop()
                    else:
                        current_folder_id = None
                    selected_idx = 0
                    continue
                return None

            if k == Key.UP and entries:
                selected_idx = max(0, selected_idx - 1)
                continue

            if k == Key.DOWN and entries:
                selected_idx = min(len(entries) - 1, selected_idx + 1)
                continue

            if k in (Key.SPACE, "space", " ") and entries:
                entry_type, entry_obj = entries[selected_idx]
                if entry_type == "file":
                    target_item = entry_obj
                    if target_item.id in selected_item_ids:
                        selected_item_ids.remove(target_item.id)
                    else:
                        selected_item_ids.add(target_item.id)
                continue

            if k in (Key.ENTER, "enter"):
                if not entries and not selected_item_ids:
                    continue

                if selected_item_ids:
                    today_str = self.daily_service.get_today_str()
                    items_to_add = [
                        it for it in all_source_items if it.id in selected_item_ids
                    ]
                    for it in items_to_add:
                        task = self.task_service.get_or_create_filemeta_task(
                            it, source_name=source.name
                        )
                        self.daily_service.add_task_to_date(task.id, today_str)
                    selected_item_ids.clear()
                    return "added"

                if entries:
                    entry_type, entry_obj = entries[selected_idx]
                    if entry_type == "parent":
                        if folder_history:
                            current_folder_id, _ = folder_history.pop()
                        else:
                            current_folder_id = None
                        selected_idx = 0
                        continue
                    elif entry_type == "folder":
                        folder_history.append((current_folder_id, entry_obj.name))
                        current_folder_id = entry_obj.id
                        selected_idx = 0
                        continue
                    elif entry_type == "file":
                        target_item = entry_obj
                        task = self.task_service.get_or_create_filemeta_task(
                            target_item, source_name=source.name
                        )
                        today_str = self.daily_service.get_today_str()
                        self.daily_service.add_task_to_date(task.id, today_str)
                        return "added"

            if k == "n" and entries:
                unfinished_indices = [
                    i
                    for i, (e_type, e_obj) in enumerate(entries)
                    if e_type == "file" and not e_obj.completed
                ]
                if unfinished_indices:
                    next_idx = next(
                        (i for i in unfinished_indices if i > selected_idx), None
                    )
                    if next_idx is None:
                        next_idx = unfinished_indices[0]
                    selected_idx = next_idx
                continue

            if k in ("t", "toggle") and entries:
                entry_type, entry_obj = entries[selected_idx]
                if entry_type == "file":
                    self.source_service.toggle_item_completion(entry_obj.id)
                    entry_obj.completed = not entry_obj.completed
                continue

            if k in ("r", "rescan"):
                scan_target_path = source.path
                if not self.device_manager.is_source_accessible(
                    scan_target_path, attempt_reconnect=True
                ):
                    if source.canonical_uri:
                        ok_can, resolved_can = self.device_manager.resolve_canonical_uri(
                            source.canonical_uri
                        )
                        if (
                            ok_can
                            and resolved_can
                            and self.device_manager.is_source_accessible(
                                resolved_can, attempt_reconnect=True
                            )
                        ):
                            scan_target_path = resolved_can
                            source.path = resolved_can
                    if not self.device_manager.is_source_accessible(
                        scan_target_path, attempt_reconnect=False
                    ):
                        status_msg = f"Cannot rescan: device is disconnected or path '{source.path}' is inaccessible."
                        status_style = "yellow"
                        continue

                timeout_val = getattr(
                    self.filemeta_adapter, "get_timeout_for_path", lambda p: 60
                )(scan_target_path)
                is_dev = getattr(
                    self.filemeta_adapter, "is_device_path", lambda p: False
                )(scan_target_path)
                status_text = (
                    f"[bold cyan]Rescanning device folder (timeout {timeout_val}s)...[/bold cyan]"
                    if is_dev
                    else "[bold cyan]Rescanning folder...[/bold cyan]"
                )
                with console.status(status_text, spinner="dots"):
                    try:
                        scan_res = self.filemeta_adapter.scan_folder(scan_target_path)
                        self.source_service.add_or_update_source(
                            scan_res, is_rescan=True
                        )
                        is_accessible = True
                    except FilemetaTimeoutError as e:
                        status_msg = f"Rescan timed out after {e.timeout}s: device may be slow or unreachable."
                        status_style = "red"
                    except Exception as e:
                        status_msg = f"Rescan failed: {e}"
                        status_style = "red"
                continue

            if k in ("d", "delete") and entries:
                delete_mode = True
                selected_delete_ids.clear()
                continue

            if k.isdigit() and entries:
                sel_num = int(k)
                if 1 <= sel_num <= len(entries):
                    entry_type, entry_obj = entries[sel_num - 1]
                    if entry_type == "parent":
                        if folder_history:
                            current_folder_id, _ = folder_history.pop()
                        else:
                            current_folder_id = None
                        selected_idx = 0
                        continue
                    elif entry_type == "folder":
                        folder_history.append((current_folder_id, entry_obj.name))
                        current_folder_id = entry_obj.id
                        selected_idx = 0
                        continue
                    elif entry_type == "file":
                        task = self.task_service.get_or_create_filemeta_task(
                            entry_obj, source_name=source.name
                        )
                        today_str = self.daily_service.get_today_str()
                        self.daily_service.add_task_to_date(task.id, today_str)
                        selected_item_ids.clear()
                        return "added"

