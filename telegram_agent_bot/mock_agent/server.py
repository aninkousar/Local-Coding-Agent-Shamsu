"""Reference / mock implementation of the agent-side API contract this bot
expects (see the API Specification in the design brief).

IMPORTANT, stated plainly: this is NOT the user's actual local coding agent.
The real local-code-agent project (a CLI/GUI tool) does not currently expose
this REST+WebSocket API - it would need a separate adapter layer built to
speak this exact contract before this bot could control it for real. This
mock exists so the bot itself is genuinely runnable and testable end-to-end
today, simulating a project going through a realistic sequence of states
(queued -> running -> completed) with believable log/progress events, driven
by a background asyncio task per project rather than canned static data.

Run standalone: python -m mock_agent.server
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Dict, List, Set

import psutil
from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from bot.models.schemas import (
    FileContent,
    FileEntry,
    FollowUpPromptRequest,
    Project,
    ProjectStatus,
    PromptRequest,
    PromptResponse,
    SystemStatus,
    WSEvent,
    WSEventType,
)

app = FastAPI(title="Mock Local Coding Agent API")

_START_TIME = time.monotonic()

# In-memory "database" for the mock - genuinely simple on purpose, this
# component exists to exercise the bot's client code, not to be a real
# project manager itself.
_projects: Dict[str, Project] = {}
_logs: Dict[str, List[dict]] = {}
_ws_clients: Set[WebSocket] = set()
_active_tasks: Dict[str, asyncio.Task] = {}

_SIMULATED_STEPS = [
    ("Generating project structure...", 10),
    ("Creating models...", 30),
    ("Writing authentication.py...", 55),
    ("Running tests...", 75),
    ("2 tests failed, fixing tests...", 85),
    ("Tests passed. Finalizing...", 95),
    ("Completed.", 100),
]


async def _broadcast(event: WSEvent) -> None:
    dead = []
    for ws in _ws_clients:
        try:
            await ws.send_text(event.model_dump_json())
        except Exception:
            dead.append(ws)
    for ws in dead:
        _ws_clients.discard(ws)


async def _run_simulated_project(project_id: str, prompt: str) -> None:
    """Simulates the agent actually working on a project: walks through a
    believable sequence of steps, updating progress/status and emitting log
    + WS events at each one, matching the spec's example event stream."""
    project = _projects[project_id]
    project.status = ProjectStatus.RUNNING
    project.updated_at = datetime.now(timezone.utc)
    await _broadcast(WSEvent(type=WSEventType.STATUS_CHANGE, project_id=project_id,
                              message="Started", status=ProjectStatus.RUNNING))

    for message, progress in _SIMULATED_STEPS:
        await asyncio.sleep(1.5)
        project.progress = progress
        project.current_task = message
        project.updated_at = datetime.now(timezone.utc)
        _logs.setdefault(project_id, []).append({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": "info",
            "message": message,
        })
        await _broadcast(WSEvent(type=WSEventType.PROGRESS, project_id=project_id,
                                  message=message, progress=progress))

    project.status = ProjectStatus.COMPLETED
    project.current_task = None
    project.updated_at = datetime.now(timezone.utc)
    await _broadcast(WSEvent(type=WSEventType.TASK_COMPLETE, project_id=project_id,
                              message="Completed.", progress=100, status=ProjectStatus.COMPLETED))


def _new_project(prompt: str) -> Project:
    now = datetime.now(timezone.utc)
    project_id = f"PRJ-{len(_projects) + 1:03d}"
    name = prompt.strip().splitlines()[0][:60] or "Untitled project"
    project = Project(
        id=project_id, name=name, description=prompt, status=ProjectStatus.QUEUED,
        progress=0, branch="main", created_at=now, updated_at=now,
    )
    _projects[project_id] = project
    _logs[project_id] = [{
        "timestamp": now.isoformat(), "level": "success", "message": "Created project",
    }]
    return project


@app.post("/prompt")
async def post_prompt(body: PromptRequest) -> PromptResponse:
    project = _new_project(body.prompt)
    task = asyncio.create_task(_run_simulated_project(project.id, body.prompt))
    _active_tasks[project.id] = task
    return PromptResponse(
        project_id=project.id, project_name=project.name,
        status=project.status, estimated_steps=len(_SIMULATED_STEPS),
    )


