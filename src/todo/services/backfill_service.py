"""
Initial synchronization (backfill) service for Extern.
Discovers eligible existing tasks and safely, idempotently synchronizes them
to Google Tasks and Google Calendar.
"""

from dataclasses import dataclass
import json
import time
from typing import Any, Dict, List, Optional

from todo.config import Config
from todo.database import Database
from todo.services.google_calendar_service import GoogleCalendarService
from todo.services.google_tasks_service import GoogleTasksService


@dataclass
class TaskBackfillItem:
    task_id: int
    title: str
    source_type: str
    completed: bool
    completed_at: Optional[str]
    description: Optional[str]
    scheduled_date: Optional[str]
    sync_to_tasks: bool
    tasks_target_list: Optional[str] = None
    tasks_skip_reason: Optional[str] = None
    sync_to_calendar: bool = False
    calendar_target_id: Optional[str] = None
    calendar_skip_reason: Optional[str] = None


@dataclass
class BackfillPlan:
    items: List[TaskBackfillItem]
    tasks_to_sync_count: int
    tasks_already_synced_count: int
    tasks_skipped_count: int
    calendar_to_sync_count: int
    calendar_already_synced_count: int
    calendar_skipped_count: int
    has_tasks_auth: bool
    has_calendar_auth: bool
    calendar_enabled: bool


@dataclass
class BackfillResult:
    success: bool
    tasks_enqueued: int
    tasks_drained: int
    calendar_enqueued: int
    calendar_drained: int
    errors: List[str]
    message: str


