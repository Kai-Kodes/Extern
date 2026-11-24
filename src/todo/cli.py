"""Command line interface entrypoint for Extern (Todo CLI)."""

import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

import click
from rich.table import Table
from rich.text import Text

from todo.config import Config
from todo.database import Database
from todo.services.daily_service import DailyService
from todo.services.source_service import SourceService
from todo.services.sync_service import SyncService
from todo.services.task_service import TaskService
from todo.ui.app import TodoApp
from todo.ui.console import console, format_today_title


@click.group(invoke_without_command=True)
@click.option("--no-sync", is_flag=True, help="Disable automatic Dropbox sync for this session.")
@click.pass_context
def cli(ctx: click.Context, no_sync: bool) -> None:
    """Extern: Standalone local-first task and course management CLI with native sync."""
    if ctx.invoked_subcommand is None:
        config = Config.load()
        sync_svc: Optional[SyncService] = None

        if config.dropbox_refresh_token and not no_sync:
            sync_svc = SyncService(config)

        app = TodoApp(config=config, sync_service=sync_svc, no_sync=no_sync)
        app.run()


# -----------------------------------------------------------------------------
# Today & Task Commands
# -----------------------------------------------------------------------------


def maybe_show_first_run_hint(config: Config) -> None:
    """Display a welcoming first-run hint on brand-new installs."""
    if getattr(config, "is_first_run", False):
        console.print(
            "[bold bright_cyan]Welcome to Extern![/bold bright_cyan] "
            "[dim]Get started with 'extern add \"My Task\"' or run 'extern auth dropbox' / 'extern auth telegram'.[/dim]\n"
        )


@cli.command("today")
def today_cmd() -> None:
    """Print tasks scheduled for today (non-interactive)."""
    config = Config.load()
    maybe_show_first_run_hint(config)
    db = Database(config.db_path)
    daily_service = DailyService(db)

    today_str = datetime.now().date().isoformat()
    tasks = daily_service.get_active_tasks_for_date(today_str)

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
    """List imported course sources."""
    config = Config.load()
    db = Database(config.db_path)
    source_service = SourceService(db)
    sources = source_service.get_sources()

    console.print("\n[bold bright_white]IMPORTED SOURCES[/bold bright_white]\n")
    if not sources:
        console.print("  [dim italic]No sources imported yet.[/dim italic]\n")
        return

    for idx, s in enumerate(sources, start=1):
        count_lbl = f"{s.progress_pct}% ({s.completed_count}/{s.item_count} items)"
        console.print(f"  [bold cyan]{idx}.[/bold cyan] [bold white]{s.name}[/bold white] [dim]({count_lbl})[/dim]")
        console.print(f"     [dim]{s.path}[/dim]")
    console.print()


@cli.command("db-checkpoint")
def db_checkpoint_cmd() -> None:
    """Flush and truncate SQLite WAL state for safe cross-device syncing."""
    config = Config.load()
    db = Database(config.db_path)
    try:
        db.checkpoint()
    finally:
        db.close()


# -----------------------------------------------------------------------------
# Auth Commands (`extern auth dropbox` & `extern auth telegram`)
# -----------------------------------------------------------------------------


@cli.group("auth")
def auth_group() -> None:
    """Manage authentication for Dropbox sync and Telegram courses."""
    pass


@auth_group.command("dropbox")
@click.option("--logout", is_flag=True, help="Unlink Dropbox account and clear saved credentials.")
@click.option("--app-key", help="Dropbox App Key (defaults to built-in or configured app_key).")
def auth_dropbox_cmd(logout: bool, app_key: Optional[str]) -> None:
    """Authorize Dropbox for automatic cross-device database synchronization."""
    if logout:
        Config.clear_dropbox_credentials()
        console.print("\n[bold green]✓ Unlinked Dropbox account and cleared saved credentials.[/bold green]\n")
        return

    config = Config.load()
    sync_svc = SyncService(config)
    key = app_key or config.dropbox_app_key

    console.print("\n[bold bright_white]DROPBOX AUTHENTICATION (PKCE)[/bold bright_white]\n")
    try:
        auth_url, flow = sync_svc.create_pkce_flow(app_key=key)
        console.print("1. Open this link in your web browser to authorize Extern:")
        console.print(f"   [bold cyan underline]{auth_url}[/bold cyan underline]\n")
        console.print("2. Click 'Allow' and copy the authorization code.\n")

        auth_code = click.prompt("Paste authorization code here", type=str)

        refresh_token = sync_svc.finish_pkce_flow(flow, auth_code)
        Config.save_dropbox_credentials(refresh_token=refresh_token, app_key=key)

        console.print("\n[bold green]✓ Dropbox successfully linked![/bold green]")
        console.print("[dim]Testing connection and verifying remote sync...[/dim]")

        new_cfg = Config.load()
        new_svc = SyncService(new_cfg)
        ok, msg = new_svc.push()
        if ok:
            console.print("[bold green]✓ Initial database sync complete.[/bold green]\n")
        else:
            console.print(f"[yellow]Notice: {msg}[/yellow]\n")
    except Exception as e:
        console.print(f"\n[bold red]✗ Dropbox authentication failed: {e}[/bold red]\n")


