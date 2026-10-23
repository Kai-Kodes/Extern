"""YouTube library screen and video browser view for full-screen TUI."""

from datetime import datetime
from typing import Any, List, Optional, Set, Tuple

from rich.table import Table
from rich.text import Text

from todo.integrations.filemeta import FilemetaScanResult, ScannedFile
from todo.integrations.youtube import (
    YouTubeAdapter,
    YouTubeError,
    YouTubeFetchError,
    YouTubeInvalidURLError,
    YouTubeNotFoundError,
    YouTubePlaylist,
    YouTubeVideo,
    parse_youtube_url,
)
from todo.models import FileItem, Source
from todo.search import LiveSearchController
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


class YouTubeView:
    def __init__(
        self,
        youtube_adapter: YouTubeAdapter,
        source_service: SourceService,
        task_service: TaskService,
        daily_service: DailyService,
    ):
        self.adapter = youtube_adapter
        self.source_service = source_service
        self.task_service = task_service
        self.daily_service = daily_service

    def run(self, select_item_mode: bool = False) -> Any:
        """
        YouTube library screen: lists playlists already imported into Todo's SQLite database.
        Uses SQLite only on launch without network fetching.
        """
        status_msg: Optional[str] = None
        selected_idx = 0
        search = LiveSearchController[Source]()
        selected_source_ids: Set[int] = set()
        delete_mode = False

        while True:
            clear_viewport()
            sources = self.source_service.get_sources(source_type="youtube")
            if delete_mode:
                subtitle = f"DELETE MODE · {len(selected_source_ids)} selected"
            elif select_item_mode:
                subtitle = "Select Playlist for Chunking"
            else:
                subtitle = None
            render_header("YOUTUBE LIBRARY", subtitle=subtitle)

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
                    console.print("  [dim italic]No matching YouTube playlists found.[/dim italic]\n")
                else:
                    console.print(
                        "  [dim italic]No YouTube playlists imported yet.[/dim italic]\n"
                    )
                if search.is_active:
                    hints = [("Enter", "Done"), ("Esc", "Cancel / Clear")]
                elif delete_mode:
                    hints = [("Esc", "Cancel")]
                else:
                    hints = [("+ / Enter", "Add playlist / URL"), ("/", "Search"), ("b", "Back")]
                render_footer(hints)
            else:
                if selected_idx >= len(active_sources):
                    selected_idx = max(0, len(active_sources) - 1)

                table = Table.grid(padding=(0, 1))
                table.add_column("cursor", justify="right", style="cyan bold", width=2)
                if delete_mode:
                    table.add_column("select", justify="center", width=4)
                table.add_column("num", justify="right", style="cyan bold", width=4)
                table.add_column("name", style="bold white", width=36)
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
                        ("Enter", "Open playlist"),
                        ("+", "Add playlist / URL"),
                        ("d", "Delete"),
                        ("/", "Search"),
                        ("Esc", "Clear search"),
                        ("b", "Back"),
                    ]
                else:
                    hints = [
                        ("↑/↓", "Navigate"),
                        ("Enter", "Open playlist"),
                        ("+", "Add playlist / URL"),
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
                        f"Delete [bold white]{len(selected_source_ids)} YouTube playlists[/bold white]?\n"
                    )
                    console.print(
                        "[yellow]This will remove the imported playlists and cached metadata from Todo.[/yellow]"
                    )
                    console.print(
                        "[bold green]It will NOT delete anything from YouTube.[/bold green]\n"
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

            # +: Add video task or import playlist
            if k in ("+", Key.PLUS, "add"):
                ret = self.handle_add_url(select_item_mode=select_item_mode)
                if select_item_mode and ret:
                    return ret
                if ret == "added":
                    return "added"
                continue

            # d: Enter delete selection mode
            if k in ("d", "delete") and active_sources:
                delete_mode = True
                selected_source_ids.clear()
                continue

            # Enter: Open selected playlist videos immediately from SQLite or prompt add if empty
            if k in (Key.ENTER, "enter"):
                if active_sources:
                    selected_source = active_sources[selected_idx]
                    item_ret = self.handle_playlist_videos(selected_source, select_item_mode=select_item_mode)
                    if select_item_mode and item_ret:
                        return (selected_source, item_ret)
                    if item_ret == "added":
                        return "added"
                    continue
                elif not search.is_active and not search.query:
                    ret = self.handle_add_url(select_item_mode=select_item_mode)
                    if select_item_mode and ret:
                        return ret
                    if ret == "added":
                        return "added"
                    continue

            # Numeric selection
            if k.isdigit() and active_sources:
                idx = int(k)
                if 1 <= idx <= len(active_sources):
                    selected_source = active_sources[idx - 1]
                    item_ret = self.handle_playlist_videos(selected_source, select_item_mode=select_item_mode)
                    if select_item_mode and item_ret:
                        return (selected_source, item_ret)
                    if item_ret == "added":
                        return "added"
                else:
                    status_msg = f"Invalid selection: #{k}."

    def handle_add_url(self, select_item_mode: bool = False) -> Any:
        """
        Input screen: paste a single video URL or playlist URL.
        - Single video URL creates a task in SQLite with the actual YouTube title and adds to Today.
        - Playlist URL creates an imported source in SQLite and opens its video browser.
        """
        clear_viewport()
        render_header("YOUTUBE", subtitle="Add Video or Playlist")
        console.print("Enter YouTube video or playlist URL (or 'b' to cancel):\n")

        url_input = key_reader.read_text_input("URL: > ")
        if not url_input or url_input.lower() in ("b", "back", "cancel", "q", "quit"):
            return None

        # Validate URL
        try:
            parsed_info = parse_youtube_url(url_input)
        except YouTubeInvalidURLError as e:
            return self._show_error_screen("INVALID YOUTUBE URL", str(e))

        kind = parsed_info["kind"]

        if kind == "video":
            # ------------------------------------------------------------------
            # SINGLE VIDEO URL: Fetch metadata, create Task, add to Today
            # ------------------------------------------------------------------
            with console.status(
                "[bold cyan]Fetching YouTube video metadata...[/bold cyan]",
                spinner="dots",
            ):
                try:
                    video = self.adapter.fetch_single_video(url_input)
                except YouTubeNotFoundError as e:
                    return self._show_error_screen("YT-DLP NOT FOUND", str(e))
                except YouTubeError as e:
                    return self._show_error_screen("VIDEO FETCH FAILED", str(e))
                except Exception as e:
                    return self._show_error_screen("VIDEO FETCH ERROR", str(e))

            if select_item_mode:
                scanned_item = ScannedFile(
                    name=video.video_id,
                    path=video.path,
                    title=video.title,
                    type="video",
                    metadata={
                        "source_type": "youtube",
                        "video_id": video.video_id,
                        "duration": video.duration,
                        "duration_formatted": video.duration_formatted,
                        "canonical_url": video.canonical_url,
                        "channel": video.channel,
                        "uploader": video.uploader,
                    },
                )
                scan_res = FilemetaScanResult(
                    source_name="Single Videos",
                    source_path="youtube://videos",
                    items=[scanned_item],
                )
                single_source, _, _ = self.source_service.add_or_update_source(scan_res)
                items = self.source_service.get_items_by_source(single_source.id)
                file_item = next(
                    (it for it in items if it.name == video.video_id),
                    items[0] if items else None,
                )
                return (single_source, file_item)

            # Create or reuse task (deduplicating by canonical URL)
            task, _ = self.task_service.get_or_create_video_task(
                title=video.title,
                canonical_url=video.canonical_url,
                video_id=video.video_id,
            )

            # Schedule to Today
            today_str = self.daily_service.get_today_str()
            self.daily_service.add_task_to_date(task.id, today_str)

            # Strict global silence: zero success toast/message!
            return "added"

        else:
            # ------------------------------------------------------------------
            # PLAYLIST URL: Check cache or fetch metadata, create Source, open
            # ------------------------------------------------------------------
            playlist_id = parsed_info["playlist_id"]
            source_path = f"youtube://playlist/{playlist_id}"
            existing = self.source_service.get_source_by_path(source_path)

            if existing:
                # Reopen existing cached contents immediately without full scan
                item_ret = self.handle_playlist_videos(
                    existing, select_item_mode=select_item_mode
                )
                if select_item_mode and item_ret:
                    return (existing, item_ret)
                return item_ret

            # First-time import
            with console.status(
                "[bold cyan]Importing YouTube playlist metadata...[/bold cyan]",
                spinner="dots",
            ):
                try:
                    playlist = self.adapter.fetch_playlist(url_input)
                except YouTubeNotFoundError as e:
                    return self._show_error_screen("YT-DLP NOT FOUND", str(e))
                except YouTubeError as e:
                    return self._show_error_screen("PLAYLIST IMPORT FAILED", str(e))
                except Exception as e:
                    return self._show_error_screen("PLAYLIST IMPORT ERROR", str(e))

            # Store in SQLite
            items = [
                ScannedFile(
                    name=it.video_id,
                    path=it.path,
                    title=it.title,
                    type="video",
                    metadata={
                        "source_type": "youtube",
                        "video_id": it.video_id,
                        "playlist_id": playlist.playlist_id,
                        "duration": it.duration,
                        "duration_formatted": it.duration_formatted,
                        "canonical_url": it.canonical_url,
                        "channel": it.channel,
                        "uploader": it.uploader,
                        "position": it.position,
                    },
                )
                for it in playlist.items
            ]
            scan_res = FilemetaScanResult(
                source_name=playlist.title,
                source_path=playlist.source_path,
                items=items,
            )
            new_source, _, _ = self.source_service.add_or_update_source(scan_res)

            # Open the playlist's video browser immediately
            item_ret = self.handle_playlist_videos(
                new_source, select_item_mode=select_item_mode
            )
            if select_item_mode and item_ret:
                return (new_source, item_ret)
            return item_ret

    def handle_playlist_videos(
        self, source: Source, select_item_mode: bool = False
    ) -> Any:
        """
        Video browser view for an imported YouTube playlist.
        Supports navigation, live fuzzy search (/), next unfinished (n),
        multi-select (Space), schedule to Today (Enter), completion toggle (t),
        rescan (r), source deletion (d), and back (b).
        """
        selected_idx = 0
        search = LiveSearchController[FileItem]()
        selected_item_ids: Set[int] = set()
        selected_delete_ids: Set[int] = set()
        delete_mode = False
        status_msg: Optional[str] = None
        current_source = source

        while True:
            clear_viewport()
            items = self.source_service.get_items_by_source(current_source.id)

            valid_item_ids = {it.id for it in items}
            selected_item_ids.intersection_update(valid_item_ids)
            selected_delete_ids.intersection_update(valid_item_ids)

            today_str = self.daily_service.get_today_str()
            today_tasks = self.daily_service.get_tasks_for_date(today_str)
            active_today = self.daily_service.get_active_tasks_for_date(today_str)
            scheduled_item_ids = {
                t.task.source_id
                for t in (today_tasks + active_today)
                if t.task.source_type in ("youtube", "filemeta", "telegram")
                and t.task.source_id is not None
            }

            if not items:
                render_header(current_source.name, subtitle=f"{current_source.path} · 0 videos")
                print_status_message("No videos found in this playlist.", "yellow")
                render_footer([("r", "Rescan"), ("d", "Delete"), ("b", "Back")])
                sub_k = key_reader.read_key()
                if sub_k in ("r", "rescan"):
                    current_source, err = self._rescan_playlist(current_source)
                    if err:
                        status_msg = err
                    continue
                elif sub_k in ("d", "delete"):
                    if self._confirm_delete(current_source):
                        return None
                    continue
                return None

            completed_cnt, total_cnt, pct = self.source_service.get_source_progress(
                current_source.id
            )

            # Boxed header matching format: TITLE      42% · 5/12
            render_today_header(current_source.name, completed=completed_cnt, total=total_cnt)

            if delete_mode:
                print_status_message(
                    f"DELETE MODE · {len(selected_delete_ids)} selected", style="bold yellow"
                )
            elif status_msg:
                print_status_message(status_msg, style="red")
                status_msg = None
            elif select_item_mode:
                print_status_message("Select a video to create a Chunky Task", style="bold cyan")

            # Filter items if search is active
            if search.is_active:
                console.print(f"  Search: [bold cyan]{search.query}[/bold cyan]█\n")
            elif search.query:
                console.print(f"  Search: [bold cyan]{search.query}[/bold cyan] [dim](filter active, Esc to clear)[/dim]\n")

            active_items = search.filter(items)

            if not active_items:
                console.print("  [dim italic]No matching videos found.[/dim italic]\n")
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
                    render_header("DELETE VIDEOS", subtitle=current_source.name)
                    console.print(
                        f"Delete [bold white]{len(selected_delete_ids)} videos[/bold white] from this playlist?\n"
                    )
                    console.print(
                        "[yellow]This will remove the videos from Todo.[/yellow]"
                    )
                    console.print(
                        "[bold green]YouTube content remains untouched.[/bold green]\n"
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

            # Space: multi-select toggle
            if k in (Key.SPACE, "space", " ") and active_items:
                target_item = active_items[selected_idx]
                if target_item.id in selected_item_ids:
                    selected_item_ids.remove(target_item.id)
                else:
                    selected_item_ids.add(target_item.id)
                continue

            # Enter: schedule selected videos into Today
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
                    task = self.task_service.get_or_create_youtube_task(
                        it, source_name=current_source.name
                    )
                    self.daily_service.add_task_to_date(task.id, today_str)

                selected_item_ids.clear()
                # Strict global silence: zero success banner!
                return "added"

            # n: Next unfinished video with wrap-around
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

            # t: Toggle completion state in SQLite
            if k in ("t", "toggle") and active_items:
                sel_item = active_items[selected_idx]
                self.source_service.toggle_item_completion(sel_item.id)
                continue

            # r: Rescan playlist from YouTube
            if k in ("r", "rescan"):
                current_source, err = self._rescan_playlist(current_source)
                if err:
                    status_msg = err
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
                    task = self.task_service.get_or_create_youtube_task(
                        selected_item, source_name=current_source.name
                    )
                    self.daily_service.add_task_to_date(task.id, today_str)
                    selected_item_ids.clear()
                    return "added"
                else:
                    status_msg = f"Invalid selection: #{k}."

    def _rescan_playlist(self, source: Source) -> tuple[Source, Optional[str]]:
        """Rescan playlist metadata while preserving completion and scheduling state."""
        playlist_id = source.path.replace("youtube://playlist/", "")
        canonical_url = f"https://www.youtube.com/playlist?list={playlist_id}"

        with console.status(
            f"[bold cyan]Rescanning playlist {source.name}...[/bold cyan]",
            spinner="dots",
        ):
            try:
                playlist = self.adapter.fetch_playlist(canonical_url)
            except Exception as e:
                return source, f"Rescan failed: {e}"

        items = [
            ScannedFile(
                name=it.video_id,
                path=it.path,
                title=it.title,
                type="video",
                metadata={
                    "source_type": "youtube",
                    "video_id": it.video_id,
                    "playlist_id": playlist.playlist_id,
                    "duration": it.duration,
                    "duration_formatted": it.duration_formatted,
                    "canonical_url": it.canonical_url,
                    "channel": it.channel,
                    "uploader": it.uploader,
                    "position": it.position,
                },
            )
            for it in playlist.items
        ]
        scan_res = FilemetaScanResult(
            source_name=playlist.title,
            source_path=playlist.source_path,
            items=items,
        )
        updated_source, _, _ = self.source_service.add_or_update_source(scan_res)
        return updated_source, None

    def _confirm_delete(self, source: Source) -> bool:
        """Prompt confirmation and delete source cleanly."""
        clear_viewport()
        render_header("DELETE YOUTUBE PLAYLIST", subtitle=source.name)
        console.print(f"Source: [bold white]{source.name}[/bold white]")
        console.print(f"Path:   [dim]{source.path}[/dim]\n")
        console.print(
            "[yellow]This will remove the imported playlist and cached metadata from Todo.[/yellow]"
        )
        console.print(
            "[bold green]It will NOT delete anything from YouTube or affect unrelated tasks.[/bold green]\n"
        )
        render_footer([("y", "Yes, delete"), ("n", "Cancel")])
        k_conf = key_reader.read_key()
        if k_conf.lower() in ("y", "yes", Key.ENTER, "enter"):
            self.source_service.delete_source(source.id)
            return True
        return False

    def handle_delete_source(
        self, sources: List[Source], current_idx: int = 0
    ) -> Optional[str]:
        """Prompt to select and delete an imported YouTube playlist source."""
        if not sources:
            return None

        del_idx = current_idx if 0 <= current_idx < len(sources) else 0

        while True:
            clear_viewport()
            render_header("DELETE YOUTUBE PLAYLIST")
            console.print("Select playlist to remove from Todo (files remain untouched):\n")

            table = Table.grid(padding=(0, 1))
            table.add_column("cursor", justify="right", style="cyan bold", width=2)
            table.add_column("num", justify="right", style="cyan bold", width=4)
            table.add_column("name", style="bold white", width=36)
            table.add_column("path", style="dim")

            for idx, s in enumerate(sources):
                cursor = "▶" if idx == del_idx else " "
                table.add_row(cursor, f"{idx + 1}.", s.name, s.path)

            console.print(table)
            console.print()
            render_footer(
                [
                    ("↑/↓", "Navigate"),
                    ("Enter", "Select to delete"),
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
        self._confirm_delete(selected_source)
        return None

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
