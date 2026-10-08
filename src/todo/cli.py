"""Command line interface entrypoint for Todo CLI."""

import sys
from datetime import datetime

import click
from rich.table import Table
from rich.text import Text

from todo.config import Config
from todo.database import Database
from todo.services.daily_service import DailyService
from todo.services.source_service import SourceService
from todo.services.task_service import TaskService
from todo.ui.app import TodoApp
from todo.ui.console import console, format_today_title


@click.group(invoke_without_command=True)
@click.pass_context
def cli(ctx: click.Context) -> None:
    """Personal Todo CLI application with Filemeta integration."""
    if ctx.invoked_subcommand is None:
        app = TodoApp()
        app.run()


@cli.command("today")
def today_cmd() -> None:
    """Print tasks scheduled for today (non-interactive)."""
    config = Config.load()
    db = Database(config.db_path)
    daily_service = DailyService(db)

    today_str = datetime.now().date().isoformat()
    tasks = daily_service.get_tasks_for_date(today_str)

    console.print(f"\n[bold bright_white]{format_today_title()}[/bold bright_white]\n")

    if not tasks:
        console.print("  [dim italic]No tasks for today.[/dim italic]\n")
        return

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


@cli.command("add")
@click.argument("title")
@click.option("-u", "--universal", is_flag=True, help="Add to universal tasks instead of today.")
def add_cmd(title: str, universal: bool) -> None:
    """Add a new task manually."""
    config = Config.load()
    db = Database(config.db_path)
    task_service = TaskService(db)
    daily_service = DailyService(db)

    source_type = "universal" if universal else "manual"
    task = task_service.create_task(title=title, source_type=source_type)

    if universal:
        console.print(f"[green]✓ Added \"{task.title}\" to universal task list.[/green]")
    else:
        today_str = datetime.now().date().isoformat()
        daily_service.add_task_to_date(task.id, today_str)
        console.print(f"[green]✓ Added \"{task.title}\" to today's tasks.[/green]")


@cli.command("list")
def list_cmd() -> None:
    """List all universal tasks."""
    config = Config.load()
    db = Database(config.db_path)
    task_service = TaskService(db)
    tasks = task_service.get_universal_tasks()

    console.print("\n[bold bright_white]UNIVERSAL TASKS[/bold bright_white]\n")
    if not tasks:
        console.print("  [dim italic]No universal tasks found.[/dim italic]\n")
        return

    for idx, t in enumerate(tasks, start=1):
        console.print(f"  [bold cyan]{idx}.[/bold cyan] [bold white]{t.title}[/bold white]")
    console.print()


@cli.command("sources")
def sources_cmd() -> None:
    """List imported Filemeta sources."""
    config = Config.load()
    db = Database(config.db_path)
    source_service = SourceService(db)
    sources = source_service.get_sources()

    console.print("\n[bold bright_white]FILEMETA SOURCES[/bold bright_white]\n")
    if not sources:
        console.print("  [dim italic]No sources imported yet.[/dim italic]\n")
        return

    for idx, s in enumerate(sources, start=1):
        count_lbl = f"{s.progress_pct}% ({s.completed_count}/{s.item_count} items)"
        console.print(f"  [bold cyan]{idx}.[/bold cyan] [bold white]{s.name}[/bold white] [dim]({count_lbl})[/dim]")
        console.print(f"     [dim]{s.path}[/dim]")
    console.print()


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
