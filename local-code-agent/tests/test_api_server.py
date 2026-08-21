"""Tests for agent/api_server.py - the FastAPI bridge exposing the real
agent to telegram_agent_bot over REST + WebSocket.

The bridge's module-level state (_session, _project, _logs, _active_thread,
etc.) is designed for one long-running process, not isolated test runs - the
isolate_bridge_state fixture below resets every one of them before AND after
each test, since leaking state between tests here would be worse than most
places: it's exactly the kind of thing that could make a test pass only
because a PRIOR test happened to leave convenient state behind.

Only the LLM call is stubbed throughout (no real Ollama server in a test
environment) - the real ToolRegistry, ContextManager, permission manager,
FastAPI routing, and background thread/event-pump machinery all run exactly
as they do in production.
"""
from __future__ import annotations

import time

import numpy as np
import pytest
from fastapi.testclient import TestClient

pytest.importorskip(
    "bot.models.schemas",
    reason="agent/api_server.py requires the sibling telegram_agent_bot repo on PYTHONPATH "
           "(see tests/conftest.py) - not found, skipping bridge tests.",
)

import agent.api_server as api_server
from agent.ollama_client import OllamaClient


# --- isolation and stubbing -----------------------------------------------------------

@pytest.fixture(autouse=True)
def isolate_bridge_state():
    """Resets every module-level global agent/api_server.py holds, before
    AND after each test - this module was written for a single long-running
    process, so nothing here does this automatically between test runs."""
    def _reset():
        api_server._session = None
        api_server._loop = None
        api_server._project = None
        api_server._logs.clear()
        api_server._ws_clients.clear()
        api_server._active_thread = None
        api_server._paused.clear()
        api_server._stop_requested.clear()

    _reset()
    yield
    thread = api_server._active_thread
    if thread is not None and thread.is_alive():
        thread.join(timeout=5)
    _reset()


def _stub_chat_stream(monkeypatch, script: list[dict]) -> None:
    call_index = [0]

    def fake_chat_stream(self, messages, tools=None, images_b64=None, _ctx_override=None):
        idx = min(call_index[0], len(script) - 1)
        call_index[0] += 1
        step = script[idx]
        content = step.get("content", "")
        if content:
            yield {"type": "content", "delta": content}
        yield {
            "type": "done", "content": content, "tool_calls": step.get("tool_calls", []),
            "prompt_tokens": 100, "completion_tokens": 20,
        }

    monkeypatch.setattr(OllamaClient, "chat_stream", fake_chat_stream)


@pytest.fixture
def bridge(tmp_path, monkeypatch):
    """Initializes the real bridge against an isolated tmp_path project, then
    yields a TestClient as a context manager so FastAPI's startup event
    fires (starting the real event-pump task) without re-running
    init_bridge - _session is already set by the time startup checks for it,
    matching exactly how a real deployment behaves when a caller pre-inits
    before serving."""
    monkeypatch.setattr(OllamaClient, "embed",
                         lambda self, texts: [np.zeros(8, dtype=np.float32).tolist() for _ in texts])
    # Also stub the host-resolution probe so tests don't depend on a real
    # Ollama server answering /api/tags - this project's ollama_client.py
    # probes the host before every call (see _get_working_host), which
    # would otherwise make every test here fail with "could not reach
    # Ollama" in an environment with no real server running.
    monkeypatch.setattr(OllamaClient, "_get_working_host", lambda self: self.host)
    api_server.init_bridge(tmp_path)
    with TestClient(api_server.app) as client:
        yield client, tmp_path


