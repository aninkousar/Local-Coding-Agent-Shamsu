"""FastAPI adapter implementing the exact REST + WebSocket contract
telegram_agent_bot expects (bot/models/schemas.py, mirrored by
telegram_agent_bot/mock_agent/server.py), backed by the REAL local agent -
real ToolRegistry, real ContextManager, real Ollama calls - instead of
mock_agent's simulated data. Point the bot's AGENT_API_BASE_URL /
AGENT_WS_URL at this server instead of mock_agent and the bot controls the
real agent with zero changes on the bot side.

Architecture decisions made explicitly here, not guessed silently (see
local-code-agent/README.md's "Bot bridge mode" section for the fuller
writeup):

1. SINGLE-PROJECT MODE. The real agent runs one session against one fixed
   codebase per process (gui/server.py's init_agent(project_root) - there's
   no "scaffold an unrelated new project" capability). The bot's contract
   assumes N independently-addressable projects; this bridge represents the
   one running codebase as a single fixed project, id="local". A first
   POST /prompt initializes it; every subsequent call (POST /prompt again,
   or POST /project/local/prompt) is a new turn against the SAME session,
   not a new isolated codebase.

2. AUTO-APPROVE PERMISSIONS. permission_mode "ask" blocks the agent loop on
   a human y/n for every file read/write/shell command - the bot's
   WSEventType has no PERMISSION_REQUEST case and no way to answer one
   today. This bridge uses AutoApprovePermissionManager
   (agent/auto_permissions.py): auto-approved within allowed_roots/
   hard_denylist, Telegram's ALLOWED_USER_IDS whitelist is the real gate on
   who can drive the agent at all. Every auto-approved (and denied) action
   is still logged and visible via GET /project/local/logs - "no
   interactive prompt" doesn't mean "no visibility". Full parity (a real
   PERMISSION_REQUEST round-trip over Telegram) is a materially bigger
   feature, deliberately not built here - see the README section.

3. Progress is derived from the real task plan (update_plan's state), not
   simulated - percent-complete = completed steps / total steps when a plan
   exists, a coarser running/done signal when it doesn't (a single-step
   task the model never bothered to plan).

4. pause/resume/stop: no cooperative cancellation exists in the tool loop.
   stop cancels the background thread at the Python level (best-effort - an
   in-flight shell command can't be un-run) and marks the project failed.
   pause/resume set an in-memory flag checked before the *next* tool call,
   not true mid-tool suspension.
"""
from __future__ import annotations

import asyncio
import logging
import queue
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set

import psutil
from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect

