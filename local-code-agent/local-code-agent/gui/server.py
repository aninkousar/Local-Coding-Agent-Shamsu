from __future__ import annotations
import json
import threading
from pathlib import Path

from flask import Flask, request, Response, jsonify, send_from_directory

from agent.context_manager import summarize_task_history_for_episode
from agent.ollama_client import OllamaClient
from agent.tools import ToolRegistry

from . import events
from .agent_setup import build_agent_session
from .permissions_gui import GuiPermissionManager
from .agent_loop_gui import GuiAgentLoop

STATIC_DIR = Path(__file__).parent / "static"

app = Flask(__name__, static_folder=str(STATIC_DIR), static_url_path="")

_state: dict = {}


def init_agent(project_root: Path) -> None:
    session = build_agent_session(
        project_root,
        lambda cfg: GuiPermissionManager(allowed_roots=cfg.allowed_roots, hard_denylist=cfg.hard_denylist),
    )

    loop = GuiAgentLoop(session.client, session.tools, session.memory,
                         max_iterations=session.cfg.max_tool_iterations, logger=session.logger)

    _state["cfg"] = session.cfg
    _state["client"] = session.client
    _state["tools"] = session.tools
    _state["memory"] = session.memory
    _state["logger"] = session.logger
    _state["loop"] = loop
    _state["project_root"] = str(project_root)


def close_session() -> None:
    """Called by the launcher when the window closes or the process is stopped,
    so the session log gets a proper closing entry rather than just trailing off."""
    memory = _state.get("memory")
    if memory:
        memory.record_episode(summarize_task_history_for_episode(memory.task_history))
    logger = _state.get("logger")
    if logger:
        logger.session_end()


@app.get("/")
def index_page():
    return send_from_directory(STATIC_DIR, "index.html")


@app.get("/api/status")
def status():
    client: OllamaClient = _state["client"]
    tools: ToolRegistry = _state["tools"]
    ok = client.ping()
    has_chat = client.has_model(client.chat_model) if ok else False
    has_embed = client.has_model(client.embed_model) if ok else False
    return jsonify({
        "ollama_reachable": ok,
        "chat_model": client.chat_model,
        "chat_model_ready": has_chat,
        "embed_model": client.embed_model,
        "embed_model_ready": has_embed,
        "project_root": _state["project_root"],
        "stats": tools.index.stats(),
        "session_log_path": str(_state["logger"].log_path) if _state.get("logger") else None,
    })


@app.post("/api/send")
def send_message():
    data = request.get_json(force=True) or {}
    user_input = (data.get("message") or "").strip()
    if not user_input:
        return jsonify({"error": "empty message"}), 400

    def _run():
        try:
            _state["loop"].run_turn(user_input)
        except Exception as e:  # noqa: BLE001 - last resort so the UI never hangs silently
            events.push_event({"type": "error", "message": f"Unexpected error: {e}"})
            events.push_event({"type": "turn_complete"})

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"status": "started"})


@app.post("/api/permission_response")
def permission_response():
    data = request.get_json(force=True) or {}
    request_id = data.get("id")
    decision = data.get("decision", "n")
    ok = events.resolve_permission(request_id, decision)
    return jsonify({"ok": ok})


@app.post("/api/reindex")
def reindex():
    tools: ToolRegistry = _state["tools"]

    def _run():
        events.push_event({"type": "reindex_progress", "current": 0, "total": 0})
        try:
            def _progress(current: int, total: int, path):
                # throttle: first, last, and every 5th file - enough for a live progress
                # readout without flooding the SSE stream on large projects.
                if current == 1 or current == total or current % 5 == 0:
                    events.push_event({
                        "type": "reindex_progress",
                        "current": current,
                        "total": total,
                    })

            tools.index.build(
                tools.root,
                tools.index_cfg.get("ignore_dirs", set()),
                tools.index_cfg.get("chunk_lines", 80),
                tools.index_cfg.get("chunk_overlap_lines", 10),
                tools.index_cfg.get("max_file_size_kb", 512),
                progress_cb=_progress,
            )
            stats = tools.index.stats()
            _state["memory"].set_file_index(
                build_file_index_text(tools.root, tools.index_cfg.get("ignore_dirs", set()),
                                       max_entries=_state["cfg"].file_index_max_entries)
            )
            if _state.get("logger"):
                _state["logger"].note(f"Manual reindex: {stats['files']} files, {stats['chunks']} chunks.")
            events.push_event({
                "type": "reindex_done",
                "files": stats["files"],
                "chunks": stats["chunks"],
            })
        except Exception as e:  # noqa: BLE001
            events.push_event({"type": "error", "message": f"Reindex failed: {e}"})
            events.push_event({"type": "reindex_done", "files": None, "chunks": None})
            if _state.get("logger"):
                _state["logger"].note(f"Manual reindex FAILED: {e}")

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"status": "started"})


@app.get("/events")
def sse_events():
    def stream():
        q = events.get_event_queue()
        # a little hello so the browser's EventSource fires 'open' promptly
        yield "retry: 2000\n\n"
        while True:
            event = q.get()
            yield f"data: {json.dumps(event)}\n\n"

    return Response(stream(), mimetype="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    })
