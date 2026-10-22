"""Google Tasks API REST client, OAuth 2.0 manager, and Google Calendar date compatibility."""

import json
import os
import socket
import sys
import threading
import time
import urllib.parse
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import requests


class GoogleTasksError(Exception):
    """Base exception for Google Tasks integration."""
    pass


class GoogleTasksAuthError(GoogleTasksError):
    """Raised when authentication or token refresh fails."""
    pass


class GoogleTasksNetworkError(GoogleTasksError):
    """Raised when network connectivity fails (offline mode)."""
    pass


class GoogleTasksAPIError(GoogleTasksError):
    """Raised when Google Tasks API returns an HTTP error."""
    def __init__(self, message: str, status_code: Optional[int] = None, response_body: Optional[str] = None):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body


class GoogleTasksNotFoundError(GoogleTasksAPIError):
    """Raised when a requested resource (e.g. task list or task) is not found (HTTP 404)."""
    def __init__(self, message: str, response_body: Optional[str] = None):
        super().__init__(message, status_code=404, response_body=response_body)


def format_rfc3339_due_date(date_str: str) -> str:
    """
    Format a YYYY-MM-DD date string to RFC 3339 UTC midnight format.
    Google Calendar displays all-day tasks at UTC midnight: YYYY-MM-DDT00:00:00.000Z.
    """
    clean_date = date_str[:10].strip()
    return f"{clean_date}T00:00:00.000Z"


def parse_rfc3339_due_date(due_str: Optional[str]) -> Optional[str]:
    """
    Parse an RFC 3339 timestamp string from Google Tasks into YYYY-MM-DD date string.
    Returns None if due_str is None or empty.
    """
    if not due_str:
        return None
    return due_str[:10]


@dataclass
class GoogleOAuthTokens:
    access_token: str
    refresh_token: Optional[str] = None
    expires_at: float = 0.0
    token_type: str = "Bearer"
    scope: str = ""

    @property
    def is_expired(self) -> bool:
        """Check if access token is expired or expiring within 60 seconds."""
        if not self.expires_at:
            return True
        return time.time() >= (self.expires_at - 60)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at,
            "token_type": self.token_type,
            "scope": self.scope,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "GoogleOAuthTokens":
        return cls(
            access_token=data.get("access_token", ""),
            refresh_token=data.get("refresh_token"),
            expires_at=float(data.get("expires_at", 0.0)),
            token_type=data.get("token_type", "Bearer"),
            scope=data.get("scope", ""),
        )