from bot.models.schemas import (  # type: ignore[import]
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

from agent.auto_permissions import AutoApprovePermissionManager
from agent.context_manager import summarize_task_history_for_episode
from agent.tools import ToolResult, parse_plan_steps
from gui import events as gui_events
from gui.agent_loop_gui import GuiAgentLoop
from gui.agent_setup import AgentSession, build_agent_session

logger = logging.getLogger(__name__)

LOCAL_PROJECT_ID = "local"

app = FastAPI(title="local-code-agent API bridge")

_session: Optional[AgentSession] = None
_loop: Optional[GuiAgentLoop] = None
_project: Optional[Project] = None
_logs: List[dict] = []
_ws_clients: Set[WebSocket] = set()
_start_time = time.monotonic()
_active_thread: Optional[threading.Thread] = None
_paused = threading.Event()
_stop_requested = threading.Event()


def _wait_if_paused() -> None:
    while _paused.is_set() and not _stop_requested.is_set():
        time.sleep(0.2)


def _wrap_tool_execution_for_pause_stop(tools) -> None:
    """Wraps ToolRegistry.execute/execute_batch so pause/resume/stop have
    somewhere to actually take effect, without modifying ToolRegistry or
    GuiAgentLoop themselves - both are shared with the CLI/GUI entry
    points, and this bridge's pause/stop semantics are specific to running
    unattended behind a bot, not something those other entry points need.
    Checked before each call, not mid-tool - a shell command or file write
    already in flight runs to completion, exactly as documented.
    """
    original_execute = tools.execute
    original_execute_batch = tools.execute_batch

    def wrapped_execute(name: str, args: dict):
        _wait_if_paused()
        if _stop_requested.is_set():
            return ToolResult(text="Stopped by user request - this tool call was not run.")
        return original_execute(name, args)

    def wrapped_execute_batch(calls):
        _wait_if_paused()
        if _stop_requested.is_set():
            return [ToolResult(text="Stopped by user request - this tool call was not run.") for _ in calls]
        return original_execute_batch(calls)

    tools.execute = wrapped_execute
    tools.execute_batch = wrapped_execute_batch


def init_bridge(project_root: Path) -> None:
    """Call once, before the FastAPI app starts serving - builds the real
    agent session with auto-approve permissions instead of interactive
    ones. Mirrors gui/server.py's init_agent(), via the same shared
    build_agent_session() so both entry points build identical sessions
    apart from which permission manager they use.
    """
    global _session, _loop, _project

    def _permission_factory(cfg):
        return AutoApprovePermissionManager(
            allowed_roots=cfg.allowed_roots, hard_denylist=cfg.hard_denylist,
            on_action=_record_auto_approved_action,
        )

    _session = build_agent_session(project_root, _permission_factory)
    _loop = GuiAgentLoop(_session.client, _session.tools, _session.memory,
                          max_iterations=_session.cfg.max_tool_iterations, logger=_session.logger)
    _wrap_tool_execution_for_pause_stop(_session.tools)

    now = datetime.now(timezone.utc)
    _project = Project(
        id=LOCAL_PROJECT_ID, name=project_root.name, description="",
        status=ProjectStatus.QUEUED, progress=0, branch=_current_git_branch(project_root),
        current_task=None, created_at=now, updated_at=now,
    )
    _add_log("info", f"Bridge initialized for {project_root}")
    logger.info("API bridge initialized - project_root=%s", project_root)


def _current_git_branch(project_root: Path) -> str:
    head_file = project_root / ".git" / "HEAD"
    try:
        content = head_file.read_text(encoding="utf-8").strip()
        if content.startswith("ref: refs/heads/"):
            return content.split("refs/heads/", 1)[1]
    except OSError:
        pass
    return "main"


def _add_log(level: str, message: str) -> None:
    _logs.append({"timestamp": datetime.now(timezone.utc).isoformat(), "level": level, "message": message})


def _record_auto_approved_action(description: str) -> None:
    """The AutoApprovePermissionManager's on_action hook - every
    auto-approved (or denied) action becomes a visible log entry, so
    skipping interactive approval doesn't also mean skipping visibility
    into what the agent actually did."""
    level = "warning" if description.startswith("Blocked") else "info"
    _add_log(level, description)


def _compute_progress() -> tuple[int, Optional[str]]:
    """Derives a progress percentage and current-task description from the
    real task plan, when one exists - not simulated. Falls back to a
    coarser signal for a simple task the model never bothered to plan."""
    if _session is None:
        return 0, None
    plan = _session.tools._current_plan
    if not plan:
        return (50 if _project and _project.status == ProjectStatus.RUNNING else 0), None
    total = len(plan)
    completed = sum(1 for s in plan if s.get("status") == "completed")
    in_progress = next((s for s in plan if s.get("status") == "in_progress"), None)
    current_task = in_progress.get("description") if in_progress else None
    percent = round(100 * completed / total) if total else 0
    return percent, current_task


def _get_project_or_404() -> Project:
    if _project is None:
        raise HTTPException(status_code=404, detail=f"No such project: {LOCAL_PROJECT_ID}")
    return _project


async def _broadcast(event: WSEvent) -> None:
    dead = []
    for ws in _ws_clients:
        try:
            await ws.send_text(event.model_dump_json())
        except Exception:
            dead.append(ws)
    for ws in dead:
        _ws_clients.discard(ws)


def _run_turn_in_background(prompt: str) -> None:
    """Runs one agent turn on a background thread, exactly like
    gui/server.py's POST /api/send already does - FastAPI's async endpoints
    must never block on the synchronous agent loop."""
    global _active_thread
    _stop_requested.clear()

    def _run():
        try:
            _loop.run_turn(prompt)
        except Exception as e:  # noqa: BLE001 - last resort so a turn failure doesn't hang the bridge silently
            gui_events.push_event({"type": "error", "message": f"Unexpected error: {e}"})
            gui_events.push_event({"type": "turn_complete"})

    _active_thread = threading.Thread(target=_run, daemon=True)
    _active_thread.start()


@app.on_event("startup")
async def _start_event_pump() -> None:
    """Bridges gui.events' synchronous, thread-safe queue (which GuiAgentLoop,
    running on a background thread, pushes to) into asyncio WebSocket
    broadcasts. A short polling interval rather than a blocking get(),
    since a blocking call on the asyncio loop would stall every other
    request this server needs to handle."""
    if _session is None:
        # Not already initialized by a test/caller - follow the exact same
        # convention the CLI uses (agent/main.py's project_root = Path.cwd()):
        # run this from within the target project directory.
        init_bridge(Path.cwd())
    asyncio.create_task(_pump_events_forever())


async def _pump_events_forever() -> None:
    q = gui_events.get_event_queue()
    while True:
        try:
            event = q.get_nowait()
        except queue.Empty:
            await asyncio.sleep(0.05)
            continue
        await _handle_gui_event(event)


async def _handle_gui_event(event: dict) -> None:
    """Translates one GuiAgentLoop event dict into the bot's WSEvent
    contract, updating the single local project's state and log along the
    way. See gui/agent_loop_gui.py for the full set of event types this
    switches on.
    """
    global _project
    if _project is None:
        return
    etype = event.get("type")

    if etype == "content_delta":
        return  # streamed token deltas - too fine-grained for the bot's per-line progress stream
    if etype == "content_done":
        return

    if etype == "status":
        message = event.get("message", "")
        _add_log("info", message)
        await _broadcast(WSEvent(type=WSEventType.LOG, project_id=LOCAL_PROJECT_ID, message=message))
        return

    if etype == "error":
        message = event.get("message", "")
        _project.status = ProjectStatus.FAILED
        _project.updated_at = datetime.now(timezone.utc)
        _add_log("error", message)
        await _broadcast(WSEvent(type=WSEventType.TASK_FAILED, project_id=LOCAL_PROJECT_ID, message=message,
                                  status=ProjectStatus.FAILED))
        return

    if etype == "plan_update":
        percent, current_task = _compute_progress()
        _project.progress = percent
        _project.current_task = current_task
        _project.updated_at = datetime.now(timezone.utc)
        message = f"Plan updated ({percent}% complete)"
        _add_log("info", message)
        await _broadcast(WSEvent(type=WSEventType.PROGRESS, project_id=LOCAL_PROJECT_ID,
                                  message=message, progress=percent))
        return

    if etype == "tool_call":
        name = event.get("name", "")
        message = f"Running {name}..."
        percent, current_task = _compute_progress()
        _project.progress = percent
        _project.current_task = current_task or message
        _project.updated_at = datetime.now(timezone.utc)
        _add_log("info", message)
        await _broadcast(WSEvent(type=WSEventType.PROGRESS, project_id=LOCAL_PROJECT_ID,
                                  message=message, progress=percent))
        return

    if etype == "tool_result":
        name = event.get("name", "")
        text = event.get("text", "")
        summary = text.splitlines()[0][:150] if text else ""
        _add_log("info", f"{name}: {summary}" if summary else name)
        return

    if etype == "turn_complete":
        if _project.status != ProjectStatus.FAILED:
            _project.status = ProjectStatus.COMPLETED
            _project.current_task = None
            _project.progress = 100
            _project.updated_at = datetime.now(timezone.utc)
            _add_log("success", "Turn complete")
            await _broadcast(WSEvent(type=WSEventType.TASK_COMPLETE, project_id=LOCAL_PROJECT_ID,
                                      message="Completed.", progress=100, status=ProjectStatus.COMPLETED))
        if _session:
            _session.memory.record_episode(summarize_task_history_for_episode(_session.memory.task_history))
        return


# --- REST endpoints, matching mock_agent/server.py field-for-field --------------

def _turn_in_progress() -> bool:
    """Checks the REAL thread state, not just _project.status - status could
    lag behind (e.g. briefly still 'running' right after a fast-failing
    turn) or a caller could have mutated it some other way. This is the one
    source of truth for whether it's actually safe to start another turn:
    ContextManager and ToolRegistry are not designed for concurrent access
    (see local-code-agent's established architecture - one CPU, one model,
    concurrent calls queue rather than run in parallel), so two turns
    racing against the same session risks silent state corruption, not
    just a slower response.
    """
    return _active_thread is not None and _active_thread.is_alive()


@app.post("/prompt")
async def post_prompt(body: PromptRequest) -> PromptResponse:
    if _project is None:
        raise HTTPException(status_code=503, detail="Bridge not initialized")
    if _turn_in_progress():
        raise HTTPException(status_code=409, detail="A turn is already running - wait for it to finish, or stop it first.")
    name = body.prompt.strip().splitlines()[0][:60] or _project.name
    _project.name = name
    _project.description = body.prompt
    _project.status = ProjectStatus.RUNNING
    _project.progress = 0
    _project.updated_at = datetime.now(timezone.utc)
    _add_log("success", f"New prompt: {body.prompt}")
    await _broadcast(WSEvent(type=WSEventType.STATUS_CHANGE, project_id=LOCAL_PROJECT_ID,
                              message="Started", status=ProjectStatus.RUNNING))
    _run_turn_in_background(body.prompt)
    plan_len = len(_session.tools._current_plan) if _session else 0
    return PromptResponse(project_id=LOCAL_PROJECT_ID, project_name=_project.name,
                           status=_project.status, estimated_steps=plan_len or None)


@app.post("/project/{project_id}/prompt")
async def post_followup_prompt(project_id: str, body: FollowUpPromptRequest) -> PromptResponse:
    if project_id != LOCAL_PROJECT_ID or _project is None:
        raise HTTPException(status_code=404, detail=f"No such project: {project_id}")
    if _turn_in_progress():
        raise HTTPException(status_code=409, detail="A turn is already running - wait for it to finish, or stop it first.")
    _project.status = ProjectStatus.RUNNING
    _project.updated_at = datetime.now(timezone.utc)
    _add_log("info", f"Follow-up: {body.prompt}")
    await _broadcast(WSEvent(type=WSEventType.STATUS_CHANGE, project_id=LOCAL_PROJECT_ID,
                              message="Started", status=ProjectStatus.RUNNING))
    _run_turn_in_background(body.prompt)
    plan_len = len(_session.tools._current_plan) if _session else 0
    return PromptResponse(project_id=LOCAL_PROJECT_ID, project_name=_project.name,
                           status=_project.status, estimated_steps=plan_len or None)


@app.get("/projects")
async def get_projects() -> dict:
    return {"projects": [_project.model_dump(mode="json")] if _project else []}


@app.get("/project/{project_id}")
async def get_project(project_id: str) -> Project:
    if project_id != LOCAL_PROJECT_ID:
        raise HTTPException(status_code=404, detail=f"No such project: {project_id}")
    return _get_project_or_404()


@app.get("/project/{project_id}/logs")
async def get_project_logs(project_id: str, limit: int = Query(20, ge=1, le=200),
                            offset: int = Query(0, ge=0)) -> dict:
    if project_id != LOCAL_PROJECT_ID:
        raise HTTPException(status_code=404, detail=f"No such project: {project_id}")
    entries = list(reversed(_logs))[offset:offset + limit]
    return {"logs": entries}


@app.get("/project/{project_id}/files")
async def get_project_files(project_id: str, path: str = "", offset: int = 0, limit: int = 100):
    if project_id != LOCAL_PROJECT_ID or _session is None:
        raise HTTPException(status_code=404, detail=f"No such project: {project_id}")

    root = _session.project_root
    target = (root / path).resolve() if path else root
    try:
        target.relative_to(root.resolve())
    except ValueError:
        raise HTTPException(status_code=403, detail="Path escapes the project root")

    if path and target.is_file():
        try:
            text = target.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            raise HTTPException(status_code=404, detail=f"Could not read {path}: {e}")
        lines = text.splitlines()
        selected = lines[offset:offset + limit]
        stat = target.stat()
        return FileContent(
            path=path, size_bytes=stat.st_size,
            last_modified=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
            total_lines=len(lines), lines=selected, offset=offset,
            truncated=(offset + limit) < len(lines),
        )

    if not target.exists() or not target.is_dir():
        raise HTTPException(status_code=404, detail=f"No such path: {path}")

    ignore_dirs = _session.cfg.ignore_dirs
    entries = []
    for child in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name)):
        if child.name in ignore_dirs:
            continue
        rel_name = child.name + ("/" if child.is_dir() else "")
        if child.is_dir():
            entries.append(FileEntry(path=rel_name, is_dir=True))
        else:
            stat = child.stat()
            entries.append(FileEntry(path=rel_name, is_dir=False, size_bytes=stat.st_size,
                                      last_modified=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)))
    return {"files": [e.model_dump(mode="json") for e in entries]}


