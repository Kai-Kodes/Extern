"""Today view screen and interaction logic."""

from datetime import datetime
from typing import Optional

from rich import box
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from todo.models import DailyTask
from todo.services.daily_service import DailyService
from todo.ui.console import console, format_today_title, print_status_message, prompt_input


class TodayView:
    def __init__(self, daily_service: DailyService):
        self.daily_service = daily_service

    def render(self, tasks: list[DailyTask], status_msg: Optional[str] = None) -> None:
        """Render the Today screen."""
        header_title = format_today_title()
        
        # Header Box
        title_text = Text(header_title, style="bold bright_white", justify="center")
        console.print(
            Panel(
                title_text,
                box=box.ROUNDED,
                border_style="cyan",
                padding=(0, 2),
            )
        )

        if status_msg:
            print_status_message(status_msg)

        if not tasks:
            console.print("  [dim italic]No tasks for today.[/dim italic]\n")
            console.print("  [bold cyan]\\[+][/bold cyan] Add task")
            console.print("  [bold cyan]\\[u][/bold cyan] Universal tasks")
            console.print("  [bold cyan]\\[q][/bold cyan] Quit\n")
        else:
            table = Table.grid(padding=(0, 1))
            table.add_column("num", justify="right", style="cyan bold")
            table.add_column("icon", justify="center")
            table.add_column("title")

            for idx, dt in enumerate(tasks, start=1):
                task_title = dt.task.title if dt.task else f"Task #{dt.task_id}"
                if dt.is_completed:
                    icon = Text("✓", style="bold green")
                    title = Text(task_title, style="dim strike")
                else:
                    icon = Text("○", style="bright_yellow")
                    title = Text(task_title, style="bold white")

                table.add_row(f"{idx}.", icon, title)

            console.print(table)
            console.print()
            console.print("[dim]─" * 46 + "[/dim]\n")
            console.print(f"  [bold cyan]\\[1-{len(tasks)}][/bold cyan] Toggle completion")
            console.print("  [bold cyan]\\[+][/bold cyan]   Add task")
            console.print("  [bold cyan]\\[u][/bold cyan]   Universal tasks")
            console.print("  [bold cyan]\\[q][/bold cyan]   Quit\n")