@app.post("/project/{project_id}/prompt")
async def post_followup_prompt(project_id: str, body: FollowUpPromptRequest) -> PromptResponse:
    if project_id not in _projects:
        raise HTTPException(status_code=404, detail=f"No such project: {project_id}")
    project = _projects[project_id]
    _logs[project_id].append({
        "timestamp": datetime.now(timezone.utc).isoformat(), "level": "info",
        "message": f"Follow-up: {body.prompt}",
    })
    task = asyncio.create_task(_run_simulated_project(project_id, body.prompt))
    _active_tasks[project_id] = task
    return PromptResponse(
        project_id=project.id, project_name=project.name,
        status=ProjectStatus.RUNNING, estimated_steps=len(_SIMULATED_STEPS),
    )


@app.get("/projects")
async def get_projects() -> dict:
    return {"projects": [p.model_dump(mode="json") for p in _projects.values()]}


@app.get("/project/{project_id}")
async def get_project(project_id: str) -> Project:
    if project_id not in _projects:
        raise HTTPException(status_code=404, detail=f"No such project: {project_id}")
    return _projects[project_id]


@app.get("/project/{project_id}/logs")
async def get_project_logs(project_id: str, limit: int = Query(20, ge=1, le=200),
                            offset: int = Query(0, ge=0)) -> dict:
    if project_id not in _logs:
        raise HTTPException(status_code=404, detail=f"No such project: {project_id}")
    entries = list(reversed(_logs[project_id]))[offset:offset + limit]
    return {"logs": entries}


@app.get("/project/{project_id}/files")
async def get_project_files(project_id: str, path: str = "", offset: int = 0, limit: int = 100):
    if project_id not in _projects:
        raise HTTPException(status_code=404, detail=f"No such project: {project_id}")
    if path:
        # Mock file content - a believable stand-in, not a real filesystem.
        fake_lines = [f"line {i}: mock content for {path}" for i in range(1, 251)]
        selected = fake_lines[offset:offset + limit]
        return FileContent(
            path=path, size_bytes=len("\n".join(fake_lines)), last_modified=datetime.now(timezone.utc),
            total_lines=len(fake_lines), lines=selected, offset=offset,
            truncated=(offset + limit) < len(fake_lines),
        )
    entries = [
        FileEntry(path="src/", is_dir=True),
        FileEntry(path="api/", is_dir=True),
        FileEntry(path="models/", is_dir=True),
        FileEntry(path="tests/", is_dir=True),
        FileEntry(path="README.md", is_dir=False, size_bytes=1024, last_modified=datetime.now(timezone.utc)),
    ]
    return {"files": [e.model_dump(mode="json") for e in entries]}


@app.get("/system")
async def get_system() -> SystemStatus:
    running = sum(1 for p in _projects.values() if p.status == ProjectStatus.RUNNING)
    return SystemStatus(
        agent_online=True,
        cpu_percent=psutil.cpu_percent(interval=0.1),
        ram_percent=psutil.virtual_memory().percent,
        ram_used_gb=psutil.virtual_memory().used / (1024 ** 3),
        ram_total_gb=psutil.virtual_memory().total / (1024 ** 3),
        active_projects=len(_projects),
        running_tasks=running,
        queue_length=0,
        uptime_seconds=time.monotonic() - _START_TIME,
    )


@app.post("/project/{project_id}/pause")
async def pause_project(project_id: str) -> JSONResponse:
    if project_id not in _projects:
        raise HTTPException(status_code=404, detail=f"No such project: {project_id}")
    _projects[project_id].status = ProjectStatus.PAUSED
    return JSONResponse({"ok": True})


@app.post("/project/{project_id}/resume")
async def resume_project(project_id: str) -> JSONResponse:
    if project_id not in _projects:
        raise HTTPException(status_code=404, detail=f"No such project: {project_id}")
    _projects[project_id].status = ProjectStatus.RUNNING
    return JSONResponse({"ok": True})


@app.post("/project/{project_id}/stop")
async def stop_project(project_id: str) -> JSONResponse:
    if project_id not in _projects:
        raise HTTPException(status_code=404, detail=f"No such project: {project_id}")
    task = _active_tasks.get(project_id)
    if task and not task.done():
        task.cancel()
    _projects[project_id].status = ProjectStatus.FAILED
    return JSONResponse({"ok": True})


@app.websocket("/events")
async def ws_events(websocket: WebSocket) -> None:
    await websocket.accept()
    _ws_clients.add(websocket)
    try:
        while True:
            await websocket.receive_text()  # mock doesn't expect client->server messages, just keeps the connection alive
    except WebSocketDisconnect:
        pass
    finally:
        _ws_clients.discard(websocket)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