@auth_group.command("telegram")
@click.option("--logout", is_flag=True, help="Log out and remove local Telegram session.")
def auth_telegram_cmd(logout: bool) -> None:
    """Interactively authenticate Telegram for video lesson courses."""
    config = Config.load()
    session_path = config.telegram_session_path or (config.get_data_dir() / "telegram.session")

    if logout:
        session_file = session_path
        if session_file and not str(session_file).endswith(".session"):
            session_file = Path(f"{session_file}.session")

        if session_file and session_file.exists():
            try:
                session_file.unlink()
            except OSError as e:
                console.print(f"[yellow]Warning: Could not remove session file: {e}[/yellow]")

        Config.clear_telegram_credentials()
        console.print("\n[bold green]✓ Logged out of Telegram and removed local session file.[/bold green]\n")
        return

    console.print("\n[bold bright_white]TELEGRAM AUTHENTICATION[/bold bright_white]\n")
    api_id = config.telegram_api_id
    api_hash = config.telegram_api_hash

    if not api_id or not api_hash:
        console.print("Get your API credentials from [bold cyan]https://my.telegram.org[/bold cyan] (API development tools).\n")
        api_id = click.prompt("  Telegram API ID", type=int)
        api_hash = click.prompt("  Telegram API Hash", type=str)

    Config.save_telegram_credentials(api_id, api_hash, session_path=session_path)

    session_path.parent.mkdir(parents=True, exist_ok=True)
    session_base = str(session_path)
    if session_base.endswith(".session"):
        session_base = session_base[:-8]

    try:
        from telethon.sync import TelegramClient

        console.print("\n[dim]Connecting to Telegram...[/dim]")
        with TelegramClient(session_base, api_id, api_hash) as client:
            if not client.is_user_authorized():
                client.start()

            me = client.get_me()
            name = f"{me.first_name} {me.last_name or ''}".strip()
            handle = f"@{me.username}" if me.username else me.phone
            console.print(f"\n[bold green]✓ Successfully authenticated as {name} ({handle})![/bold green]")
            console.print(f"[dim]Session saved to: {session_path}[/dim]\n")
    except Exception as e:
        console.print(f"\n[bold red]✗ Telegram authentication failed: {e}[/bold red]\n")


# -----------------------------------------------------------------------------
# Sync Commands (`extern sync` / `pull` / `push` / `status`)
# -----------------------------------------------------------------------------


@cli.group("sync", invoke_without_command=True)
@click.pass_context
def sync_group(ctx: click.Context) -> None:
    """Manage native Dropbox synchronization."""
    if ctx.invoked_subcommand is None:
        ctx.invoke(sync_status_cmd)


@sync_group.command("pull")
@click.option("-f", "--force", is_flag=True, help="Force pull even if cached revision matches.")
def sync_pull_cmd(force: bool) -> None:
    """Pull the latest database from Dropbox."""
    config = Config.load()
    sync_svc = SyncService(config)
    if not sync_svc.is_configured:
        console.print("[yellow]Dropbox sync is not configured. Run 'extern auth dropbox' first.[/yellow]")
        return

    console.print("Pulling latest database from Dropbox...")
    ok, msg = sync_svc.pull(force=force)
    if ok:
        console.print(f"[bold green]✓ {msg}[/bold green]")
    else:
        console.print(f"[bold red]✗ {msg}[/bold red]")


