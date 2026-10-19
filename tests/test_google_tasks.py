"""Comprehensive test suite for Google Tasks integration, OAuth manager, client, and UI."""

import io
import json
import os
import stat
import tempfile
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
        "access_token": "ya29.mock_access_token",
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
    assert tokens.access_token == "ya29.mock_access_token"
    assert tokens.refresh_token == "mock_refresh_token"
    assert tokens.is_expired is False


def test_oauth_manager_exchange_code(gtasks_env):
    """Verify exchange_code extracts code and exchanges it via POST."""
    oauth_mgr: GoogleOAuthManager = gtasks_env["oauth_mgr"]

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "access_token": "ya29.exchanged_token",
        "refresh_token": "1//mock_refresh",
        "expires_in": 3600,
    }

    with patch("requests.post", return_value=mock_resp) as mock_post:
        # User pasted full URL with code
        url = "http://127.0.0.1:8080/?code=4/0AfgeXmockCode&scope=https://www.googleapis.com/auth/tasks"
        tokens = oauth_mgr.exchange_code(url, "http://127.0.0.1:8080/")

        assert tokens.access_token == "ya29.exchanged_token"
        assert tokens.refresh_token == "1//mock_refresh"
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
        "access_token": "ya29.new_refreshed_token",
        "expires_in": 3600,
    }

    with patch("requests.post", return_value=mock_resp):
        new_token = oauth_mgr.get_valid_access_token()

    assert new_token == "ya29.new_refreshed_token"
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

def test_add_task_menu_has_google_tasks_option(gtasks_env):
    """Verify Add Task menu includes Option 6 for Google Tasks."""
    cfg: Config = gtasks_env["cfg"]
    app = TodoApp(cfg)

    # Inputs: "6" (select Google Tasks), "b" (back from setup screen)
    inputs = ["6\n", "b\n", "q\n"]
    with patch.object(app.google_tasks_view, "run") as mock_gtasks_run:
        with patch("sys.stdin", io.StringIO("".join(inputs))):
            app.handle_add_task_menu()
        mock_gtasks_run.assert_called_once()


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
