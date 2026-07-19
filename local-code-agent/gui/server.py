from __future__ import annotations
import json
import threading
from pathlib import Path

from flask import Flask, request, Response, jsonify, send_from_directory

from agent.config import Config
from agent.ollama_client import OllamaClient
from agent.indexer import CodebaseIndex
from agent.tools import ToolRegistry
from agent.memory import ConversationMemory
from agent.prompts import build_system_prompt

from . import events
from .permissions_gui import GuiPermissionManager
from .agent_loop_gui import GuiAgentLoop

STATIC_DIR = Path(__file__).parent / "static"

app = Flask(__name__, static_folder=str(STATIC_DIR), static_url_path="")

_state: dict = {}


def init_agent(project_root: Path) -> None:
    cfg = Config.load(project_root=project_root)
    client = OllamaClient(
        host=cfg.ollama_host,
        chat_model=cfg.chat_model,
        embed_model=cfg.embed_model,
        context_window=cfg.context_window,
        temperature=cfg.temperature,
        enable_thinking=cfg.enable_thinking,
        keep_alive=cfg.keep_alive,
        embed_batch_size=cfg.embed_batch_size,
    )
    perm = GuiPermissionManager(allowed_roots=cfg.allowed_roots, hard_denylist=cfg.hard_denylist)
    index = CodebaseIndex(cfg.index_db_path, client)
    index_cfg = {
        "ignore_dirs": cfg.ignore_dirs,
        "chunk_lines": cfg.chunk_lines,
        "chunk_overlap_lines": cfg.chunk_overlap_lines,
        "max_file_size_kb": cfg.max_file_size_kb,
    }
    tools = ToolRegistry(project_root, perm, index, index_cfg)
    stats = index.stats()
    memory = ConversationMemory(
        system_prompt=build_system_prompt(str(project_root), stats),
        soft_limit_tokens=cfg.history_soft_limit_tokens,
    )
    loop = GuiAgentLoop(client, tools, memory, max_iterations=cfg.max_tool_iterations)

    _state["cfg"] = cfg
    _state["client"] = client
    _state["tools"] = tools
    _state["loop"] = loop
    _state["project_root"] = str(project_root)


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
            events.push_event({
                "type": "reindex_done",
                "files": stats["files"],
                "chunks": stats["chunks"],
            })
        except Exception as e:  # noqa: BLE001
            events.push_event({"type": "error", "message": f"Reindex failed: {e}"})
            events.push_event({"type": "reindex_done", "files": None, "chunks": None})

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
