"""Filemeta workflow views with centralized keyboard input, fuzzy search, multi-select, and course progress."""

from datetime import datetime
from typing import List, Optional, Tuple

from rich.table import Table
from rich.text import Text

from todo.models import FileItem, Source
from todo.search import fuzzy_search
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
    ):
        self.source_service = source_service
        self.task_service = task_service
        self.daily_service = daily_service
        self.filemeta_adapter = filemeta_adapter

    def run(self) -> Optional[str]:
        """Main loop for Filemeta sources in full-screen TUI."""
        status_msg: Optional[str] = None
        status_style: str = "red"
        selected_idx = 0

        while True:
            clear_viewport()
            sources = self.source_service.get_sources(source_type="filemeta")
            render_header("FILEMETA SOURCES")

            if status_msg:
                print_status_message(status_msg, style=status_style)
                status_msg = None
                status_style = "red"

            if not sources:
                console.print(
                    "  [dim italic]No Filemeta sources imported yet.[/dim italic]\n"
                )
                hints = [("+", "Add folder"), ("b", "Back")]
                render_footer(hints)
            else:
                if selected_idx >= len(sources):
                    selected_idx = max(0, len(sources) - 1)

                table = Table.grid(padding=(0, 1))
                table.add_column("cursor", justify="right", style="cyan bold", width=2)
                table.add_column("num", justify="right", style="cyan bold", width=4)
                table.add_column("name", style="bold white", width=32)
                table.add_column("progress", style="cyan")

                for idx, src in enumerate(sources):
                    cursor = "▶" if idx == selected_idx else " "
                    pct_str = (
                        f"{src.progress_pct}% ({src.completed_count}/{src.item_count})"
                    )
                    table.add_row(
                        cursor, f"{idx + 1}.", src.name, Text(pct_str, style="dim cyan")
                    )
                    table.add_row("", "", Text(f"   {src.path}", style="dim"), "")

                console.print(table)
                console.print()

                hints = [
                    ("↑/↓", "Navigate"),
                    ("Enter", "Open course"),
                    ("+", "Add folder"),
                    ("d", "Delete source"),
                    ("b", "Back"),
                ]
                render_footer(hints)

            k = key_reader.read_key()

            if k in (Key.ESCAPE, "b", "q", "cancel"):
                return None

            if k == Key.UP and sources:
                selected_idx = max(0, selected_idx - 1)
                continue

            if k == Key.DOWN and sources:
                selected_idx = min(len(sources) - 1, selected_idx + 1)
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

            if k in ("d", "delete") and sources:
                del_err = self.handle_delete_source(sources, current_idx=selected_idx)
                if del_err:
                    status_msg = del_err
                    status_style = "red"
                continue

            if (k in (Key.ENTER, "enter")) and sources:
                selected_source = sources[selected_idx]
                item_ret = self.handle_source_items(selected_source)
                if item_ret:
                    return item_ret
                continue

            if k.isdigit() and sources:
                idx = int(k)
                if 1 <= idx <= len(sources):
                    selected_source = sources[idx - 1]
                    item_ret = self.handle_source_items(selected_source)
                    if item_ret:
                        return item_ret
                else:
                    status_msg = f"Invalid selection: #{k}."
                    status_style = "red"

    def handle_add_folder(self) -> Tuple[Optional[str], Optional[Source]]:
        """Prompt for folder path and scan with filemeta."""
        clear_viewport()
        render_header("ADD FILEMETA SOURCE")
        console.print("Enter folder path (or 'b' to cancel):\n")
        path_input = key_reader.read_text_input("Folder: > ")

        if not path_input or path_input.lower() in ("b", "back", "cancel"):
            return None, None

        with console.status(
            "[bold cyan]Scanning folder with filemeta...[/bold cyan]", spinner="dots"
        ):
            try:
                scan_result = self.filemeta_adapter.scan_folder(path_input)
            except Exception as e:
                return f"Error scanning folder: {e}", None

        source, _, _ = self.source_service.add_or_update_source(scan_result)
        return None, source

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
        """Course details view: displays progress, item status, search, multi-select, and task addition."""
        selected_idx = 0
        search_mode = False
        search_query = ""
        selected_item_ids: set[int] = set()
        status_msg: Optional[str] = None
        status_style: str = "red"

        while True:
            clear_viewport()
            items = self.source_service.get_items_by_source(source.id)
            if not items:
                render_header(source.name, subtitle=source.path)
                print_status_message("No items found in this source.", "yellow")
                render_footer([("b", "Back")])
                key_reader.read_key()
                return None

            completed_cnt, total_cnt, pct = self.source_service.get_source_progress(
                source.id
            )

            # Boxed horizontal header matching Part E & F:
            # COPYWRITING COURSE                         42% · 5/12
            render_today_header(source.name, completed=completed_cnt, total=total_cnt)

            if status_msg:
                print_status_message(status_msg, style=status_style)
                status_msg = None
                status_style = "red"

            # Filter items if search is active
            if search_mode and search_query:
                console.print(f"  Search: [bold cyan]{search_query}[/bold cyan]\n")
                active_items = fuzzy_search(items, search_query)
            else:
                active_items = items

            if not active_items:
                console.print("  [dim italic]No matching items found.[/dim italic]\n")
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
                    visible_slice = active_items[
                        scroll_offset : scroll_offset + window_size
                    ]
                else:
                    scroll_offset = 0
                    visible_slice = active_items

                table = Table.grid(padding=(0, 1))
                table.add_column("cursor", justify="right", style="cyan bold", width=2)
                table.add_column("select", justify="center", width=4)
                table.add_column("icon", justify="center", width=2)
                table.add_column("num", justify="right", style="cyan bold", width=4)
                table.add_column("title")
                table.add_column("info", style="dim")

            today_str = datetime.now().date().isoformat()
            today_tasks = self.daily_service.get_tasks_for_date(today_str)
            scheduled_item_ids = {
                t.task.source_id
                for t in today_tasks
                if t.task.source_type in ("filemeta", "telegram") and t.task.source_id is not None
            }

            for i, it in enumerate(visible_slice):
                actual_i = scroll_offset + i
                cursor = "▶" if actual_i == selected_idx else " "
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
                    info_text.append(f"({it.metadata['duration_formatted']})", style="dim green")
                elif "pages" in it.metadata and it.metadata["pages"]:
                    info_text.append(f"({it.metadata['pages']} pages)", style="dim green")

                table.add_row(
                    cursor,
                    chk_text,
                    icon,
                    f"{actual_i + 1}.",
                    Text(it.title, style=title_style),
                    info_text,
                )

                console.print(table)
                console.print()

            if search_mode:
                hints = [
                    ("↑/↓", "Navigate"),
                    ("Space", "Select"),
                    ("Enter", "Add"),
                    ("/", "Search"),
                    ("n", "Next"),
                    ("Esc", "Exit Search"),
                ]
            else:
                hints = [
                    ("↑/↓", "Navigate"),
                    ("Space", "Select"),
                    ("Enter", "Add"),
                    ("/", "Search"),
                    ("n", "Next"),
                    ("t", "Toggle"),
                    ("r", "Rescan"),
                    ("d", "Delete"),
                    ("b", "Back"),
                ]
            render_footer(hints)

            k = key_reader.read_key()

            if k in (Key.ESCAPE, "escape"):
                if search_mode:
                    search_mode = False
                    search_query = ""
                    continue
                return None

            if k in ("b", "cancel"):
                if search_mode:
                    search_mode = False
                    search_query = ""
                    continue
                return None

            if k in ("q", "quit"):
                return None

            if k == Key.UP and active_items:
                selected_idx = max(0, selected_idx - 1)
                continue

            if k == Key.DOWN and active_items:
                selected_idx = min(len(active_items) - 1, selected_idx + 1)
                continue

            # Space: multi-select toggle
            if k in (Key.SPACE, "space", " ") and active_items:
                target_item = active_items[selected_idx]
                if target_item.id in selected_item_ids:
                    selected_item_ids.remove(target_item.id)
                else:
                    selected_item_ids.add(target_item.id)
                continue

            # Enter: add selected items (or highlighted item) to Today
            if k in (Key.ENTER, "enter"):
                if not active_items and not selected_item_ids:
                    continue

                today_str = datetime.now().date().isoformat()
                if selected_item_ids:
                    items_to_add = [it for it in items if it.id in selected_item_ids]
                else:
                    items_to_add = [active_items[selected_idx]]

                for it in items_to_add:
                    task = self.task_service.get_or_create_filemeta_task(
                        it, source_name=source.name
                    )
                    self.daily_service.add_task_to_date(task.id, today_str)

                selected_item_ids.clear()
                # SILENT! Return back to Today
                return "added"

            # n: Next unfinished item with wrap-around
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
                # If all items complete: do nothing, silent!
                continue

            # /: Search mode
            if k == "/":
                clear_viewport()
                render_today_header(
                    source.name, completed=completed_cnt, total=total_cnt
                )
                console.print("Enter search term (or 'Esc'/empty to clear):\n")
                q_input = key_reader.read_text_input("Search: > ")
                if q_input:
                    search_query = q_input
                    search_mode = True
                    selected_idx = 0
                else:
                    search_query = ""
                    search_mode = False
                continue

            # t: Toggle completion directly from Filemeta view
            if k in ("t", "toggle") and active_items:
                sel_item = active_items[selected_idx]
                self.source_service.toggle_item_completion(sel_item.id)
                # SILENT!
                continue

            # r: Rescan folder
            if k in ("r", "rescan"):
                with console.status(
                    "[bold cyan]Rescanning folder...[/bold cyan]", spinner="dots"
                ):
                    try:
                        scan_res = self.filemeta_adapter.scan_folder(source.path)
                        self.source_service.add_or_update_source(scan_res)
                        # SILENT!
                    except Exception as e:
                        status_msg = f"Rescan failed: {e}"
                        status_style = "red"
                continue

            # d: Delete source
            if k in ("d", "delete"):
                clear_viewport()
                render_header("DELETE FILEMETA SOURCE", subtitle=source.name)
                console.print(f"Source: [bold white]{source.name}[/bold white]")
                console.print(f"Path:   [dim]{source.path}[/dim]\n")
                console.print(
                    "[yellow]This will remove the imported source and cached metadata from Todo.[/yellow]"
                )
                console.print(
                    "[bold green]It will NOT delete anything from the actual filesystem.[/bold green]\n"
                )
                render_footer([("y", "Yes, delete"), ("n", "Cancel")])
                k_conf = key_reader.read_key()
                if k_conf.lower() in ("y", "yes", Key.ENTER, "enter"):
                    self.source_service.delete_source(source.id)
                    # SILENT!
                    return None
                continue

            # Numeric selection
            if k.isdigit() and active_items:
                sel_num = int(k)
                if 1 <= sel_num <= len(active_items):
                    selected_item = active_items[sel_num - 1]
                    task = self.task_service.get_or_create_filemeta_task(
                        selected_item, source_name=source.name
                    )
                    today_str = datetime.now().date().isoformat()
                    self.daily_service.add_task_to_date(task.id, today_str)
                    selected_item_ids.clear()
                    # SILENT!
                    return "added"
