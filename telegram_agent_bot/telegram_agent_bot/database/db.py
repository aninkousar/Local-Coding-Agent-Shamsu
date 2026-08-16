"""Async SQLite access layer - a local cache of project/task/log state,
kept in sync as the bot receives data from the agent's REST API and
WebSocket stream. One connection per Database instance, reused across
calls (aiosqlite connections are safe for sequential async use).
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import List, Optional

import aiosqlite

from bot.models.schemas import LogEntry, LogLevel, Project, ProjectStatus, Task, TaskStatus
from utils.time_utils import utcnow

_SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"


class Database:
    """Owns one aiosqlite connection. Call `await Database.connect(path)` to
    construct - not the bare constructor - so the schema is guaranteed to
    exist before any query runs against it."""

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    @classmethod
    async def connect(cls, path: Path) -> "Database":
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(str(path))
        conn.row_factory = aiosqlite.Row
        schema_sql = _SCHEMA_PATH.read_text(encoding="utf-8")
        await conn.executescript(schema_sql)
        await conn.commit()
        return cls(conn)

    async def close(self) -> None:
        await self._conn.close()

    # --- projects -----------------------------------------------------------

    async def upsert_project(self, project: Project) -> None:
        await self._conn.execute(
            """
            INSERT INTO projects (id, name, description, status, progress, branch, current_task, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name=excluded.name, description=excluded.description, status=excluded.status,
                progress=excluded.progress, branch=excluded.branch, current_task=excluded.current_task,
                updated_at=excluded.updated_at
            """,
            (
                project.id, project.name, project.description, project.status.value,
                project.progress, project.branch, project.current_task,
                project.created_at.isoformat(), project.updated_at.isoformat(),
            ),
        )
        await self._conn.commit()

    async def get_project(self, project_id: str) -> Optional[Project]:
        cur = await self._conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,))
        row = await cur.fetchone()
        return _row_to_project(row) if row else None

    async def list_projects(self) -> List[Project]:
        cur = await self._conn.execute("SELECT * FROM projects ORDER BY updated_at DESC")
        rows = await cur.fetchall()
        return [_row_to_project(r) for r in rows]

    # --- tasks -----------------------------------------------------------------

    async def upsert_task(self, task: Task) -> None:
        await self._conn.execute(
            """
            INSERT INTO tasks (id, project_id, prompt, status, started_at, finished_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                status=excluded.status, started_at=excluded.started_at, finished_at=excluded.finished_at
            """,
            (
                task.id, task.project_id, task.prompt, task.status.value,
                task.started_at.isoformat() if task.started_at else None,
                task.finished_at.isoformat() if task.finished_at else None,
            ),
        )
        await self._conn.commit()

    async def list_tasks(self, project_id: str) -> List[Task]:
        cur = await self._conn.execute(
            "SELECT * FROM tasks WHERE project_id = ? ORDER BY started_at DESC", (project_id,)
        )
        rows = await cur.fetchall()
        return [_row_to_task(r) for r in rows]

    # --- logs -----------------------------------------------------------------

    async def add_log(self, project_id: str, level: LogLevel, message: str,
                       timestamp: Optional[datetime] = None) -> None:
        ts = timestamp or utcnow()
        await self._conn.execute(
            "INSERT INTO logs (project_id, timestamp, level, message) VALUES (?, ?, ?, ?)",
            (project_id, ts.isoformat(), level.value, message),
        )
        await self._conn.commit()

    async def get_logs(self, project_id: str, limit: int = 20, offset: int = 0) -> List[LogEntry]:
        """Paginated, most-recent-first - matches the spec's "Paginate logs" requirement."""
        cur = await self._conn.execute(
            "SELECT * FROM logs WHERE project_id = ? ORDER BY id DESC LIMIT ? OFFSET ?",
            (project_id, limit, offset),
        )
        rows = await cur.fetchall()
        return [_row_to_log(r) for r in rows]

    async def count_logs(self, project_id: str) -> int:
        cur = await self._conn.execute("SELECT COUNT(*) AS c FROM logs WHERE project_id = ?", (project_id,))
        row = await cur.fetchone()
        return int(row["c"]) if row else 0


def _row_to_project(row: aiosqlite.Row) -> Project:
    return Project(
        id=row["id"], name=row["name"], description=row["description"],
        status=ProjectStatus(row["status"]), progress=row["progress"], branch=row["branch"],
        current_task=row["current_task"],
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


def _row_to_task(row: aiosqlite.Row) -> Task:
    return Task(
        id=row["id"], project_id=row["project_id"], prompt=row["prompt"],
        status=TaskStatus(row["status"]),
        started_at=datetime.fromisoformat(row["started_at"]) if row["started_at"] else None,
        finished_at=datetime.fromisoformat(row["finished_at"]) if row["finished_at"] else None,
    )


def _row_to_log(row: aiosqlite.Row) -> LogEntry:
    return LogEntry(
        id=row["id"], project_id=row["project_id"],
        timestamp=datetime.fromisoformat(row["timestamp"]),
        level=LogLevel(row["level"]), message=row["message"],
    )
