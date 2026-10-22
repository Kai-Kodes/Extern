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
            title = Text(task_title, style="green")
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


@cli.command("auth")
@click.option("--timeout", default=120, help="Authorization timeout in seconds (default: 120).")
@click.option("--no-browser", is_flag=True, help="Do not attempt to open the system browser automatically.")
def auth_cmd(timeout: int, no_browser: bool) -> None:
    """Authorize or re-authorize Google Tasks and Google Calendar."""
    import select
    import time
    import webbrowser
    from todo.integrations.google_tasks import GoogleOAuthManager, run_loopback_auth_server
    from todo.integrations.google_calendar import GoogleCalendarClient

    config = Config.load()
    oauth_manager = GoogleOAuthManager(
        token_path=config.google_token_path,
        credentials_file=config.google_credentials_file,
    )

    if not oauth_manager.has_configured_credentials():
        console.print("[bold red]Google OAuth credentials not found.[/bold red]\n")
        console.print(f"Please ensure credentials exist at: [dim]{config.google_credentials_file}[/dim]")
        console.print("or configure 'client_id' and 'client_secret' in ~/.config/todo/config.toml.\n")
        return

    is_authenticated = oauth_manager.is_authenticated()
    has_cal = oauth_manager.has_calendar_scope()

    console.print("\n[bold bright_white]GOOGLE INTEGRATION AUTHORIZATION[/bold bright_white]\n")
    if is_authenticated and not has_cal:
        console.print("[bold yellow]Adding Google Calendar events scope to your account.[/bold yellow]")
        console.print("[dim]Existing Google Tasks authorization and data are preserved.[/dim]\n")
    elif is_authenticated:
        console.print("[bold green]Google Tasks & Google Calendar are already authorized.[/bold green]")
        console.print("[dim]Re-authorizing to refresh permissions...[/dim]\n")
    else:
        console.print("[bold cyan]Authorizing Google Tasks and Google Calendar...[/bold cyan]\n")

    try:
        auth_url, redirect_uri, captured = run_loopback_auth_server(
            oauth_manager, timeout_seconds=timeout
        )
    except Exception as e:
        console.print(f"[bold red]Failed to start local authorization server:[/bold red] {e}\n")
        return

    console.print(f"Authorization URL:\n[bold cyan]{auth_url}[/bold cyan]\n")
    if not no_browser:
        console.print("Opening your browser to complete authorization...\n")
        try:
            webbrowser.open(auth_url)
        except Exception:
            console.print("[dim]Could not open browser automatically. Please open the link above.[/dim]\n")
    else:
        console.print("Please visit the link above in your browser.\n")

    console.print(f"[dim]Waiting up to {timeout}s for authorization callback...[/dim]")
    console.print("[dim](Or paste the authorization code or redirect URL here)[/dim]\n")

    code_to_exchange = None
    start_time = time.time()

    while time.time() - start_time < timeout:
        if captured:
            code_to_exchange = captured[0]
            break
        if sys.stdin and not sys.stdin.closed:
            try:
                rlist, _, _ = select.select([sys.stdin], [], [], 0.25)
                if rlist:
                    user_input = sys.stdin.readline().strip()
                    if user_input:
                        code_to_exchange = user_input
                        break
            except Exception:
                time.sleep(0.25)
        else:
            time.sleep(0.25)

    if not code_to_exchange:
        console.print("[bold red]Authorization timed out or was cancelled.[/bold red]\n")
        return

    console.print("[cyan]Exchanging authorization code...[/cyan]")
    try:
        oauth_manager.exchange_code(code_to_exchange, redirect_uri)
    except Exception as e:
        console.print(f"[bold red]Failed to exchange authorization code:[/bold red] {e}\n")
        return

    tasks_ok = oauth_manager.has_tasks_scope()
    cal_ok = oauth_manager.has_calendar_scope()

    console.print("\n[bold green]✓ Authorization completed successfully![/bold green]")
    console.print(f"  • Google Tasks: {'[green]Granted[/green]' if tasks_ok else '[yellow]Missing[/yellow]'}")
    console.print(f"  • Google Calendar: {'[green]Granted[/green]' if cal_ok else '[yellow]Missing[/yellow]'}")

    if cal_ok:
        try:
            cal_client = GoogleCalendarClient(oauth_manager=oauth_manager)
            verification = cal_client.verify_access(calendar_id=config.google_default_calendar)
            console.print(f"  • Primary Calendar: [green]Verified ({verification.get('summary', 'primary')})[/green]")
        except Exception as e:
            console.print(f"  • Calendar verification notice: [dim]{e}[/dim]")

    console.print(f"\n[dim]Token securely saved to {oauth_manager.token_path} (0600 permissions).[/dim]")
    console.print("[green]You can now launch 'todo' to use your synchronized tasks and calendar.[/green]\n")


cli.add_command(auth_cmd, name="google-auth")


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
