"""Chunky Task view: turns portions of large imported resources into actionable tasks."""

from datetime import datetime
from typing import Any, List, Optional, Tuple

from rich.table import Table
from rich.text import Text

from todo.models import ChunkyResource, ChunkyTask, FileItem, Source
from todo.services.chunky_service import (
    ChunkyService,
    calculate_coverage,
    format_human_duration,
    format_range,
    format_remaining,
    propose_next_chunk,
)
from todo.services.daily_service import DailyService
from todo.services.task_service import TaskService
from todo.ui.console import (
    clear_viewport,
    console,
    print_status_message,
    render_footer,
    render_header,
)
from todo.ui.filemeta_view import FilemetaView
from todo.ui.key_reader import Key, key_reader
from todo.ui.telegram_view import TelegramView
from todo.ui.youtube_view import YouTubeView


class ChunkyTaskView:
    def __init__(
        self,
        chunky_service: ChunkyService,
        filemeta_view: FilemetaView,
        youtube_view: YouTubeView,
        telegram_view: TelegramView,
        task_service: TaskService,
        daily_service: DailyService,
    ):
        self.chunky_service = chunky_service
        self.filemeta_view = filemeta_view
        self.youtube_view = youtube_view
        self.telegram_view = telegram_view
        self.task_service = task_service
        self.daily_service = daily_service

    def run(self) -> Optional[str]:
        """
        Main interactive loop for Chunky Tasks.
        Step 1: Category Picker (Filemeta, YouTube, Telegram)
        Step 2: Browse & Select Resource via existing importers
        Step 3: Validate Natural Units & Range
        Step 4: Select Chunk Size
        Step 5: Preview Next Gap & Confirm
        """
        while True:
            # -----------------------------------------------------------------
            # Step 1: Category Picker
            # -----------------------------------------------------------------
            clear_viewport()
            render_header("CHUNKY TASKS", subtitle="Select Source Type")
            console.print("Choose resource type to chunk:\n")
            console.print("  [bold cyan]1.[/bold cyan] Filemeta   [dim](Course files, PDFs, videos)[/dim]")
            console.print("  [bold cyan]2.[/bold cyan] YouTube    [dim](Playlists and videos)[/dim]")
            console.print("  [bold cyan]3.[/bold cyan] Telegram   [dim](Channel lessons)[/dim]\n")
            render_footer([
                ("1-3", "Select source type"),
                ("b", "Back / Cancel"),
            ])

            k = key_reader.read_key()
            if k in (Key.ESCAPE, "escape", "b", "q", "cancel"):
                return None

            selected_tuple: Optional[Tuple[Source, FileItem]] = None

            if k in ("1", "f", "filemeta"):
                res = self.filemeta_view.run(select_item_mode=True)
                if isinstance(res, tuple) and len(res) == 2:
                    selected_tuple = res
            elif k in ("2", "y", "youtube"):
                res = self.youtube_view.run(select_item_mode=True)
                if isinstance(res, tuple) and len(res) == 2:
                    selected_tuple = res
            elif k in ("3", "t", "telegram"):
                res = self.telegram_view.run(select_item_mode=True)
                if isinstance(res, tuple) and len(res) == 2:
                    selected_tuple = res
            else:
                continue

            if not selected_tuple:
                # User pressed back/cancelled in the item browser
                continue

            source, file_item = selected_tuple

            # Process the selected resource
            result = self._process_resource(source, file_item)
            if result == "added":
                return "added"

    def _process_resource(self, source: Source, file_item: FileItem) -> Optional[str]:
        """Validate units, select chunk size, preview proposal, and schedule task."""
        metadata = file_item.metadata or {}
        source_type = source.source_type
        total_range: float = 0.0
        range_unit: str = "seconds"

        # ---------------------------------------------------------------------
        # Step 3: Natural Units & Total Range Validation
        # ---------------------------------------------------------------------
        if source_type == "youtube":
            range_unit = "seconds"
            dur = metadata.get("duration")
            if dur is not None and float(dur) > 0:
                total_range = float(dur)
            else:
                self._show_info_screen(
                    "CANNOT CHUNK YOUTUBE VIDEO",
                    f"'{file_item.title}' does not have duration metadata available.\n"
                    "Chunking requires a valid duration.",
                )
                return None

        elif source_type == "telegram":
            range_unit = "seconds"
            dur = metadata.get("duration")
            if dur is not None and float(dur) > 0:
                total_range = float(dur)
            else:
                self._show_info_screen(
                    "CANNOT CHUNK TELEGRAM LESSON",
                    f"'{file_item.title}' does not have duration metadata available.\n"
                    "Only video or audio lessons with duration can be chunked.",
                )
                return None

        elif source_type == "filemeta":
            pages = metadata.get("pages") or metadata.get("page_count")
            dur = metadata.get("duration")
            if pages is not None and float(pages) > 0:
                range_unit = "pages"
                total_range = float(pages)
            elif dur is not None and float(dur) > 0:
                range_unit = "seconds"
                total_range = float(dur)
            else:
                self._show_info_screen(
                    "CANNOT CHUNK FILEMETA RESOURCE",
                    f"'{file_item.title}' does not have page count or duration metadata.\n"
                    "Chunking requires paginated documents (e.g. PDF/EPUB) or media with duration.",
                )
                return None
        else:
            self._show_info_screen(
                "UNSUPPORTED SOURCE TYPE",
                f"Source type '{source_type}' does not support chunking.",
            )
            return None

        # Determine canonical URI
        canonical_uri = metadata.get("canonical_url") or file_item.path or f"{source_type}://{file_item.id}"

        # Get or create ChunkyResource
        resource = self.chunky_service.get_or_create_resource(
            source_type=source_type,
            canonical_uri=canonical_uri,
            title=file_item.title,
            total_range=total_range,
            range_unit=range_unit,
            file_item_id=file_item.id,
        )

        # Check if already completed
        completed_ranges = self.chunky_service.get_completed_ranges(resource.id)
        is_completed = resource.is_fully_completed or file_item.completed
        covered, pct = calculate_coverage(completed_ranges, resource.total_range, is_completed)

        if is_completed or covered >= resource.total_range:
            self._show_info_screen(
                "RESOURCE FULLY COMPLETED",
                f"'{resource.title}' is already 100% completed.\n"
                "There are no remaining chunks to schedule.",
            )
            return None

        # ---------------------------------------------------------------------
        # Step 4: Chunk Size Selection
        # ---------------------------------------------------------------------
        chunk_size: float = 0.0

        if range_unit == "seconds":
            default_chunk = 1200.0  # 20 minutes
            presets = [
                ("1", "15 minutes", 900.0),
                ("2", "20 minutes [Default]", 1200.0),
                ("3", "30 minutes", 1800.0),
                ("4", "45 minutes", 2700.0),
                ("5", "60 minutes", 3600.0),
                ("6", "Custom duration...", -1.0),
            ]
        else:
            default_chunk = 10.0  # 10 pages
            presets = [
                ("1", "5 pages", 5.0),
                ("2", "10 pages [Default]", 10.0),
                ("3", "15 pages", 15.0),
                ("4", "20 pages", 20.0),
                ("5", "25 pages", 25.0),
                ("6", "Custom page count...", -1.0),
            ]

        remaining = max(0.0, resource.total_range - covered)

        while True:
            clear_viewport()
            render_header("CHUNKY TASK", subtitle="Select Chunk Size")
            console.print(f"Resource:   [bold white]{resource.title}[/bold white]")
            if range_unit == "seconds":
                console.print(
                    f"Total:      [dim]{format_human_duration(resource.total_range)} ({format_range(range_unit, 0, resource.total_range)})[/dim]"
                )
                console.print(
                    f"Completed:  [green]{format_human_duration(covered)}[/green] [dim]({pct}%)[/dim]"
                )
                console.print(
                    f"Remaining:  [bold cyan]{format_human_duration(remaining)}[/bold cyan]\n"
                )
            else:
                total_int = int(round(resource.total_range))
                covered_int = int(round(covered))
                rem_int = int(round(remaining))
                console.print(f"Total:      [dim]{total_int} pages[/dim]")
                console.print(f"Completed:  [green]{covered_int} pages[/green] [dim]({pct}%)[/dim]")
                console.print(f"Remaining:  [bold cyan]{rem_int} pages[/bold cyan]\n")

            console.print("Choose chunk size:\n")

            for key_str, label, val in presets:
                console.print(f"  [bold cyan]{key_str}.[/bold cyan] {label}")
            console.print()

            render_footer([
                ("Enter", "Accept default"),
                ("1-6", "Select size"),
                ("b", "Cancel"),
            ])

            k = key_reader.read_key()
            if k in (Key.ESCAPE, "escape", "b", "q", "cancel"):
                return None

            selected_size: Optional[float] = None
            if k in (Key.ENTER, "enter"):
                selected_size = default_chunk
            else:
                for key_str, label, val in presets:
                    if k == key_str:
                        if val < 0:
                            # Custom size input
                            selected_size = self._prompt_custom_size(range_unit, resource.total_range)
                        else:
                            selected_size = val
                        break

            if selected_size is None or selected_size <= 0:
                continue

            chunk_size = selected_size

            # -----------------------------------------------------------------
            # Step 5: Next Gap Proposal & Preview
            # -----------------------------------------------------------------
            proposed = propose_next_chunk(
                completed_ranges,
                resource.total_range,
                chunk_size,
                resource.is_fully_completed,
            )

            if not proposed:
                size_label = (
                    format_human_duration(chunk_size)
                    if range_unit == "seconds"
                    else f"{int(chunk_size)} pages"
                )
                self._show_info_screen(
                    "NO CHUNK AVAILABLE",
                    f"No uncompleted range remaining for chunk size {size_label}.\n"
                    "Resource coverage is complete.",
                )
                return None

            start_pos, end_pos = proposed
            formatted_range = format_range(range_unit, start_pos, end_pos)
            task_title = f"{resource.title} [{formatted_range}]"
            today_str = self.daily_service.get_today_str()

            preview_action = self._preview_and_confirm(
                resource=resource,
                pct=pct,
                formatted_range=formatted_range,
                task_title=task_title,
                today_str=today_str,
                start_pos=start_pos,
                end_pos=end_pos,
            )

            if preview_action == "confirm":
                task, chunky_task = self.chunky_service.create_chunk_task(
                    resource=resource,
                    start_pos=start_pos,
                    end_pos=end_pos,
                    task_service=self.task_service,
                    task_title=task_title,
                )
                self.daily_service.add_task_to_date(task.id, today_str)
                # Strict global silence: zero success banner!
                return "added"
            elif preview_action == "change_size":
                continue
            else:
                # Cancelled
                return None

    def _prompt_custom_size(self, range_unit: str, total_range: float) -> Optional[float]:
        """Prompt user for custom chunk size in natural units."""
        clear_viewport()
        render_header("CUSTOM CHUNK SIZE")
        if range_unit == "seconds":
            console.print("Enter chunk duration in minutes (e.g. 25):\n")
            val_str = key_reader.read_text_input("Minutes: > ")
            if not val_str or val_str.lower() in ("b", "cancel"):
                return None
            try:
                mins = float(val_str.strip())
                if mins <= 0:
                    return None
                return mins * 60.0
            except ValueError:
                return None
        else:
            console.print("Enter page count (e.g. 15):\n")
            val_str = key_reader.read_text_input("Pages: > ")
            if not val_str or val_str.lower() in ("b", "cancel"):
                return None
            try:
                pages = float(val_str.strip())
                if pages <= 0:
                    return None
                return pages
            except ValueError:
                return None

    def _preview_and_confirm(
        self,
        resource: ChunkyResource,
        pct: int,
        formatted_range: str,
        task_title: str,
        today_str: str,
        start_pos: Optional[float] = None,
        end_pos: Optional[float] = None,
    ) -> str:
        """Display chunky task preview screen and await confirmation."""
        clear_viewport()
        render_header("CHUNKY TASK", subtitle="Preview & Confirm")
        console.print(f"Resource:       [bold white]{resource.title}[/bold white]")
        console.print(f"Source:         [dim]{resource.source_type.capitalize()}[/dim]")
        console.print(f"Progress:       [cyan]{pct}% complete[/cyan]")

        if start_pos is not None and end_pos is not None:
            chunk_len = max(0.0, end_pos - start_pos)
            if resource.range_unit == "seconds":
                chunk_size_str = format_human_duration(chunk_len)
                console.print(
                    f"Proposed Chunk: [bold green]{chunk_size_str}[/bold green] [dim]({formatted_range})[/dim]"
                )
            else:
                chunk_size_str = f"{int(round(chunk_len))} pages"
                console.print(
                    f"Proposed Chunk: [bold green]{chunk_size_str}[/bold green] [dim]({formatted_range})[/dim]"
                )
        else:
            console.print(f"Proposed Chunk: [bold green]{formatted_range}[/bold green]")

        console.print(f"Task Title:     [bold white]{task_title}[/bold white]")
        console.print(f"Scheduled For:  [bold cyan]Today ({today_str})[/bold cyan]\n")

        render_footer([
            ("Enter / y", "Confirm & schedule"),
            ("c", "Change chunk size"),
            ("b", "Cancel"),
        ])

        while True:
            k = key_reader.read_key()
            if k in (Key.ENTER, "enter", "y", "yes"):
                return "confirm"
            elif k in ("c", "change"):
                return "change_size"
            elif k in (Key.ESCAPE, "escape", "b", "q", "cancel"):
                return "cancel"

    def _show_info_screen(self, title: str, message: str) -> None:
        """Display an actionable notice screen and wait for user to dismiss."""
        clear_viewport()
        render_header(title)
        console.print(f"  [yellow]{message}[/yellow]\n")
        console.print("  [dim]Press 'b' or Enter to return.[/dim]\n")
        render_footer([("b / Enter", "Return")])
        while True:
            k = key_reader.read_key()
            if k in (Key.ESCAPE, "escape", "b", "q", "cancel", Key.ENTER, "enter"):
                break
