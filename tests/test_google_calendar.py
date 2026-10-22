"""Deterministic tests for Google Calendar integration, all-day event semantics, and client operations."""

from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

from todo.config import Config
from todo.database import Database
from todo.integrations.google_calendar import (
    GoogleCalendarAPIError,
    GoogleCalendarAuthError,
    GoogleCalendarClient,
    GoogleCalendarNetworkError,
    GoogleCalendarNotFoundError,
    calculate_exclusive_end_date,
)
from todo.integrations.google_tasks import GoogleOAuthManager, GoogleOAuthTokens
from todo.models import Task
from todo.services.google_calendar_service import GoogleCalendarService


# =========================================================================
# 1. Date Calculation & All-Day Semantics Tests
# =========================================================================


def test_calculate_exclusive_end_date_standard_day():
    """Verify single standard day calculates exclusive end date as next calendar day."""
    assert calculate_exclusive_end_date("2026-10-25") == "2026-10-26"
    assert calculate_exclusive_end_date("2026-05-15") == "2026-05-16"


def test_calculate_exclusive_end_date_month_boundary():
    """Verify end of month transitions to 1st of next month."""
    assert calculate_exclusive_end_date("2026-10-31") == "2026-11-01"
    assert calculate_exclusive_end_date("2026-04-30") == "2026-05-01"


def test_calculate_exclusive_end_date_leap_year_february():
    """Verify leap year handling for February 28 and February 29."""
    # 2024 is a leap year
    assert calculate_exclusive_end_date("2024-02-28") == "2024-02-29"
    assert calculate_exclusive_end_date("2024-02-29") == "2024-03-01"

    # 2026 is not a leap year
    assert calculate_exclusive_end_date("2026-02-28") == "2026-03-01"


def test_calculate_exclusive_end_date_year_boundary():
    """Verify December 31 transitions to January 1 of the following year."""
    assert calculate_exclusive_end_date("2026-12-31") == "2027-01-01"


# =========================================================================
# 2. GoogleCalendarClient Tests
# =========================================================================


@pytest.fixture
def fake_oauth_manager(tmp_path):
    token_file = tmp_path / "test_token.json"
    mgr = GoogleOAuthManager(
        token_path=token_file,
        client_id="fake_client_id",
        client_secret="fake_client_secret",
    )
    tokens = {
        "access_token": "valid_cal_access_token",
        "refresh_token": "valid_refresh_token",
        "expires_in": 3600,
        "scope": "https://www.googleapis.com/auth/tasks https://www.googleapis.com/auth/calendar.events",
    }
    mgr.save_tokens(tokens)
    return mgr


def test_client_request_headers_and_auth(fake_oauth_manager):
    """Verify client sends Authorization Bearer header and content-type."""
    client = GoogleCalendarClient(fake_oauth_manager)

    with patch("requests.request") as mock_req:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = b'{"kind": "calendar#calendarList"}'
        mock_resp.json.return_value = {"kind": "calendar#calendarList", "items": []}
        mock_req.return_value = mock_resp

        client.list_calendars()

        assert mock_req.call_count == 1
        call_kwargs = mock_req.call_args[1]
        assert call_kwargs["headers"]["Authorization"] == "Bearer valid_cal_access_token"
        assert call_kwargs["headers"]["Content-Type"] == "application/json"


def test_client_request_401_triggers_token_refresh(fake_oauth_manager):
    """Verify client automatically refreshes token on 401 Unauthorized."""
    client = GoogleCalendarClient(fake_oauth_manager)

    with patch("requests.request") as mock_req, patch.object(
        fake_oauth_manager, "refresh_access_token", return_value="new_refreshed_token"
    ) as mock_refresh:
        resp_401 = MagicMock(status_code=401)
        resp_200 = MagicMock(status_code=200, content=b'{"items": []}')
        resp_200.json.return_value = {"items": []}
        mock_req.side_effect = [resp_401, resp_200]

        res = client.list_calendars()
        assert res == []
        assert mock_refresh.call_count == 1
        assert mock_req.call_count == 2
        assert mock_req.call_args[1]["headers"]["Authorization"] == "Bearer new_refreshed_token"


def test_client_request_404_raises_not_found_error(fake_oauth_manager):
    """Verify HTTP 404 raises GoogleCalendarNotFoundError."""
    client = GoogleCalendarClient(fake_oauth_manager)

    with patch("requests.request") as mock_req:
        resp_404 = MagicMock(status_code=404, text="Not Found")
        mock_req.return_value = resp_404

        with pytest.raises(GoogleCalendarNotFoundError):
            client.get_event("primary", "non_existent_event_id")


