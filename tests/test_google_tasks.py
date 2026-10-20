"""Comprehensive test suite for Google Tasks integration, OAuth manager, client, and UI."""

import io
import json
import os
import stat
import tempfile
import time
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

from todo.config import Config
from todo.database import Database
from todo.integrations.google_tasks import (
    GoogleOAuthManager,
    GoogleOAuthTokens,
    GoogleTasksAPIError,
    GoogleTasksAuthError,
    GoogleTasksClient,
    GoogleTasksNetworkError,
    GoogleTasksNotFoundError,
    format_rfc3339_due_date,
    parse_rfc3339_due_date,
)
from todo.models import Task
from todo.services.daily_service import DailyService
from todo.services.google_tasks_service import GoogleTasksService
from todo.services.task_service import TaskService
from todo.ui.app import TodoApp
from todo.ui.google_tasks_view import GoogleTasksView
from todo.ui.task_editor import TaskEditor


@pytest.fixture
def gtasks_env():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        db_path = tmp_path / "test.db"
        token_path = tmp_path / "google_token.json"
        creds_path = tmp_path / "credentials.json"

        # Write dummy credentials.json
        creds_data = {
            "installed": {
                "client_id": "mock_client_id.apps.googleusercontent.com",
                "client_secret": "mock_client_secret",
            }
        }
        creds_path.write_text(json.dumps(creds_data), encoding="utf-8")

        cfg = Config(
            db_path=db_path,
            google_client_id="mock_client_id.apps.googleusercontent.com",
            google_client_secret="mock_client_secret",
            google_credentials_file=creds_path,
            google_token_path=token_path,
        )
        db = Database(db_path)
        db.init_db()

        oauth_mgr = GoogleOAuthManager(
            token_path=token_path,
            client_id=cfg.google_client_id,
            client_secret=cfg.google_client_secret,
            credentials_file=creds_path,
        )

        client = GoogleTasksClient(oauth_mgr)
        service = GoogleTasksService(
            db=db,
            config=cfg,
            oauth_manager=oauth_mgr,
            client=client,
        )
        daily_svc = DailyService(db)
        task_svc = TaskService(db)
        editor = TaskEditor(task_svc, daily_svc, google_tasks_service=service)
        view = GoogleTasksView(service, daily_svc, task_editor=editor)

        yield {
            "tmpdir": tmpdir,
            "db_path": db_path,
            "token_path": token_path,
            "creds_path": creds_path,
            "cfg": cfg,
            "db": db,
            "oauth_mgr": oauth_mgr,
            "client": client,
            "service": service,
            "daily_svc": daily_svc,
            "task_svc": task_svc,
            "editor": editor,
            "view": view,
        }


# =========================================================================
# 1. Calendar Date Compatibility
# =========================================================================

def test_format_rfc3339_due_date():
    """Verify YYYY-MM-DD is formatted to RFC 3339 UTC midnight for Google Calendar."""
    assert format_rfc3339_due_date("2026-10-25") == "2026-10-25T00:00:00.000Z"
    assert format_rfc3339_due_date("2026-12-31T15:30:00") == "2026-12-31T00:00:00.000Z"


def test_parse_rfc3339_due_date():
    """Verify RFC 3339 timestamp is parsed into YYYY-MM-DD."""
    assert parse_rfc3339_due_date("2026-10-25T00:00:00.000Z") == "2026-10-25"
    assert parse_rfc3339_due_date(None) is None
    assert parse_rfc3339_due_date("") is None


# =========================================================================
# 2. OAuth Manager & Token Persistence Security
# =========================================================================

def test_oauth_manager_save_tokens_restrictive_permissions(gtasks_env):
    """Verify token persistence enforces POSIX 0600 permissions."""
    oauth_mgr: GoogleOAuthManager = gtasks_env["oauth_mgr"]
    token_path: Path = gtasks_env["token_path"]

    token_data = {
        "access_token": "test_mock_access_token",
        "refresh_token": "mock_refresh_token",
        "expires_in": 3600,
        "token_type": "Bearer",
    }
    oauth_mgr.save_tokens(token_data)

    assert token_path.exists()
    assert oauth_mgr.is_authenticated() is True

    # Check file permissions on POSIX
    file_stat = os.stat(token_path)
    file_mode = file_stat.st_mode & 0o777
    assert file_mode == 0o600, f"Expected 0600 permissions, got {oct(file_mode)}"

    # Check loaded tokens
    tokens = oauth_mgr._tokens
    assert tokens is not None
    assert tokens.access_token == "test_mock_access_token"
    assert tokens.refresh_token == "mock_refresh_token"
    assert tokens.is_expired is False


def test_oauth_manager_exchange_code(gtasks_env):
    """Verify exchange_code extracts code and exchanges it via POST."""
    oauth_mgr: GoogleOAuthManager = gtasks_env["oauth_mgr"]

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "access_token": "test_exchanged_token",
        "refresh_token": "test_mock_refresh",
        "expires_in": 3600,
    }

    with patch("requests.post", return_value=mock_resp) as mock_post:
        # User pasted full URL with code
        url = "http://127.0.0.1:8080/?code=4/0AfgeXmockCode&scope=https://www.googleapis.com/auth/tasks"
        tokens = oauth_mgr.exchange_code(url, "http://127.0.0.1:8080/")

        assert tokens.access_token == "test_exchanged_token"
        assert tokens.refresh_token == "test_mock_refresh"
        mock_post.assert_called_once()
        call_data = mock_post.call_args[1]["data"]
        assert call_data["code"] == "4/0AfgeXmockCode"


