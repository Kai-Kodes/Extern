"""Filemeta workflow views with centralized keyboard input and course progress."""

from datetime import datetime
from typing import List, Optional, Tuple

from rich.table import Table
from rich.text import Text

from todo.models import FileItem, Source
from todo.services.daily_service import DailyService
from todo.services.source_service import SourceService
from todo.services.task_service import TaskService
from todo.ui.console import (
    clear_viewport,
    console,
    print_status_message,
    render_footer,
    render_header,
    render_progress_bar,
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
        status_style: str = "green"
        selected_idx = 0

        while True:
            clear_viewport()
            sources = self.source_service.get_sources()
            render_header("FILEMETA SOURCES")

            if status_msg:
                print_status_message(status_msg, style=status_style)
                status_msg = None
                status_style = "green"

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
                res_msg, res_src = self.handle_add_folder()
                if res_src:
                    item_msg = self.handle_source_items(res_src)
                    if item_msg:
                        return item_msg
                elif res_msg:
                    status_msg = res_msg
                    status_style = "yellow"
                continue

            if k in ("d", "delete") and sources:
                del_msg = self.handle_delete_source(sources, current_idx=selected_idx)
                if del_msg:
                    status_msg = del_msg
                    status_style = "yellow"
                continue

            if (k == Key.ENTER or k == "enter") and sources:
                selected_source = sources[selected_idx]
                item_msg = self.handle_source_items(selected_source)
                if item_msg:
                    return item_msg
                continue

            if k.isdigit() and sources:
                idx = int(k)
                if 1 <= idx <= len(sources):
                    selected_source = sources[idx - 1]
                    item_msg = self.handle_source_items(selected_source)
                    if item_msg:
                        return item_msg
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

        # Upsert source and items into DB
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
            return f'Deleted source "{selected_source.name}". Real files remain untouched.'

        return "Deletion cancelled."

    def handle_source_items(self, source: Source) -> Optional[str]:
        """Course details view: displays progress, item status, completion toggles, and task addition."""
        page_size = 20
        current_page = 0
        selected_idx = 0
        status_msg: Optional[str] = None
        status_style: str = "green"

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
            total_pages = (len(items) + page_size - 1) // page_size

            subtitle = f"Path: {source.path} · {total_cnt} items"
            if total_pages > 1:
                subtitle += f" (Page {current_page + 1}/{total_pages})"

            render_header(source.name, subtitle=subtitle)

            if status_msg:
                print_status_message(status_msg, style=status_style)
                status_msg = None
                status_style = "green"

            # Progress Bar Section
            progress_bar = render_progress_bar(completed_cnt, total_cnt, width=20)
            console.print(
                f"  Progress: [bold cyan]{progress_bar}[/bold cyan] [dim]({completed_cnt}/{total_cnt} completed)[/dim]\n"
            )

            start_idx = current_page * page_size
            end_idx = min(start_idx + page_size, len(items))
            page_items = items[start_idx:end_idx]

            if selected_idx >= len(page_items):
                selected_idx = max(0, len(page_items) - 1)

            table = Table.grid(padding=(0, 1))
            table.add_column("cursor", justify="right", style="cyan bold", width=2)
            table.add_column("num", justify="right", style="cyan bold", width=4)
            table.add_column("icon", justify="center", width=3)
            table.add_column("title")
            table.add_column("info", style="dim")

            for i, it in enumerate(page_items):
                cursor = "▶" if i == selected_idx else " "
                icon = (
                    Text("✓", style="bold green")
                    if it.completed
                    else Text("○", style="bright_yellow")
                )
                title_style = "dim strike" if it.completed else "bold white"

                info_parts = []
                if (
                    "duration_formatted" in it.metadata
                    and it.metadata["duration_formatted"]
                ):
                    info_parts.append(f"({it.metadata['duration_formatted']})")
                elif "pages" in it.metadata and it.metadata["pages"]:
                    info_parts.append(f"({it.metadata['pages']} pages)")

                info_str = " ".join(info_parts)
                table.add_row(
                    cursor,
                    f"{start_idx + i + 1}.",
                    icon,
                    Text(it.title, style=title_style),
                    Text(info_str, style="dim green"),
                )

            console.print(table)
            console.print()

            hints = [
                ("↑/↓", "Navigate"),
                ("Enter", "Add to Today"),
                ("t", "Toggle complete"),
                ("r", "Rescan"),
                ("b", "Back"),
            ]
            if total_pages > 1:
                hints.append(("n/p", "Next/Prev page"))

            render_footer(hints)

            k = key_reader.read_key()

            if k in (Key.ESCAPE, "b", "q", "cancel"):
                return None

            if k == Key.UP:
                selected_idx = max(0, selected_idx - 1)
                continue

            if k == Key.DOWN:
                selected_idx = min(len(page_items) - 1, selected_idx + 1)
                continue

            if k in ("n", Key.RIGHT) and current_page < total_pages - 1:
                current_page += 1
                selected_idx = 0
                continue

            if k in ("p", Key.LEFT) and current_page > 0:
                current_page -= 1
                selected_idx = 0
                continue

            # Toggle completion directly from Filemeta view
            if k in ("t", "toggle"):
                sel_item = page_items[selected_idx]
                updated = self.source_service.toggle_item_completion(sel_item.id)
                if updated and updated.completed:
                    status_msg = f'✓ Completed "{updated.title}".'
                    status_style = "green"
                elif updated:
                    status_msg = f'○ Marked pending: "{updated.title}".'
                    status_style = "yellow"
                continue

            # Rescan folder
            if k in ("r", "rescan"):
                with console.status(
                    "[bold cyan]Rescanning folder...[/bold cyan]", spinner="dots"
                ):
                    try:
                        scan_res = self.filemeta_adapter.scan_folder(source.path)
                        _, added_cnt, upd_cnt = self.source_service.add_or_update_source(
                            scan_res
                        )
                        status_msg = f"Rescanned: {upd_cnt} updated, {added_cnt} added."
                        status_style = "green"
                    except Exception as e:
                        status_msg = f"Rescan failed: {e}"
                        status_style = "red"
                continue

            if k in (Key.ENTER, "enter"):
                selected_item = page_items[selected_idx]
                # Confirmation modal
                clear_viewport()
                render_header(source.name, subtitle=selected_item.title)
                console.print(
                    f"Add [bold white]\"{selected_item.title}\"[/bold white] to today's tasks?\n"
                )
                render_footer([("y / Enter", "Add to Today"), ("n", "Cancel")])
                conf = key_reader.read_key()
                if conf.lower() in ("y", "yes", Key.ENTER, "enter"):
                    task = self.task_service.get_or_create_filemeta_task(
                        selected_item, source_name=source.name
                    )
                    today_str = datetime.now().date().isoformat()
                    _, is_new = self.daily_service.add_task_to_date(
                        task.id, today_str
                    )
                    if is_new:
                        return f'Added "{task.title}" to today\'s tasks!'
                    else:
                        return f'"{task.title}" is already scheduled for today!'
                continue

            if k.isdigit():
                sel_num = int(k)
                if 1 <= sel_num <= len(items):
                    selected_item = items[sel_num - 1]
                    task = self.task_service.get_or_create_filemeta_task(
                        selected_item, source_name=source.name
                    )
                    today_str = datetime.now().date().isoformat()
                    _, is_new = self.daily_service.add_task_to_date(
                        task.id, today_str
                    )
                    if is_new:
                        return f'Added "{task.title}" to today\'s tasks!'
                    else:
                        return f'"{task.title}" is already scheduled for today!'
