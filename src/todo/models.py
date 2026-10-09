"""Data models for Todo CLI."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Optional


@dataclass
class Source:
    name: str
    path: str
    id: Optional[int] = None
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())
    item_count: int = 0
    completed_count: int = 0

    @property
    def source_type(self) -> str:
        if self.path.startswith("telegram://"):
            return "telegram"
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
