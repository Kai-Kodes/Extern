"""Database management and SQLite connection abstraction."""

import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional


TABLES_SQL = """
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    path TEXT NOT NULL UNIQUE,
    canonical_uri TEXT,
    scan_status TEXT NOT NULL DEFAULT 'completed',
    scanned_at TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS source_folders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    parent_id INTEGER REFERENCES source_folders(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(source_id, relative_path)
);

CREATE TABLE IF NOT EXISTS file_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    folder_id INTEGER REFERENCES source_folders(id) ON DELETE SET NULL,
    path TEXT NOT NULL,
    relative_path TEXT,
    file_size INTEGER DEFAULT 0,
    extension TEXT,
    title TEXT NOT NULL,
    type TEXT NOT NULL DEFAULT 'file',
    metadata TEXT NOT NULL DEFAULT '{}',
    completed INTEGER NOT NULL DEFAULT 0,
    completed_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(source_id, path)
);

CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    source_type TEXT NOT NULL DEFAULT 'manual',
    source_id INTEGER,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS daily_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    date TEXT NOT NULL,
    position INTEGER NOT NULL DEFAULT 0,
    completed_at TEXT,
    UNIQUE(task_id, date)
);

CREATE TABLE IF NOT EXISTS telegram_channels_cache (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    username TEXT,
    is_channel INTEGER NOT NULL DEFAULT 1,
    is_group INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pomodoro_presets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    work_duration INTEGER NOT NULL DEFAULT 25,
    short_break_duration INTEGER NOT NULL DEFAULT 5,
    long_break_duration INTEGER NOT NULL DEFAULT 15,
    sessions_before_long_break INTEGER NOT NULL DEFAULT 4,
    auto_advance INTEGER NOT NULL DEFAULT 0,
    sound_enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pomodoro_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    preset_id INTEGER REFERENCES pomodoro_presets(id) ON DELETE SET NULL,
    phase TEXT NOT NULL DEFAULT 'work',
    state TEXT NOT NULL DEFAULT 'idle',
    deadline_wall_clock TEXT,
    remaining_seconds_when_paused INTEGER,
    completed_work_sessions INTEGER NOT NULL DEFAULT 0,
    current_session_index INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS google_task_lists_cache (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    updated TEXT,
    etag TEXT,
    cached_at TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS google_calendars_cache (
    id TEXT PRIMARY KEY,
    summary TEXT NOT NULL,
    description TEXT,
    time_zone TEXT,
    access_role TEXT,
    primary_calendar INTEGER NOT NULL DEFAULT 0,
    cached_at TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS google_calendar_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    calendar_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    summary TEXT,
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    etag TEXT,
    html_link TEXT,
    status TEXT NOT NULL DEFAULT 'confirmed',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(task_id, calendar_id)
);

CREATE TABLE IF NOT EXISTS task_google_tasks_map (
    task_id INTEGER PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
    list_id TEXT NOT NULL,
    remote_id TEXT NOT NULL,
    etag TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS google_tasks_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER,
    action TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    target TEXT NOT NULL DEFAULT 'tasks',
    retry_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    locked_until REAL DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS chunky_resources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_item_id INTEGER REFERENCES file_items(id) ON DELETE CASCADE,
    source_type TEXT NOT NULL,
    canonical_uri TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    range_unit TEXT NOT NULL,
    total_range REAL NOT NULL,
    completed_range REAL NOT NULL DEFAULT 0.0,
    is_fully_completed INTEGER NOT NULL DEFAULT 0,
    fully_completed_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chunky_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    resource_id INTEGER NOT NULL REFERENCES chunky_resources(id) ON DELETE CASCADE,
    task_id INTEGER NOT NULL UNIQUE REFERENCES tasks(id) ON DELETE CASCADE,
    chunk_index INTEGER NOT NULL DEFAULT 1,
    start_pos REAL NOT NULL,
    end_pos REAL NOT NULL,
    is_completed INTEGER NOT NULL DEFAULT 0,
    completed_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(resource_id, start_pos, end_pos)
);

CREATE TABLE IF NOT EXISTS chunky_completion_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chunk_id INTEGER NOT NULL REFERENCES chunky_tasks(id) ON DELETE CASCADE,
    resource_id INTEGER NOT NULL REFERENCES chunky_resources(id) ON DELETE CASCADE,
    task_id INTEGER NOT NULL,
    start_pos REAL NOT NULL,
    end_pos REAL NOT NULL,
    completed_at TEXT NOT NULL,
    action TEXT NOT NULL DEFAULT 'completed',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chunky_presets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL COLLATE NOCASE,
    duration REAL NOT NULL,
    range_unit TEXT NOT NULL DEFAULT 'seconds',
    is_default INTEGER NOT NULL DEFAULT 0,
    is_builtin INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(range_unit, name)
);
"""