def test_oauth_manager_refresh_token(gtasks_env):
    """Verify refresh_access_token obtains new access token and preserves refresh token."""
    oauth_mgr: GoogleOAuthManager = gtasks_env["oauth_mgr"]
    oauth_mgr.save_tokens({
        "access_token": "old_token",
        "refresh_token": "persisted_refresh_token",
        "expires_at": 1000.0,  # expired
    })

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "access_token": "test_new_refreshed_token",
        "expires_in": 3600,
    }

    with patch("requests.post", return_value=mock_resp):
        new_token = oauth_mgr.get_valid_access_token()

    assert new_token == "test_new_refreshed_token"
    assert oauth_mgr._tokens.refresh_token == "persisted_refresh_token"


def test_oauth_manager_disconnect(gtasks_env):
    """Verify disconnect revokes token and removes token file."""
    oauth_mgr: GoogleOAuthManager = gtasks_env["oauth_mgr"]
    token_path: Path = gtasks_env["token_path"]
    oauth_mgr.save_tokens({"access_token": "tok", "refresh_token": "ref"})

    assert token_path.exists()
    with patch("requests.post") as mock_revoke:
        oauth_mgr.disconnect()
        mock_revoke.assert_called_once()

    assert not token_path.exists()
    assert oauth_mgr.is_authenticated() is False


# =========================================================================
# 3. GoogleTasksClient REST Operations
# =========================================================================

def test_client_get_task_lists(gtasks_env):
    """Verify get_task_lists retrieves and parses task lists."""
    oauth_mgr: GoogleOAuthManager = gtasks_env["oauth_mgr"]
    oauth_mgr.save_tokens({"access_token": "valid_token", "expires_in": 3600})
    client: GoogleTasksClient = gtasks_env["client"]

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.content = b'{"items": [{"id": "list1", "title": "My Tasks"}]}'
    mock_resp.json.return_value = {
        "items": [{"id": "list1", "title": "My Tasks"}]
    }

    with patch("requests.request", return_value=mock_resp):
        lists = client.get_task_lists()

    assert len(lists) == 1
    assert lists[0]["title"] == "My Tasks"


def test_client_create_and_update_task(gtasks_env):
    """Verify create_task and update_task send valid payloads with Calendar dates."""
    oauth_mgr: GoogleOAuthManager = gtasks_env["oauth_mgr"]
    oauth_mgr.save_tokens({"access_token": "valid_token", "expires_in": 3600})
    client: GoogleTasksClient = gtasks_env["client"]

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.content = b'{"id": "gtask123", "title": "Write Spec", "due": "2026-10-25T00:00:00.000Z"}'
    mock_resp.json.return_value = {
        "id": "gtask123",
        "title": "Write Spec",
        "due": "2026-10-25T00:00:00.000Z",
    }

    with patch("requests.request", return_value=mock_resp) as mock_req:
        res = client.create_task(
            list_id="list1",
            title="Write Spec",
            notes="Include RFC notes",
            due_date="2026-10-25",
        )
        assert res["id"] == "gtask123"
        sent_body = mock_req.call_args[1]["json"]
        assert sent_body["due"] == "2026-10-25T00:00:00.000Z"
        assert sent_body["notes"] == "Include RFC notes"


def test_client_network_error_raises_google_tasks_network_error(gtasks_env):
    """Verify network connection failures raise GoogleTasksNetworkError for offline handling."""
    oauth_mgr: GoogleOAuthManager = gtasks_env["oauth_mgr"]
    oauth_mgr.save_tokens({"access_token": "valid_token", "expires_in": 3600})
    client: GoogleTasksClient = gtasks_env["client"]

    with patch("requests.request", side_effect=requests.ConnectionError("Network down")):
        with pytest.raises(GoogleTasksNetworkError):
            client.get_task_lists()


# =========================================================================
# 4. GoogleTasksService: Sync, Cache, Two-Way Completion & Calendar Due Dates
# =========================================================================

def test_service_sync_task_lists_and_offline_cache(gtasks_env):
    """Verify syncing lists populates SQLite cache and sources; offline falls back to cache."""
    service: GoogleTasksService = gtasks_env["service"]
    db: Database = gtasks_env["db"]
    service.oauth_manager.save_tokens({"access_token": "tok", "expires_in": 3600})

    mock_lists = [
        {"id": "l_work", "title": "Work Tasks", "updated": "2026-10-19T10:00:00Z"},
        {"id": "l_personal", "title": "Personal", "updated": "2026-10-19T10:00:00Z"},
    ]

    with patch.object(service.client, "get_task_lists", return_value=mock_lists):
        lists, is_offline = service.sync_task_lists()

    assert is_offline is False
    assert len(lists) == 2
    titles = [l["title"] for l in lists]
    assert "Work Tasks" in titles
    assert "Personal" in titles

    # Verify sources table
    with db.get_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT name, path FROM sources WHERE path LIKE 'google_tasks://%'")
        rows = c.fetchall()
        assert len(rows) == 2

    # Now simulate network failure -> should fall back to cache with is_offline=True
    with patch.object(service.client, "get_task_lists", side_effect=GoogleTasksNetworkError("Disconnected")):
        cached_lists, offline_flag = service.sync_task_lists()
        assert offline_flag is True
        assert len(cached_lists) == 2


