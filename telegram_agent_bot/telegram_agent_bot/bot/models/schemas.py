"""Typed data models shared across the whole bot - the API client, the
database layer, and the handlers all speak these same types, so a change to
the agent's API contract only needs to change in one place.

These mirror the API Specification and Database sections of the design
exactly: project/task/log shapes, the request/response bodies for each
endpoint, and the WebSocket event envelope.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field

from utils.time_utils import utcnow


class ProjectStatus(str, Enum):
    """Mirrors the spec's status emojis: 🟢 running, 🟡 paused, 🔴 failed,
    ✅ completed. queued covers a project that's been created but hasn't
    started work yet - not in the original emoji legend, kept text-only."""
    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    FAILED = "failed"
    COMPLETED = "completed"

    @property
    def emoji(self) -> str:
        return {
            ProjectStatus.QUEUED: "⚪",
            ProjectStatus.RUNNING: "🟢",
            ProjectStatus.PAUSED: "🟡",
            ProjectStatus.FAILED: "🔴",
            ProjectStatus.COMPLETED: "✅",
        }[self]


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class LogLevel(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    SUCCESS = "success"


# --- Core entities ------------------------------------------------------

class Project(BaseModel):
    id: str
    name: str
    description: str = ""
    status: ProjectStatus
    progress: int = Field(ge=0, le=100, default=0)
    branch: str = "main"
    current_task: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    def status_line(self) -> str:
        return f"{self.status.emoji} {self.status.value.capitalize()}"


class Task(BaseModel):
    id: str
    project_id: str
    prompt: str
    status: TaskStatus
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None


class LogEntry(BaseModel):
    id: int
    project_id: str
    timestamp: datetime
    level: LogLevel
    message: str


class FileEntry(BaseModel):
    """One entry in a project's file tree - either a directory (is_dir=True,
    no size/content) or a file."""
    path: str
    is_dir: bool
    size_bytes: Optional[int] = None
    last_modified: Optional[datetime] = None


class FileContent(BaseModel):
    path: str
    size_bytes: int
    last_modified: datetime
    total_lines: int
    lines: List[str]
    offset: int = 0  # first line included, for pagination
    truncated: bool = False


class SystemStatus(BaseModel):
    agent_online: bool
    cpu_percent: float
    ram_percent: float
    ram_used_gb: float
    ram_total_gb: float
    active_projects: int
    running_tasks: int
    queue_length: int
    uptime_seconds: float


# --- Request bodies -------------------------------------------------------

class PromptRequest(BaseModel):
    """POST /prompt - starts a brand new project from a natural-language prompt."""
    prompt: str = Field(min_length=1)


class FollowUpPromptRequest(BaseModel):
    """POST /project/{id}/prompt - continues an existing project's own
    conversation context, per the spec's per-project follow-up flow."""
    project_id: str
    prompt: str = Field(min_length=1)


class PromptResponse(BaseModel):
    """What both prompt endpoints return - matches the spec's "Bot returns:
    Project ID / Project name / Status / Estimated steps"."""
    project_id: str
    project_name: str
    status: ProjectStatus
    estimated_steps: Optional[int] = None


# --- WebSocket event envelope ----------------------------------------------

class WSEventType(str, Enum):
    PROGRESS = "progress"
    STATUS_CHANGE = "status_change"
    LOG = "log"
    TASK_COMPLETE = "task_complete"
    TASK_FAILED = "task_failed"


class WSEvent(BaseModel):
    """The envelope for every message sent over WS /events. `message` is the
    short human-readable line (e.g. "Writing authentication.py..." from the
    spec's example stream) used to build the continuously-edited Telegram
    message; the rest is structured detail for anything that needs it."""
    type: WSEventType
    project_id: str
    message: str
    progress: Optional[int] = None
    status: Optional[ProjectStatus] = None
    timestamp: datetime = Field(default_factory=utcnow)