def test_client_request_network_error_raises_network_error(fake_oauth_manager):
    """Verify network connection failures raise GoogleCalendarNetworkError."""
    client = GoogleCalendarClient(fake_oauth_manager)

    with patch("requests.request", side_effect=requests.ConnectionError("Offline")):
        with pytest.raises(GoogleCalendarNetworkError):
            client.list_calendars()


def test_client_create_all_day_event_payload(fake_oauth_manager):
    """Verify create_all_day_event creates event with proper extended properties and dates."""
    client = GoogleCalendarClient(fake_oauth_manager)

    with patch("requests.request") as mock_req:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = b'{"id": "evt_123"}'
        mock_resp.json.return_value = {
            "id": "evt_123",
            "summary": "Plan release",
            "start": {"date": "2026-10-25"},
            "end": {"date": "2026-10-26"},
        }
        mock_req.return_value = mock_resp

        res = client.create_all_day_event(
            calendar_id="primary",
            task_id=42,
            title="Plan release",
            start_date="2026-10-25",
            description="Detailed notes",
        )

        assert res["id"] == "evt_123"
        call_kwargs = mock_req.call_args[1]
        json_body = call_kwargs["json"]
        assert json_body["summary"] == "Plan release"
        assert json_body["description"] == "Detailed notes"
        assert json_body["start"]["date"] == "2026-10-25"
        assert json_body["end"]["date"] == "2026-10-26"
        assert json_body["extendedProperties"]["private"]["extern_task_id"] == "42"
        assert json_body["extendedProperties"]["private"]["extern_source"] == "extern"


def test_client_update_and_delete_event(fake_oauth_manager):
    """Verify update patches all-day event and delete returns True."""
    client = GoogleCalendarClient(fake_oauth_manager)

    with patch("requests.request") as mock_req:
        mock_resp = MagicMock(status_code=200, content=b'{"id": "evt_123", "summary": "Updated"}')
        mock_resp.json.return_value = {"id": "evt_123", "summary": "Updated"}
        mock_req.return_value = mock_resp

        res = client.update_all_day_event(
            calendar_id="primary",
            event_id="evt_123",
            task_id=42,
            title="Updated Title",
            start_date="2026-11-01",
        )
        assert res["summary"] == "Updated"
        patch_json = mock_req.call_args[1]["json"]
        assert patch_json["summary"] == "Updated Title"
        assert patch_json["start"]["date"] == "2026-11-01"
        assert patch_json["end"]["date"] == "2026-11-02"

        # Test delete
        del_resp = MagicMock(status_code=204, content=b"")
        mock_req.return_value = del_resp
        assert client.delete_event("primary", "evt_123") is True


# =========================================================================
# 3. GoogleCalendarService & SQLite Caching Tests
# =========================================================================


@pytest.fixture
def service_env(tmp_path):
    db_path = tmp_path / "test_calendar.db"
    db = Database(db_path)
    db.init_db()

    token_file = tmp_path / "token.json"
    mgr = GoogleOAuthManager(token_path=token_file, client_id="cid", client_secret="sec")
    mgr.save_tokens({
        "access_token": "valid_token",
        "refresh_token": "refresh_token",
        "expires_in": 3600,
        "scope": "https://www.googleapis.com/auth/tasks https://www.googleapis.com/auth/calendar.events",
    })

    config = Config(
        db_path=db_path,
        google_token_path=token_file,
        google_calendar_enabled=True,
        google_default_calendar="primary",
    )
    mock_client = MagicMock(spec=GoogleCalendarClient)
    svc = GoogleCalendarService(db=db, config=config, oauth_manager=mgr, client=mock_client)
    return {"db": db, "config": config, "mgr": mgr, "client": mock_client, "svc": svc}


def test_calendar_cache_persistence(service_env):
    """Verify writable calendars can be cached and retrieved from SQLite."""
    svc: GoogleCalendarService = service_env["svc"]
    calendars = [
        {"id": "primary", "summary": "My Primary", "accessRole": "owner", "primary": True},
        {"id": "cal_work", "summary": "Work Tasks", "accessRole": "writer"},
    ]

    svc.cache_calendars(calendars)
    cached = svc.get_cached_calendars()

    assert len(cached) == 2
    assert cached[0]["id"] == "primary"
    assert cached[0]["primary_calendar"] == 1
    assert cached[1]["id"] == "cal_work"
    assert cached[1]["primary_calendar"] == 0