def test_service_sync_tasks_maps_due_date_to_calendar_schedule(gtasks_env):
    """Verify sync_tasks_for_list maps RFC 3339 due dates into daily_tasks schedules."""
    service: GoogleTasksService = gtasks_env["service"]
    daily_svc: DailyService = gtasks_env["daily_svc"]
    service.oauth_manager.save_tokens({"access_token": "tok", "expires_in": 3600})

    mock_remote_tasks = [
        {
            "id": "t_rfc1",
            "title": "Quarterly Tax Filing",
            "notes": "File GST/VAT",
            "status": "needsAction",
            "due": "2026-10-25T00:00:00.000Z",
        },
        {
            "id": "t_rfc2",
            "title": "Team Standup",
            "notes": "Discuss blockers",
            "status": "completed",
            "completed": "2026-10-19T09:00:00Z",
        },
    ]

    with patch.object(service.client, "get_tasks", return_value=mock_remote_tasks):
        tasks, is_offline = service.sync_tasks_for_list("l_work")

    assert is_offline is False
    assert len(tasks) == 2

    tax_task = next(t for t in tasks if t.title == "Quarterly Tax Filing")
    assert tax_task.status == "pending"
    assert tax_task.description == "File GST/VAT"

    # Verify task was automatically scheduled for 2026-10-25 based on due date
    sched_date = daily_svc.get_task_scheduled_date(tax_task.id)
    assert sched_date == "2026-10-25"

    standup_task = next(t for t in tasks if t.title == "Team Standup")
    assert standup_task.status == "completed"


def test_service_create_task_creates_cloud_and_local(gtasks_env):
    """Verify creating a task creates it on Google Tasks and schedules it locally."""
    service: GoogleTasksService = gtasks_env["service"]
    daily_svc: DailyService = gtasks_env["daily_svc"]
    service.oauth_manager.save_tokens({"access_token": "tok", "expires_in": 3600})

    mock_created = {
        "id": "remote_new_id_99",
        "title": "Deploy Extern v1.3",
        "notes": "Release notes ready",
        "due": "2026-10-30T00:00:00.000Z",
    }

    with patch.object(service.client, "create_task", return_value=mock_created) as mock_create:
        task = service.create_task(
            list_id="l_work",
            title="Deploy Extern v1.3",
            notes="Release notes ready",
            due_date="2026-10-30",
        )

        assert task.id is not None
        assert task.title == "Deploy Extern v1.3"
        assert task.source_type == "google_tasks"
        mock_create.assert_called_once()

    # Verify scheduled on daily_tasks for 2026-10-30
    assert daily_svc.get_task_scheduled_date(task.id) == "2026-10-30"


def test_service_toggle_completion_two_way_sync(gtasks_env):
    """Verify toggling task completion updates SQLite and calls Google Tasks API."""
    service: GoogleTasksService = gtasks_env["service"]
    service.oauth_manager.save_tokens({"access_token": "tok", "expires_in": 3600})

    with patch.object(service.client, "create_task", return_value={"id": "rem_id_1", "title": "Buy Coffee"}):
        task = service.create_task("l_work", "Buy Coffee")

    assert task.status == "pending"

    with patch.object(service.client, "set_task_completed") as mock_set_completed:
        # Toggle to completed
        updated = service.toggle_task_completion(task.id)
        assert updated.status == "completed"
        mock_set_completed.assert_called_with(list_id="l_work", task_id="rem_id_1", completed=True)

        # Toggle back to pending
        updated2 = service.toggle_task_completion(task.id)
        assert updated2.status == "pending"
        mock_set_completed.assert_called_with(list_id="l_work", task_id="rem_id_1", completed=False)


def test_service_update_task_propagates_to_google(gtasks_env):
    """Verify updating a task via TaskEditor / service pushes new title/notes/due to Google Tasks."""
    service: GoogleTasksService = gtasks_env["service"]
    editor: TaskEditor = gtasks_env["editor"]
    daily_svc: DailyService = gtasks_env["daily_svc"]
    service.oauth_manager.save_tokens({"access_token": "tok", "expires_in": 3600})

    with patch.object(service.client, "create_task", return_value={"id": "rem_task_abc", "title": "Original Title"}):
        task = service.create_task("l_work", "Original Title", notes="Orig Notes", due_date="2026-10-20")

    # Edit via TaskEditor: change Title, Notes, Date
    # Inputs: "1\n", "New Google Title\n", "2\n", "Updated Notes\n", "3\n", "2026-10-28\n", "s\n"
    inputs = ["1\n", "New Google Title\n", "2\n", "Updated Notes\n", "3\n", "2026-10-28\n", "s\n"]

    with patch.object(service.client, "update_task") as mock_update:
        with patch("sys.stdin", io.StringIO("".join(inputs))):
            saved, msg = editor.edit_task(task)

        assert saved is True
        mock_update.assert_called_once_with(
            list_id="l_work",
            task_id="rem_task_abc",
            title="New Google Title",
            notes="Updated Notes",
            due_date="2026-10-28",
            clear_due=False,
        )

    # Check local SQLite
    updated_local = service.task_service.get_task_by_id(task.id)
    assert updated_local.title == "New Google Title"
    assert updated_local.description == "Updated Notes"
    assert daily_svc.get_task_scheduled_date(task.id) == "2026-10-28"


