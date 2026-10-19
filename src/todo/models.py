"""Data models for Todo CLI."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Optional


@dataclass
class SourceFolder:
    source_id: int
    name: str
    relative_path: str
    id: Optional[int] = None
    parent_id: Optional[int] = None
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())
    item_count: int = 0
    completed_count: int = 0

    @property
    def progress_pct(self) -> int:
        if self.item_count <= 0:
            return 0
        return int((self.completed_count / self.item_count) * 100)


@dataclass
class Source:
    name: str
    path: str
    id: Optional[int] = None
    canonical_uri: Optional[str] = None
    scan_status: str = "completed"  # 'pending', 'completed', 'failed'
    scanned_at: Optional[str] = None
    last_error: Optional[str] = None
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())
    item_count: int = 0
    completed_count: int = 0

    @property
    def source_type(self) -> str:
        if self.path.startswith("telegram://"):
            return "telegram"
        if self.path.startswith("youtube://"):
            return "youtube"
        if self.path.startswith("google_tasks://"):
            return "google_tasks"
        return "filemeta"

    @property
    def progress_pct(self) -> int:
        if self.item_count <= 0:
            return 0
        return int((self.completed_count / self.item_count) * 100)


@dataclass
class FileItem:
    source_id: int
    path: str
    title: str
    type: str = "file"
    metadata: Dict[str, Any] = field(default_factory=dict)
    id: Optional[int] = None
    folder_id: Optional[int] = None
    relative_path: Optional[str] = None
    file_size: int = 0
    extension: Optional[str] = None
    completed: bool = False
    completed_at: Optional[str] = None
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())


@dataclass
class Task:
    title: str
    description: str = ""
    source_type: str = "manual"  # 'manual', 'universal', 'filemeta'
    source_id: Optional[int] = None  # references file_items.id if filemeta
    status: str = "pending"  # 'pending', 'completed'
    id: Optional[int] = None
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())


@dataclass
class DailyTask:
    task_id: int
    date: str  # YYYY-MM-DD
    position: int = 0
    completed_at: Optional[str] = None
    id: Optional[int] = None
    task: Optional[Task] = None

    @property
    def is_completed(self) -> bool:
        return self.completed_at is not None


from enum import Enum


class PomodoroSessionPhase(str, Enum):
    WORK = "work"
    SHORT_BREAK = "short_break"
    LONG_BREAK = "long_break"


class PomodoroTimerState(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"


@dataclass
class PomodoroPreset:
    name: str
    work_duration: int = 25  # in minutes
    short_break_duration: int = 5  # in minutes
    long_break_duration: int = 15  # in minutes
    sessions_before_long_break: int = 4
    auto_advance: bool = False
    sound_enabled: bool = True
    id: Optional[int] = None
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())


@dataclass
class PomodoroStateRecord:
    preset_id: int
    phase: str  # 'work', 'short_break', 'long_break'
    state: str  # 'idle', 'running', 'paused', 'completed'
    deadline_wall_clock: Optional[str] = None  # ISO format string
    remaining_seconds_when_paused: Optional[int] = None
    completed_work_sessions: int = 0
    current_session_index: int = 1
    id: int = 1
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())