def test_local_event_mapping_persistence(service_env):
    """Verify local event mapping persists in google_calendar_events and handles updates."""
    svc: GoogleCalendarService = service_env["svc"]
    db: Database = service_env["db"]

    # Insert a task to satisfy foreign key constraint
    with db.get_connection() as conn:
        conn.execute(
            "INSERT INTO tasks (id, title, status, created_at, updated_at) VALUES (10, 'Test Task', 'pending', 'now', 'now')"
        )
        conn.commit()

    svc.save_event_mapping(
        task_id=10,
        calendar_id="primary",
        event_id="evt_remote_10",
        summary="Test Event",
        start_date="2026-10-25",
        end_date="2026-10-26",
    )

    mapping = svc.get_event_mapping(10, "primary")
    assert mapping is not None
    assert mapping["event_id"] == "evt_remote_10"
    assert mapping["summary"] == "Test Event"
    assert mapping["start_date"] == "2026-10-25"
    assert mapping["end_date"] == "2026-10-26"

    # Update mapping
    svc.save_event_mapping(
        task_id=10,
        calendar_id="primary",
        event_id="evt_remote_10",
        summary="Test Event Updated",
        start_date="2026-10-27",
        end_date="2026-10-28",
    )
    updated = svc.get_event_mapping(10, "primary")
    assert updated["summary"] == "Test Event Updated"
    assert updated["start_date"] == "2026-10-27"


def test_drain_calendar_outbox_idempotency(service_env):
    """Verify drain_outbox creates event, saves local mapping, and avoids duplicate creation."""
    svc: GoogleCalendarService = service_env["svc"]
    db: Database = service_env["db"]
    mock_client: MagicMock = service_env["client"]

    # Insert task to satisfy foreign key
    with db.get_connection() as conn:
        conn.execute(
            "INSERT INTO tasks (id, title, status, created_at, updated_at) VALUES (15, 'Ship feature', 'pending', 'now', 'now')"
        )
        conn.commit()

    mock_client.find_events_by_task_id.return_value = []
    mock_client.create_all_day_event.return_value = {
        "id": "evt_created_123",
        "summary": "Ship feature",
        "start": {"date": "2026-10-25"},
        "end": {"date": "2026-10-26"},
    }

    # Enqueue creation
    svc.enqueue_outbox(
        action="calendar_create",
        task_id=15,
        payload={"calendar_id": "primary", "title": "Ship feature", "date": "2026-10-25"},
    )

    assert len(svc.get_pending_outbox()) == 1

    processed = svc.drain_outbox()
    assert processed == 1
    assert len(svc.get_pending_outbox()) == 0

    # Local mapping should now exist
    mapping = svc.get_event_mapping(15, "primary")
    assert mapping is not None
    assert mapping["event_id"] == "evt_created_123"

    # If another create is enqueued for same task, it should call update rather than duplicate create
    mock_client.update_all_day_event.return_value = {
        "id": "evt_created_123",
        "summary": "Ship feature v2",
    }
    svc.enqueue_outbox(
        action="calendar_create",
        task_id=15,
        payload={"calendar_id": "primary", "title": "Ship feature v2", "date": "2026-10-25"},
    )

    processed_2 = svc.drain_outbox()
    assert processed_2 == 1
    assert mock_client.create_all_day_event.call_count == 1
    assert mock_client.update_all_day_event.call_count == 1


def test_scope_detection_and_reauthorization():
    """Verify missing calendar scope triggers needs_reauthorization_for_calendar."""
    token_data = {
        "access_token": "tasks_only_token",
        "refresh_token": "refresh_me",
        "expires_in": 3600,
        "scope": "https://www.googleapis.com/auth/tasks",  # Missing calendar scope
    }
    tokens = GoogleOAuthTokens.from_dict(token_data)
    mgr = GoogleOAuthManager(
        token_path=Path("/tmp/non_existent.json"),
        client_id="cid",
        client_secret="sec",
    )
    mgr._tokens = tokens

    assert mgr.is_authenticated() is True
    assert mgr.has_tasks_scope() is True
    assert mgr.has_calendar_scope() is False
    assert mgr.needs_reauthorization_for_calendar() is True

    # Check generated auth URL has both scopes
    auth_url = mgr.get_authorization_url(redirect_uri="http://localhost:8080")
    assert "https%3A%2F%2Fwww.googleapis.com%2Fauth%2Fcalendar.events" in auth_url
    assert "https%3A%2F%2Fwww.googleapis.com%2Fauth%2Ftasks" in auth_url