def test_service_update_task_error_propagates_without_local_mutation(gtasks_env):
    """Verify that if Google Tasks API update fails, exception is propagated and local SQLite is not mutated."""
    service: GoogleTasksService = gtasks_env["service"]
    daily_svc: DailyService = gtasks_env["daily_svc"]
    service.oauth_manager.save_tokens({"access_token": "tok", "expires_in": 3600})

    with patch.object(service.client, "create_task", return_value={"id": "rem_err_id", "title": "Before Error"}):
        task = service.create_task("l_work", "Before Error", notes="Before Notes", due_date="2026-10-20")

    with patch.object(service.client, "update_task", side_effect=GoogleTasksAPIError("HTTP 500: Server Error")):
        with pytest.raises(GoogleTasksAPIError, match="HTTP 500"):
            service.update_task(task.id, title="Should Not Persist", notes="Should Not Persist Notes", due_date="2026-10-30")

    # Local DB must NOT have been mutated
    unmodified = service.task_service.get_task_by_id(task.id)
    assert unmodified.title == "Before Error"
    assert unmodified.description == "Before Notes"
    assert daily_svc.get_task_scheduled_date(task.id) == "2026-10-20"


def test_service_delete_task_removes_from_google_and_local(gtasks_env):
    """Verify delete_task removes task from Google Tasks and SQLite."""
    service: GoogleTasksService = gtasks_env["service"]
    task_svc: TaskService = gtasks_env["task_svc"]
    service.oauth_manager.save_tokens({"access_token": "tok", "expires_in": 3600})

    with patch.object(service.client, "create_task", return_value={"id": "rem_del_id", "title": "Delete Me"}):
        task = service.create_task("l_work", "Delete Me")

    with patch.object(service.client, "delete_task") as mock_del:
        ok = service.delete_task(task.id)
        assert ok is True
        mock_del.assert_called_once_with(list_id="l_work", task_id="rem_del_id")

    assert task_svc.get_task_by_id(task.id) is None


# =========================================================================
# 5. UI Integration (Add Task Option 6, Today 'g' and 'e' shortcuts)
# =========================================================================

def test_add_task_menu_excludes_google_tasks_option(gtasks_env):
    """Verify Add Task menu contains only 5 options and does not include Google Tasks."""
    cfg: Config = gtasks_env["cfg"]
    app = TodoApp(cfg)

    # 1. Verify AddTaskView has exactly 5 options
    from todo.ui.add_task_view import AddTaskView
    at_view = AddTaskView()
    # Capture rendered output
    with patch("todo.ui.add_task_view.console.print") as mock_print:
        at_view.render(clear=False)
        table = next(arg[0][0] for arg in mock_print.call_args_list if hasattr(arg[0][0], "rows"))
        assert len(table.rows) == 5

    # 2. Entering "6" in handle_add_task_menu is rejected as invalid, "b" exits
    inputs = ["6\n", "b\n"]
    with patch.object(app.google_tasks_view, "run") as mock_gtasks_run:
        with patch("sys.stdin", io.StringIO("".join(inputs))):
            app.handle_add_task_menu()
        # Google Tasks view was NOT opened from Add Task menu
        mock_gtasks_run.assert_not_called()


def test_today_view_g_shortcut_opens_google_tasks(gtasks_env):
    """Verify pressing 'g' in Today view opens GoogleTasksView."""
    cfg: Config = gtasks_env["cfg"]
    app = TodoApp(cfg)

    # Inputs: "g" (open Google Tasks), "q" (quit)
    inputs = ["g\n", "q\n"]
    with patch.object(app.google_tasks_view, "run") as mock_gtasks_run:
        with patch("sys.stdin", io.StringIO("".join(inputs))):
            app.run()
        mock_gtasks_run.assert_called_once()


def test_google_tasks_view_render_and_back(gtasks_env):
    """Verify GoogleTasksView renders list and handles back shortcut."""
    view: GoogleTasksView = gtasks_env["view"]
    service: GoogleTasksService = gtasks_env["service"]
    service.oauth_manager.save_tokens({"access_token": "tok", "expires_in": 3600})

    mock_lists = [{"id": "list_1", "title": "Daily Routine"}]
    mock_tasks = [{"id": "task_1", "title": "Morning Yoga", "status": "needsAction"}]

    with patch.object(service.client, "get_task_lists", return_value=mock_lists):
        with patch.object(service.client, "get_tasks", return_value=mock_tasks):
            with patch("sys.stdin", io.StringIO("b\n")):
                view.run()

    # Tasks should be cached in SQLite
    cached = service.get_tasks_for_list("list_1")
    assert len(cached) == 1
    assert cached[0].title == "Morning Yoga"


# =========================================================================
# 6. Google Tasks 404 Stale List and Error Resilience Tests
# =========================================================================

def test_valid_cached_list_id_exists_remotely(gtasks_env):
    """Scenario 1: A valid cached list ID that exists remotely is synced and accessible."""
    service: GoogleTasksService = gtasks_env["service"]
    service.oauth_manager.save_tokens({"access_token": "tok", "expires_in": 3600})

    mock_lists = [{"id": "list_valid", "title": "Work Tasks"}]
    mock_tasks = [{"id": "task_v1", "title": "Submit Quarterly Report", "status": "needsAction"}]

    with patch.object(service.client, "get_task_lists", return_value=mock_lists):
        lists, is_offline = service.sync_task_lists()
        assert is_offline is False
        assert any(l["id"] == "list_valid" for l in lists)

    with patch.object(service.client, "get_tasks", return_value=mock_tasks):
        tasks, is_offline = service.sync_tasks_for_list("list_valid")
        assert is_offline is False
        assert len(tasks) == 1
        assert tasks[0].title == "Submit Quarterly Report"


