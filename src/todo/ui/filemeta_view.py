"""Filemeta workflow views: sources list, folder importer, course details, and completion tracking."""

from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

from rich.table import Table
from rich.text import Text

from todo.integrations.filemeta import (
    FilemetaAdapter,
    FilemetaError,
    FilemetaExecutionError,
    FilemetaNotFoundError,
    FilemetaParseError,
    FolderNotFoundError,
    InvalidFolderError,
    PermissionDeniedError,
)
from todo.models import FileItem, Source
from todo.services.daily_service import DailyService
from todo.services.source_service import SourceService
from todo.services.task_service import TaskService
from todo.ui.console import (
    clear_viewport,
    console,
    print_status_message,
    prompt_input,
    render_footer,
    render_header,
    render_progress_bar,
)


class FilemetaView:
    def __init__(
        self,
        source_service: SourceService,
        task_service: TaskService,
        daily_service: DailyService,
        filemeta_adapter: FilemetaAdapter,
    ):
        self.source_service = source_service
        self.task_service = task_service
        self.daily_service = daily_service
        self.filemeta_adapter = filemeta_adapter

    def run(self) -> Optional[str]:
        """
        Main loop for Filemeta sources in full-screen TUI.
        Returns a status message to display on the Today view if a task was added.
        """
        status_msg: Optional[str] = None
        status_style: str = "green"

        while True:
            clear_viewport()
            sources = self.source_service.get_sources()
            render_header("FILEMETA SOURCES")

            if status_msg:
                print_status_message(status_msg, style=status_style)
                status_msg = None
                status_style = "green"

            if not sources:
                console.print("  [dim italic]No Filemeta sources imported yet.[/dim italic]\n")
                hints = [("+", "Add folder"), ("b", "Back")]
                render_footer(hints)
            else:
                table = Table.grid(padding=(0, 1))
                table.add_column("num", justify="right", style="cyan bold", width=4)
                table.add_column("name", style="bold white", width=32)
                table.add_column("progress", style="cyan")

                for idx, src in enumerate(sources, start=1):
                    pct_str = f"{src.progress_pct}% ({src.completed_count}/{src.item_count})"
                    table.add_row(f"{idx}.", src.name, Text(pct_str, style="dim cyan"))
                    table.add_row("", Text(f"   {src.path}", style="dim"), "")

                console.print(table)
                console.print()

                hints = [
                    ("1-" + str(len(sources)), "Open course"),
                    ("+", "Add folder"),
                    ("d", "Delete source"),
                    ("b", "Back"),
                ]
                render_footer(hints)

            hint = (
                f"Select source [1-{len(sources)}, +, d, b]: "
                if sources
                else "Option [+, b]: "
            )
            choice = prompt_input(hint)

            if choice.lower() in ("b", "back", "q", "quit", "cancel"):
                return None

            if choice in ("+", "add"):
                res_msg, res_src = self.handle_add_folder()
                if res_src:
                    item_msg = self.handle_source_items(res_src)
                    if item_msg:
                        return item_msg
                elif res_msg:
                    status_msg = res_msg
                    status_style = "yellow"
                continue

            if choice.lower() in ("d", "delete"):
                del_msg = self.handle_delete_source(sources)
                if del_msg:
                    status_msg = del_msg
                    status_style = "yellow"
                continue

            if choice.isdigit() and sources:
                idx = int(choice)
                if 1 <= idx <= len(sources):
                    selected_source = sources[idx - 1]
                    item_msg = self.handle_source_items(selected_source)
                    if item_msg:
                        return item_msg
                else:
                    status_msg = f"Invalid selection: #{choice}. Please select 1-{len(sources)}."
                    status_style = "red"
            else:
                status_msg = "Please enter a valid option."
                status_style = "yellow"

    def handle_add_folder(self) -> Tuple[Optional[str], Optional[Source]]:
        """Prompt for folder path and scan with filemeta."""
        clear_viewport()
        render_header("ADD FILEMETA SOURCE")
        console.print("Enter folder path (or 'b' to cancel):\n")
        path_input = prompt_input("Folder: > ")

        if not path_input or path_input.lower() in ("b", "back", "cancel"):
            return None, None

        with console.status("[bold cyan]Scanning folder with filemeta...[/bold cyan]", spinner="dots"):
            try:
                scan_result = self.filemeta_adapter.scan_folder(path_input)
            except FilemetaNotFoundError as e:
                return f"Error: {e}", None
            except FolderNotFoundError as e:
                return f"Error: {e}", None
            except InvalidFolderError as e:
                return f"Error: {e}", None
            except PermissionDeniedError as e:
                return f"Error: {e}", None
            except FilemetaExecutionError as e:
                return f"Filemeta error: {e}", None
            except FilemetaParseError as e:
                return f"Parsing error: {e}", None
            except Exception as e:
                return f"Unexpected error scanning folder: {e}", None

        # Upsert source and items into DB
        source, added_count, updated_count = self.source_service.add_or_update_source(scan_result)
        return None, source

    def handle_delete_source(self, sources: List[Source]) -> Optional[str]:
        """Prompt to delete an imported Filemeta source."""
        if not sources:
            return "No sources to delete."

        clear_viewport()
        render_header("DELETE FILEMETA SOURCE")
        console.print("Select source to delete:\n")

        for idx, src in enumerate(sources, start=1):
            console.print(f"  [bold cyan]{idx}.[/bold cyan] [bold white]{src.name}[/bold white]")
            console.print(f"     [dim]{src.path}[/dim]")

        console.print("\n  [bold cyan]\\[b][/bold cyan] Cancel\n")
        choice = prompt_input(f"Select source [1-{len(sources)}, b]: ")

        if choice.lower() in ("b", "back", "cancel"):
            return None

        if not choice.isdigit() or not (1 <= int(choice) <= len(sources)):
            return "Invalid source selection."

        selected_source = sources[int(choice) - 1]

        clear_viewport()
        render_header("DELETE FILEMETA SOURCE", subtitle=selected_source.name)
        console.print(f"Source: [bold white]{selected_source.name}[/bold white]")
        console.print(f"Path:   [dim]{selected_source.path}[/dim]\n")
        console.print("[yellow]This will remove the imported source and cached metadata from Todo.[/yellow]")
        console.print("[bold green]It will NOT delete anything from the actual filesystem.[/bold green]\n")

        confirm = prompt_input("Confirm delete? [y/n]: ")
        if confirm.lower() in ("y", "yes"):
            self.source_service.delete_source(selected_source.id)
            return f"Deleted source \"{selected_source.name}\". Real files remain untouched."

        return "Deletion cancelled."

    def handle_source_items(self, source: Source) -> Optional[str]:
        """Course details view: displays progress, item status, completion toggles, and task addition."""
        page_size = 20
        current_page = 0
        status_msg: Optional[str] = None
        status_style: str = "green"

        while True:
            clear_viewport()
            items = self.source_service.get_items_by_source(source.id)
            if not items:
                render_header(source.name, subtitle=source.path)
                print_status_message("No items found in this source.", "yellow")
                prompt_input("Press Enter to go back...")
                return None

            completed_cnt, total_cnt, pct = self.source_service.get_source_progress(source.id)
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
            console.print(f"  Progress: [bold cyan]{progress_bar}[/bold cyan] [dim]({completed_cnt}/{total_cnt} completed)[/dim]\n")

            start_idx = current_page * page_size
            end_idx = min(start_idx + page_size, len(items))
            page_items = items[start_idx:end_idx]

            table = Table.grid(padding=(0, 1))
            table.add_column("num", justify="right", style="cyan bold", width=4)
            table.add_column("icon", justify="center", width=3)
            table.add_column("title")
            table.add_column("info", style="dim")

            for i, it in enumerate(page_items, start=start_idx + 1):
                icon = Text("✓", style="bold green") if it.completed else Text("○", style="bright_yellow")
                title_style = "dim strike" if it.completed else "bold white"

                info_parts = []
                if "duration_formatted" in it.metadata and it.metadata["duration_formatted"]:
                    info_parts.append(f"({it.metadata['duration_formatted']})")
                elif "pages" in it.metadata and it.metadata["pages"]:
                    info_parts.append(f"({it.metadata['pages']} pages)")

                info_str = " ".join(info_parts)
                table.add_row(f"{i}.", icon, Text(it.title, style=title_style), Text(info_str, style="dim green"))

            console.print(table)
            console.print()

            hints = [
                ("1-" + str(len(items)), "Add to Today"),
                ("t", "Toggle complete"),
                ("r", "Rescan"),
                ("b", "Back"),
            ]
            if total_pages > 1:
                if current_page < total_pages - 1:
                    hints.insert(0, ("n", "Next page"))
                if current_page > 0:
                    hints.insert(0, ("p", "Prev page"))

            render_footer(hints)

            prompt_hint = f"Select [1-{len(items)}, t, r, b]: "
            choice = prompt_input(prompt_hint)

            if choice.lower() in ("b", "back", "q", "quit", "cancel"):
                return None

            if choice.lower() in ("n", "next") and current_page < total_pages - 1:
                current_page += 1
                continue

            if choice.lower() in ("p", "prev") and current_page > 0:
                current_page -= 1
                continue

            # Toggle completion directly from Filemeta view
            if choice.lower() in ("t", "toggle"):
                t_choice = prompt_input(f"Item number to toggle [1-{len(items)}, b]: ")
                if t_choice.isdigit() and 1 <= int(t_choice) <= len(items):
                    sel_item = items[int(t_choice) - 1]
                    updated = self.source_service.toggle_item_completion(sel_item.id)
                    if updated and updated.completed:
                        status_msg = f"✓ Completed \"{updated.title}\"."
                        status_style = "green"
                    elif updated:
                        status_msg = f"○ Marked pending: \"{updated.title}\"."
                        status_style = "yellow"
                continue

            # Rescan folder
            if choice.lower() in ("r", "rescan"):
                with console.status("[bold cyan]Rescanning folder...[/bold cyan]", spinner="dots"):
                    try:
                        scan_res = self.filemeta_adapter.scan_folder(source.path)
                        _, added_cnt, upd_cnt = self.source_service.add_or_update_source(scan_res)
                        status_msg = f"Rescanned: {upd_cnt} updated, {added_cnt} added. Completion state preserved."
                        status_style = "green"
                    except Exception as e:
                        status_msg = f"Rescan failed: {e}"
                        status_style = "red"
                continue

            if choice.isdigit():
                sel_num = int(choice)
                if 1 <= sel_num <= len(items):
                    selected_item = items[sel_num - 1]
                    # Create or get task entity
                    task = self.task_service.get_or_create_filemeta_task(
                        selected_item, source_name=source.name
                    )
                    today_str = datetime.now().date().isoformat()
                    _, is_new = self.daily_service.add_task_to_date(task.id, today_str)

                    if is_new:
                        return f"Added \"{task.title}\" to today's tasks!"
                    else:
                        return f"\"{task.title}\" is already scheduled for today!"
                else:
                    status_msg = f"Invalid selection: #{choice}. Please select 1-{len(items)}."
                    status_style = "red"
            else:
                status_msg = "Please enter a valid item number, 't' to toggle, 'r' to rescan, or 'b' to go back."
                status_style = "yellow"
