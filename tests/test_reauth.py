"""Tests for Google Tasks and Google Calendar reauthorization, status visibility, and API verification."""

import json
import os
import stat
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from todo.cli import cli
from todo.config import Config
from todo.database import Database
from todo.integrations.google_calendar import GoogleCalendarClient, GoogleCalendarError
from todo.integrations.google_tasks import GoogleOAuthManager, GoogleOAuthTokens
from todo.services.daily_service import DailyService
from todo.services.google_calendar_service import GoogleCalendarService
from todo.services.google_tasks_service import GoogleTasksService
from todo.services.google_tasks_sync_manager import GoogleTasksSyncManager
from todo.services.task_service import TaskService
from todo.ui.console import render_footer
from todo.ui.today_view import TodayView


@pytest.fixture
def test_env(tmp_path):
    """Create an isolated test environment with temporary DB and token path."""
    db_path = tmp_path / "test_reauth.db"
    db = Database(db_path)
    db.init_db()

    token_path = tmp_path / "google_token.json"
    credentials_path = tmp_path / "credentials.json"
    credentials_path.write_text(json.dumps({
        "installed": {
            "client_id": "test_id.apps.googleusercontent.com",
            "client_secret": "test_secret_123",
            "redirect_uris": ["http://localhost"],
        }
    }))

    config = Config(
        db_path=db_path,
        google_token_path=token_path,
        google_credentials_file=credentials_path,
        google_tasks_enabled=True,
        google_calendar_enabled=True,
    )

    oauth_manager = GoogleOAuthManager(
        token_path=token_path,
        credentials_file=credentials_path,
    )

    tasks_svc = GoogleTasksService(db=db, oauth_manager=oauth_manager, config=config)
    cal_svc = GoogleCalendarService(db=db, oauth_manager=oauth_manager, config=config)
    sync_mgr = GoogleTasksSyncManager(google_tasks_service=tasks_svc, google_calendar_service=cal_svc)

    return {
        "db": db,
        "config": config,
        "token_path": token_path,
        "credentials_path": credentials_path,
        "oauth_manager": oauth_manager,
        "tasks_svc": tasks_svc,
        "cal_svc": cal_svc,
        "sync_mgr": sync_mgr,
    }


# =============================================================================
# 1. Scope Detection & Metadata Tests
# =============================================================================


def test_missing_calendar_scope_detection(test_env):
    """Existing token with Tasks scope only detects that Calendar scope is missing."""
    oauth_mgr: GoogleOAuthManager = test_env["oauth_manager"]
    cal_svc: GoogleCalendarService = test_env["cal_svc"]
    sync_mgr: GoogleTasksSyncManager = test_env["sync_mgr"]

    oauth_mgr.save_tokens({
        "access_token": "mock_access",
        "refresh_token": "mock_refresh",
        "expires_in": 3600,
        "scope": "https://www.googleapis.com/auth/tasks",
    })

    assert oauth_mgr.is_authenticated() is True
    assert oauth_mgr.has_tasks_scope() is True
    assert oauth_mgr.has_calendar_scope() is False
    assert oauth_mgr.needs_reauthorization_for_calendar() is True
    assert cal_svc.needs_reauthorization() is True

    status = sync_mgr.get_status_text()
    assert "Tasks Connected · Calendar Auth Needed" in status


def test_existing_token_with_both_scopes(test_env):
    """Token with both scopes detects that Calendar scope is granted."""
    oauth_mgr: GoogleOAuthManager = test_env["oauth_manager"]
    cal_svc: GoogleCalendarService = test_env["cal_svc"]
    sync_mgr: GoogleTasksSyncManager = test_env["sync_mgr"]

    oauth_mgr.save_tokens({
        "access_token": "mock_access",
        "refresh_token": "mock_refresh",
        "expires_in": 3600,
        "scope": "https://www.googleapis.com/auth/tasks https://www.googleapis.com/auth/calendar.events",
    })

    assert oauth_mgr.is_authenticated() is True
    assert oauth_mgr.has_tasks_scope() is True
    assert oauth_mgr.has_calendar_scope() is True
    assert oauth_mgr.needs_reauthorization_for_calendar() is False
    assert cal_svc.needs_reauthorization() is False

    status = sync_mgr.get_status_text()
    assert "Tasks Connected · Calendar Connected" in status