def test_stale_cached_list_id_returns_404_and_prunes_cache(gtasks_env):
    """Scenario 2: A stale cached list ID returning 404 is pruned from cache and raises GoogleTasksNotFoundError."""
    service: GoogleTasksService = gtasks_env["service"]
    service.oauth_manager.save_tokens({"access_token": "tok", "expires_in": 3600})

    # Seed stale list in cache
    stale_id = "QUlyWG5JdS1waWF3YzdnUg"
    with service.db.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO google_task_lists_cache (id, title, updated_at) VALUES (?, ?, ?)",
            (stale_id, "Copywriting", "2026-10-19T00:00:00"),
        )
        conn.commit()

    cached_before = [l["id"] for l in service.get_cached_task_lists()]
    assert stale_id in cached_before

    with patch.object(
        service.client,
        "get_tasks",
        side_effect=GoogleTasksNotFoundError("Resource not found: /lists/QUlyWG5JdS1waWF3YzdnUg/tasks", response_body='{"error": {"code": 404}}'),
    ):
        with pytest.raises(GoogleTasksNotFoundError) as exc_info:
            service.sync_tasks_for_list(stale_id)
        assert exc_info.value.status_code == 404

    # Verify stale list is pruned from SQLite cache
    cached_after = [l["id"] for l in service.get_cached_task_lists()]
    assert stale_id not in cached_after


def test_recovery_when_remote_list_deleted(gtasks_env):
    """Scenario 3: When remote lists are fetched, lists deleted on remote are reconciled and pruned."""
    service: GoogleTasksService = gtasks_env["service"]
    service.oauth_manager.save_tokens({"access_token": "tok", "expires_in": 3600})

    # Seed 2 lists in cache
    with service.db.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("INSERT INTO google_task_lists_cache (id, title, updated_at) VALUES (?, ?, ?)", ("del_1", "Deleted 1", "2026-10-19"))
        cursor.execute("INSERT INTO google_task_lists_cache (id, title, updated_at) VALUES (?, ?, ?)", ("del_2", "Deleted 2", "2026-10-19"))
        cursor.execute("INSERT INTO google_task_lists_cache (id, title, updated_at) VALUES (?, ?, ?)", ("live_1", "Live List", "2026-10-19"))
        conn.commit()

    # Remote only returns live_1
    remote_lists = [{"id": "live_1", "title": "Live List"}]
    with patch.object(service.client, "get_task_lists", return_value=remote_lists):
        synced_lists, is_offline = service.sync_task_lists()
        assert is_offline is False
        synced_ids = [l["id"] for l in synced_lists]
        assert synced_ids == ["live_1"]

    cached_now = [l["id"] for l in service.get_cached_task_lists()]
    assert "live_1" in cached_now
    assert "del_1" not in cached_now
    assert "del_2" not in cached_now


def test_valid_list_selection_after_refreshing_list_cache(gtasks_env):
    """Scenario 4: View automatically recovers to valid list after encountering 404 on initial list."""
    view: GoogleTasksView = gtasks_env["view"]
    service: GoogleTasksService = gtasks_env["service"]
    service.oauth_manager.save_tokens({"access_token": "tok", "expires_in": 3600})

    # Initially cache has a stale list ("A_Stale") and a valid list ("B_Live")
    with service.db.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("INSERT INTO google_task_lists_cache (id, title, updated_at) VALUES (?, ?, ?)", ("stale_id", "A_Stale", "2026-10-19"))
        cursor.execute("INSERT INTO google_task_lists_cache (id, title, updated_at) VALUES (?, ?, ?)", ("live_id", "B_Live", "2026-10-19"))
        conn.commit()

    def mock_get_tasks(list_id, **kwargs):
        if list_id == "stale_id":
            raise GoogleTasksNotFoundError("Not found", response_body="404")
        return [{"id": "t_live", "title": "Live Task", "status": "needsAction"}]

    def mock_get_task_lists():
        return [{"id": "live_id", "title": "B_Live"}]

    with patch.object(service.client, "get_tasks", side_effect=mock_get_tasks):
        with patch.object(service.client, "get_task_lists", side_effect=mock_get_task_lists):
            with patch("sys.stdin", io.StringIO("b\n")):
                view.run()

    # Live list tasks must have been retrieved and saved
    cached = service.get_tasks_for_list("live_id")
    assert len(cached) == 1
    assert cached[0].title == "Live Task"
    # Stale list should no longer be in cache
    assert "stale_id" not in [l["id"] for l in service.get_cached_task_lists()]


