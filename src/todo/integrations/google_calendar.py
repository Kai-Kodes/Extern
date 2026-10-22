"""Google Calendar API REST client and all-day event compatibility."""

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple, Union

import requests

from todo.integrations.google_tasks import (
    GoogleOAuthManager,
    GoogleTasksAuthError,
    GoogleTasksNetworkError,
)


class GoogleCalendarError(Exception):
    """Base exception for Google Calendar integration."""
    pass


class GoogleCalendarAuthError(GoogleCalendarError):
    """Raised when authentication or token refresh fails."""
    pass


class GoogleCalendarNetworkError(GoogleCalendarError):
    """Raised when network connectivity fails."""
    pass


class GoogleCalendarAPIError(GoogleCalendarError):
    """Raised when Google Calendar API returns an HTTP error."""

    def __init__(
        self,
        message: str,
        status_code: Optional[int] = None,
        response_body: Optional[str] = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body


class GoogleCalendarNotFoundError(GoogleCalendarAPIError):
    """Raised when a calendar or event is not found (HTTP 404)."""

    def __init__(self, message: str, response_body: Optional[str] = None):
        super().__init__(message, status_code=404, response_body=response_body)


def calculate_exclusive_end_date(start_date_str: str) -> str:
    """
    Calculate RFC 3339 exclusive end date for an all-day event.
    Google Calendar requires all-day event start.date and exclusive end.date.
    For a single-day event on YYYY-MM-DD, end.date is YYYY-MM-DD + 1 day.
    """
    clean_date = start_date_str[:10].strip()
    dt = datetime.strptime(clean_date, "%Y-%m-%d")
    next_day = dt + timedelta(days=1)
    return next_day.strftime("%Y-%m-%d")


class GoogleCalendarClient:
    """REST API client for Google Calendar v3."""

    BASE_URL = "https://www.googleapis.com/calendar/v3"

    def __init__(
        self,
        oauth_manager: GoogleOAuthManager,
        timeout: Union[float, Tuple[float, float]] = (3.0, 7.0),
    ):
        self.oauth_manager = oauth_manager
        self.timeout = timeout

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
    ) -> Any:
        """Execute authenticated HTTP request with automatic token refresh on 401."""
        url = f"{self.BASE_URL}/{path.lstrip('/')}"
        try:
            token = self.oauth_manager.get_valid_access_token()
        except GoogleTasksAuthError as e:
            raise GoogleCalendarAuthError(f"Authentication failed: {e}")

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

        try:
            resp = requests.request(
                method=method,
                url=url,
                params=params,
                json=json_body,
                headers=headers,
                timeout=self.timeout,
            )
        except requests.RequestException as e:
            raise GoogleCalendarNetworkError(f"Network connection failed: {e}")

        # If unauthorized, try refreshing once
        if resp.status_code == 401:
            try:
                new_token = self.oauth_manager.refresh_access_token()
                headers["Authorization"] = f"Bearer {new_token}"
                resp = requests.request(
                    method=method,
                    url=url,
                    params=params,
                    json=json_body,
                    headers=headers,
                    timeout=self.timeout,
                )
            except requests.RequestException as e:
                raise GoogleCalendarNetworkError(f"Network connection failed: {e}")
            except GoogleTasksAuthError as e:
                raise GoogleCalendarAuthError(f"Token refresh failed: {e}")

        if resp.status_code == 404:
            raise GoogleCalendarNotFoundError(
                f"Resource not found: {path}", response_body=resp.text
            )

        if resp.status_code >= 400:
            raise GoogleCalendarAPIError(
                f"Google Calendar API request failed: HTTP {resp.status_code} - {resp.text}",
                status_code=resp.status_code,
                response_body=resp.text,
            )

        if resp.status_code == 204 or not resp.content:
            return None

        try:
            return resp.json()
        except Exception:
            return None

    def list_calendars(self, min_access_role: str = "writer") -> List[Dict[str, Any]]:
        """
        List available calendars accessible by the authenticated user.
        Filters for writable calendars (e.g. 'owner', 'writer') when min_access_role='writer'.
        """
        params = {"minAccessRole": min_access_role}
        data = self._request("GET", "users/me/calendarList", params=params)
        return data.get("items", []) if data else []

    def get_calendar(self, calendar_id: str = "primary") -> Dict[str, Any]:
        """Get calendar metadata by ID."""
        import urllib.parse
        encoded_id = urllib.parse.quote(calendar_id, safe="")
        return self._request("GET", f"calendars/{encoded_id}")

    def get_event(self, calendar_id: str, event_id: str) -> Dict[str, Any]:
        """Retrieve a specific calendar event."""
        import urllib.parse
        encoded_cal = urllib.parse.quote(calendar_id, safe="")
        encoded_evt = urllib.parse.quote(event_id, safe="")
        return self._request("GET", f"calendars/{encoded_cal}/events/{encoded_evt}")

    def find_events_by_task_id(
        self, calendar_id: str, task_id: int
    ) -> List[Dict[str, Any]]:
        """Search for events correlated with an Extern task ID using private extended properties."""
        import urllib.parse
        encoded_cal = urllib.parse.quote(calendar_id, safe="")
        params = {
            "privateExtendedProperty": f"extern_task_id={task_id}",
            "showDeleted": "false",
        }
        data = self._request("GET", f"calendars/{encoded_cal}/events", params=params)
        return data.get("items", []) if data else []

    def create_all_day_event(
        self,
        calendar_id: str,
        task_id: int,
        title: str,
        start_date: str,
        description: str = "",
        end_date: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Create an all-day event correlated with an Extern task.
        Uses exclusive end date (start_date + 1 day for single-day event).
        """
        import urllib.parse
        encoded_cal = urllib.parse.quote(calendar_id, safe="")
        exclusive_end = end_date or calculate_exclusive_end_date(start_date)

        event_body = {
            "summary": title,
            "description": description or "",
            "start": {"date": start_date[:10]},
            "end": {"date": exclusive_end[:10]},
            "extendedProperties": {
                "private": {
                    "extern_task_id": str(task_id),
                    "extern_source": "extern",
                }
            },
        }

        return self._request(
            "POST", f"calendars/{encoded_cal}/events", json_body=event_body
        )

    def update_all_day_event(
        self,
        calendar_id: str,
        event_id: str,
        task_id: int,
        title: Optional[str] = None,
        start_date: Optional[str] = None,
        description: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Patch an existing all-day event correlated with an Extern task.
        Updates summary, date, description while maintaining extended properties.
        """
        import urllib.parse
        encoded_cal = urllib.parse.quote(calendar_id, safe="")
        encoded_evt = urllib.parse.quote(event_id, safe="")

        patch_body: Dict[str, Any] = {
            "extendedProperties": {
                "private": {
                    "extern_task_id": str(task_id),
                    "extern_source": "extern",
                }
            }
        }
        if title is not None:
            patch_body["summary"] = title
        if description is not None:
            patch_body["description"] = description
        if start_date is not None:
            exclusive_end = end_date or calculate_exclusive_end_date(start_date)
            patch_body["start"] = {"date": start_date[:10]}
            patch_body["end"] = {"date": exclusive_end[:10]}

        return self._request(
            "PATCH", f"calendars/{encoded_cal}/events/{encoded_evt}", json_body=patch_body
        )

    def delete_event(self, calendar_id: str, event_id: str) -> bool:
        """
        Delete a calendar event. Returns True if deleted or already absent (404).
        """
        import urllib.parse
        encoded_cal = urllib.parse.quote(calendar_id, safe="")
        encoded_evt = urllib.parse.quote(event_id, safe="")
        try:
            self._request("DELETE", f"calendars/{encoded_cal}/events/{encoded_evt}")
            return True
        except GoogleCalendarNotFoundError:
            return True
