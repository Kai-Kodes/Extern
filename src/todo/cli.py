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


@cli.command("audit-duplicates")
@click.option("--fix", is_flag=True, help="Safely reconcile detected duplicate task echoes.")
def audit_duplicates_cmd(fix: bool) -> None:
    """Audit and safely reconcile duplicate tasks across local and synced sources."""
    from rich.table import Table

    config = Config.load()
    db = Database(config.db_path)

    # First run a read-only detection query before any potential fix
    with db.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT 
                t1.id AS original_id,
                t1.title AS original_title,
                t1.source_type AS original_source,
                t1.status AS original_status,
                t1.created_at AS original_created_at,
                m.remote_id,
                m.list_id,
                t2.id AS echo_id,
                t2.title AS echo_title,
                t2.source_type AS echo_source,
                t2.status AS echo_status,
                t2.created_at AS echo_created_at
            FROM task_google_tasks_map m
            JOIN tasks t1 ON t1.id = m.task_id
            JOIN file_items fi ON fi.path = m.remote_id
            JOIN tasks t2 ON t2.source_id = fi.id
            WHERE t1.id != t2.id
              AND t1.source_type IN ('manual', 'universal')
              AND t2.source_type = 'google_tasks'
              AND t1.title = t2.title
              AND NOT m.remote_id LIKE 'local_%'
            ORDER BY t1.id ASC
            """
        )
        echoes = [dict(r) for r in cursor.fetchall()]

        # Also check for duplicate mappings sharing remote_id
        cursor.execute(
            """
            SELECT m1.task_id AS orig_id, m2.task_id AS echo_id, m1.remote_id, m1.list_id
            FROM task_google_tasks_map m1
            JOIN task_google_tasks_map m2 ON m1.remote_id = m2.remote_id AND m1.task_id < m2.task_id
            WHERE NOT m1.remote_id LIKE 'local_%'
            """
        )
        dup_maps = [dict(r) for r in cursor.fetchall()]

    console.print("\n[bold bright_white]TASK DUPLICATION AUDIT[/bold bright_white]\n")

    if not echoes and not dup_maps:
        console.print("[bold green]✓ No duplicate tasks or spurious echoes detected in database.[/bold green]\n")
        return

    # Display detected duplicate echo pairs in a clear table
    table = Table(title="Detected Duplicate Task Pairs", show_header=True, header_style="bold cyan")
    table.add_column("Task ID", style="bold yellow", justify="right")
    table.add_column("Title", style="white")
    table.add_column("Source", style="cyan")
    table.add_column("Status", style="green")
    table.add_column("Scheduled Date", style="blue")
    table.add_column("Remote ID (Redacted)", style="dim")
    table.add_column("Role", style="bold magenta")

    with db.get_connection() as conn:
        cursor = conn.cursor()
        for echo in echoes:
            r = echo["remote_id"]
            redacted_remote = f"{r[:4]}...{r[-4:]}" if len(r) > 8 else "***"

            cursor.execute("SELECT date FROM daily_tasks WHERE task_id = ? ORDER BY date DESC LIMIT 1", (echo["original_id"],))
            d1_row = cursor.fetchone()
            d1_str = d1_row["date"] if d1_row else "(Universal)"

            cursor.execute("SELECT date FROM daily_tasks WHERE task_id = ? ORDER BY date DESC LIMIT 1", (echo["echo_id"],))
            d2_row = cursor.fetchone()
            d2_str = d2_row["date"] if d2_row else "(Universal)"

            table.add_row(
                str(echo["original_id"]),
                echo["original_title"],
                echo["original_source"],
                echo["original_status"],
                d1_str,
                redacted_remote,
                "Original Task",
            )
            table.add_row(
                str(echo["echo_id"]),
                echo["echo_title"],
                echo["echo_source"],
                echo["echo_status"],
                d2_str,
                redacted_remote,
                "Spurious Echo",
            )

    console.print(table)
    console.print()

    if fix:
        console.print("[cyan]Reconciling detected duplicates...[/cyan]")
        reconciled = db.reconcile_duplicate_echoes()
        console.print(f"[bold green]✓ Successfully reconciled {len(reconciled)} duplicate echo pair(s):[/bold green]")
        for item in reconciled:
            r = item["remote_id"]
            redacted_remote = f"{r[:4]}...{r[-4:]}" if len(r) > 8 else "***"
            console.print(
                f"  • Preserved original Task #{item['original_id']} ('{item['title']}'), "
                f"removed echo Task #{item['echo_id']} (Remote: {redacted_remote})"
            )
        console.print("\n[green]All duplicate echoes resolved. Database is clean.[/green]\n")
    else:
        console.print(f"[bold yellow]Found {len(echoes)} duplicate echo pair(s).[/bold yellow]")
        console.print("Run [bold cyan]todo audit-duplicates --fix[/bold cyan] to safely reconcile pre-existing duplicate echoes.\n")


cli.add_command(audit_duplicates_cmd, name="reconcile-duplicates")


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