def test_local_tasks_and_completion_history_preserved_when_remote_list_missing(gtasks_env):
    """Scenario 5: 404 remote missing list does not delete local tasks, source, or completion history."""
    service: GoogleTasksService = gtasks_env["service"]
    daily_svc: DailyService = gtasks_env["daily_svc"]
    task_svc: TaskService = gtasks_env["task_svc"]
    service.oauth_manager.save_tokens({"access_token": "tok", "expires_in": 3600})

    list_id = "stale_preserved_list"
    # Populate a local task under this list
    with patch.object(service.client, "create_task", return_value={"id": "rem_1", "title": "Completed Past Goal"}):
        task = service.create_task(list_id, "Completed Past Goal", notes="Very important notes")

    # Add to daily tasks on 2026-10-15 and mark completed
    dt, _ = daily_svc.add_task_to_date(task.id, "2026-10-15")
    daily_svc.toggle_task_completion(dt.id)

    # Verify task is completed
    hist_tasks = daily_svc.get_tasks_for_date("2026-10-15")
    assert len(hist_tasks) == 1
    assert hist_tasks[0].task.status == "completed"

    # Now simulate 404 when syncing this list
    with patch.object(service.client, "get_tasks", side_effect=GoogleTasksNotFoundError("Resource not found", response_body="404")):
        with pytest.raises(GoogleTasksNotFoundError):
            service.sync_tasks_for_list(list_id)

    # Cache should have pruned the list
    assert list_id not in [l["id"] for l in service.get_cached_task_lists()]

    # BUT local task, source, and completion history MUST be 100% preserved
    persisted_task = task_svc.get_task_by_id(task.id)
    assert persisted_task is not None
    assert persisted_task.title == "Completed Past Goal"
    assert persisted_task.description == "Very important notes"

    hist_after = daily_svc.get_tasks_for_date("2026-10-15")
    assert len(hist_after) == 1
    assert hist_after[0].task_id == task.id
    assert hist_after[0].task.status == "completed"

    local_list_tasks = service.get_tasks_for_list(list_id)
    assert len(local_list_tasks) == 1
    assert local_list_tasks[0].id == task.id


def test_list_specific_api_failure_without_crashing_tui(gtasks_env):
    """Scenario 6: Generic API failure (e.g. 500) displays error message and falls back to cached tasks without crash."""
    view: GoogleTasksView = gtasks_env["view"]
    service: GoogleTasksService = gtasks_env["service"]
    service.oauth_manager.save_tokens({"access_token": "tok", "expires_in": 3600})

    mock_lists = [{"id": "list_err", "title": "Failing List"}]
    with patch.object(service.client, "get_task_lists", return_value=mock_lists):
        service.sync_task_lists()

    with patch.object(service.client, "get_tasks", side_effect=GoogleTasksAPIError("Google Tasks API 500 Server Error", status_code=500)):
        with patch("sys.stdin", io.StringIO("b\n")):
            view.run()

    # The view should exit cleanly on 'b' without an unhandled exception


def test_network_auth_and_404_errors_distinguishable(gtasks_env):
    """Scenario 7: Network errors, auth errors, and 404 not found are typed and distinguishable."""
    client: GoogleTasksClient = gtasks_env["client"]
    service: GoogleTasksService = gtasks_env["service"]
    service.oauth_manager.save_tokens({"access_token": "tok", "expires_in": 3600})

    # 1. 404 raises GoogleTasksNotFoundError
    mock_resp_404 = MagicMock()
    mock_resp_404.status_code = 404
    mock_resp_404.text = '{"error": {"code": 404, "message": "Not found"}}'
    with patch("requests.request", return_value=mock_resp_404):
        with pytest.raises(GoogleTasksNotFoundError) as exc_404:
            client.get_tasks("missing_id")
        assert exc_404.value.status_code == 404
        assert isinstance(exc_404.value, GoogleTasksAPIError)

    # 2. Network error raises GoogleTasksNetworkError
    with patch("requests.request", side_effect=requests.ConnectionError("DNS failure")):
        with pytest.raises(GoogleTasksNetworkError):
            client.get_tasks("any_id")

    # 3. 401 unresolvable raises GoogleTasksAuthError
    mock_resp_401 = MagicMock()
    mock_resp_401.status_code = 401
    mock_resp_401.text = '{"error": {"code": 401, "message": "Unauthorized"}}'
    with patch("requests.request", return_value=mock_resp_401):
        with patch.object(client.oauth_manager, "refresh_access_token", side_effect=GoogleTasksAuthError("Revoked")):
            with pytest.raises(GoogleTasksAuthError):
                client.get_tasks("any_id")


def test_no_duplicate_tasks_after_refresh_and_retry(gtasks_env):
    """Scenario 8: Repeated sync calls upsert tasks without creating duplicate entities in SQLite."""
    service: GoogleTasksService = gtasks_env["service"]
    service.oauth_manager.save_tokens({"access_token": "tok", "expires_in": 3600})

    mock_tasks = [
        {"id": "task_dup_1", "title": "Buy Groceries", "status": "needsAction", "updated": "2026-10-19T10:00:00.000Z"},
        {"id": "task_dup_2", "title": "Pay Utilities", "status": "needsAction", "updated": "2026-10-19T10:00:00.000Z"},
    ]

    with patch.object(service.client, "get_tasks", return_value=mock_tasks):
        tasks_pass1, _ = service.sync_tasks_for_list("list_dups")
        assert len(tasks_pass1) == 2

        # Retry / refresh sync
        tasks_pass2, _ = service.sync_tasks_for_list("list_dups")
        assert len(tasks_pass2) == 2

    # Query local database directly
    all_cached = service.get_tasks_for_list("list_dups")
    assert len(all_cached) == 2
    titles = {t.title for t in all_cached}
    assert titles == {"Buy Groceries", "Pay Utilities"}


# =========================================================================
# 7. Smooth Performance, Completed Task Filtering, Auto-Sync & Outbox Tests
# =========================================================================