def test_malformed_or_absent_scope_metadata(test_env):
    """Malformed or empty scope string is handled gracefully without crashing."""
    oauth_mgr: GoogleOAuthManager = test_env["oauth_manager"]

    # Test with empty scope
    oauth_mgr.save_tokens({
        "access_token": "mock_access",
        "refresh_token": "mock_refresh",
        "expires_in": 3600,
        "scope": "",
    })
    assert oauth_mgr.has_calendar_scope() is False
    assert oauth_mgr.has_tasks_scope() is False

    # Test with None scope (should fallback to existing or default)
    tokens = GoogleOAuthTokens(access_token="tok", scope="")
    assert tokens.scope == ""
    assert oauth_mgr.get_granted_scopes() == []


def test_save_tokens_preserves_scope_on_refresh(test_env):
    """Token refresh omitting 'scope' preserves the previously granted scopes."""
    oauth_mgr: GoogleOAuthManager = test_env["oauth_manager"]

    # Initial token has tasks only
    oauth_mgr.save_tokens({
        "access_token": "tok_1",
        "refresh_token": "ref_1",
        "scope": "https://www.googleapis.com/auth/tasks",
    })
    assert oauth_mgr.has_calendar_scope() is False

    # Refresh response has new access token but no scope field
    oauth_mgr.save_tokens({
        "access_token": "tok_2",
        "expires_in": 3599,
        # 'scope' is omitted by Google
    })
    # Scope must remain tasks only, NOT reset to DEFAULT_SCOPES!
    assert oauth_mgr.has_calendar_scope() is False
    assert oauth_mgr.has_tasks_scope() is True
    assert oauth_mgr._tokens.refresh_token == "ref_1"


def test_save_tokens_persists_with_0600_permissions(test_env):
    """Token file is persisted with secure 0600 POSIX permissions."""
    oauth_mgr: GoogleOAuthManager = test_env["oauth_manager"]
    token_path: Path = test_env["token_path"]

    oauth_mgr.save_tokens({
        "access_token": "tok_secret",
        "refresh_token": "ref_secret",
        "expires_in": 3600,
    })

    assert token_path.exists()
    mode = stat.S_IMODE(os.stat(token_path).st_mode)
    assert mode == 0o600


# =============================================================================
# 2. Configuration & Policy Tests
# =============================================================================


def test_calendar_disabled_suppresses_auth_needed(test_env):
    """When calendar sync is disabled in config, 'Calendar Auth Needed' is suppressed."""
    config: Config = test_env["config"]
    config.google_calendar_enabled = False
    cal_svc: GoogleCalendarService = test_env["cal_svc"]
    sync_mgr: GoogleTasksSyncManager = test_env["sync_mgr"]
    oauth_mgr: GoogleOAuthManager = test_env["oauth_manager"]

    oauth_mgr.save_tokens({
        "access_token": "tok",
        "refresh_token": "ref",
        "scope": "https://www.googleapis.com/auth/tasks",
    })

    assert cal_svc.needs_reauthorization() is False
    status = sync_mgr.get_status_text()
    assert "Calendar Auth Needed" not in status
    assert "Connected" in status


# =============================================================================
# 3. Authorization URL & Exchange Tests
# =============================================================================


def test_reauthorization_url_contains_all_scopes_and_incremental_param(test_env):
    """Generated authorization URL contains tasks, calendar.events, consent prompt, and incremental auth."""
    import urllib.parse
    oauth_mgr: GoogleOAuthManager = test_env["oauth_manager"]
    url = oauth_mgr.get_authorization_url(redirect_uri="http://127.0.0.1:8080/")
    unquoted = urllib.parse.unquote(url)

    assert "https://www.googleapis.com/auth/tasks" in unquoted
    assert "https://www.googleapis.com/auth/calendar.events" in unquoted
    assert "prompt=consent" in unquoted
    assert "include_granted_scopes=true" in unquoted
    assert "access_type=offline" in unquoted


def test_exchange_code_updates_scopes_and_preserves_refresh_token(test_env):
    """Exchanging authorization code saves new scopes and retains existing refresh token."""
    oauth_mgr: GoogleOAuthManager = test_env["oauth_manager"]

    oauth_mgr.save_tokens({
        "access_token": "old_access",
        "refresh_token": "persistent_refresh_token",
        "scope": "https://www.googleapis.com/auth/tasks",
    })

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "access_token": "new_access_token",
        "expires_in": 3600,
        "scope": "https://www.googleapis.com/auth/tasks https://www.googleapis.com/auth/calendar.events",
        "token_type": "Bearer",
        # refresh_token omitted by Google
    }

    with patch("requests.post", return_value=mock_resp):
        tokens = oauth_mgr.exchange_code("mock_auth_code", "http://127.0.0.1:8080/")

    assert tokens.access_token == "new_access_token"
    assert tokens.refresh_token == "persistent_refresh_token"
    assert oauth_mgr.has_calendar_scope() is True
    assert oauth_mgr.needs_reauthorization_for_calendar() is False