@sync_group.command("push")
@click.option("-f", "--force", is_flag=True, help="Force push local database to Dropbox.")
def sync_push_cmd(force: bool) -> None:
    """Checkpoint local SQLite WAL and push database to Dropbox."""
    config = Config.load()
    sync_svc = SyncService(config)
    if not sync_svc.is_configured:
        console.print("[yellow]Dropbox sync is not configured. Run 'extern auth dropbox' first.[/yellow]")
        return

    console.print("Pushing database to Dropbox...")
    ok, msg = sync_svc.push(force=force)
    if ok:
        console.print(f"[bold green]✓ {msg}[/bold green]")
    else:
        console.print(f"[bold red]✗ {msg}[/bold red]")


@sync_group.command("status")
def sync_status_cmd() -> None:
    """Display Dropbox sync status and cached revision details."""
    config = Config.load()
    sync_svc = SyncService(config)
    st = sync_svc.status()

    console.print("\n[bold bright_white]DROPBOX SYNC STATUS[/bold bright_white]\n")
    table = Table.grid(padding=(0, 2))
    table.add_column("Key", style="bold cyan")
    table.add_column("Value")

    table.add_row("Configured", "[green]Yes[/green]" if st["configured"] else "[dim]No (Run 'extern auth dropbox')[/dim]")
    table.add_row("Remote Path", st["remote_path"])
    table.add_row("Local DB", f"{st['local_db_path']} ({st['local_db_size_bytes']} bytes)")
    table.add_row("Connection", st["connection"])
    table.add_row("Cached Rev", st["cached_rev"] or "[dim]None[/dim]")
    table.add_row("Last Pull", st["last_pull_at"] or "[dim]Never[/dim]")
    table.add_row("Last Push", st["last_push_at"] or "[dim]Never[/dim]")
    table.add_row("Sync Status", st["last_sync_status"])

    console.print(table)
    console.print()


# -----------------------------------------------------------------------------
# Diagnostics (`extern doctor` / `extern status`)
# -----------------------------------------------------------------------------


@cli.command("doctor")
def doctor_cmd() -> None:
    """Inspect system configuration, database, scanner, and integrations status."""
    config = Config.load()

    console.print("\n[bold bright_white]EXTERN SYSTEM DIAGNOSTICS[/bold bright_white]\n")
    table = Table(box=None, padding=(0, 2))
    table.add_column("Component", style="bold cyan", width=22)
    table.add_column("Status", width=14)
    table.add_column("Details", style="dim")

    # 1. Config file
    cfg_path = config.get_config_path()
    if cfg_path.exists():
        table.add_row("Configuration", "[bold green]OK[/bold green]", str(cfg_path))
    else:
        table.add_row("Configuration", "[yellow]Default[/yellow]", f"{cfg_path} (not created yet, using defaults)")

    # 2. Database
    db_path = config.db_path
    if db_path.exists():
        db_size = f"{db_path.stat().st_size / 1024:.1f} KiB"
        wal_file = Path(f"{db_path}-wal")
        wal_status = "active WAL" if wal_file.exists() and wal_file.stat().st_size > 0 else "clean (checkpointed)"
        table.add_row("SQLite Database", "[bold green]OK[/bold green]", f"{db_path} ({db_size}, {wal_status})")
    else:
        table.add_row("SQLite Database", "[yellow]Not Found[/yellow]", f"{db_path} (will be created on first run)")

    # 3. Course Scanner
    fm_cmd = shutil.which(config.filemeta_command)
    try:
        import tinytag  # noqa: F401
        tinytag_ok = True
    except ImportError:
        tinytag_ok = False

    if fm_cmd:
        scanner_detail = f"External filemeta ({fm_cmd}) + Pure-Python TinyTag fallback"
    elif tinytag_ok:
        scanner_detail = "Built-in Pure-Python scanner with TinyTag duration extraction"
    else:
        scanner_detail = "Built-in Pure-Python scanner (tinytag not installed)"

    table.add_row("Course Scanner", "[bold green]OK[/bold green]", scanner_detail)

    # 4. YouTube integration
    try:
        import yt_dlp  # noqa: F401
        table.add_row("YouTube (yt-dlp)", "[bold green]OK[/bold green]", f"yt-dlp version {yt_dlp.version.__version__}")
    except ImportError:
        table.add_row("YouTube (yt-dlp)", "[yellow]Missing[/yellow]", "yt-dlp is not installed in the Python environment")

    # 5. Telegram integration
    try:
        import telethon  # noqa: F401
        telethon_installed = True
    except ImportError:
        telethon_installed = False

    session_file = config.telegram_session_path
    if session_file and not str(session_file).endswith(".session"):
        session_file = Path(f"{session_file}.session")

    if not telethon_installed:
        table.add_row("Telegram", "[yellow]Missing[/yellow]", "Telethon library is not installed")
    elif not config.telegram_api_id or not config.telegram_api_hash:
        table.add_row("Telegram", "[dim]Not Configured[/dim]", "API credentials not set (run 'extern auth telegram')")
    elif not session_file or not session_file.exists():
        table.add_row("Telegram", "[yellow]Not Signed In[/yellow]", f"Session file not found at {session_file} (run 'extern auth telegram')")
    else:
        table.add_row("Telegram", "[bold green]Authenticated[/bold green]", f"Session: {session_file}")

    # 6. Dropbox sync engine
    sync_svc = SyncService(config)
    st = sync_svc.status()
    if not st["configured"]:
        table.add_row("Dropbox Sync", "[dim]Not Configured[/dim]", "No refresh token (run 'extern auth dropbox')")
    else:
        rev_info = f"rev {st['cached_rev']}" if st["cached_rev"] else "not synced yet"
        conn_info = st["connection"]
        if conn_info == "online":
            table.add_row("Dropbox Sync", "[bold green]Linked[/bold green]", f"Online, {rev_info} -> {st['remote_path']}")
        else:
            table.add_row("Dropbox Sync", "[yellow]Unreachable[/yellow]", f"{conn_info}, {rev_info}")

    console.print(table)
    console.print()