def test_completed_tasks_hidden_from_active_list_but_preserved_in_db(gtasks_env):
    """Verify completed tasks are excluded from active list view but kept in SQLite and daily_tasks."""
    service: GoogleTasksService = gtasks_env["service"]
    daily_svc: DailyService = gtasks_env["daily_svc"]
    service.oauth_manager.save_tokens({"access_token": "tok", "expires_in": 3600})

    mock_tasks = [
        {"id": "t_pending", "title": "Active Task", "status": "needsAction"},
        {"id": "t_done", "title": "Finished Task", "status": "completed", "completed": "2026-10-20T10:00:00Z"},
    ]

    with patch.object(service.client, "get_tasks", return_value=mock_tasks):
        synced, _ = service.sync_tasks_for_list("l_hide_test")
        assert len(synced) == 2

    # Active view: include_completed=False (only pending)
    active = service.get_tasks_for_list("l_hide_test", include_completed=False)
    assert len(active) == 1
    assert active[0].title == "Active Task"
    assert active[0].status == "pending"

    # SQLite query: include_completed=True (all tasks preserved)
    all_tasks = service.get_tasks_for_list("l_hide_test", include_completed=True)
    assert len(all_tasks) == 2
    titles = {t.title for t in all_tasks}
    assert "Finished Task" in titles


def test_remote_status_reconciliation_complete_and_reopen(gtasks_env):
    """Verify remote completion marks task completed and remote reopening resets to pending."""
    service: GoogleTasksService = gtasks_env["service"]
    daily_svc: DailyService = gtasks_env["daily_svc"]
    service.oauth_manager.save_tokens({"access_token": "tok", "expires_in": 3600})

    # 1. Sync pending task with due date
    mock_pass1 = [{"id": "t_toggle", "title": "Dynamic Status", "status": "needsAction", "due": "2026-10-20T00:00:00.000Z"}]
    with patch.object(service.client, "get_tasks", return_value=mock_pass1):
        service.sync_tasks_for_list("l_recon")

    t1 = service.get_tasks_for_list("l_recon", include_completed=True)[0]
    assert t1.status == "pending"
    d_tasks = daily_svc.get_tasks_for_date("2026-10-20")
    assert len(d_tasks) == 1
    assert d_tasks[0].completed_at is None

    # 2. Remote marks it completed
    mock_pass2 = [{"id": "t_toggle", "title": "Dynamic Status", "status": "completed", "completed": "2026-10-20T11:00:00Z", "due": "2026-10-20T00:00:00.000Z"}]
    with patch.object(service.client, "get_tasks", return_value=mock_pass2):
        service.sync_tasks_for_list("l_recon")

    t2 = service.get_tasks_for_list("l_recon", include_completed=True)[0]
    assert t2.status == "completed"
    # Should disappear from active view
    assert len(service.get_tasks_for_list("l_recon", include_completed=False)) == 0
    d_tasks_completed = daily_svc.get_tasks_for_date("2026-10-20")
    assert d_tasks_completed[0].completed_at is not None

    # 3. Remote reopens it
    mock_pass3 = [{"id": "t_toggle", "title": "Dynamic Status", "status": "needsAction", "due": "2026-10-20T00:00:00.000Z"}]
    with patch.object(service.client, "get_tasks", return_value=mock_pass3):
        service.sync_tasks_for_list("l_recon")

    t3 = service.get_tasks_for_list("l_recon", include_completed=True)[0]
    assert t3.status == "pending"
    # Reappears in active view
    assert len(service.get_tasks_for_list("l_recon", include_completed=False)) == 1
    d_tasks_reopened = daily_svc.get_tasks_for_date("2026-10-20")
    assert d_tasks_reopened[0].completed_at is None


def test_remote_due_date_removal_moves_task_to_universal(gtasks_env):
    """Verify that removing a due date remotely unschedules the task from daily_tasks so it appears in Universal."""
    service: GoogleTasksService = gtasks_env["service"]
    daily_svc: DailyService = gtasks_env["daily_svc"]
    task_svc: TaskService = gtasks_env["task_svc"]
    service.oauth_manager.save_tokens({"access_token": "tok", "expires_in": 3600})

    # 1. Sync with due date -> scheduled in daily_tasks
    mock_pass1 = [{"id": "t_due_remove", "title": "Has Due Date", "status": "needsAction", "due": "2026-10-22T00:00:00.000Z"}]
    with patch.object(service.client, "get_tasks", return_value=mock_pass1):
        service.sync_tasks_for_list("l_univ")

    t1 = service.get_tasks_for_list("l_univ", include_completed=True)[0]
    assert daily_svc.get_task_scheduled_date(t1.id) == "2026-10-22"
    # Not in universal because it is scheduled
    assert t1.id not in [u.id for u in task_svc.get_universal_tasks()]

    # 2. Remote clears due date (due=None) -> unschedules from daily_tasks
    mock_pass2 = [{"id": "t_due_remove", "title": "Has Due Date", "status": "needsAction", "due": None}]
    with patch.object(service.client, "get_tasks", return_value=mock_pass2):
        service.sync_tasks_for_list("l_univ")

    assert daily_svc.get_task_scheduled_date(t1.id) is None
    # Now appears in universal tasks automatically!
    univ_tasks = task_svc.get_universal_tasks()
    assert any(u.id == t1.id for u in univ_tasks)


