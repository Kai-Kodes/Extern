"""Service for managing Google Calendar synchronization, all-day event persistence, and outbox mutations."""

import json
import time
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

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
from todo.integrations.google_tasks import GoogleOAuthManager
from todo.models import Task


class GoogleCalendarService:
    """Coordinates Google Calendar synchronization, local SQLite caching, event mappings, and outbox."""

    def __init__(
        self,
        db: Database,
        config: Config,
        date_provider: Optional[Callable[[], datetime]] = None,
        oauth_manager: Optional[GoogleOAuthManager] = None,
        client: Optional[GoogleCalendarClient] = None,
    ):
        self.db = db
        self.config = config
        self.date_provider = date_provider
        self.sync_manager: Optional[Any] = None

        if oauth_manager:
            self.oauth_manager = oauth_manager
        else:
            self.oauth_manager = GoogleOAuthManager(
                token_path=config.google_token_path or (config.db_path.parent / "google_token.json"),
                client_id=config.google_client_id,
                client_secret=config.google_client_secret,
                credentials_file=config.google_credentials_file,
            )

        self.client = client or GoogleCalendarClient(
            self.oauth_manager,
            timeout=(3.0, getattr(config, "google_sync_timeout", 7.0)),
        )

    def get_now(self) -> datetime:
        return self.date_provider() if self.date_provider else datetime.now()

    def is_authenticated(self) -> bool:
        """Check if OAuth tokens exist and have calendar scope."""
        return self.oauth_manager.is_authenticated() and self.oauth_manager.has_calendar_scope()

    def has_credentials(self) -> bool:
        return self.oauth_manager.has_configured_credentials()

    def needs_reauthorization(self) -> bool:
        """Check if authenticated for tasks but missing calendar scope."""
        if not self.config.google_calendar_enabled:
            return False
        return self.oauth_manager.needs_reauthorization_for_calendar()

    def verify_access(self, calendar_id: Optional[str] = None) -> Dict[str, Any]:
        """Safely verify Google Calendar access using the narrow calendar.events scope."""
        cal_id = calendar_id or self.config.google_default_calendar
        return self.client.verify_access(calendar_id=cal_id)

    # -------------------------------------------------------------------------
    # Local SQLite Calendar Cache
    # -------------------------------------------------------------------------

    def get_cached_calendars(self) -> List[Dict[str, Any]]:
        """Retrieve cached writable calendars from local SQLite."""
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, summary, description, time_zone, access_role, primary_calendar, cached_at
                FROM google_calendars_cache
                ORDER BY primary_calendar DESC, summary ASC
                """
            )
            rows = cursor.fetchall()
        return [dict(r) for r in rows]

    def cache_calendars(self, calendars: List[Dict[str, Any]]) -> None:
        """Update local cache of writable calendars."""
        now_iso = self.get_now().isoformat()
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            for cal in calendars:
                cal_id = cal.get("id", "")
                if not cal_id:
                    continue
                summary = cal.get("summary", "")
                description = cal.get("description", "")
                time_zone = cal.get("timeZone", "")
                access_role = cal.get("accessRole", "writer")
                is_primary = 1 if cal.get("primary", False) else 0

                cursor.execute(
                    """
                    INSERT INTO google_calendars_cache (
                        id, summary, description, time_zone, access_role, primary_calendar, cached_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        summary=excluded.summary,
                        description=excluded.description,
                        time_zone=excluded.time_zone,
                        access_role=excluded.access_role,
                        primary_calendar=excluded.primary_calendar,
                        cached_at=excluded.cached_at,
                        updated_at=excluded.updated_at
                    """,
                    (cal_id, summary, description, time_zone, access_role, is_primary, now_iso, now_iso),
                )
            conn.commit()

    def sync_calendars_list(self) -> List[Dict[str, Any]]:
        """Fetch writable calendars from Google Calendar API and cache locally."""
        if not self.is_authenticated():
            return self.get_cached_calendars()
        try:
            calendars = self.client.list_calendars(min_access_role="writer")
            if calendars:
                self.cache_calendars(calendars)
        except Exception:
            pass

        cached = self.get_cached_calendars()
        if not cached:
            # Seed default primary calendar if list_calendars was restricted by narrow scope
            self.cache_calendars([{
                "id": self.config.google_default_calendar,
                "summary": "Primary Calendar",
                "primary": True,
                "accessRole": "owner",
            }])
            cached = self.get_cached_calendars()
        return cached

    # -------------------------------------------------------------------------
    # Local Event Associations
    # -------------------------------------------------------------------------

    def get_event_mapping(self, task_id: int, calendar_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Look up existing local event mapping for a task."""
        cal_id = calendar_id or self.config.google_default_calendar
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, task_id, calendar_id, event_id, summary, start_date, end_date, etag, html_link, status, created_at, updated_at
                FROM google_calendar_events
                WHERE task_id = ? AND calendar_id = ?
                """,
                (task_id, cal_id),
            )
            row = cursor.fetchone()
        return dict(row) if row else None

    def save_event_mapping(
        self,
        task_id: int,
        calendar_id: str,
        event_id: str,
        summary: str,
        start_date: str,
        end_date: str,
        etag: str = "",
        html_link: str = "",
        status: str = "confirmed",
    ) -> None:
        """Persist or update local event mapping."""
        now_iso = self.get_now().isoformat()
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO google_calendar_events (
                    task_id, calendar_id, event_id, summary, start_date, end_date, etag, html_link, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id, calendar_id) DO UPDATE SET
                    event_id=excluded.event_id,
                    summary=excluded.summary,
                    start_date=excluded.start_date,
                    end_date=excluded.end_date,
                    etag=excluded.etag,
                    html_link=excluded.html_link,
                    status=excluded.status,
                    updated_at=excluded.updated_at
                """,
                (task_id, calendar_id, event_id, summary, start_date, end_date, etag, html_link, status, now_iso, now_iso),
            )
            conn.commit()

    def remove_event_mapping(self, task_id: int, calendar_id: Optional[str] = None) -> None:
        """Remove local event mapping for a task."""
        cal_id = calendar_id or self.config.google_default_calendar
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM google_calendar_events WHERE task_id = ? AND calendar_id = ?",
                (task_id, cal_id),
            )
            conn.commit()

    # -------------------------------------------------------------------------
    # Calendar Outbox Queue & Draining
    # -------------------------------------------------------------------------

    def enqueue_outbox(
        self, action: str, task_id: int, payload: Dict[str, Any]
    ) -> int:
        """Enqueue a calendar operation in the durable outbox."""
        now_iso = self.get_now().isoformat()
        payload_json = json.dumps(payload, ensure_ascii=False)
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO google_tasks_outbox (task_id, action, payload, created_at, target, retry_count)
                VALUES (?, ?, ?, ?, 'calendar', 0)
                """,
                (task_id, action, payload_json, now_iso),
            )
            outbox_id = cursor.lastrowid
            conn.commit()
        return outbox_id

    def get_pending_outbox(self) -> List[Dict[str, Any]]:
        """Retrieve all pending calendar operations in FIFO order."""
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, task_id, action, payload, created_at, target, retry_count
                FROM google_tasks_outbox
                WHERE target = 'calendar'
                ORDER BY id ASC
                """
            )
            rows = cursor.fetchall()
        return [dict(r) for r in rows]

    def remove_outbox_item(self, outbox_id: int) -> None:
        """Remove processed calendar mutation from outbox."""
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM google_tasks_outbox WHERE id = ?", (outbox_id,))
            conn.commit()

    def drain_outbox(self) -> int:
        """
        Process queued calendar outbox mutations against Google Calendar API.
        Returns the number of processed mutations.
        """
        if not self.config.google_calendar_enabled or not self.is_authenticated():
            return 0

        items = self.get_pending_outbox()
        if not items:
            return 0

        processed = 0
        for item in items:
            action = item["action"]
            task_id = item["task_id"]
            payload = json.loads(item["payload"] or "{}")
            cal_id = payload.get("calendar_id") or self.config.google_default_calendar

            try:
                if action == "calendar_create" or action == "create":
                    start_date = payload["date"]
                    title = payload.get("title", "")
                    description = payload.get("description", "")
                    source = payload.get("source", "extern")
                    exclusive_end = calculate_exclusive_end_date(start_date)

                    # Idempotency check: see if event was already created
                    existing_mapping = self.get_event_mapping(task_id, cal_id)
                    event_id = existing_mapping.get("event_id") if existing_mapping else None

                    if not event_id:
                        # Remote discovery safeguard via private extended properties
                        remotes = self.client.find_events_by_task_id(cal_id, task_id)
                        if remotes:
                            event_id = remotes[0].get("id")

                    if event_id:
                        # Already exists remotely, update it instead of creating duplicate
                        res = self.client.update_all_day_event(
                            calendar_id=cal_id,
                            event_id=event_id,
                            task_id=task_id,
                            title=title,
                            start_date=start_date,
                            description=description,
                            source=source,
                        )
                    else:
                        res = self.client.create_all_day_event(
                            calendar_id=cal_id,
                            task_id=task_id,
                            title=title,
                            start_date=start_date,
                            description=description,
                            source=source,
                        )

                    if res and res.get("id"):
                        self.save_event_mapping(
                            task_id=task_id,
                            calendar_id=cal_id,
                            event_id=res["id"],
                            summary=title,
                            start_date=start_date,
                            end_date=exclusive_end,
                            etag=res.get("etag", ""),
                            html_link=res.get("htmlLink", ""),
                            status=res.get("status", "confirmed"),
                        )

                elif action == "calendar_update" or action == "update":
                    existing_mapping = self.get_event_mapping(task_id, cal_id)
                    event_id = payload.get("event_id") or (existing_mapping.get("event_id") if existing_mapping else None)
                    if not event_id:
                        remotes = self.client.find_events_by_task_id(cal_id, task_id)
                        if remotes:
                            event_id = remotes[0].get("id")

                    if event_id:
                        start_date = payload.get("date")
                        source = payload.get("source", "extern")
                        res = self.client.update_all_day_event(
                            calendar_id=cal_id,
                            event_id=event_id,
                            task_id=task_id,
                            title=payload.get("title"),
                            start_date=start_date,
                            description=payload.get("description"),
                            source=source,
                        )
                        if res and res.get("id"):
                            exclusive_end = calculate_exclusive_end_date(start_date) if start_date else (existing_mapping.get("end_date", "") if existing_mapping else "")
                            self.save_event_mapping(
                                task_id=task_id,
                                calendar_id=cal_id,
                                event_id=res["id"],
                                summary=res.get("summary", payload.get("title", "")),
                                start_date=start_date or (existing_mapping.get("start_date", "") if existing_mapping else ""),
                                end_date=exclusive_end,
                                etag=res.get("etag", ""),
                                html_link=res.get("htmlLink", ""),
                                status=res.get("status", "confirmed"),
                            )

                elif action == "calendar_delete" or action == "delete":
                    existing_mapping = self.get_event_mapping(task_id, cal_id)
                    event_id = payload.get("event_id") or (existing_mapping.get("event_id") if existing_mapping else None)

                    if not event_id:
                        remotes = self.client.find_events_by_task_id(cal_id, task_id)
                        if remotes:
                            event_id = remotes[0].get("id")

                    if event_id:
                        # Safety check: ensure event is correlated with task before deleting
                        self.client.delete_event(cal_id, event_id)
                    self.remove_event_mapping(task_id, cal_id)

                self.remove_outbox_item(item["id"])
                processed += 1

            except GoogleCalendarNotFoundError:
                # Event already gone remotely, clean up local mapping & outbox
                self.remove_event_mapping(task_id, cal_id)
                self.remove_outbox_item(item["id"])
                processed += 1
            except (GoogleCalendarNetworkError, Exception) as e:
                # Network failure or API rate-limit: increment retry count and pause draining
                with self.db.get_connection() as conn:
                    cursor = conn.cursor()
                    cursor.execute(
                        "UPDATE google_tasks_outbox SET retry_count = retry_count + 1, last_error = ? WHERE id = ?",
                        (str(e), item["id"]),
                    )
                    conn.commit()
                break

        return processed

    # -------------------------------------------------------------------------
    # High-Level Event Lifecycle Hooks
    # -------------------------------------------------------------------------

    def on_task_scheduled(
        self, task: Task, scheduled_date: str, calendar_id: Optional[str] = None
    ) -> None:
        """Enqueue creation or update of an all-day calendar event when task is scheduled."""
        if not self.config.google_calendar_enabled:
            return
        cal_id = calendar_id or self.config.google_default_calendar
        self.enqueue_outbox(
            action="calendar_create",
            task_id=task.id,
            payload={
                "calendar_id": cal_id,
                "title": task.title,
                "description": task.description or "",
                "date": scheduled_date[:10],
                "source": task.source_type or "extern",
            },
        )
        if self.sync_manager:
            self.sync_manager.trigger_sync()

    def on_task_rescheduled(
        self, task: Task, new_date: str, calendar_id: Optional[str] = None
    ) -> None:
        """Enqueue update of calendar event date."""
        if not self.config.google_calendar_enabled:
            return
        cal_id = calendar_id or self.config.google_default_calendar
        self.enqueue_outbox(
            action="calendar_update",
            task_id=task.id,
            payload={
                "calendar_id": cal_id,
                "title": task.title,
                "description": task.description or "",
                "date": new_date[:10],
                "source": task.source_type or "extern",
            },
        )
        if self.sync_manager:
            self.sync_manager.trigger_sync()

    def on_task_unscheduled(
        self, task_id: int, calendar_id: Optional[str] = None
    ) -> None:
        """Apply unschedule policy (e.g. delete event from calendar)."""
        if not self.config.google_calendar_enabled:
            return
        if self.config.google_calendar_unschedule_policy == "delete":
            cal_id = calendar_id or self.config.google_default_calendar
            mapping = self.get_event_mapping(task_id, cal_id)
            event_id = mapping.get("event_id") if mapping else None
            self.enqueue_outbox(
                action="calendar_delete",
                task_id=task_id,
                payload={"calendar_id": cal_id, "event_id": event_id},
            )
            if self.sync_manager:
                self.sync_manager.trigger_sync()

    def on_task_deleted(
        self, task_id: int, calendar_id: Optional[str] = None
    ) -> None:
        """Apply delete policy (e.g. remove event from calendar)."""
        if not self.config.google_calendar_enabled:
            return
        if self.config.google_calendar_delete_policy == "delete":
            cal_id = calendar_id or self.config.google_default_calendar
            mapping = self.get_event_mapping(task_id, cal_id)
            event_id = mapping.get("event_id") if mapping else None
            self.enqueue_outbox(
                action="calendar_delete",
                task_id=task_id,
                payload={"calendar_id": cal_id, "event_id": event_id},
            )
            if self.sync_manager:
                self.sync_manager.trigger_sync()

    def handle_task_completed(
        self, task_id: int, is_completed: bool, calendar_id: Optional[str] = None
    ) -> bool:
        """Apply completed task policy for calendar (default keep, preserving history)."""
        if not self.config.google_calendar_enabled:
            return True
        if self.config.google_calendar_complete_policy == "delete" and is_completed:
            self.on_task_deleted(task_id, calendar_id=calendar_id)
            return True
        return True

    def on_task_edited(
        self, task: Task, calendar_id: Optional[str] = None
    ) -> None:
        """Update event summary / description when task details change."""
        if not self.config.google_calendar_enabled:
            return
        cal_id = calendar_id or self.config.google_default_calendar
        mapping = self.get_event_mapping(task.id, cal_id)
        if mapping:
            self.enqueue_outbox(
                action="calendar_update",
                task_id=task.id,
                payload={
                    "calendar_id": cal_id,
                    "event_id": mapping.get("event_id"),
                    "title": task.title,
                    "description": task.description or "",
                },
            )
            if self.sync_manager:
                self.sync_manager.trigger_sync()