class GoogleOAuthManager:
    """Manages Google OAuth 2.0 flow, token persistence (0600 permissions), and refresh."""

    AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
    TOKEN_URL = "https://oauth2.googleapis.com/token"
    REVOKE_URL = "https://oauth2.googleapis.com/revoke"
    TASKS_SCOPE = "https://www.googleapis.com/auth/tasks"
    CALENDAR_EVENTS_SCOPE = "https://www.googleapis.com/auth/calendar.events"
    DEFAULT_SCOPES = [TASKS_SCOPE, CALENDAR_EVENTS_SCOPE]

    def __init__(
        self,
        token_path: Path,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        credentials_file: Optional[Path] = None,
    ):
        self.token_path = Path(token_path).expanduser().resolve()
        self.client_id = client_id
        self.client_secret = client_secret
        self.credentials_file = (
            Path(credentials_file).expanduser().resolve()
            if credentials_file
            else None
        )
        self._tokens: Optional[GoogleOAuthTokens] = None
        self._load_tokens_from_file()

    def get_credentials(self) -> Tuple[Optional[str], Optional[str]]:
        """Resolve client_id and client_secret from explicit values, credentials file, or defaults."""
        if self.client_id and self.client_secret:
            return self.client_id, self.client_secret

        candidates = []
        if self.credentials_file:
            candidates.append(self.credentials_file)

        # Check default locations
        config_dir = self.token_path.parent
        candidates.extend([
            config_dir / "credentials.json",
            config_dir / "client_secret.json",
        ])

        for path in candidates:
            if path and path.exists() and path.is_file():
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    client_info = data.get("installed") or data.get("web") or data
                    c_id = client_info.get("client_id")
                    c_secret = client_info.get("client_secret")
                    if c_id and c_secret:
                        self.client_id = str(c_id).strip()
                        self.client_secret = str(c_secret).strip()
                        return self.client_id, self.client_secret
                except Exception:
                    continue

        return self.client_id, self.client_secret

    def has_configured_credentials(self) -> bool:
        """Check if client ID and secret can be resolved."""
        c_id, c_secret = self.get_credentials()
        return bool(c_id and c_secret)

    def is_authenticated(self) -> bool:
        """Check if valid or refreshable tokens are stored."""
        if not self._tokens:
            self._load_tokens_from_file()
        if not self._tokens:
            return False
        return bool(self._tokens.access_token or self._tokens.refresh_token)

    def _load_tokens_from_file(self) -> None:
        """Load tokens from local file."""
        if not self.token_path.exists():
            self._tokens = None
            return
        try:
            with open(self.token_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self._tokens = GoogleOAuthTokens.from_dict(data)
        except Exception:
            self._tokens = None

    def save_tokens(self, token_data: Dict[str, Any]) -> None:
        """Save tokens to disk with restrictive 0600 POSIX permissions."""
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        # Calculate absolute expiration time if expires_in provided
        expires_at = token_data.get("expires_at")
        if not expires_at:
            expires_in = token_data.get("expires_in", 3600)
            expires_at = time.time() + float(expires_in)

        # Preserve existing refresh_token if new response omitted it
        refresh_token = token_data.get("refresh_token")
        if not refresh_token and self._tokens and self._tokens.refresh_token:
            refresh_token = self._tokens.refresh_token

        tokens = GoogleOAuthTokens(
            access_token=token_data.get("access_token", ""),
            refresh_token=refresh_token,
            expires_at=float(expires_at),
            token_type=token_data.get("token_type", "Bearer"),
            scope=token_data.get("scope", " ".join(self.DEFAULT_SCOPES)),
        )
        self._tokens = tokens

        tmp_path = self.token_path.with_suffix(".tmp")
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(tokens.to_dict(), f, indent=2)
            try:
                os.chmod(tmp_path, 0o600)
            except Exception:
                pass
            tmp_path.replace(self.token_path)
            try:
                os.chmod(self.token_path, 0o600)
            except Exception:
                pass
        except Exception as e:
            if tmp_path.exists():
                tmp_path.unlink()
            raise GoogleTasksAuthError(f"Failed to persist token file: {e}")

    def get_granted_scopes(self) -> List[str]:
        """Return list of scopes granted in the current stored token."""
        if not self._tokens or not self._tokens.scope:
            return []
        return self._tokens.scope.split()

    def has_scope(self, scope: str) -> bool:
        """Check if a specific scope is granted in the current token."""
        granted = self.get_granted_scopes()
        return scope in granted

    def has_tasks_scope(self) -> bool:
        """Check if Google Tasks scope is granted."""
        return self.has_scope(self.TASKS_SCOPE)

    def has_calendar_scope(self) -> bool:
        """Check if Google Calendar events scope is granted."""
        return self.has_scope(self.CALENDAR_EVENTS_SCOPE)

    def needs_reauthorization_for_calendar(self) -> bool:
        """Check if user has authenticated previously with tasks only and lacks calendar events scope."""
        return self.is_authenticated() and not self.has_calendar_scope()

    def get_authorization_url(
        self,
        redirect_uri: str,
        state: Optional[str] = None,
        login_hint: Optional[str] = None,
        scopes: Optional[List[str]] = None,
    ) -> str:
        """Build Google OAuth 2.0 authorization URL."""
        c_id, _ = self.get_credentials()
        if not c_id:
            raise GoogleTasksAuthError("Google OAuth Client ID is missing.")

        target_scopes = scopes if scopes is not None else self.DEFAULT_SCOPES
        params = {
            "client_id": c_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(target_scopes),
            "access_type": "offline",
            "prompt": "consent",
        }
        if state:
            params["state"] = state
        if login_hint:
            params["login_hint"] = login_hint
        return f"{self.AUTH_URL}?{urllib.parse.urlencode(params)}"

    def exchange_code(self, code_or_url: str, redirect_uri: str) -> GoogleOAuthTokens:
        """Exchange authorization code (or full redirect URL) for access & refresh tokens."""
        code = code_or_url.strip()
        if "code=" in code:
            # Parse from query parameters if full URL was pasted
            parsed = urllib.parse.urlparse(code)
            qs = urllib.parse.parse_qs(parsed.query)
            if "code" in qs and qs["code"]:
                code = qs["code"][0]

        c_id, c_secret = self.get_credentials()
        if not c_id or not c_secret:
            raise GoogleTasksAuthError("Google Client ID or Client Secret not configured.")

        data = {
            "code": code,
            "client_id": c_id,
            "client_secret": c_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        }

        try:
            resp = requests.post(self.TOKEN_URL, data=data, timeout=15)
        except requests.RequestException as e:
            raise GoogleTasksNetworkError(f"Network error during code exchange: {e}")

        if resp.status_code != 200:
            raise GoogleTasksAuthError(
                f"Failed to exchange authorization code: HTTP {resp.status_code} - {resp.text}"
            )

        token_json = resp.json()
        self.save_tokens(token_json)
        return self._tokens

    def refresh_access_token(self) -> str:
        """Refresh access token using stored refresh token."""
        if not self._tokens or not self._tokens.refresh_token:
            raise GoogleTasksAuthError("No refresh token available. Re-authorization required.")

        c_id, c_secret = self.get_credentials()
        if not c_id or not c_secret:
            raise GoogleTasksAuthError("Google Client ID or Client Secret not configured.")

        data = {
            "client_id": c_id,
            "client_secret": c_secret,
            "refresh_token": self._tokens.refresh_token,
            "grant_type": "refresh_token",
        }

        try:
            resp = requests.post(self.TOKEN_URL, data=data, timeout=15)
        except requests.RequestException as e:
            raise GoogleTasksNetworkError(f"Network error during token refresh: {e}")

        if resp.status_code != 200:
            raise GoogleTasksAuthError(
                f"Failed to refresh access token: HTTP {resp.status_code} - {resp.text}"
            )

        token_json = resp.json()
        self.save_tokens(token_json)
        return self._tokens.access_token

    def get_valid_access_token(self) -> str:
        """Get an active access token, refreshing automatically if expired."""
        if not self._tokens:
            self._load_tokens_from_file()
        if not self._tokens or (not self._tokens.access_token and not self._tokens.refresh_token):
            raise GoogleTasksAuthError("Not authenticated with Google Tasks.")

        if self._tokens.is_expired:
            return self.refresh_access_token()
        return self._tokens.access_token

    def revoke_token(self) -> None:
        """Revoke token with Google servers (best-effort)."""
        token = None
        if self._tokens:
            token = self._tokens.refresh_token or self._tokens.access_token
        if token:
            try:
                requests.post(self.REVOKE_URL, params={"token": token}, timeout=10)
            except Exception:
                pass

    def disconnect(self) -> None:
        """Revoke token and delete local token file."""
        self.revoke_token()
        if self.token_path.exists():
            try:
                self.token_path.unlink()
            except Exception:
                pass
        self._tokens = None


class GoogleTasksClient:
    """REST API client for Google Tasks v1."""

    BASE_URL = "https://tasks.googleapis.com/tasks/v1"

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
        token = self.oauth_manager.get_valid_access_token()
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
            raise GoogleTasksNetworkError(f"Network connection failed: {e}")

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
                raise GoogleTasksNetworkError(f"Network connection failed on retry: {e}")
            except GoogleTasksAuthError:
                raise GoogleTasksAuthError("Session expired. Please reconnect Google Tasks.")
            if resp.status_code == 401:
                raise GoogleTasksAuthError("Unauthorized: Invalid or revoked Google Tasks token.")

        if resp.status_code in (200, 201):
            if resp.content:
                try:
                    return resp.json()
                except Exception:
                    return {}
            return {}
        elif resp.status_code == 204:
            return {}
        elif resp.status_code == 404:
            raise GoogleTasksNotFoundError(f"Resource not found: {path}", response_body=resp.text)
        else:
            raise GoogleTasksAPIError(
                f"Google Tasks API error ({resp.status_code}): {resp.text}",
                status_code=resp.status_code,
                response_body=resp.text,
            )

    def get_task_lists(self) -> List[Dict[str, Any]]:
        """Retrieve all task lists for the authenticated user."""
        all_lists = []
        page_token = None
        while True:
            params = {"maxResults": 100}
            if page_token:
                params["pageToken"] = page_token
            data = self._request("GET", "/users/@me/lists", params=params)
            items = data.get("items", [])
            all_lists.extend(items)
            page_token = data.get("nextPageToken")
            if not page_token:
                break
        return all_lists

    def get_tasks(
        self,
        list_id: str,
        show_completed: bool = True,
        show_hidden: bool = True,
        updated_min: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Retrieve tasks in a task list, optionally filtered by updated_min (RFC 3339)."""
        all_tasks = []
        page_token = None
        while True:
            params: Dict[str, Any] = {
                "maxResults": 100,
                "showCompleted": "true" if show_completed else "false",
                "showHidden": "true" if show_hidden else "false",
            }
            if updated_min:
                params["updatedMin"] = updated_min
            if page_token:
                params["pageToken"] = page_token
            data = self._request("GET", f"/lists/{list_id}/tasks", params=params)
            items = data.get("items", [])
            all_tasks.extend(items)
            page_token = data.get("nextPageToken")
            if not page_token:
                break
        return all_tasks

    def get_task(self, list_id: str, task_id: str) -> Dict[str, Any]:
        """Retrieve a specific task."""
        return self._request("GET", f"/lists/{list_id}/tasks/{task_id}")

    def create_task(
        self,
        list_id: str,
        title: str,
        notes: str = "",
        due_date: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Create a new task in a list.
        due_date can be YYYY-MM-DD or RFC 3339. It will be converted to UTC midnight RFC 3339.
        """
        body: Dict[str, Any] = {"title": title.strip()}
        if notes:
            body["notes"] = notes.strip()
        if due_date:
            body["due"] = format_rfc3339_due_date(due_date)
        return self._request("POST", f"/lists/{list_id}/tasks", json_body=body)

    def update_task(
        self,
        list_id: str,
        task_id: str,
        title: Optional[str] = None,
        notes: Optional[str] = None,
        due_date: Optional[str] = None,
        status: Optional[str] = None,
        clear_due: bool = False,
    ) -> Dict[str, Any]:
        """
        Update an existing task in a list.
        Uses PATCH to update specified fields.
        """
        body: Dict[str, Any] = {}
        if title is not None:
            body["title"] = title.strip()
        if notes is not None:
            body["notes"] = notes.strip()
        if clear_due:
            body["due"] = None
        elif due_date is not None:
            body["due"] = format_rfc3339_due_date(due_date)
        if status is not None:
            body["status"] = status
            if status == "completed":
                body["completed"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            else:
                body["completed"] = None

        return self._request("PATCH", f"/lists/{list_id}/tasks/{task_id}", json_body=body)

    def set_task_completed(self, list_id: str, task_id: str, completed: bool = True) -> Dict[str, Any]:
        """Toggle task completion status on Google Tasks."""
        status = "completed" if completed else "needsAction"
        return self.update_task(list_id=list_id, task_id=task_id, status=status)

    def delete_task(self, list_id: str, task_id: str) -> bool:
        """Permanently delete a task from Google Tasks."""
        self._request("DELETE", f"/lists/{list_id}/tasks/{task_id}")
        return True


def run_loopback_auth_server(
    oauth_manager: GoogleOAuthManager,
    timeout_seconds: int = 120,
    login_hint: Optional[str] = None,
) -> Tuple[str, str, List[str]]:
    """
    Launch a local HTTP loopback server on an ephemeral port,
    generate the auth URL, and wait for redirect.
    Returns (auth_url, redirect_uri, captured_code).
    """
    captured_code: List[str] = []
    server_error: List[str] = []

    class OAuthHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            qs = urllib.parse.parse_qs(parsed.query)
            if "code" in qs and qs["code"]:
                captured_code.append(qs["code"][0])
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                html = (
                    "<html><body style='font-family: sans-serif; text-align: center; padding: 40px;'>"
                    "<h2 style='color: #2e7d32;'>Authorization Successful!</h2>"
                    "<p>Extern has connected to Google Tasks. You may close this tab and return to your terminal.</p>"
                    "</body></html>"
                )
                self.wfile.write(html.encode("utf-8"))
            else:
                err = qs.get("error", ["Unknown error"])[0]
                server_error.append(err)
                self.send_response(400)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                html = f"<html><body><h2>Authorization Failed</h2><p>{err}</p></body></html>"
                self.wfile.write(html.encode("utf-8"))

        def log_message(self, format, *args):
            # Suppress console logging from HTTP server
            pass

    server = HTTPServer(("127.0.0.1", 0), OAuthHandler)
    server.timeout = 1.0
    port = server.server_port
    redirect_uri = f"http://127.0.0.1:{port}/"
    auth_url = oauth_manager.get_authorization_url(redirect_uri, login_hint=login_hint)

    def serve():
        start = time.time()
        while time.time() - start < timeout_seconds and not captured_code and not server_error:
            server.handle_request()
        server.server_close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()

    return auth_url, redirect_uri, captured_code
