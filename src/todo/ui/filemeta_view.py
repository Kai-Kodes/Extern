"""Filemeta workflow views: sources list, folder importer, and item selector."""

from datetime import datetime
from pathlib import Path
from typing import List, Optional

from rich import box
from rich.panel import Panel
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
from todo.ui.console import console, print_status_message, prompt_input


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
        Main loop for Filemeta sources.
        Returns a status message to display on the Today view if a task was added,
        or None if just returning back.
        """
        status_msg: Optional[str] = None
        status_style: str = "green"

        while True:
            sources = self.source_service.get_sources()

            title_text = Text("FILEMETA SOURCES", style="bold bright_white", justify="center")
            console.print(
                Panel(
                    title_text,
                    box=box.ROUNDED,
                    border_style="cyan",
                    padding=(0, 2),
                )
            )

            if status_msg:
                print_status_message(status_msg, style=status_style)
                status_msg = None
                status_style = "green"

            if not sources:
                console.print("  [dim italic]No Filemeta sources imported yet.[/dim italic]\n")
                console.print("  [bold cyan]\\[+][/bold cyan] Add folder")
                console.print("  [bold cyan]\\[b][/bold cyan] Back\n")
            else:
                for idx, src in enumerate(sources, start=1):
                    count_label = f"({src.item_count} items)" if src.item_count != 1 else "(1 item)"
                    console.print(f"  [bold cyan]{idx}.[/bold cyan] [bold white]{src.name}[/bold white] [dim]{count_label}[/dim]")
                console.print()
                console.print("  [bold cyan]\\[+][/bold cyan] Add folder")
                console.print("  [bold cyan]\\[b][/bold cyan] Back\n")

            choice = prompt_input("Select source [+, b, or number]: ")
            if choice.lower() in ("b", "back", "q", "quit"):
                return None

            if choice in ("+", "add"):
                res_msg, res_src = self.handle_add_folder()
                if res_src:
                    # Directly navigate into the imported source
                    item_msg = self.handle_source_items(res_src)
                    if item_msg:
                        return item_msg
                elif res_msg:
                    status_msg = res_msg
                    status_style = "yellow"
                continue

            if choice.isdigit():
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
                status_msg = "Invalid input. Please enter a number, '+' to add folder, or 'b' to go back."
                status_style = "yellow"

    def handle_add_folder(self) -> tuple[Optional[str], Optional[Source]]:
        """Prompt for folder path and scan with filemeta."""
        title_text = Text("ADD FILEMETA SOURCE", style="bold bright_white", justify="center")
        console.print(
            Panel(
                title_text,
                box=box.ROUNDED,
                border_style="cyan",
                padding=(0, 2),
            )
        )
        console.print("Enter folder path (or 'b' to cancel):")
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

        if updated_count > 0 and added_count == 0:
            console.print(
                f"\n[green]✓ Source '{source.name}' updated ({updated_count} items rescanned).[/green]\n"
            )
        else:
            console.print(
                f"\n[green]✓ Source '{source.name}' imported ({source.item_count} items found).[/green]\n"
            )

        return None, source

    def handle_source_items(self, source: Source) -> Optional[str]:
        """Show items for a selected source and allow adding one to today."""
        items = self.source_service.get_items_by_source(source.id)
        if not items:
            print_status_message(f"No items found in source '{source.name}'.", "yellow")
            prompt_input("Press Enter to continue...")
            return None

        page_size = 20
        current_page = 0
        total_pages = (len(items) + page_size - 1) // page_size
        status_msg: Optional[str] = None

        while True:
            title_text = Text(source.name.upper(), style="bold bright_white", justify="center")
            subtitle = f"Path: {source.path} · {len(items)} items"
            if total_pages > 1:
                subtitle += f" (Page {current_page + 1}/{total_pages})"

            console.print(
                Panel(
                    title_text,
                    box=box.ROUNDED,
                    border_style="cyan",
                    subtitle=f"[dim]{subtitle}[/dim]",
                    padding=(0, 2),
                )
            )

            if status_msg:
                print_status_message(status_msg, "yellow")
                status_msg = None

            start_idx = current_page * page_size
            end_idx = min(start_idx + page_size, len(items))
            page_items = items[start_idx:end_idx]

            table = Table.grid(padding=(0, 1))
            table.add_column("num", justify="right", style="cyan bold")
            table.add_column("title")
            table.add_column("info", style="dim")

            for i, it in enumerate(page_items, start=start_idx + 1):
                # Extra metadata info (duration, pages, size)
                info_parts = []
                if "duration_formatted" in it.metadata and it.metadata["duration_formatted"]:
                    info_parts.append(f"({it.metadata['duration_formatted']})")
                elif "pages" in it.metadata and it.metadata["pages"]:
                    info_parts.append(f"({it.metadata['pages']} pages)")

                info_str = " ".join(info_parts)
                table.add_row(f"{i}.", Text(it.title, style="white"), Text(info_str, style="dim green"))

            console.print(table)
            console.print()

            nav_options = []
            if total_pages > 1:
                if current_page < total_pages - 1:
                    nav_options.append("[bold cyan]\\[n][/bold cyan] Next page")
                if current_page > 0:
                    nav_options.append("[bold cyan]\\[p][/bold cyan] Prev page")
            nav_options.append("[bold cyan]\\[b][/bold cyan] Back")

            console.print("  " + "   ".join(nav_options) + "\n")

            prompt_hint = f"Select item [1-{len(items)}, b]: "
            choice = prompt_input(prompt_hint)

            if choice.lower() in ("b", "back", "q", "quit"):
                return None

            if choice.lower() in ("n", "next") and current_page < total_pages - 1:
                current_page += 1
                continue

            if choice.lower() in ("p", "prev") and current_page > 0:
                current_page -= 1
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
            else:
                status_msg = "Please enter an item number, or 'b' to go back."