# =============================================================================
# 4. Google Calendar API Verification Tests
# =============================================================================


def test_calendar_api_verification_primary_calendar(test_env):
    """Safe calendar verification confirms target calendar access without mutating events."""
    oauth_mgr: GoogleOAuthManager = test_env["oauth_manager"]
    oauth_mgr.save_tokens({
        "access_token": "valid_token",
        "refresh_token": "valid_refresh",
        "scope": "https://www.googleapis.com/auth/tasks https://www.googleapis.com/auth/calendar.events",
    })

    client = GoogleCalendarClient(oauth_manager=oauth_mgr)

    with patch.object(
        client,
        "_request",
        return_value={
            "summary": "Nikhil Calendar",
            "timeZone": "Asia/Kolkata",
            "items": [],
        },
    ) as mock_req:
        result = client.verify_access(calendar_id="primary")

    assert result["verified"] is True
    assert result["calendar_id"] == "primary"
    assert result["summary"] == "Nikhil Calendar"
    assert result["time_zone"] == "Asia/Kolkata"

    # Must call GET calendars/primary/events?maxResults=1 (READ ONLY)
    mock_req.assert_called_once_with("GET", "calendars/primary/events", params={"maxResults": 1})


def test_calendar_sync_list_falls_back_gracefully_on_narrow_scope(test_env):
    """When calendarList.list returns 403 due to narrow scope, primary calendar is still seeded and usable."""
    cal_svc: GoogleCalendarService = test_env["cal_svc"]
    oauth_mgr: GoogleOAuthManager = test_env["oauth_manager"]
    oauth_mgr.save_tokens({
        "access_token": "valid_token",
        "refresh_token": "valid_refresh",
        "scope": "https://www.googleapis.com/auth/tasks https://www.googleapis.com/auth/calendar.events",
    })

    # Simulate 403 on users/me/calendarList
    with patch.object(cal_svc.client, "list_calendars", side_effect=GoogleCalendarError("403 Forbidden")):
        calendars = cal_svc.sync_calendars_list()

    assert len(calendars) >= 1
    assert calendars[0]["id"] == "primary"
    assert calendars[0]["primary_calendar"] == 1


# =============================================================================
# 5. UI Status Rendering Tests
# =============================================================================


def test_today_view_omits_sync_status_in_footer(test_env, capsys):
    """TodayView.render omits sync status text from the footer while preserving shortcut hints."""
    daily_svc: DailyService = DailyService(test_env["db"])
    task_svc: TaskService = TaskService(test_env["db"])
    view = TodayView(daily_svc, task_svc)

    with patch("todo.ui.console.console.print") as mock_print:
        view.render(
            tasks=[],
            sync_status="● Tasks Connected · Calendar Auth Needed",
            clear=False,
        )

        all_calls = [str(call) for call in mock_print.call_args_list]
        # Verify status text line is completely absent
        assert not any("● Tasks Connected · Calendar Auth Needed" in c for c in all_calls)
        assert not any("Calendar Auth Needed" in c for c in all_calls if "Google (Auth)" not in c)
        # Verify shortcut hint for Google (Auth) is preserved
        assert any("Google (Auth)" in c for c in all_calls)


def test_footer_omits_status_and_extra_blank_rows():
    """Footer completely omits sync status line and prevents extra blank rows."""
    with patch("todo.ui.console.console.print") as mock_print:
        render_footer([("q", "Quit")], status="● Tasks Connected · Calendar Auth Needed")

        # Must have exactly 3 print calls: divider, hints, trailing newline
        assert mock_print.call_count == 3
        calls = [call[0] for call in mock_print.call_args_list]

        # 1. Divider
        assert any("─" in str(arg) for arg in calls[0])
        # 2. Hints
        assert any("Quit" in str(arg) for arg in calls[1])
        # 3. Trailing newline (empty call)
        assert calls[2] == ()

        # Assert no status or color styling from status was printed
        all_printed = " ".join(str(c) for c in mock_print.call_args_list)
        assert "Calendar Auth Needed" not in all_printed
        assert "bold yellow" not in all_printed
        assert "bold cyan" not in all_printed or "Quit" in all_printed

    with patch("todo.ui.console.console.print") as mock_print:
        render_footer([("q", "Quit")], status="● Tasks & Calendar Syncing…")
        assert mock_print.call_count == 3
        all_printed = " ".join(str(c) for c in mock_print.call_args_list)
        assert "Syncing" not in all_printed

    with patch("todo.ui.console.console.print") as mock_print:
        render_footer([("q", "Quit")], status="● Connected · Tasks & Calendar Synced 10:15")
        assert mock_print.call_count == 3
        all_printed = " ".join(str(c) for c in mock_print.call_args_list)
        assert "Connected · Tasks & Calendar Synced" not in all_printed