INDEXES_SQL = """
CREATE INDEX IF NOT EXISTS idx_source_folders_source_id ON source_folders(source_id);
CREATE INDEX IF NOT EXISTS idx_source_folders_parent_id ON source_folders(source_id, parent_id);
CREATE INDEX IF NOT EXISTS idx_source_folders_rel_path ON source_folders(source_id, relative_path);

CREATE INDEX IF NOT EXISTS idx_file_items_source_id ON file_items(source_id);
CREATE INDEX IF NOT EXISTS idx_file_items_folder_id ON file_items(source_id, folder_id);
CREATE INDEX IF NOT EXISTS idx_file_items_rel_path ON file_items(source_id, relative_path);
CREATE INDEX IF NOT EXISTS idx_file_items_path ON file_items(path);

CREATE INDEX IF NOT EXISTS idx_tasks_source ON tasks(source_type, source_id);
CREATE INDEX IF NOT EXISTS idx_daily_tasks_date ON daily_tasks(date);
CREATE INDEX IF NOT EXISTS idx_daily_tasks_completed_at ON daily_tasks(completed_at);
CREATE INDEX IF NOT EXISTS idx_telegram_cache_title ON telegram_channels_cache(title);
CREATE INDEX IF NOT EXISTS idx_google_task_lists_cache_title ON google_task_lists_cache(title);
CREATE INDEX IF NOT EXISTS idx_google_calendars_cache_summary ON google_calendars_cache(summary);
CREATE INDEX IF NOT EXISTS idx_gcal_events_task ON google_calendar_events(task_id);
CREATE INDEX IF NOT EXISTS idx_gcal_events_event ON google_calendar_events(calendar_id, event_id);
CREATE INDEX IF NOT EXISTS idx_task_gt_map_remote ON task_google_tasks_map(list_id, remote_id);
CREATE INDEX IF NOT EXISTS idx_google_tasks_outbox_created ON google_tasks_outbox(created_at);
CREATE INDEX IF NOT EXISTS idx_google_tasks_outbox_target ON google_tasks_outbox(target, created_at);
CREATE INDEX IF NOT EXISTS idx_pomodoro_presets_name ON pomodoro_presets(name);
CREATE INDEX IF NOT EXISTS idx_chunky_resources_uri ON chunky_resources(canonical_uri);
CREATE INDEX IF NOT EXISTS idx_chunky_resources_item ON chunky_resources(file_item_id);
CREATE INDEX IF NOT EXISTS idx_chunky_tasks_resource ON chunky_tasks(resource_id);
CREATE INDEX IF NOT EXISTS idx_chunky_tasks_task ON chunky_tasks(task_id);
CREATE INDEX IF NOT EXISTS idx_chunky_history_resource ON chunky_completion_history(resource_id);
CREATE INDEX IF NOT EXISTS idx_chunky_history_chunk ON chunky_completion_history(chunk_id);
CREATE INDEX IF NOT EXISTS idx_chunky_presets_unit ON chunky_presets(range_unit);
"""

SCHEMA_SQL = TABLES_SQL


