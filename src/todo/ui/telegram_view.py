"""Telegram channels home screen and video lessons browser view for full-screen TUI."""

from datetime import datetime
from typing import Any, List, Optional, Set, Tuple, Union

from rich.table import Table
from rich.text import Text

from todo.integrations.telegram import (
    TelegramAdapter,
    TelegramChannel,
    TelegramConnectionError,
    TelegramIntegrationError,
    TelegramNotConfiguredError,
    TelegramSessionNotFoundError,
    TelegramUnauthorizedError,
)
from todo.models import FileItem, Source
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


class TelegramView:
    def __init__(
        self,
        telegram_adapter: TelegramAdapter,
        source_service: SourceService,
        task_service: TaskService,
        daily_service: DailyService,
    ):
        self.adapter = telegram_adapter
        self.source_service = source_service
        self.task_service = task_service
        self.daily_service = daily_service

    def run(self, select_item_mode: bool = False) -> Any:
        """
        Telegram home screen: displays channels already imported into Todo's SQLite database.
        Follows Filemeta workflow: uses SQLite only on launch without network fetching.
        """
        status_msg: Optional[str] = None
        selected_idx = 0
        search = LiveSearchController[Source]()
        selected_source_ids: Set[int] = set()
        delete_mode = False

        while True:
            clear_viewport()
            sources = self.source_service.get_sources(source_type="telegram")
            if delete_mode:
                subtitle = f"DELETE MODE · {len(selected_source_ids)} selected"
            elif select_item_mode:
                subtitle = "Select Channel for Chunking"
            else:
                subtitle = None
            render_header("TELEGRAM CHANNELS", subtitle=subtitle)

            valid_ids = {s.id for s in sources}
            selected_source_ids.intersection_update(valid_ids)

            if status_msg:
                print_status_message(status_msg, style="red")
                status_msg = None

            if search.is_active:
                console.print(f"  Search: [bold cyan]{search.query}[/bold cyan]█\n")
            elif search.query:
                console.print(f"  Search: [bold cyan]{search.query}[/bold cyan] [dim](filter active, Esc to clear)[/dim]\n")

            active_sources = search.filter(sources)

            if not active_sources:
                if search.query:
                    console.print("  [dim italic]No matching Telegram channels found.[/dim italic]\n")
                else:
                    console.print(
                        "  [dim italic]No Telegram channels imported yet.[/dim italic]\n"
                    )
                if search.is_active:
                    hints = [("Enter", "Done"), ("Esc", "Cancel / Clear")]
                elif delete_mode:
                    hints = [("Esc", "Cancel")]
                else:
                    hints = [("+", "Add channel"), ("/", "Search"), ("b", "Back")]
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
                    table.add_row(*empty_cols, Text(f"   {src.path}", style="dim"), "")

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
                        ("Enter", "Open channel"),
                        ("+", "Add channel"),
                        ("d", "Delete"),
                        ("/", "Search"),
                        ("Esc", "Clear search"),
                        ("b", "Back"),
                    ]
                else:
                    hints = [
                        ("↑/↓", "Navigate"),
                        ("Enter", "Open channel"),
                        ("+", "Add channel"),
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
                        f"Delete [bold white]{len(selected_source_ids)} Telegram channels[/bold white]?\n"
                    )
                    console.print(
                        "[yellow]This will remove the imported channels and cached metadata from Todo.[/yellow]"
                    )
                    console.print(
                        "[bold green]It will NOT delete anything from Telegram.[/bold green]\n"
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

            # +: Open channel picker to discover and import a Telegram channel
            if k in ("+", Key.PLUS, "add"):
                add_ret = self.handle_add_channel(select_item_mode=select_item_mode)
                if select_item_mode and add_ret:
                    return add_ret
                if add_ret == "added":
                    return "added"
                continue

            # d: Enter delete selection mode
            if k in ("d", "delete") and active_sources:
                delete_mode = True
                selected_source_ids.clear()
                continue

            # Enter: Open selected channel lessons immediately from SQLite
            if (k in (Key.ENTER, "enter")) and active_sources:
                selected_source = active_sources[selected_idx]
                item_ret = self.handle_channel_lessons(
                    selected_source, select_item_mode=select_item_mode
                )
                if select_item_mode and item_ret:
                    return (selected_source, item_ret)
                if item_ret == "added":
                    return "added"
                continue

            # Numeric selection
            if k.isdigit() and active_sources:
                idx = int(k)
                if 1 <= idx <= len(active_sources):
                    selected_source = active_sources[idx - 1]
                    item_ret = self.handle_channel_lessons(
                        selected_source, select_item_mode=select_item_mode
                    )
                    if select_item_mode and item_ret:
                        return (selected_source, item_ret)
                    if item_ret == "added":
                        return "added"
                else:
                    status_msg = f"Invalid selection: #{k}."

    def _show_error_screen(self, title: str, message: str) -> None:
        """Display a structured error screen and wait for user to press back/quit."""
        clear_viewport()
        render_header(title)
        console.print(f"  [bold red]{message}[/bold red]\n")
        console.print("  [dim]Press 'b' to return to the previous menu.[/dim]\n")
        render_footer([("b", "Back")])
        while True:
            k = key_reader.read_key()
            if k in (Key.ESCAPE, "b", "q", "cancel"):
                break
        return None

    def handle_delete_source(
        self, sources: List[Source], current_idx: int = 0
    ) -> Optional[str]:
        """Prompt to delete an imported Telegram source from Todo without touching Telegram."""
        if not sources:
            return "No sources to delete."

        del_idx = current_idx
        while True:
            clear_viewport()
            render_header("DELETE TELEGRAM SOURCE")
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
        render_header("DELETE TELEGRAM SOURCE", subtitle=selected_source.name)
        console.print(f"Source: [bold white]{selected_source.name}[/bold white]")
        console.print(f"Path:   [dim]{selected_source.path}[/dim]\n")
        console.print(
            "[yellow]This will remove the imported source and cached metadata from Todo.[/yellow]"
        )
        console.print(
            "[bold green]It will NOT delete anything from Telegram.[/bold green]\n"
        )
        render_footer([("y", "Yes, delete"), ("n", "Cancel")])

        k_conf = key_reader.read_key()
        if k_conf.lower() in ("y", "yes", Key.ENTER, "enter"):
            self.source_service.delete_source(selected_source.id)
            return None

        return None

    def handle_add_channel(self, select_item_mode: bool = False) -> Any:
        """
        Channel picker: discover, search, and import Telegram channels into Todo.
        Uses locally cached Telegram channels list; refreshes only on explicit 'r'.
        """
        ok, err = self.adapter.check_availability()
        if not ok:
            return self._show_error_screen(
                "TELEGRAM NOT CONFIGURED", err or "Telegram is not available."
            )

        cached_channels = self.source_service.get_cached_telegram_channels()
        if not cached_channels:
            with console.status(
                "[bold cyan]Discovering Telegram channels...[/bold cyan]", spinner="dots"
            ):
                try:
                    self.adapter.connect()
                    cached_channels = self.adapter.discover_channels()
                    self.source_service.save_cached_telegram_channels(cached_channels)
                except TelegramNotConfiguredError as e:
                    return self._show_error_screen("TELEGRAM NOT CONFIGURED", str(e))
                except TelegramSessionNotFoundError as e:
                    return self._show_error_screen("TELEGRAM SESSION NOT FOUND", str(e))
                except TelegramUnauthorizedError as e:
                    return self._show_error_screen("TELEGRAM UNAUTHORIZED", str(e))
                except TelegramConnectionError as e:
                    return self._show_error_screen("TELEGRAM CONNECTION ERROR", str(e))
                except Exception as e:
                    return self._show_error_screen("CHANNEL DISCOVERY FAILED", str(e))

        selected_idx = 0
        search = LiveSearchController[TelegramChannel](key_fn=lambda c: c.title)
        status_msg: Optional[str] = None

        while True:
            clear_viewport()
            channels = cached_channels or []

            imported_map = {
                s.path: s for s in self.source_service.get_sources(source_type="telegram")
            }

            render_header(
                "ADD TELEGRAM CHANNEL",
                subtitle=f"{len(channels)} channels available",
            )

            if status_msg:
                print_status_message(status_msg, style="red")
                status_msg = None

            # Filter channels if search is active
            if search.is_active:
                console.print(f"  Search: [bold cyan]{search.query}[/bold cyan]█\n")
            elif search.query:
                console.print(f"  Search: [bold cyan]{search.query}[/bold cyan] [dim](filter active, Esc to clear)[/dim]\n")

            active_channels = search.filter(channels)

            if not active_channels:
                console.print("  [dim italic]No matching channels found.[/dim italic]\n")
                if search.is_active:
                    hints = [("Enter", "Done"), ("Esc", "Cancel / Clear")]
                else:
                    hints = [("/", "Search"), ("r", "Refresh list"), ("b", "Back")]
                render_footer(hints)
            else:
                if selected_idx >= len(active_channels):
                    selected_idx = max(0, len(active_channels) - 1)

                term_height = console.size.height if console.is_terminal else 24
                window_size = max(6, term_height - 9)
                if len(active_channels) > window_size:
                    scroll_offset = max(
                        0,
                        min(
                            selected_idx - window_size // 2,
                            len(active_channels) - window_size,
                        ),
                    )
                    visible_slice = active_channels[scroll_offset : scroll_offset + window_size]
                else:
                    scroll_offset = 0
                    visible_slice = active_channels

                table = Table.grid(padding=(0, 1))
                table.add_column("cursor", justify="right", style="cyan bold", width=2)
                table.add_column("num", justify="right", style="cyan bold", width=4)
                table.add_column("name", style="bold white", width=36)
                table.add_column("status", style="cyan")

                for idx, chan in enumerate(visible_slice):
                    actual_i = scroll_offset + idx
                    cursor = "▶" if actual_i == selected_idx else " "

                    chan_uri = f"telegram://{chan.id}"
                    status_text = ""
                    if chan_uri in imported_map:
                        src = imported_map[chan_uri]
                        lesson_label = "lesson" if src.item_count == 1 else "lessons"
                        status_text = f"(Imported: {src.item_count} {lesson_label})"

                    table.add_row(
                        cursor,
                        f"{actual_i + 1}.",
                        chan.title,
                        Text(status_text, style="dim cyan"),
                    )

                console.print(table)
                console.print()

                if search.is_active:
                    hints = [
                        ("↑/↓", "Navigate"),
                        ("Enter", "Done"),
                        ("Esc", "Cancel / Clear"),
                    ]
                elif search.query:
                    hints = [
                        ("↑/↓", "Navigate"),
                        ("Enter", "Select"),
                        ("/", "Search"),
                        ("Esc", "Clear search"),
                        ("r", "Refresh list"),
                        ("b", "Back"),
                    ]
                else:
                    hints = [
                        ("↑/↓", "Navigate"),
                        ("Enter", "Select"),
                        ("/", "Search"),
                        ("r", "Refresh list"),
                        ("b", "Back"),
                    ]
                render_footer(hints)

            k = key_reader.read_key()

            handled, selected_idx = search.handle_key(k, len(active_channels), selected_idx)
            if handled:
                continue

            if k in (Key.ESCAPE, "escape", "b", "cancel"):
                return None

            if k in ("q", "quit"):
                return None

            if k == Key.UP and active_channels:
                selected_idx = max(0, selected_idx - 1)
                continue

            if k == Key.DOWN and active_channels:
                selected_idx = min(len(active_channels) - 1, selected_idx + 1)
                continue

            # r: Refresh channels list from Telegram
            if k in ("r", "refresh"):
                with console.status(
                    "[bold cyan]Refreshing Telegram channels...[/bold cyan]", spinner="dots"
                ):
                    try:
                        self.adapter.connect()
                        cached_channels = self.adapter.discover_channels()
                        self.source_service.save_cached_telegram_channels(cached_channels)
                    except Exception as e:
                        status_msg = f"Refresh failed: {e}"
                continue

            # Enter / select channel
            if (k in (Key.ENTER, "enter")) and active_channels:
                chosen_channel = active_channels[selected_idx]
                chan_uri = f"telegram://{chosen_channel.id}"
                existing_source = self.source_service.get_source_by_path(chan_uri)

                if existing_source:
                    # Open already-imported cached lessons directly without scan!
                    item_ret = self.handle_channel_lessons(
                        existing_source, select_item_mode=select_item_mode
                    )
                    if select_item_mode and item_ret:
                        return (existing_source, item_ret)
                    return item_ret
                else:
                    # First-time import!
                    clear_viewport()
                    render_header(
                        chosen_channel.title,
                        subtitle="Importing Telegram channel lessons...",
                    )
                    with console.status(
                        f"[bold cyan]Importing lessons for {chosen_channel.title}...[/bold cyan]",
                        spinner="dots",
                    ):
                        try:
                            self.adapter.connect()
                            scan_res = self.adapter.scan_channel(chosen_channel.id)
                            new_source, _, _ = self.source_service.add_or_update_source(scan_res)
                        except Exception as e:
                            self._show_error_screen("CHANNEL IMPORT FAILED", str(e))
                            return None

                    item_ret = self.handle_channel_lessons(
                        new_source, select_item_mode=select_item_mode
                    )
                    if select_item_mode and item_ret:
                        return (new_source, item_ret)
                    return item_ret

            # Numeric selection
            if k.isdigit() and active_channels:
                sel_num = int(k)
                if 1 <= sel_num <= len(active_channels):
                    chosen_channel = active_channels[sel_num - 1]
                    chan_uri = f"telegram://{chosen_channel.id}"
                    existing_source = self.source_service.get_source_by_path(chan_uri)

                    if existing_source:
                        item_ret = self.handle_channel_lessons(
                            existing_source, select_item_mode=select_item_mode
                        )
                        if select_item_mode and item_ret:
                            return (existing_source, item_ret)
                        return item_ret
                    else:
                        clear_viewport()
                        render_header(
                            chosen_channel.title,
                            subtitle="Importing Telegram channel lessons...",
                        )
                        with console.status(
                            f"[bold cyan]Importing lessons for {chosen_channel.title}...[/bold cyan]",
                            spinner="dots",
                        ):
                            try:
                                self.adapter.connect()
                                scan_res = self.adapter.scan_channel(chosen_channel.id)
                                new_source, _, _ = self.source_service.add_or_update_source(scan_res)
                            except Exception as e:
                                self._show_error_screen("CHANNEL IMPORT FAILED", str(e))
                                return None

                        item_ret = self.handle_channel_lessons(
                            new_source, select_item_mode=select_item_mode
                        )
                        if select_item_mode and item_ret:
                            return (new_source, item_ret)
                        return item_ret
                else:
                    status_msg = f"Invalid selection: #{k}."

    def handle_channel_lessons(
        self,
        target: Union[Source, TelegramChannel],
        select_item_mode: bool = False,
    ) -> Any:
        """
        Browse video lessons inside a selected Telegram channel.
        Supports fuzzy search, next unfinished navigation, multi-select, and Today scheduling.
        Loads from SQLite cache immediately if already imported.
        """
        if isinstance(target, Source):
            existing_source: Optional[Source] = target
            channel_uri = target.path
            channel_id_str = target.path.replace("telegram://", "").split("/")[0]
            try:
                channel_id = int(channel_id_str)
            except ValueError:
                channel_id = channel_id_str
            channel_title = target.name
        else:
            channel_id = target.id
            channel_title = target.title
            channel_uri = f"telegram://{target.id}"
            existing_source = self.source_service.get_source_by_path(channel_uri)
            if not existing_source:
                clear_viewport()
                render_header(channel_title, subtitle="Importing Telegram channel lessons...")
                with console.status(
                    f"[bold cyan]Importing lessons for {channel_title}...[/bold cyan]",
                    spinner="dots",
                ):
                    try:
                        self.adapter.connect()
                        scan_result = self.adapter.scan_channel(target.id)
                        existing_source, _, _ = self.source_service.add_or_update_source(
                            scan_result
                        )
                    except Exception as e:
                        self._show_error_screen("CHANNEL IMPORT FAILED", str(e))
                        return None

        # Main lesson browser loop
        selected_idx = 0
        search = LiveSearchController[FileItem]()
        selected_item_ids: Set[int] = set()
        selected_delete_ids: Set[int] = set()
        delete_mode = False
        status_msg: Optional[str] = None

        while True:
            clear_viewport()
            assert existing_source is not None
            items = self.source_service.get_items_by_source(existing_source.id)

            valid_item_ids = {it.id for it in items}
            selected_item_ids.intersection_update(valid_item_ids)
            selected_delete_ids.intersection_update(valid_item_ids)

            today_str = self.daily_service.get_today_str()
            today_tasks = self.daily_service.get_tasks_for_date(today_str)
            active_today = self.daily_service.get_active_tasks_for_date(today_str)
            scheduled_item_ids = {
                t.task.source_id
                for t in (today_tasks + active_today)
                if t.task.source_type in ("telegram", "filemeta", "youtube") and t.task.source_id is not None
            }

            if not items:
                render_header(existing_source.name, subtitle=f"{channel_uri} · 0 videos")
                print_status_message("No video lessons found in this channel.", "yellow")
                render_footer([("r", "Rescan"), ("d", "Delete"), ("b", "Back")])
                sub_k = key_reader.read_key()
                if sub_k in ("r", "rescan"):
                    with console.status(
                        f"[bold cyan]Rescanning lessons for {channel_title}...[/bold cyan]",
                        spinner="dots",
                    ):
                        try:
                            self.adapter.connect()
                            scan_res = self.adapter.scan_channel(channel_id)
                            existing_source, _, _ = self.source_service.add_or_update_source(
                                scan_res
                            )
                        except Exception as e:
                            status_msg = f"Rescan failed: {e}"
                    continue
                elif sub_k in ("d", "delete"):
                    clear_viewport()
                    render_header("DELETE TELEGRAM SOURCE", subtitle=existing_source.name)
                    console.print(f"Source: [bold white]{existing_source.name}[/bold white]")
                    console.print(f"Path:   [dim]{existing_source.path}[/dim]\n")
                    console.print(
                        "[yellow]This will remove the imported source and cached metadata from Todo.[/yellow]"
                    )
                    console.print(
                        "[bold green]It will NOT delete anything from Telegram.[/bold green]\n"
                    )
                    render_footer([("y", "Yes, delete"), ("n", "Cancel")])
                    k_conf = key_reader.read_key()
                    if k_conf.lower() in ("y", "yes", Key.ENTER, "enter"):
                        self.source_service.delete_source(existing_source.id)
                        return None
                    continue
                return None

            completed_cnt, total_cnt, pct = self.source_service.get_source_progress(
                existing_source.id
            )

            # Boxed header matching format: TITLE      42% · 5/12
            render_today_header(existing_source.name, completed=completed_cnt, total=total_cnt)

            if delete_mode:
                print_status_message(
                    f"DELETE MODE · {len(selected_delete_ids)} selected", style="bold yellow"
                )
            elif status_msg:
                print_status_message(status_msg, style="red")
                status_msg = None
            elif select_item_mode:
                print_status_message("Select a lesson to create a Chunky Task", style="bold cyan")

            # Filter items if search is active
            if search.is_active:
                console.print(f"  Search: [bold cyan]{search.query}[/bold cyan]█\n")
            elif search.query:
                console.print(f"  Search: [bold cyan]{search.query}[/bold cyan] [dim](filter active, Esc to clear)[/dim]\n")

            active_items = search.filter(items)

            if not active_items:
                console.print("  [dim italic]No matching lessons found.[/dim italic]\n")
            else:
                if selected_idx >= len(active_items):
                    selected_idx = max(0, len(active_items) - 1)

                term_height = console.size.height if console.is_terminal else 24
                window_size = max(6, term_height - 9)
                if len(active_items) > window_size:
                    scroll_offset = max(
                        0,
                        min(
                            selected_idx - window_size // 2,
                            len(active_items) - window_size,
                        ),
                    )
                    visible_slice = active_items[scroll_offset : scroll_offset + window_size]
                else:
                    scroll_offset = 0
                    visible_slice = active_items

                show_checkbox = delete_mode or bool(selected_item_ids)

                table = Table.grid(padding=(0, 1))
                table.add_column("cursor", justify="right", style="cyan bold", width=2)
                if show_checkbox:
                    table.add_column("select", justify="center", width=4)
                table.add_column("icon", justify="center", width=2)
                table.add_column("num", justify="right", style="cyan bold", width=4)
                table.add_column("title")
                table.add_column("info", style="dim")

                for i, it in enumerate(visible_slice):
                    actual_i = scroll_offset + i
                    cursor = "▶" if actual_i == selected_idx else " "
                    if delete_mode:
                        is_selected = it.id in selected_delete_ids
                    else:
                        is_selected = it.id in selected_item_ids
                    chk_text = (
                        Text("[x]", style="bold cyan")
                        if is_selected
                        else Text("[ ]", style="dim")
                    )
                    icon = (
                        Text("✓", style="bold green")
                        if it.completed
                        else Text("○", style="bright_yellow")
                    )
                    title_style = "green" if it.completed else "bold white"

                    info_text = Text()
                    if it.id in scheduled_item_ids:
                        info_text.append("[Today] ", style="bold cyan")
                    if (
                        "duration_formatted" in it.metadata
                        and it.metadata["duration_formatted"]
                    ):
                        info_text.append(
                            f"({it.metadata['duration_formatted']})", style="dim green"
                        )

                    row_parts = [cursor]
                    if show_checkbox:
                        row_parts.append(chk_text)
                    row_parts.extend([
                        icon,
                        f"{actual_i + 1}.",
                        Text(it.title, style=title_style),
                        info_text,
                    ])
                    table.add_row(*row_parts)

                console.print(table)
                console.print()

            if delete_mode:
                hints = [
                    ("↑/↓", "Navigate"),
                    ("Space", "Select"),
                    ("Enter", f"Confirm delete ({len(selected_delete_ids)})" if selected_delete_ids else "Confirm delete"),
                    ("Esc", "Cancel"),
                ]
            elif search.is_active:
                hints = [
                    ("↑/↓", "Navigate"),
                    ("Enter", "Done"),
                    ("Esc", "Cancel / Clear"),
                ]
            elif select_item_mode:
                hints = [
                    ("↑/↓", "Navigate"),
                    ("Enter", "Select for chunking"),
                    ("/", "Search"),
                    ("b", "Back"),
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
                    ("Esc", "Clear search"),
                    ("n", "Next"),
                    ("t", "Toggle"),
                    ("r", "Rescan"),
                    ("d", "Delete"),
                    ("b", "Back"),
                ]
            else:
                hints = [
                    ("↑/↓", "Navigate"),
                    ("Space", "Select"),
                    ("Enter", "Add to Today"),
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

                if k == Key.UP and active_items:
                    selected_idx = max(0, selected_idx - 1)
                    continue

                if k == Key.DOWN and active_items:
                    selected_idx = min(len(active_items) - 1, selected_idx + 1)
                    continue

                if k in (Key.SPACE, "space", " ") and active_items:
                    target_item = active_items[selected_idx]
                    if target_item.id in selected_delete_ids:
                        selected_delete_ids.remove(target_item.id)
                    else:
                        selected_delete_ids.add(target_item.id)
                    continue

                if k in (Key.ENTER, "enter"):
                    if not selected_delete_ids:
                        continue
                    clear_viewport()
                    render_header("DELETE LESSONS", subtitle=existing_source.name)
                    console.print(
                        f"Delete [bold white]{len(selected_delete_ids)} lessons[/bold white] from this channel?\n"
                    )
                    console.print(
                        "[yellow]This will remove the lessons from Todo.[/yellow]"
                    )
                    console.print(
                        "[bold green]Telegram messages remain untouched.[/bold green]\n"
                    )
                    render_footer([("y", "Yes, delete"), ("n", "Cancel")])
                    k_conf = key_reader.read_key()
                    if k_conf.lower() in ("y", "yes", Key.ENTER, "enter"):
                        self.source_service.delete_file_items_bulk(list(selected_delete_ids))
                    selected_delete_ids.clear()
                    delete_mode = False
                    continue

                # Block all other keys in delete mode
                continue

            handled, selected_idx = search.handle_key(k, len(active_items), selected_idx)
            if handled:
                continue

            if k in (Key.ESCAPE, "escape"):
                if selected_item_ids:
                    selected_item_ids.clear()
                    continue
                return None

            if k in ("b", "cancel", "q", "quit"):
                return None

            if k == Key.UP and active_items:
                selected_idx = max(0, selected_idx - 1)
                continue

            if k == Key.DOWN and active_items:
                selected_idx = min(len(active_items) - 1, selected_idx + 1)
                continue

            # Space: toggle selection
            if k in (Key.SPACE, "space", " ") and active_items:
                target_item = active_items[selected_idx]
                if target_item.id in selected_item_ids:
                    selected_item_ids.remove(target_item.id)
                else:
                    selected_item_ids.add(target_item.id)
                continue

            # Enter: schedule selected lessons into Today (batch scheduling)
            if k in (Key.ENTER, "enter"):
                if not active_items and not selected_item_ids:
                    continue

                if select_item_mode and active_items:
                    return active_items[selected_idx]

                if selected_item_ids:
                    items_to_add = [it for it in items if it.id in selected_item_ids]
                else:
                    items_to_add = [active_items[selected_idx]]

                for it in items_to_add:
                    task = self.task_service.get_or_create_telegram_task(
                        it, source_name=existing_source.name
                    )
                    self.daily_service.add_task_to_date(task.id, today_str)

                selected_item_ids.clear()
                # SILENT! Return back to Today
                return "added"

            # n: Next unfinished lesson with wrap-around
            if k == "n" and active_items:
                unfinished_indices = [
                    i for i, it in enumerate(active_items) if not it.completed
                ]
                if unfinished_indices:
                    next_idx = next(
                        (i for i in unfinished_indices if i > selected_idx), None
                    )
                    if next_idx is None:
                        next_idx = unfinished_indices[0]
                    selected_idx = next_idx
                continue

            # t: Toggle completion locally in SQLite
            if k in ("t", "toggle") and active_items:
                sel_item = active_items[selected_idx]
                self.source_service.toggle_item_completion(sel_item.id)
                # SILENT!
                continue

            # r: Rescan channel messages from Telegram
            if k in ("r", "rescan"):
                with console.status(
                    f"[bold cyan]Rescanning lessons for {channel_title}...[/bold cyan]",
                    spinner="dots",
                ):
                    try:
                        self.adapter.connect()
                        scan_res = self.adapter.scan_channel(channel_id)
                        existing_source, _, _ = self.source_service.add_or_update_source(
                            scan_res
                        )
                        # SILENT on success!
                    except Exception as e:
                        status_msg = f"Rescan failed: {e}"
                continue

            # d: Enter delete selection mode
            if k in ("d", "delete") and active_items:
                delete_mode = True
                selected_delete_ids.clear()
                continue

            # Numeric selection
            if k.isdigit() and active_items:
                sel_num = int(k)
                if 1 <= sel_num <= len(active_items):
                    selected_item = active_items[sel_num - 1]
                    if select_item_mode:
                        return selected_item
                    task = self.task_service.get_or_create_telegram_task(
                        selected_item, source_name=existing_source.name
                    )
                    self.daily_service.add_task_to_date(task.id, today_str)
                    selected_item_ids.clear()
                    # SILENT!
                    return "added"

    # Alias for parity with FilemetaView
    handle_source_items = handle_channel_lessons
