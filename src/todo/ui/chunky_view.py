"""Chunky Task view: turns portions of large imported resources into actionable tasks."""

from datetime import datetime
import math
from typing import Any, Dict, List, Optional, Tuple

from rich import box
from rich.table import Table
from rich.text import Text

from todo.models import ChunkyPreset, ChunkyResource, ChunkyTask, FileItem, Source
from todo.services.chunky_service import (
    ChunkyService,
    calculate_coverage,
    format_human_duration,
    format_range,
    format_remaining,
    get_remaining_intervals,
    propose_entire_remaining_chunk,
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

        while True:
            # Re-read completed ranges and check coverage in case loops repeat
            completed_ranges = self.chunky_service.get_completed_ranges(resource.id)
            is_completed = resource.is_fully_completed or file_item.completed
            covered, pct = calculate_coverage(completed_ranges, resource.total_range, is_completed)
            gaps = get_remaining_intervals(completed_ranges, resource.total_range)

            if is_completed or covered >= resource.total_range or not gaps:
                self._show_info_screen(
                    "RESOURCE FULLY COMPLETED",
                    f"'{resource.title}' is already 100% completed.\n"
                    "There are no remaining chunks to schedule.",
                )
                return None

            remaining = max(0.0, resource.total_range - covered)
            raw_presets = self.chunky_service.list_presets(range_unit=range_unit)
            if not isinstance(raw_presets, list) or not raw_presets:
                try:
                    self.chunky_service.seed_default_presets()
                    raw_presets = self.chunky_service.list_presets(range_unit=range_unit)
                except Exception:
                    pass

            if not isinstance(raw_presets, list) or not raw_presets:
                if range_unit == "seconds":
                    presets = [
                        ChunkyPreset(name="15 minutes", duration=900.0, range_unit="seconds", is_builtin=True),
                        ChunkyPreset(name="20 minutes", duration=1200.0, range_unit="seconds", is_default=True, is_builtin=True),
                        ChunkyPreset(name="30 minutes", duration=1800.0, range_unit="seconds", is_builtin=True),
                        ChunkyPreset(name="45 minutes", duration=2700.0, range_unit="seconds", is_builtin=True),
                        ChunkyPreset(name="60 minutes", duration=3600.0, range_unit="seconds", is_builtin=True),
                    ]
                else:
                    presets = [
                        ChunkyPreset(name="5 pages", duration=5.0, range_unit="pages", is_builtin=True),
                        ChunkyPreset(name="10 pages", duration=10.0, range_unit="pages", is_default=True, is_builtin=True),
                        ChunkyPreset(name="15 pages", duration=15.0, range_unit="pages", is_builtin=True),
                        ChunkyPreset(name="20 pages", duration=20.0, range_unit="pages", is_builtin=True),
                        ChunkyPreset(name="25 pages", duration=25.0, range_unit="pages", is_builtin=True),
                    ]
            else:
                presets = raw_presets

            default_preset = next((p for p in presets if getattr(p, "is_default", False)), presets[0] if presets else None)
            try:
                default_chunk = float(default_preset.duration)
            except (ValueError, TypeError, AttributeError):
                default_chunk = 1200.0 if range_unit == "seconds" else 10.0

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

            console.print("[bold]Presets:[/bold]")
            preset_key_map: Dict[str, ChunkyPreset] = {}
            for idx, p in enumerate(presets[:9], start=1):
                key_str = str(idx)
                preset_key_map[key_str] = p
                def_tag = " [bold green][Default][/bold green]" if p.is_default else ""
                if range_unit == "seconds":
                    dur_fmt = format_human_duration(p.duration)
                    label = p.name if dur_fmt.lower() in p.name.lower() or p.name.endswith("minutes") else f"{p.name} ({dur_fmt})"
                else:
                    dur_fmt = f"{int(round(p.duration))} pages"
                    label = p.name if dur_fmt.lower() in p.name.lower() or p.name.endswith("pages") else f"{p.name} ({dur_fmt})"
                console.print(f"  [bold cyan]{key_str}.[/bold cyan] {label}{def_tag}")

            console.print("\n[bold]Options:[/bold]")
            first_gap_len = gaps[0][1] - gaps[0][0] if gaps else remaining
            if len(gaps) > 1:
                gap_label = format_human_duration(first_gap_len) if range_unit == "seconds" else f"{int(round(first_gap_len))} pages"
                console.print(f"  [bold cyan]r.[/bold cyan] Entire remaining resource [dim]({gap_label} — earliest contiguous gap of {len(gaps)})[/dim]")
            else:
                rem_label = format_human_duration(remaining) if range_unit == "seconds" else f"{int(round(remaining))} pages"
                console.print(f"  [bold cyan]r.[/bold cyan] Entire remaining resource ({rem_label})")

            custom_label = "Custom duration..." if range_unit == "seconds" else "Custom page count..."
            console.print(f"  [bold cyan]c.[/bold cyan] {custom_label}")
            console.print(f"  [bold cyan]p.[/bold cyan] Manage presets...\n")

            default_hint = f"Default ({format_human_duration(default_chunk)})" if range_unit == "seconds" else f"Default ({int(default_chunk)}p)"
            render_footer([
                ("Enter", default_hint),
                (f"1-{len(preset_key_map)}", "Select preset"),
                ("r", "Entire remaining"),
                ("c", "Custom"),
                ("p", "Manage presets"),
                ("b", "Cancel"),
            ])

            k = key_reader.read_key()
            if not k:
                continue
            if k in (Key.ESCAPE, "escape", "b", "q", "cancel"):
                return None

            selected_size: Optional[float] = None
            if k in (Key.ENTER, "enter"):
                selected_size = default_chunk
            elif k.lower() in ("r", "remaining"):
                selected_size = resource.total_range
            elif k.lower() in ("c", "custom"):
                selected_size = self._prompt_custom_size(range_unit, resource.total_range)
            elif k.lower() in ("p", "preset", "m", "manage"):
                self._manage_presets(range_unit)
                continue
            elif k in preset_key_map:
                selected_size = preset_key_map[k].duration

            try:
                if selected_size is None or float(selected_size) <= 0:
                    continue
                selected_size = float(selected_size)
            except (ValueError, TypeError):
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
                completed_ranges=completed_ranges,
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
                if mins <= 0 or not math.isfinite(mins):
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
                if pages <= 0 or not math.isfinite(pages):
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
        completed_ranges: Optional[List[Tuple[float, float]]] = None,
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
                if completed_ranges is not None:
                    proj_covered, proj_pct = calculate_coverage(
                        completed_ranges + [(start_pos, end_pos)], resource.total_range
                    )
                    proj_rem = max(0.0, resource.total_range - proj_covered)
                    console.print(
                        f"After Chunk:    [green]{format_human_duration(proj_covered)}[/green] [dim]({proj_pct}% complete · {format_human_duration(proj_rem)} remaining)[/dim]"
                    )
            else:
                chunk_size_str = f"{int(round(chunk_len))} pages"
                console.print(
                    f"Proposed Chunk: [bold green]{chunk_size_str}[/bold green] [dim]({formatted_range})[/dim]"
                )
                if completed_ranges is not None:
                    proj_covered, proj_pct = calculate_coverage(
                        completed_ranges + [(start_pos, end_pos)], resource.total_range
                    )
                    proj_rem = max(0.0, resource.total_range - proj_covered)
                    console.print(
                        f"After Chunk:    [green]{int(round(proj_covered))} pages[/green] [dim]({proj_pct}% complete · {int(round(proj_rem))} pages remaining)[/dim]"
                    )
        else:
            console.print(f"Proposed Chunk: [bold green]{formatted_range}[/bold green]")

        if completed_ranges is not None:
            active_gaps = get_remaining_intervals(completed_ranges, resource.total_range)
            if len(active_gaps) > 1:
                console.print(
                    f"  [dim yellow]Note: Resource has {len(active_gaps)} disjoint uncompleted intervals; chunk covers the earliest contiguous gap.[/dim yellow]"
                )

        console.print(f"Task Title:     [bold white]{task_title}[/bold white]")
        console.print(f"Scheduled For:  [bold cyan]Today ({today_str})[/bold cyan]\n")

        render_footer([
            ("Enter / y", "Confirm & schedule"),
            ("c", "Change chunk size"),
            ("b", "Cancel"),
        ])

        while True:
            k = key_reader.read_key()
            if not k:
                continue
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
            if not k:
                continue
            if k in (Key.ESCAPE, "escape", "b", "q", "cancel", Key.ENTER, "enter"):
                break

    def _manage_presets(self, range_unit: str) -> None:
        """Full-screen TUI for managing chunk size presets."""
        selected_idx = 0
        status_msg: Optional[str] = None
        status_style: str = "green"

        while True:
            presets = self.chunky_service.list_presets(range_unit=range_unit)
            if not presets:
                self.chunky_service.seed_default_presets()
                presets = self.chunky_service.list_presets(range_unit=range_unit)

            selected_idx = max(0, min(selected_idx, len(presets) - 1))

            clear_viewport()
            unit_title = "Media Presets (Minutes)" if range_unit == "seconds" else "Document Presets (Pages)"
            render_header("CHUNKY PRESETS", subtitle=f"Manage {unit_title}")

            if status_msg:
                console.print(f"  [{status_style}]{status_msg}[/{status_style}]\n")
                status_msg = None
                status_style = "green"

            table = Table(box=box.SIMPLE, show_header=True, header_style="bold cyan")
            table.add_column("  ", width=2)
            table.add_column("Name", style="bold white")
            table.add_column("Duration", justify="right")
            table.add_column("Default", justify="center")
            table.add_column("Type", justify="center")

            for idx, p in enumerate(presets):
                cursor = "▶" if idx == selected_idx else " "
                if range_unit == "seconds":
                    dur_str = format_human_duration(p.duration)
                else:
                    dur_str = f"{int(round(p.duration))} pages"

                def_str = "[bold green]Default[/bold green]" if p.is_default else "[dim]—[/dim]"
                type_str = "[dim]Built-in[/dim]" if p.is_builtin else "[cyan]Custom[/cyan]"

                table.add_row(cursor, p.name, dur_str, def_str, type_str)

            console.print(table)
            console.print()

            render_footer([
                ("↑/↓", "Navigate"),
                ("Enter / *", "Set as default"),
                ("a", "Add preset"),
                ("e", "Edit custom"),
                ("d", "Delete custom"),
                ("Esc / b", "Back"),
            ])

            k = key_reader.read_key()
            if not k:
                continue
            if k in (Key.ESCAPE, "escape", "b", "q", "cancel"):
                break

            if k in (Key.UP, "up"):
                selected_idx = max(0, selected_idx - 1)
                continue

            if k in (Key.DOWN, "down"):
                selected_idx = min(len(presets) - 1, selected_idx + 1)
                continue

            if k in (Key.ENTER, "enter", "*", "s"):
                chosen = presets[selected_idx]
                try:
                    self.chunky_service.set_default_preset(chosen.id)
                    status_msg = f"'{chosen.name}' set as default preset."
                    status_style = "green"
                except Exception as e:
                    status_msg = str(e)
                    status_style = "red"
                continue

            if k.lower() in ("a", "+", "add"):
                status_msg, status_style = self._handle_add_preset(range_unit)
                continue

            if k.lower() in ("e", "edit"):
                chosen = presets[selected_idx]
                if chosen.is_builtin:
                    status_msg = f"Built-in preset '{chosen.name}' cannot be edited. Press 'a' to add a custom preset."
                    status_style = "yellow"
                    continue
                status_msg, status_style = self._handle_edit_preset(chosen, range_unit)
                continue

            if k.lower() in ("d", "delete"):
                chosen = presets[selected_idx]
                if chosen.is_builtin:
                    status_msg = f"Built-in preset '{chosen.name}' cannot be deleted. Only custom presets can be deleted."
                    status_style = "yellow"
                    continue
                status_msg, status_style = self._handle_delete_preset(chosen, len(presets))
                continue

    def _handle_add_preset(self, range_unit: str) -> Tuple[str, str]:
        """Prompt user to create a new custom preset."""
        clear_viewport()
        render_header("CREATE PRESET", subtitle="Add Custom Chunk Size")
        try:
            name = key_reader.read_text_input("Preset Name: > ").strip()
            if not name or name.lower() in ("b", "cancel"):
                return ("Preset creation cancelled.", "yellow")

            if range_unit == "seconds":
                dur_str = key_reader.read_text_input("Duration in minutes (e.g. 35): > ").strip()
                if not dur_str or dur_str.lower() in ("b", "cancel"):
                    return ("Preset creation cancelled.", "yellow")
                try:
                    mins = float(dur_str)
                    if mins <= 0 or not math.isfinite(mins):
                        return ("Duration must be a positive number of minutes.", "red")
                    duration = mins * 60.0
                except ValueError:
                    return ("Invalid duration number.", "red")
            else:
                dur_str = key_reader.read_text_input("Page count (e.g. 20): > ").strip()
                if not dur_str or dur_str.lower() in ("b", "cancel"):
                    return ("Preset creation cancelled.", "yellow")
                try:
                    pages = float(dur_str)
                    if pages <= 0 or not math.isfinite(pages):
                        return ("Page count must be a positive number.", "red")
                    duration = pages
                except ValueError:
                    return ("Invalid page count number.", "red")

            def_ans = key_reader.read_text_input("Set as default preset? [y/N]: > ").strip().lower()
            is_default = def_ans in ("y", "yes")

            new_preset = ChunkyPreset(
                name=name,
                duration=duration,
                range_unit=range_unit,
                is_default=is_default,
                is_builtin=False,
            )
            self.chunky_service.create_preset(new_preset)
            return (f"Preset '{name}' created successfully.", "green")
        except ValueError as e:
            return (str(e), "red")
        except Exception as e:
            return (f"Error creating preset: {e}", "red")

    def _handle_edit_preset(self, preset: ChunkyPreset, range_unit: str) -> Tuple[str, str]:
        """Prompt user to edit/rename an existing preset."""
        clear_viewport()
        render_header("EDIT PRESET", subtitle=f"Editing '{preset.name}'")
        try:
            new_name = key_reader.read_text_input("Preset Name: > ", prefill=preset.name).strip()
            if not new_name or new_name.lower() in ("b", "cancel"):
                return ("Edit cancelled.", "yellow")

            if range_unit == "seconds":
                curr_mins = preset.duration / 60.0
                curr_str = f"{curr_mins:g}"
                dur_str = key_reader.read_text_input("Duration in minutes: > ", prefill=curr_str).strip()
                if not dur_str or dur_str.lower() in ("b", "cancel"):
                    return ("Edit cancelled.", "yellow")
                try:
                    mins = float(dur_str)
                    if mins <= 0 or not math.isfinite(mins):
                        return ("Duration must be a positive number of minutes.", "red")
                    new_duration = mins * 60.0
                except ValueError:
                    return ("Invalid duration number.", "red")
            else:
                curr_str = f"{preset.duration:g}"
                dur_str = key_reader.read_text_input("Page count: > ", prefill=curr_str).strip()
                if not dur_str or dur_str.lower() in ("b", "cancel"):
                    return ("Edit cancelled.", "yellow")
                try:
                    pages = float(dur_str)
                    if pages <= 0 or not math.isfinite(pages):
                        return ("Page count must be a positive number.", "red")
                    new_duration = pages
                except ValueError:
                    return ("Invalid page count number.", "red")

            preset.name = new_name
            preset.duration = new_duration
            self.chunky_service.update_preset(preset)
            return (f"Preset '{preset.name}' updated successfully.", "green")
        except ValueError as e:
            return (str(e), "red")
        except Exception as e:
            return (f"Error updating preset: {e}", "red")

    def _handle_delete_preset(self, preset: ChunkyPreset, total_count: int) -> Tuple[str, str]:
        """Prompt confirmation and delete preset."""
        if total_count <= 1:
            return ("Cannot delete the only preset for this resource type.", "yellow")

        clear_viewport()
        render_header("DELETE PRESET")
        console.print(f"Are you sure you want to delete preset [bold white]'{preset.name}'[/bold white]?")
        console.print("[dim]This will not affect any existing tasks.[/dim]\n")
        console.print("Press [bold red]'y'[/bold red] to confirm, or any other key to cancel.\n")
        render_footer([("y", "Confirm delete"), ("Any key", "Cancel")])

        k = key_reader.read_key()
        if k not in ("y", "Y"):
            return ("Deletion cancelled.", "yellow")

        try:
            self.chunky_service.delete_preset(preset.id)
            return (f"Preset '{preset.name}' deleted.", "green")
        except ValueError as e:
            return (str(e), "red")
        except Exception as e:
            return (f"Error deleting preset: {e}", "red")