class Database:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._ensure_parent_dir()
        self.init_db()

    def _ensure_parent_dir(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    def get_connection(self) -> sqlite3.Connection:
        """Create and return an SQLite connection configured with Row factory and busy timeout."""
        conn = sqlite3.connect(str(self.db_path), timeout=15.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute("PRAGMA busy_timeout = 15000;")
        return conn

    def init_db(self) -> None:
        """Initialize the database schema, apply necessary column migrations, and create indexes."""
        with self.get_connection() as conn:
            # 1. Create base tables if they don't already exist (without indexes on migrated columns)
            conn.executescript(TABLES_SQL)
            cursor = conn.cursor()

            # 2. Apply column migrations for existing tables
            # Migration check for sources
            cursor.execute("PRAGMA table_info(sources)")
            s_columns = {row["name"] for row in cursor.fetchall()}
            if "canonical_uri" not in s_columns:
                cursor.execute("ALTER TABLE sources ADD COLUMN canonical_uri TEXT")
            if "scan_status" not in s_columns:
                cursor.execute("ALTER TABLE sources ADD COLUMN scan_status TEXT NOT NULL DEFAULT 'completed'")
            if "scanned_at" not in s_columns:
                cursor.execute("ALTER TABLE sources ADD COLUMN scanned_at TEXT")
            if "last_error" not in s_columns:
                cursor.execute("ALTER TABLE sources ADD COLUMN last_error TEXT")

            # Migration check for file_items
            cursor.execute("PRAGMA table_info(file_items)")
            fi_columns = {row["name"] for row in cursor.fetchall()}
            if "completed" not in fi_columns:
                cursor.execute(
                    "ALTER TABLE file_items ADD COLUMN completed INTEGER NOT NULL DEFAULT 0"
                )
            if "completed_at" not in fi_columns:
                cursor.execute("ALTER TABLE file_items ADD COLUMN completed_at TEXT")
            if "folder_id" not in fi_columns:
                cursor.execute(
                    "ALTER TABLE file_items ADD COLUMN folder_id INTEGER REFERENCES source_folders(id) ON DELETE SET NULL"
                )
            if "relative_path" not in fi_columns:
                cursor.execute("ALTER TABLE file_items ADD COLUMN relative_path TEXT")
            if "file_size" not in fi_columns:
                cursor.execute("ALTER TABLE file_items ADD COLUMN file_size INTEGER DEFAULT 0")
            if "extension" not in fi_columns:
                cursor.execute("ALTER TABLE file_items ADD COLUMN extension TEXT")

            # Migration check for daily_tasks
            cursor.execute("PRAGMA table_info(daily_tasks)")
            dt_columns = {row["name"] for row in cursor.fetchall()}
            if "completed_at" not in dt_columns:
                cursor.execute("ALTER TABLE daily_tasks ADD COLUMN completed_at TEXT")

            # Migration check for google_task_lists_cache
            cursor.execute("PRAGMA table_info(google_task_lists_cache)")
            gtc_columns = {row["name"] for row in cursor.fetchall()}
            if "updated" not in gtc_columns:
                cursor.execute("ALTER TABLE google_task_lists_cache ADD COLUMN updated TEXT")
            if "etag" not in gtc_columns:
                cursor.execute("ALTER TABLE google_task_lists_cache ADD COLUMN etag TEXT")
            if "cached_at" not in gtc_columns:
                cursor.execute("ALTER TABLE google_task_lists_cache ADD COLUMN cached_at TEXT")

            # Ensure google_tasks_outbox table exists and has updated columns
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS google_tasks_outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id INTEGER,
                    action TEXT NOT NULL,
                    payload TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    target TEXT NOT NULL DEFAULT 'tasks',
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    locked_until REAL DEFAULT 0.0
                )
                """
            )
            cursor.execute("PRAGMA table_info(google_tasks_outbox)")
            gto_columns = {row["name"] for row in cursor.fetchall()}
            if "target" not in gto_columns:
                cursor.execute("ALTER TABLE google_tasks_outbox ADD COLUMN target TEXT NOT NULL DEFAULT 'tasks'")
            if "retry_count" not in gto_columns:
                cursor.execute("ALTER TABLE google_tasks_outbox ADD COLUMN retry_count INTEGER NOT NULL DEFAULT 0")
            if "last_error" not in gto_columns:
                cursor.execute("ALTER TABLE google_tasks_outbox ADD COLUMN last_error TEXT")
            if "locked_until" not in gto_columns:
                cursor.execute("ALTER TABLE google_tasks_outbox ADD COLUMN locked_until REAL DEFAULT 0.0")

            # Ensure task_google_tasks_map table exists
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS task_google_tasks_map (
                    task_id INTEGER PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
                    list_id TEXT NOT NULL,
                    remote_id TEXT NOT NULL,
                    etag TEXT,
                    updated_at TEXT NOT NULL
                )
                """
            )

            # Ensure google_calendar_events and google_calendars_cache tables exist
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS google_calendars_cache (
                    id TEXT PRIMARY KEY,
                    summary TEXT NOT NULL,
                    description TEXT,
                    time_zone TEXT,
                    access_role TEXT,
                    primary_calendar INTEGER NOT NULL DEFAULT 0,
                    cached_at TEXT,
                    updated_at TEXT NOT NULL
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS google_calendar_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                    calendar_id TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    summary TEXT,
                    start_date TEXT NOT NULL,
                    end_date TEXT NOT NULL,
                    etag TEXT,
                    html_link TEXT,
                    status TEXT NOT NULL DEFAULT 'confirmed',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(task_id, calendar_id)
                )
                """
            )

            # Backfill task_google_tasks_map from existing Google Tasks
            cursor.execute(
                """
                INSERT OR IGNORE INTO task_google_tasks_map (task_id, list_id, remote_id, etag, updated_at)
                SELECT t.id,
                       COALESCE(json_extract(fi.metadata, '$.list_id'), '@default'),
                       fi.path,
                       COALESCE(json_extract(fi.metadata, '$.etag'), ''),
                       t.updated_at
                FROM tasks t
                JOIN file_items fi ON t.source_id = fi.id
                WHERE t.source_type = 'google_tasks'
                  AND fi.path IS NOT NULL
                  AND NOT fi.path LIKE 'local_%'
                  AND NOT EXISTS (
                      SELECT 1 FROM task_google_tasks_map m
                      WHERE m.remote_id = fi.path AND m.task_id != t.id
                  )
                """
            )

            # Safely create unique index if no duplicates violate it
            try:
                cursor.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_task_gt_map_unique_remote
                    ON task_google_tasks_map(list_id, remote_id)
                    WHERE NOT remote_id LIKE 'local_%'
                    """
                )
            except sqlite3.IntegrityError:
                pass

            # Ensure chunky tables exist
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS chunky_resources (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    file_item_id INTEGER REFERENCES file_items(id) ON DELETE CASCADE,
                    source_type TEXT NOT NULL,
                    canonical_uri TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL,
                    range_unit TEXT NOT NULL,
                    total_range REAL NOT NULL,
                    completed_range REAL NOT NULL DEFAULT 0.0,
                    is_fully_completed INTEGER NOT NULL DEFAULT 0,
                    fully_completed_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS chunky_tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    resource_id INTEGER NOT NULL REFERENCES chunky_resources(id) ON DELETE CASCADE,
                    task_id INTEGER NOT NULL UNIQUE REFERENCES tasks(id) ON DELETE CASCADE,
                    chunk_index INTEGER NOT NULL DEFAULT 1,
                    start_pos REAL NOT NULL,
                    end_pos REAL NOT NULL,
                    is_completed INTEGER NOT NULL DEFAULT 0,
                    completed_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(resource_id, start_pos, end_pos)
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS chunky_completion_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chunk_id INTEGER NOT NULL REFERENCES chunky_tasks(id) ON DELETE CASCADE,
                    resource_id INTEGER NOT NULL REFERENCES chunky_resources(id) ON DELETE CASCADE,
                    task_id INTEGER NOT NULL,
                    start_pos REAL NOT NULL,
                    end_pos REAL NOT NULL,
                    completed_at TEXT NOT NULL,
                    action TEXT NOT NULL DEFAULT 'completed',
                    created_at TEXT NOT NULL
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS chunky_presets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL COLLATE NOCASE,
                    duration REAL NOT NULL,
                    range_unit TEXT NOT NULL DEFAULT 'seconds',
                    is_default INTEGER NOT NULL DEFAULT 0,
                    is_builtin INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(range_unit, name)
                )
                """
            )

            # 3. Create all indexes safely now that tables and columns are guaranteed to exist
            conn.executescript(INDEXES_SQL)

            # 4. Seed default Pomodoro preset if none exist
            cursor.execute("SELECT count(*) as count FROM pomodoro_presets")
            preset_count = cursor.fetchone()["count"]
            if preset_count == 0:
                from datetime import datetime
                now_iso = datetime.now().isoformat()
                cursor.execute(
                    """
                    INSERT INTO pomodoro_presets (
                        name, work_duration, short_break_duration, long_break_duration,
                        sessions_before_long_break, auto_advance, sound_enabled,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    ("Classic", 25, 5, 15, 4, 0, 1, now_iso, now_iso),
                )

            # 5. Seed default Chunky presets if none exist
            cursor.execute("SELECT count(*) as count FROM chunky_presets WHERE range_unit = 'seconds'")
            if cursor.fetchone()["count"] == 0:
                from datetime import datetime
                now_iso = datetime.now().isoformat()
                default_seconds = [
                    ("15 minutes", 900.0, 0, 1),
                    ("20 minutes", 1200.0, 1, 1),
                    ("30 minutes", 1800.0, 0, 1),
                    ("45 minutes", 2700.0, 0, 1),
                    ("60 minutes", 3600.0, 0, 1),
                ]
                for name, dur, is_def, is_built in default_seconds:
                    cursor.execute(
                        """
                        INSERT OR IGNORE INTO chunky_presets (
                            name, duration, range_unit, is_default, is_builtin, created_at, updated_at
                        ) VALUES (?, ?, 'seconds', ?, ?, ?, ?)
                        """,
                        (name, dur, is_def, is_built, now_iso, now_iso),
                    )

            cursor.execute("SELECT count(*) as count FROM chunky_presets WHERE range_unit = 'pages'")
            if cursor.fetchone()["count"] == 0:
                from datetime import datetime
                now_iso = datetime.now().isoformat()
                default_pages = [
                    ("5 pages", 5.0, 0, 1),
                    ("10 pages", 10.0, 1, 1),
                    ("15 pages", 15.0, 0, 1),
                    ("20 pages", 20.0, 0, 1),
                    ("25 pages", 25.0, 0, 1),
                ]
                for name, dur, is_def, is_built in default_pages:
                    cursor.execute(
                        """
                        INSERT OR IGNORE INTO chunky_presets (
                            name, duration, range_unit, is_default, is_builtin, created_at, updated_at
                        ) VALUES (?, ?, 'pages', ?, ?, ?, ?)
                        """,
                        (name, dur, is_def, is_built, now_iso, now_iso),
                    )

            conn.commit()

    def reconcile_duplicate_echoes(self, conn: Optional[sqlite3.Connection] = None) -> List[Dict[str, Any]]:
        """
        Safely reconcile spurious duplicate task echoes created when a local manual/universal
        task was synced to Google Tasks and then re-imported as a google_tasks item.

        Preserves original task ID, title, creation history, daily scheduling, completion state,
        notes, outbox entries, calendar event associations, and remote task mapping.

        Returns a list of reconciled duplicate summaries.
        """
        should_close = False
        if conn is None:
            conn = self.get_connection()
            should_close = True

        reconciled: List[Dict[str, Any]] = []
        try:
            cursor = conn.cursor()

            # 1. Detect echoes where an original manual/universal task shares the same remote_id with a google_tasks item
            cursor.execute(
                """
                SELECT 
                    t1.id AS original_id,
                    t1.title AS original_title,
                    t1.description AS original_desc,
                    t1.status AS original_status,
                    t1.source_type AS original_source,
                    t1.created_at AS original_created_at,
                    m.remote_id,
                    m.list_id,
                    t2.id AS echo_id,
                    t2.title AS echo_title,
                    t2.description AS echo_desc,
                    t2.status AS echo_status,
                    t2.created_at AS echo_created_at,
                    fi.id AS echo_fi_id
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

            for echo in echoes:
                orig_id = echo["original_id"]
                echo_id = echo["echo_id"]
                echo_fi_id = echo["echo_fi_id"]

                # A. Merge completion status if echo was completed
                if echo["echo_status"] == "completed" and echo["original_status"] != "completed":
                    cursor.execute(
                        "UPDATE tasks SET status = 'completed', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                        (orig_id,),
                    )
                    cursor.execute(
                        "SELECT completed_at FROM daily_tasks WHERE task_id = ? AND completed_at IS NOT NULL LIMIT 1",
                        (echo_id,),
                    )
                    dt_completed = cursor.fetchone()
                    if dt_completed:
                        cursor.execute(
                            "UPDATE daily_tasks SET completed_at = ? WHERE task_id = ? AND completed_at IS NULL",
                            (dt_completed["completed_at"], orig_id),
                        )

                # B. Merge description/notes if original was empty
                if echo["echo_desc"] and not echo["original_desc"]:
                    cursor.execute(
                        "UPDATE tasks SET description = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                        (echo["echo_desc"], orig_id),
                    )

                # C. Handle daily_tasks scheduling
                cursor.execute("SELECT id, date FROM daily_tasks WHERE task_id = ?", (orig_id,))
                orig_daily = cursor.fetchall()
                cursor.execute("SELECT id, date, position, completed_at FROM daily_tasks WHERE task_id = ?", (echo_id,))
                echo_daily = cursor.fetchall()

                if not orig_daily and echo_daily:
                    cursor.execute("UPDATE daily_tasks SET task_id = ? WHERE task_id = ?", (orig_id, echo_id))
                else:
                    cursor.execute("DELETE FROM daily_tasks WHERE task_id = ?", (echo_id,))

                # D. Consolidate dependent outbox, calendar, pomodoro associations
                cursor.execute("UPDATE OR IGNORE google_tasks_outbox SET task_id = ? WHERE task_id = ?", (orig_id, echo_id))
                cursor.execute("DELETE FROM google_tasks_outbox WHERE task_id = ?", (echo_id,))

                cursor.execute("UPDATE OR IGNORE google_calendar_events SET task_id = ? WHERE task_id = ?", (orig_id, echo_id))
                cursor.execute("DELETE FROM google_calendar_events WHERE task_id = ?", (echo_id,))

                cursor.execute("DELETE FROM task_google_tasks_map WHERE task_id = ?", (echo_id,))

                # E. Delete spurious echo task and linked file_item
                cursor.execute("DELETE FROM tasks WHERE id = ?", (echo_id,))
                if echo_fi_id:
                    cursor.execute("DELETE FROM file_items WHERE id = ?", (echo_fi_id,))

                reconciled.append({
                    "original_id": orig_id,
                    "echo_id": echo_id,
                    "title": echo["original_title"],
                    "remote_id": echo["remote_id"],
                    "list_id": echo["list_id"],
                })

            # 2. Check for duplicate mappings in task_google_tasks_map sharing same remote_id
            cursor.execute(
                """
                SELECT m1.task_id AS orig_id, m2.task_id AS echo_id, m1.remote_id, m1.list_id
                FROM task_google_tasks_map m1
                JOIN task_google_tasks_map m2 ON m1.remote_id = m2.remote_id AND m1.task_id < m2.task_id
                WHERE NOT m1.remote_id LIKE 'local_%'
                """
            )
            dup_maps = [dict(r) for r in cursor.fetchall()]
            for dm in dup_maps:
                cursor.execute("SELECT id, source_type, source_id, title FROM tasks WHERE id = ?", (dm["echo_id"],))
                echo_task = cursor.fetchone()
                if echo_task and echo_task["source_type"] == "google_tasks":
                    cursor.execute("DELETE FROM task_google_tasks_map WHERE task_id = ?", (dm["echo_id"],))
                    cursor.execute("DELETE FROM daily_tasks WHERE task_id = ?", (dm["echo_id"],))
                    cursor.execute("DELETE FROM tasks WHERE id = ?", (dm["echo_id"],))
                    if echo_task["source_id"]:
                        cursor.execute("DELETE FROM file_items WHERE id = ?", (echo_task["source_id"],))
                    reconciled.append({
                        "original_id": dm["orig_id"],
                        "echo_id": dm["echo_id"],
                        "title": echo_task["title"],
                        "remote_id": dm["remote_id"],
                        "list_id": dm["list_id"],
                    })

            try:
                cursor.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_task_gt_map_unique_remote
                    ON task_google_tasks_map(list_id, remote_id)
                    WHERE NOT remote_id LIKE 'local_%'
                    """
                )
            except sqlite3.IntegrityError:
                pass

            conn.commit()
            return reconciled
        finally:
            if should_close:
                conn.close()