@app.get("/system")
async def get_system() -> SystemStatus:
    vm = psutil.virtual_memory()
    running = 1 if (_project and _project.status == ProjectStatus.RUNNING) else 0
    return SystemStatus(
        agent_online=_session is not None,
        cpu_percent=psutil.cpu_percent(interval=0.1),
        ram_percent=vm.percent,
        ram_used_gb=vm.used / (1024 ** 3),
        ram_total_gb=vm.total / (1024 ** 3),
        active_projects=1 if _project else 0,
        running_tasks=running,
        queue_length=0,
        uptime_seconds=time.monotonic() - _start_time,
    )


@app.post("/project/{project_id}/pause")
async def pause_project(project_id: str) -> dict:
    if project_id != LOCAL_PROJECT_ID or _project is None:
        raise HTTPException(status_code=404, detail=f"No such project: {project_id}")
    _paused.set()
    _project.status = ProjectStatus.PAUSED
    _project.updated_at = datetime.now(timezone.utc)
    _add_log("warning", "Paused (in-memory flag - checked before the next tool call, not mid-tool)")
    return {"ok": True}


@app.post("/project/{project_id}/resume")
async def resume_project(project_id: str) -> dict:
    if project_id != LOCAL_PROJECT_ID or _project is None:
        raise HTTPException(status_code=404, detail=f"No such project: {project_id}")
    _paused.clear()
    _project.status = ProjectStatus.RUNNING
    _project.updated_at = datetime.now(timezone.utc)
    _add_log("info", "Resumed")
    return {"ok": True}


@app.post("/project/{project_id}/stop")
async def stop_project(project_id: str) -> dict:
    if project_id != LOCAL_PROJECT_ID or _project is None:
        raise HTTPException(status_code=404, detail=f"No such project: {project_id}")
    _stop_requested.set()
    _paused.clear()
    _project.status = ProjectStatus.FAILED
    _project.updated_at = datetime.now(timezone.utc)
    _add_log("warning", "Stop requested (best-effort - an in-flight shell command can't be un-run)")
    return {"ok": True}


@app.websocket("/events")
async def ws_events(websocket: WebSocket) -> None:
    await websocket.accept()
    _ws_clients.add(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        _ws_clients.discard(websocket)