@cli.command("status")
@click.pass_context
def status_alias_cmd(ctx: click.Context) -> None:
    """Alias for 'extern doctor'."""
    ctx.invoke(doctor_cmd)


# -----------------------------------------------------------------------------
# Uninstallation (`extern uninstall`)
# -----------------------------------------------------------------------------


def _detect_and_uninstall_package(skip: bool = False) -> Tuple[str, bool, str]:
    """Detect package installer (pipx, uv tool, pip) and run uninstaller."""
    if skip:
        return "skipped", True, "Package uninstallation skipped"

    prefix = str(sys.prefix).lower()
    exe = str(sys.executable).lower()

    # 1. pipx
    if "pipx" in prefix or "pipx" in exe:
        pipx_bin = shutil.which("pipx")
        if pipx_bin:
            try:
                res = subprocess.run(
                    [pipx_bin, "uninstall", "extern"],
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                if res.returncode == 0:
                    return "pipx", True, "Uninstalled 'extern' via pipx"
                res2 = subprocess.run(
                    [pipx_bin, "uninstall", "todo-cli"],
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                if res2.returncode == 0:
                    return "pipx", True, "Uninstalled 'todo-cli' via pipx"
            except Exception as e:
                return "pipx", False, str(e)

    # 2. uv tool
    if "uv/tools" in prefix or "uv/tools" in exe:
        uv_bin = shutil.which("uv")
        if uv_bin:
            try:
                res = subprocess.run(
                    [uv_bin, "tool", "uninstall", "extern"],
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                if res.returncode == 0:
                    return "uv tool", True, "Uninstalled 'extern' via uv tool"
                res2 = subprocess.run(
                    [uv_bin, "tool", "uninstall", "todo-cli"],
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                if res2.returncode == 0:
                    return "uv tool", True, "Uninstalled 'todo-cli' via uv tool"
            except Exception as e:
                return "uv tool", False, str(e)

    # 3. pip (standard / editable)
    try:
        res = subprocess.run(
            [sys.executable, "-m", "pip", "uninstall", "-y", "extern", "todo-cli"],
            capture_output=True,
            text=True,
            timeout=20,
        )
        if res.returncode == 0:
            return "pip", True, "Uninstalled package via pip"
        return "pip", False, res.stderr or res.stdout
    except Exception as e:
        return "pip", False, str(e)


@cli.command("uninstall")
@click.option(
    "-y", "--yes", is_flag=True, help="Confirm uninstallation without prompting."
)
@click.option(
    "--keep-data",
    is_flag=True,
    help="Preserve local database and task history (~/.local/share/todo/todo.db).",
)
@click.option(
    "--skip-pkg",
    is_flag=True,
    hidden=True,
    help="Skip package manager removal (for testing).",
)
def uninstall_cmd(yes: bool, keep_data: bool, skip_pkg: bool) -> None:
    """Cleanly uninstall Extern, revoke cloud sessions, and remove data."""
    if not yes:
        if not click.confirm(
            "Are you sure you want to uninstall Extern and remove configuration?",
            default=False,
        ):
            console.print("[dim]Uninstallation aborted.[/dim]")
            return

    config = Config.load(auto_create=False)
    data_dir = config.get_data_dir()
    config_dir = config.get_config_dir()
    config_file = config.get_config_path()
    db_path = config.db_path

    console.print("\n[bold bright_white]EXTERN UNINSTALLATION[/bold bright_white]\n")

    # 1. Revoke / clear sessions
    if config.dropbox_refresh_token:
        try:
            import dropbox

            dbx = dropbox.Dropbox(
                oauth2_refresh_token=config.dropbox_refresh_token,
                app_key=config.dropbox_app_key,
                timeout=3.0,
            )
            dbx.auth_token_revoke()
            console.print("  [bold green]✓[/bold green] Revoked Dropbox OAuth refresh token.")
        except Exception:
            console.print("  [dim]• Could not revoke Dropbox token remotely (offline or expired).[/dim]")

    # Telegram session
    session_targets = set()
    if config.telegram_session_path:
        session_targets.add(config.telegram_session_path)
        if not str(config.telegram_session_path).endswith(".session"):
            session_targets.add(Path(f"{config.telegram_session_path}.session"))
    session_targets.add(data_dir / "telegram.session")

    for s_file in session_targets:
        if s_file.exists():
            try:
                s_file.unlink()
                console.print(f"  [bold green]✓[/bold green] Removed Telegram session: {s_file}")
            except OSError as e:
                console.print(f"  [yellow]![/yellow] Could not remove Telegram session: {e}")
        s_journal = s_file.with_name(s_file.name + "-journal")
        if s_journal.exists():
            try:
                s_journal.unlink()
            except OSError:
                pass

    # Sync state
    for sync_state_file in {data_dir / "sync_state.json", db_path.parent / "sync_state.json"}:
        if sync_state_file.exists():
            try:
                sync_state_file.unlink()
                console.print(f"  [bold green]✓[/bold green] Removed sync state: {sync_state_file}")
            except OSError:
                pass

    if config_file.exists():
        try:
            config_file.unlink()
            console.print(f"  [bold green]✓[/bold green] Removed configuration: {config_file}")
        except OSError as e:
            console.print(f"  [yellow]![/yellow] Could not remove config file: {e}")

    if config_dir.exists():
        for tmp in config_dir.glob("*.tmp"):
            try:
                tmp.unlink()
            except OSError:
                pass
        try:
            if not any(config_dir.iterdir()):
                config_dir.rmdir()
                console.print(f"  [bold green]✓[/bold green] Removed empty directory: {config_dir}")
        except OSError:
            pass

    # 2. Database files
    if keep_data:
        console.print(f"  [bold cyan]•[/bold cyan] Preserved database and task history: {db_path}")
    else:
        for p in [db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")]:
            if p.exists():
                try:
                    p.unlink()
                except OSError:
                    pass
        if data_dir.exists():
            for p in data_dir.glob("todo.db*"):
                try:
                    p.unlink()
                except OSError:
                    pass
        console.print(f"  [bold green]✓[/bold green] Removed database files at: {db_path}")

        if data_dir.exists():
            try:
                if not any(data_dir.iterdir()):
                    data_dir.rmdir()
                    console.print(f"  [bold green]✓[/bold green] Removed empty directory: {data_dir}")
            except OSError:
                pass

    # 3. Package uninstallation
    method, ok, msg = _detect_and_uninstall_package(skip=skip_pkg)
    if method != "skipped":
        if ok:
            console.print(f"  [bold green]✓[/bold green] {msg}")
        else:
            console.print(f"  [yellow]![/yellow] Package manager ({method}) notice: {msg}")

    console.print("\n[bold green]Extern has been cleanly uninstalled.[/bold green]\n")


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