# =============================================================================
# 6. CLI Command Tests
# =============================================================================


def test_cli_auth_command_simulated_success(test_env):
    """'todo auth' command successfully coordinates loopback auth, code exchange, and verification."""
    runner = CliRunner()

    def fake_exchange(self, code, redirect_uri):
        self.save_tokens({
            "access_token": "mock_tok",
            "scope": "https://www.googleapis.com/auth/tasks https://www.googleapis.com/auth/calendar.events",
        })
        return self._tokens

    with patch("todo.config.Config.load", return_value=test_env["config"]):
        with patch(
            "todo.integrations.google_tasks.run_loopback_auth_server",
            return_value=("https://accounts.google.com/auth_test", "http://127.0.0.1:8888/", ["captured_mock_code"]),
        ):
            with patch.object(GoogleOAuthManager, "exchange_code", fake_exchange):
                with patch.object(
                    GoogleCalendarClient,
                    "verify_access",
                    return_value={"verified": True, "summary": "Nikhil Calendar"},
                ):
                    result = runner.invoke(cli, ["auth", "--no-browser", "--timeout", "5"])

    assert result.exit_code == 0
    assert "Authorization completed successfully" in result.output
    assert "Google Tasks: Granted" in result.output
    assert "Google Calendar: Granted" in result.output
    assert "Primary Calendar: Verified (Nikhil Calendar)" in result.output


def test_cli_google_auth_alias(test_env):
    """'todo google-auth' is a functioning alias for 'todo auth'."""
    runner = CliRunner()

    def fake_exchange(self, code, redirect_uri):
        self.save_tokens({
            "access_token": "mock_tok",
            "scope": "https://www.googleapis.com/auth/tasks https://www.googleapis.com/auth/calendar.events",
        })
        return self._tokens

    with patch("todo.config.Config.load", return_value=test_env["config"]):
        with patch(
            "todo.integrations.google_tasks.run_loopback_auth_server",
            return_value=("https://accounts.google.com/auth_test", "http://127.0.0.1:8888/", ["mock_code"]),
        ):
            with patch.object(GoogleOAuthManager, "exchange_code", fake_exchange):
                with patch.object(
                    GoogleCalendarClient,
                    "verify_access",
                    return_value={"verified": True, "summary": "Primary"},
                ):
                    result = runner.invoke(cli, ["google-auth", "--no-browser", "--timeout", "5"])

    assert result.exit_code == 0
    assert "Authorization completed successfully" in result.output


def test_cli_auth_command_timeout(test_env):
    """'todo auth' gracefully handles timeout when no code is captured or input."""
    runner = CliRunner()

    with patch("todo.config.Config.load", return_value=test_env["config"]):
        with patch(
            "todo.integrations.google_tasks.run_loopback_auth_server",
            return_value=("https://accounts.google.com/auth_test", "http://127.0.0.1:8888/", []),
        ):
            # Run with a 1-second timeout
            result = runner.invoke(cli, ["auth", "--no-browser", "--timeout", "1"])

    assert result.exit_code == 0
    assert "Authorization timed out or was cancelled" in result.output


def test_cli_auth_command_code_exchange_error(test_env):
    """'todo auth' cleanly handles and reports token exchange failures."""
    runner = CliRunner()

    with patch("todo.config.Config.load", return_value=test_env["config"]):
        with patch(
            "todo.integrations.google_tasks.run_loopback_auth_server",
            return_value=("https://accounts.google.com/auth_test", "http://127.0.0.1:8888/", ["bad_code"]),
        ):
            with patch.object(
                GoogleOAuthManager,
                "exchange_code",
                side_effect=GoogleCalendarError("Invalid grant: code expired"),
            ):
                result = runner.invoke(cli, ["auth", "--no-browser", "--timeout", "5"])

    assert result.exit_code == 0
    assert "Failed to exchange authorization code" in result.output
    assert "Invalid grant" in result.output