class SyncBackfillService:
    """Orchestrates initial synchronization of existing tasks to Google Tasks and Google Calendar."""

    def __init__(
        self,
        db: Database,
        config: Config,
        tasks_service: Optional[GoogleTasksService] = None,
        calendar_service: Optional[GoogleCalendarService] = None,
    ):
        self.db = db
        self.config = config
        self.tasks_service = tasks_service or GoogleTasksService(db, config)
        self.calendar_service = calendar_service or GoogleCalendarService(db, config)

    def build_plan(
        self,
        tasks_only: bool = False,
        calendar_only: bool = False,
        list_id: Optional[str] = None,
        calendar_id: Optional[str] = None,
    ) -> BackfillPlan:
        """
        Build an idempotent, read-only plan for synchronizing existing tasks.
        Inspects tasks, scheduled dates, mappings, and pending outbox entries.
        Does not mutate the database or make network calls.
        """
        has_tasks_auth = self.tasks_service.is_authenticated()
        has_calendar_auth = self.calendar_service.is_authenticated()
        calendar_enabled = getattr(self.config, "google_calendar_enabled", False)

        default_tasks_list = list_id or getattr(self.config, "google_default_task_list", "@default")
        default_cal_id = calendar_id or getattr(self.config, "google_default_calendar", "primary")

        items: List[TaskBackfillItem] = []

        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, title, description, source_type, source_id, status, created_at, updated_at
                FROM tasks
                ORDER BY id ASC
                """
            )
            rows = cursor.fetchall()

            for row in rows:
                task_id = row["id"]
                title = row["title"]
                desc = row["description"] or ""
                source_type = row["source_type"] or "manual"
                completed = (row["status"] == "completed")
                completed_at = None

                if completed:
                    cursor.execute(
                        "SELECT completed_at FROM daily_tasks WHERE task_id = ? AND completed_at IS NOT NULL ORDER BY id DESC LIMIT 1",
                        (task_id,),
                    )
                    dt_comp = cursor.fetchone()
                    if dt_comp and dt_comp["completed_at"]:
                        completed_at = dt_comp["completed_at"]
                    elif row["source_id"]:
                        cursor.execute(
                            "SELECT completed_at FROM file_items WHERE id = ? AND completed_at IS NOT NULL",
                            (row["source_id"],),
                        )
                        fi_comp = cursor.fetchone()
                        if fi_comp and fi_comp["completed_at"]:
                            completed_at = fi_comp["completed_at"]
                    if not completed_at:
                        completed_at = row["updated_at"]

                # Resolve scheduled date from daily_tasks
                cursor.execute(
                    "SELECT date FROM daily_tasks WHERE task_id = ? ORDER BY date DESC, id DESC LIMIT 1",
                    (task_id,),
                )
                dt_row = cursor.fetchone()
                scheduled_date = dt_row["date"] if dt_row else None

                # 1. Evaluate Google Tasks eligibility
                sync_to_tasks = False
                tasks_target_list: Optional[str] = None
                tasks_skip_reason: Optional[str] = None

                if calendar_only:
                    sync_to_tasks = False
                    tasks_skip_reason = "calendar-only mode"
                elif source_type in ("filemeta", "telegram", "youtube"):
                    sync_to_tasks = False
                    tasks_skip_reason = f"source-backed ({source_type})"
                elif source_type == "google_tasks":
                    sync_to_tasks = False
                    tasks_skip_reason = "already google_tasks origin"
                else:
                    mapping = self.tasks_service.get_task_google_mapping(task_id)
                    if mapping and not str(mapping.get("remote_id", "")).startswith("local_"):
                        sync_to_tasks = False
                        tasks_skip_reason = "already mapped to Google Tasks"
                    else:
                        cursor.execute(
                            """
                            SELECT id FROM google_tasks_outbox
                            WHERE task_id = ? AND (target = 'tasks' OR target IS NULL) AND action = 'create'
                            """,
                            (task_id,),
                        )
                        if cursor.fetchone():
                            sync_to_tasks = False
                            tasks_skip_reason = "pending in tasks outbox"
                        else:
                            sync_to_tasks = True
                            tasks_target_list = default_tasks_list

                # 2. Evaluate Google Calendar eligibility
                sync_to_calendar = False
                calendar_target_id: Optional[str] = None
                calendar_skip_reason: Optional[str] = None

                if tasks_only:
                    sync_to_calendar = False
                    calendar_skip_reason = "tasks-only mode"
                elif not calendar_enabled:
                    sync_to_calendar = False
                    calendar_skip_reason = "Google Calendar disabled in settings"
                elif not scheduled_date:
                    sync_to_calendar = False
                    calendar_skip_reason = "not scheduled in daily_tasks"
                elif completed and getattr(self.config, "google_calendar_complete_policy", "keep") == "delete":
                    sync_to_calendar = False
                    calendar_skip_reason = "completed (calendar delete policy)"
                else:
                    cal_map = self.calendar_service.get_event_mapping(task_id, default_cal_id)
                    if cal_map and cal_map.get("event_id"):
                        sync_to_calendar = False
                        calendar_skip_reason = "already mapped to Google Calendar"
                    else:
                        cursor.execute(
                            """
                            SELECT id FROM google_tasks_outbox
                            WHERE task_id = ? AND target = 'calendar' AND action IN ('calendar_create', 'create')
                            """,
                            (task_id,),
                        )
                        if cursor.fetchone():
                            sync_to_calendar = False
                            calendar_skip_reason = "pending in calendar outbox"
                        else:
                            sync_to_calendar = True
                            calendar_target_id = default_cal_id

                items.append(
                    TaskBackfillItem(
                        task_id=task_id,
                        title=title,
                        source_type=source_type,
                        completed=completed,
                        completed_at=completed_at,
                        description=desc,
                        scheduled_date=scheduled_date,
                        sync_to_tasks=sync_to_tasks,
                        tasks_target_list=tasks_target_list,
                        tasks_skip_reason=tasks_skip_reason,
                        sync_to_calendar=sync_to_calendar,
                        calendar_target_id=calendar_target_id,
                        calendar_skip_reason=calendar_skip_reason,
                    )
                )

        tasks_to_sync = sum(1 for i in items if i.sync_to_tasks)
        tasks_already_synced = sum(
            1 for i in items
            if not i.sync_to_tasks
            and i.tasks_skip_reason
            in (
                "already mapped to Google Tasks",
                "already google_tasks origin",
                "pending in tasks outbox",
            )
        )
        tasks_skipped = len(items) - tasks_to_sync - tasks_already_synced

        calendar_to_sync = sum(1 for i in items if i.sync_to_calendar)
        calendar_already_synced = sum(
            1 for i in items
            if not i.sync_to_calendar
            and i.calendar_skip_reason
            in (
                "already mapped to Google Calendar",
                "pending in calendar outbox",
            )
        )
        calendar_skipped = len(items) - calendar_to_sync - calendar_already_synced

        return BackfillPlan(
            items=items,
            tasks_to_sync_count=tasks_to_sync,
            tasks_already_synced_count=tasks_already_synced,
            tasks_skipped_count=tasks_skipped,
            calendar_to_sync_count=calendar_to_sync,
            calendar_already_synced_count=calendar_already_synced,
            calendar_skipped_count=calendar_skipped,
            has_tasks_auth=has_tasks_auth,
            has_calendar_auth=has_calendar_auth,
            calendar_enabled=calendar_enabled,
        )

    def execute_backfill(
        self,
        plan: Optional[BackfillPlan] = None,
        tasks_only: bool = False,
        calendar_only: bool = False,
        list_id: Optional[str] = None,
        calendar_id: Optional[str] = None,
        drain: bool = True,
    ) -> BackfillResult:
        """
        Execute backfill according to the plan:
        1. Validates auth.
        2. Atomically enqueues outbox operations and tentative mappings inside DB transaction.
        3. Drains outboxes outside DB transaction (if drain=True).
        """
        if plan is None:
            plan = self.build_plan(
                tasks_only=tasks_only,
                calendar_only=calendar_only,
                list_id=list_id,
                calendar_id=calendar_id,
            )

        errors: List[str] = []

        if plan.tasks_to_sync_count > 0 and not plan.has_tasks_auth:
            return BackfillResult(
                success=False,
                tasks_enqueued=0,
                tasks_drained=0,
                calendar_enqueued=0,
                calendar_drained=0,
                errors=["Google Tasks authentication required. Run 'todo auth' or 'todo' to connect."],
                message="Google Tasks authentication required.",
            )

        if plan.calendar_to_sync_count > 0 and not plan.has_calendar_auth:
            return BackfillResult(
                success=False,
                tasks_enqueued=0,
                tasks_drained=0,
                calendar_enqueued=0,
                calendar_drained=0,
                errors=["Google Calendar authorization required. Run 'todo auth' or 'todo' to connect."],
                message="Google Calendar authorization required.",
            )

        if plan.tasks_to_sync_count == 0 and plan.calendar_to_sync_count == 0:
            return BackfillResult(
                success=True,
                tasks_enqueued=0,
                tasks_drained=0,
                calendar_enqueued=0,
                calendar_drained=0,
                errors=[],
                message="All eligible tasks are already synchronized. Nothing to backfill.",
            )

        tasks_enqueued = 0
        calendar_enqueued = 0
        now_iso = self.tasks_service.get_now().isoformat()

        # Phase 1: Local durable enqueuing inside atomic transaction
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            for item in plan.items:
                if item.sync_to_tasks:
                    # Idempotency check inside transaction
                    cursor.execute(
                        "SELECT remote_id FROM task_google_tasks_map WHERE task_id = ?",
                        (item.task_id,),
                    )
                    gtm_row = cursor.fetchone()
                    if not gtm_row or str(gtm_row["remote_id"]).startswith("local_"):
                        temp_remote_id = f"local_{item.task_id}_{int(time.time() * 1000)}"
                        cursor.execute(
                            """
                            INSERT INTO task_google_tasks_map (task_id, list_id, remote_id, etag, updated_at)
                            VALUES (?, ?, ?, '', ?)
                            ON CONFLICT(task_id) DO UPDATE SET
                                list_id=excluded.list_id,
                                remote_id=excluded.remote_id,
                                updated_at=excluded.updated_at
                            """,
                            (item.task_id, item.tasks_target_list, temp_remote_id, now_iso),
                        )
                        payload: Dict[str, Any] = {
                            "list_id": item.tasks_target_list,
                            "title": item.title,
                            "notes": item.description or "",
                            "due_date": item.scheduled_date,
                            "local_task_id": item.task_id,
                            "task_id": temp_remote_id,
                        }
                        if item.completed:
                            payload["status"] = "completed"
                            if item.completed_at:
                                payload["completed"] = item.completed_at

                        cursor.execute(
                            """
                            INSERT INTO google_tasks_outbox (task_id, action, payload, created_at, target, retry_count)
                            VALUES (?, 'create', ?, ?, 'tasks', 0)
                            """,
                            (item.task_id, json.dumps(payload, ensure_ascii=False), now_iso),
                        )
                        tasks_enqueued += 1

                if item.sync_to_calendar:
                    cursor.execute(
                        "SELECT event_id FROM google_calendar_events WHERE task_id = ? AND calendar_id = ?",
                        (item.task_id, item.calendar_target_id),
                    )
                    cal_row = cursor.fetchone()
                    if not cal_row or not cal_row["event_id"]:
                        cal_payload = {
                            "calendar_id": item.calendar_target_id,
                            "title": item.title,
                            "description": item.description or "",
                            "date": item.scheduled_date[:10] if item.scheduled_date else "",
                            "source": item.source_type or "extern",
                        }
                        cursor.execute(
                            """
                            INSERT INTO google_tasks_outbox (task_id, action, payload, created_at, target, retry_count)
                            VALUES (?, 'calendar_create', ?, ?, 'calendar', 0)
                            """,
                            (item.task_id, json.dumps(cal_payload, ensure_ascii=False), now_iso),
                        )
                        calendar_enqueued += 1

            conn.commit()

        # Phase 2: Draining outboxes outside transaction
        tasks_drained = 0
        calendar_drained = 0
        if drain:
            if tasks_enqueued > 0:
                tasks_drained = self.tasks_service.drain_outbox()
            if calendar_enqueued > 0:
                calendar_drained = self.calendar_service.drain_outbox()

            # Check if any errors occurred during drain
            with self.db.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT task_id, target, last_error FROM google_tasks_outbox
                    WHERE retry_count > 0 AND last_error IS NOT NULL
                    """
                )
                for err_row in cursor.fetchall():
                    errors.append(f"Task #{err_row['task_id']} ({err_row['target']}): {err_row['last_error']}")

        if self.tasks_service.sync_manager:
            self.tasks_service.sync_manager.notify_change()
            self.tasks_service.sync_manager.trigger_sync()

        msg = f"Enqueued {tasks_enqueued} task(s) for Google Tasks and {calendar_enqueued} event(s) for Google Calendar."
        if drain:
            msg += f" Processed {tasks_drained} task(s) and {calendar_drained} event(s) remotely."

        return BackfillResult(
            success=(len(errors) == 0),
            tasks_enqueued=tasks_enqueued,
            tasks_drained=tasks_drained,
            calendar_enqueued=calendar_enqueued,
            calendar_drained=calendar_drained,
            errors=errors,
            message=msg,
        )