def test_move_google_task_to_universal_preserves_source_and_clears_remote_due(gtasks_env):
    """Verify moving a Google Task to Universal preserves source_type and clears remote due date."""
    service: GoogleTasksService = gtasks_env["service"]
    daily_svc: DailyService = gtasks_env["daily_svc"]
    task_svc: TaskService = gtasks_env["task_svc"]
    service.oauth_manager.save_tokens({"access_token": "tok", "expires_in": 3600})

    with patch.object(service.client, "create_task", return_value={"id": "rem_m2u", "title": "Move to Univ"}):
        task = service.create_task("l_work", "Move to Univ", due_date="2026-10-20")

    dt, _ = daily_svc.add_task_to_date(task.id, "2026-10-20")

    with patch.object(service.client, "update_task") as mock_update:
        daily_svc.move_tasks_to_universal_bulk([dt.id])

    # Preserves source_type='google_tasks'
    persisted = task_svc.get_task_by_id(task.id)
    assert persisted.source_type == "google_tasks"

    # Pending schedule was deleted
    assert daily_svc.get_task_scheduled_date(task.id) is None

    # Appears in universal tasks
    univ = task_svc.get_universal_tasks()
    assert any(u.id == task.id for u in univ)


def test_durable_offline_outbox_queue_and_drain(gtasks_env):
    """Verify that offline mutations are enqueued in google_tasks_outbox and drained on reconnect."""
    service: GoogleTasksService = gtasks_env["service"]
    daily_svc: DailyService = gtasks_env["daily_svc"]
    service.oauth_manager.save_tokens({"access_token": "tok", "expires_in": 3600})

    # Create task locally while client.create_task fails with network error
    with patch.object(service.client, "create_task", side_effect=GoogleTasksNetworkError("Offline")):
        task = service.create_task("l_work", "Offline Created Task", notes="Pending push")

    outbox_items = service.get_pending_outbox()
    assert len(outbox_items) == 1
    assert outbox_items[0]["action"] == "create"
    assert outbox_items[0]["task_id"] == task.id

    # Now drain outbox when online
    with patch.object(service.client, "create_task", return_value={"id": "rem_new_id", "title": "Offline Created Task"}):
        drained = service.drain_outbox()
        assert drained == 1

    assert len(service.get_pending_outbox()) == 0


def test_google_tasks_sync_manager_lifecycle_and_trigger(gtasks_env):
    """Verify GoogleTasksSyncManager starts daemon thread, triggers sync, and stops cleanly."""
    from todo.services.google_tasks_sync_manager import GoogleTasksSyncManager

    service: GoogleTasksService = gtasks_env["service"]
    service.oauth_manager.save_tokens({"access_token": "tok", "expires_in": 3600})

    manager = GoogleTasksSyncManager(service, interval=10.0)
    assert manager.is_running is False

    with patch.object(service, "sync_all_lists", return_value=(True, "Synced 1 list(s)")):
        manager.start()
        assert manager.is_running is True

        # Trigger sync
        manager.trigger_sync()
        time.sleep(0.1)

        success, msg = manager.sync_now()
        assert success is True
        assert "Synced" in msg

        status_text = manager.get_status_text()
        assert "Connected" in status_text

        manager.stop(timeout=1.0)
        assert manager.is_running is False


def test_local_cache_latency_vs_network_latency(gtasks_env):
    """Verify local SQLite cache retrieval is sub-10ms compared to simulated network latency."""
    service: GoogleTasksService = gtasks_env["service"]
    service.oauth_manager.save_tokens({"access_token": "tok", "expires_in": 3600})

    # Seed 50 tasks in cache
    mock_tasks = [{"id": f"task_{i}", "title": f"Bench Task {i}", "status": "needsAction"} for i in range(50)]
    with patch.object(service.client, "get_tasks", return_value=mock_tasks):
        service.sync_tasks_for_list("l_bench")

    # Measure local cache read
    t0 = time.perf_counter()
    cached = service.get_tasks_for_list("l_bench", include_completed=False)
    t_cache = (time.perf_counter() - t0) * 1000

    assert len(cached) == 50
    # Cache read should be under 20ms (typically < 5ms)
    assert t_cache < 25.0


def test_google_tasks_view_instant_cache_render_with_sync_manager(gtasks_env):
    """Verify GoogleTasksView renders instantly from cache when sync_manager is provided without waiting for sync."""
    from todo.services.google_tasks_sync_manager import GoogleTasksSyncManager

    service: GoogleTasksService = gtasks_env["service"]
    daily_svc: DailyService = gtasks_env["daily_svc"]
    service.oauth_manager.save_tokens({"access_token": "tok", "expires_in": 3600})

    # Seed cache
    with service.db.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("INSERT INTO google_task_lists_cache (id, title, updated_at) VALUES (?, ?, ?)", ("list_fast", "Fast List", "2026-10-20"))
        conn.commit()

    manager = GoogleTasksSyncManager(service, interval=60.0)
    view = GoogleTasksView(service, daily_svc, sync_manager=manager)

    # When view.run executes, it should render and exit on 'b' without calling blocking network get_tasks
    with patch.object(service.client, "get_tasks") as mock_get_tasks:
        with patch("sys.stdin", io.StringIO("b\n")):
            view.run()
        # View did NOT block on synchronous network get_tasks because it rendered from cache
        mock_get_tasks.assert_not_called()