def _wait_until_idle(client, timeout=15):
    """Polls /project/local until it's no longer 'running' - the real
    background thread + event pump need real wall-clock time to finish a
    turn, there's no synchronous way to wait for it deterministically."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r = client.get("/project/local")
        if r.json()["status"] != "running":
            return r.json()
        time.sleep(0.2)
    raise TimeoutError("turn did not finish within the timeout")


# --- the concurrency guard: the exact real bug found and fixed earlier -----------------

def test_concurrent_prompt_rejected_with_409(bridge, monkeypatch):
    """The real bug this project actually had: two prompts fired close
    together used to both start a background thread against the same
    ContextManager/ToolRegistry. Reproducing it requires realistic timing -
    a stub with no delay finishes before a second request could ever race
    it, which is exactly how the bug went unnoticed the first time."""
    client, _ = bridge
    original = OllamaClient.chat_stream

    def delayed(self, *args, **kwargs):
        time.sleep(2)
        yield from original(self, *args, **kwargs)

    _stub_chat_stream(monkeypatch, [{"tool_calls": [{"function": {"name": "update_plan",
        "arguments": {"steps": ["[ ] Slow step"]}}}]}])
    monkeypatch.setattr(OllamaClient, "chat_stream", delayed)

    r1 = client.post("/prompt", json={"prompt": "first task"})
    assert r1.status_code == 200

    r2 = client.post("/prompt", json={"prompt": "second task"})
    assert r2.status_code == 409
    assert "already running" in r2.json()["detail"].lower()


def test_prompt_accepted_again_once_previous_turn_genuinely_finished(bridge, monkeypatch):
    client, _ = bridge
    _stub_chat_stream(monkeypatch, [{"content": "Done.", "tool_calls": []}])

    r1 = client.post("/prompt", json={"prompt": "first task"})
    assert r1.status_code == 200
    _wait_until_idle(client)

    r2 = client.post("/prompt", json={"prompt": "second task"})
    assert r2.status_code == 200


def test_followup_prompt_also_respects_the_concurrency_guard(bridge, monkeypatch):
    client, _ = bridge
    original = OllamaClient.chat_stream

    def delayed(self, *args, **kwargs):
        time.sleep(2)
        yield from original(self, *args, **kwargs)

    _stub_chat_stream(monkeypatch, [{"content": "Working...", "tool_calls": []}])
    monkeypatch.setattr(OllamaClient, "chat_stream", delayed)

    r1 = client.post("/prompt", json={"prompt": "first"})
    assert r1.status_code == 200
    r2 = client.post("/project/local/prompt", json={"project_id": "local", "prompt": "second"})
    assert r2.status_code == 409


# --- real turn execution through the bridge's REST surface ----------------------------

def test_real_turn_writes_a_real_file_and_reaches_completed(bridge, monkeypatch):
    client, project_root = bridge
    (project_root / "app.py").write_text("def hello():\n    return 'world'\n")
    _stub_chat_stream(monkeypatch, [
        {"tool_calls": [{"function": {"name": "write_file", "arguments": {
            "path": "app.py",
            "content": "def hello():\n    return 'world'\n\n\ndef goodbye():\n    return 'bye'\n",
        }}}]},
        {"content": "Added it.", "tool_calls": []},
    ])

    resp = client.post("/prompt", json={"prompt": "add goodbye"})
    assert resp.status_code == 200
    assert resp.json()["project_id"] == "local"

    final = _wait_until_idle(client)
    assert final["status"] == "completed"
    assert final["progress"] == 100

    content = (project_root / "app.py").read_text()
    assert "def goodbye():" in content


def test_logs_endpoint_reflects_real_permission_manager_activity(bridge, monkeypatch):
    client, project_root = bridge
    (project_root / "app.py").write_text("X = 1\n")
    _stub_chat_stream(monkeypatch, [
        {"tool_calls": [{"function": {"name": "read_file", "arguments": {"path": "app.py"}}}]},
        {"content": "Read it.", "tool_calls": []},
    ])
    client.post("/prompt", json={"prompt": "read app.py"})
    _wait_until_idle(client)

    logs = client.get("/project/local/logs", params={"limit": 50}).json()["logs"]
    messages = [entry["message"] for entry in logs]
    assert any("Read" in m and "app.py" in m for m in messages)


# --- pause / resume / stop against real state ------------------------------------------

def test_pause_resume_change_real_project_status(bridge, monkeypatch):
    client, _ = bridge
    original = OllamaClient.chat_stream

    def delayed(self, *args, **kwargs):
        time.sleep(1.5)
        yield from original(self, *args, **kwargs)

    _stub_chat_stream(monkeypatch, [{"content": "Working", "tool_calls": []}])
    monkeypatch.setattr(OllamaClient, "chat_stream", delayed)

    client.post("/prompt", json={"prompt": "a task"})
    time.sleep(0.2)

    r_pause = client.post("/project/local/pause")
    assert r_pause.status_code == 200
    assert client.get("/project/local").json()["status"] == "paused"

    r_resume = client.post("/project/local/resume")
    assert r_resume.status_code == 200
    assert client.get("/project/local").json()["status"] == "running"


def test_stop_interrupts_a_real_turn_in_flight(bridge, monkeypatch):
    client, _ = bridge
    original = OllamaClient.chat_stream

    def delayed(self, *args, **kwargs):
        time.sleep(1.5)
        yield from original(self, *args, **kwargs)

    _stub_chat_stream(monkeypatch, [
        {"tool_calls": [{"function": {"name": "update_plan", "arguments": {"steps": ["[ ] Step"]}}}]},
        {"tool_calls": [{"function": {"name": "grep_codebase", "arguments": {"pattern": "x"}}}]},
        {"content": "Never gets here.", "tool_calls": []},
    ])
    monkeypatch.setattr(OllamaClient, "chat_stream", delayed)

    client.post("/prompt", json={"prompt": "a task"})
    time.sleep(0.2)

    r_stop = client.post("/project/local/stop")
    assert r_stop.status_code == 200

    final = _wait_until_idle(client)
    assert final["status"] == "failed"


def test_control_endpoints_404_for_unknown_project(bridge):
    client, _ = bridge
    for action in ("pause", "resume", "stop"):
        r = client.post(f"/project/nonexistent/{action}")
        assert r.status_code == 404


# --- file browsing against real files, including the path-traversal defense -----------

def test_directory_listing_shows_real_files(bridge):
    client, project_root = bridge
    (project_root / "README.md").write_text("hello")
    (project_root / "src").mkdir()

    r = client.get("/project/local/files")
    assert r.status_code == 200
    names = [f["path"] for f in r.json()["files"]]
    assert "README.md" in names
    assert "src/" in names


def test_file_content_returns_real_lines(bridge):
    client, project_root = bridge
    (project_root / "app.py").write_text("line one\nline two\nline three\n")

    r = client.get("/project/local/files", params={"path": "app.py"})
    assert r.status_code == 200
    body = r.json()
    assert body["lines"] == ["line one", "line two", "line three"]
    assert body["total_lines"] == 3


def test_path_traversal_is_blocked(bridge):
    client, _ = bridge
    r = client.get("/project/local/files", params={"path": "../../../etc/passwd"})
    assert r.status_code == 403


def test_files_endpoint_404_for_unknown_project(bridge):
    client, _ = bridge
    r = client.get("/project/nonexistent/files")
    assert r.status_code == 404


# --- system / projects listing ---------------------------------------------------------

def test_system_status_reflects_real_psutil_data(bridge):
    client, _ = bridge
    r = client.get("/system")
    assert r.status_code == 200
    body = r.json()
    assert body["agent_online"] is True
    assert 0.0 <= body["ram_percent"] <= 100.0
    assert body["ram_total_gb"] > 0


def test_projects_list_shows_the_single_local_project(bridge):
    client, _ = bridge
    r = client.get("/projects")
    assert r.status_code == 200
    projects = r.json()["projects"]
    assert len(projects) == 1
    assert projects[0]["id"] == "local"


# --- real WebSocket broadcast ------------------------------------------------------------

def test_websocket_receives_a_real_status_change_event(bridge, monkeypatch):
    client, _ = bridge
    _stub_chat_stream(monkeypatch, [{"content": "Done.", "tool_calls": []}])

    with client.websocket_connect("/events") as ws:
        client.post("/prompt", json={"prompt": "a task"})
        event = ws.receive_json()
        assert event["type"] == "status_change"
        assert event["project_id"] == "local"
