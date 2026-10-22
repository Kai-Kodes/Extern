"""Database management and SQLite connection abstraction."""

import sqlite3
from pathlib import Path
from typing import Optional


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

            conn.commit()
