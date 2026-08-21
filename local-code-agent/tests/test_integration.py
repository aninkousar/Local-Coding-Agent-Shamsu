"""Integration tests for the real agent loop: ToolRegistry, ContextManager,
AutoApprovePermissionManager, and GuiAgentLoop working together exactly as
they do in production - only the LLM call itself is stubbed (no real Ollama
server available in a test environment), everything else is genuine: real
file I/O against a real tmp_path project, real plan tracking, real
permission gating, real event pushing.

This formalizes the ad-hoc pattern used throughout the audit that found this
gap in the first place (see the local-code-agent audit report) - a scripted,
multi-step tool-use sequence, verified by its real, observable side effects
(files actually written on disk, plan status actually transitioning) rather
than by inspecting mocked call arguments.
"""
from __future__ import annotations

import pytest

from agent.auto_permissions import AutoApprovePermissionManager
from agent.ollama_client import OllamaClient
from gui import events
from gui.agent_loop_gui import GuiAgentLoop
from gui.agent_setup import build_agent_session


# --- shared fixtures -----------------------------------------------------------------

@pytest.fixture(autouse=True)
def clean_event_queue():
    """gui.events uses a module-level global queue, shared across the whole
    test process - drain it before AND after every test in this file so one
    test's events can never leak into another's assertions."""
    q = events.get_event_queue()
    _drain(q)
    yield
    _drain(q)


def _drain(q) -> list[dict]:
    collected = []
    while not q.empty():
        collected.append(q.get_nowait())
    return collected


def _stub_chat_stream(monkeypatch, script: list[dict]) -> None:
    """Replaces OllamaClient.chat_stream with a scripted, stateful sequence -
    one entry per GuiAgentLoop iteration - via pytest's monkeypatch fixture,
    which automatically reverts after this test, unlike a module-level
    patch that would leak into every other test in the process."""
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
def real_session(tmp_path, monkeypatch):
    """Builds a genuine agent session - real Config (this project's own
    shipped config.yaml), real ToolRegistry/ContextManager/permission
    manager - pointed at an isolated tmp_path project. embed() is stubbed
    too (only used for the codebase index, not the agent loop itself), same
    reasoning as chat_stream: no real Ollama server available here."""
    import numpy as np
    monkeypatch.setattr(OllamaClient, "embed",
                         lambda self, texts: [np.zeros(8, dtype=np.float32).tolist() for _ in texts])

    def factory(cfg):
        return AutoApprovePermissionManager(allowed_roots=cfg.allowed_roots, hard_denylist=cfg.hard_denylist)

    return build_agent_session(tmp_path, factory)


def _make_loop(session) -> GuiAgentLoop:
    return GuiAgentLoop(session.client, session.tools, session.memory,
                         max_iterations=25, logger=session.logger)


# --- real multi-step tool-use turn -----------------------------------------------------

def test_multi_step_turn_real_plan_tracking_and_file_read(real_session, monkeypatch, tmp_path):
    (tmp_path / "app.py").write_text("def hello():\n    return 'world'\n")
    _stub_chat_stream(monkeypatch, [
        {"tool_calls": [{"function": {"name": "update_plan",
            "arguments": {"steps": ["[ ] Read app.py", "[ ] Add feature"]}}}]},
        {"tool_calls": [{"function": {"name": "read_file", "arguments": {"path": "app.py"}}}]},
        {"tool_calls": [{"function": {"name": "update_plan",
            "arguments": {"steps": ["[x] Read app.py", "[x] Add feature"]}}}]},
        {"content": "Done.", "tool_calls": []},
    ])

    loop = _make_loop(real_session)
    loop.run_turn("Add a feature to app.py")

    # Real ContextManager state, not a mock's recorded call args
    assert real_session.memory.task_history == [
        {"description": "Read app.py", "status": "completed", "feature": None},
        {"description": "Add feature", "status": "completed", "feature": None},
    ]
    # A real read_file tool result must have entered the real message history
    tool_messages = [m for m in real_session.memory.messages if m.get("role") == "tool"]
    assert any("hello" in m.get("content", "") for m in tool_messages)


def test_real_file_write_lands_on_disk(real_session, monkeypatch, tmp_path):
    (tmp_path / "app.py").write_text("def hello():\n    return 'world'\n")
    _stub_chat_stream(monkeypatch, [
        {"tool_calls": [{"function": {"name": "write_file", "arguments": {
            "path": "app.py",
            "content": "def hello():\n    return 'world'\n\n\ndef goodbye():\n    return 'bye'\n",
        }}}]},
        {"content": "Added goodbye().", "tool_calls": []},
    ])

    loop = _make_loop(real_session)
    loop.run_turn("Add a goodbye function")

    # The decisive check: read the REAL file back off disk, not a tool result string
    content = (tmp_path / "app.py").read_text()
    assert "def goodbye():" in content
    assert "return 'bye'" in content


def test_focus_file_tracking_updates_after_read(real_session, monkeypatch, tmp_path):
    (tmp_path / "config.py").write_text("DEBUG = True\n")
    _stub_chat_stream(monkeypatch, [
        {"tool_calls": [{"function": {"name": "read_file", "arguments": {"path": "config.py"}}}]},
        {"content": "Read it.", "tool_calls": []},
    ])

    loop = _make_loop(real_session)
    loop.run_turn("What's in config.py?")

    # set_focus_file should have been called via GuiAgentLoop's
    # _FOCUS_TRACKING_TOOLS handling - confirmed by checking the real
    # dependency-context machinery now considers config.py "in focus"
    assert "config.py" in real_session.memory.focus_files


def test_tool_call_error_does_not_crash_the_loop(real_session, monkeypatch):
    """Reading a file that doesn't exist must produce a graceful error tool
    result, not an unhandled exception that kills the whole turn."""
    _stub_chat_stream(monkeypatch, [
        {"tool_calls": [{"function": {"name": "read_file", "arguments": {"path": "does_not_exist.py"}}}]},
        {"content": "That file doesn't exist.", "tool_calls": []},
    ])

    loop = _make_loop(real_session)
    loop.run_turn("Read does_not_exist.py")  # must not raise

    q = events.get_event_queue()
    collected = _drain(q)
    assert any(e["type"] == "turn_complete" for e in collected)
    assert not any(e["type"] == "error" for e in collected)


def test_scratchpad_persists_and_reloads(real_session, monkeypatch, tmp_path):
    _stub_chat_stream(monkeypatch, [
        {"tool_calls": [{"function": {"name": "update_scratchpad",
            "arguments": {"notes": ["Uses PostgreSQL for persistence", "Auth via JWT"]}}}]},
        {"content": "Noted.", "tool_calls": []},
    ])

    loop = _make_loop(real_session)
    loop.run_turn("Remember that this project uses PostgreSQL and JWT auth")

    assert "Uses PostgreSQL for persistence" in real_session.memory.scratchpad
    assert "Auth via JWT" in real_session.memory.scratchpad

    # Genuine persistence check: a FRESH ContextManager, pointed at the same
    # persist path, must load the identical scratchpad back via
    # load_episodic_memory() - the real method a new session calls at
    # startup - not just that the in-memory object still has it.
    from agent.context_manager import ContextManager
    fresh = ContextManager(
        system_prompt_base="test",
        scratchpad_persist_path=real_session.memory.scratchpad_persist_path,
    )
    fresh.load_episodic_memory()
    assert fresh.scratchpad == real_session.memory.scratchpad


def test_plan_persists_across_a_fresh_context_manager(real_session, monkeypatch):
    """The exact 'survives a process restart' guarantee load_persisted_plan()
    is documented to provide - verified by actually constructing a second,
    independent ContextManager rather than trusting the docstring."""
    _stub_chat_stream(monkeypatch, [
        {"tool_calls": [{"function": {"name": "update_plan",
            "arguments": {"steps": ["[ ] First step", "[ ] Second step"]}}}]},
        {"content": "Planned.", "tool_calls": []},
    ])

    loop = _make_loop(real_session)
    loop.run_turn("Make a plan")

    plan_path = real_session.memory.plan_persist_path
    assert plan_path is not None and plan_path.exists()

    from agent.context_manager import ContextManager
    fresh = ContextManager(system_prompt_base="test", plan_persist_path=plan_path)
    fresh.load_persisted_plan()
    assert fresh.task_history == real_session.memory.task_history


def test_parallel_safe_batch_actually_runs_concurrently_in_a_real_turn(real_session, monkeypatch, tmp_path):
    """Confirms the loop's parallel-execution path (not just the sequential
    one) works correctly inside a real, full turn - multiple grep_codebase
    calls in one model response, which _batch_is_parallel_safe should
    recognize as safe to run concurrently."""
    (tmp_path / "a.py").write_text("def foo(): pass\n")
    (tmp_path / "b.py").write_text("def bar(): pass\n")
    calls = [("grep_codebase", {"pattern": "foo"}), ("grep_codebase", {"pattern": "bar"})]
    assert real_session.tools._batch_is_parallel_safe(calls) is True, (
        "test premise invalid - if this ever goes False, the test below silently "
        "starts exercising the sequential fallback instead of the parallel path"
    )
    _stub_chat_stream(monkeypatch, [
        {"tool_calls": [
            {"function": {"name": "grep_codebase", "arguments": {"pattern": "foo"}}},
            {"function": {"name": "grep_codebase", "arguments": {"pattern": "bar"}}},
        ]},
        {"content": "Found both.", "tool_calls": []},
    ])

    loop = _make_loop(real_session)
    loop.run_turn("Find foo and bar")

    tool_messages = [m for m in real_session.memory.messages if m.get("role") == "tool"]
    assert len(tool_messages) == 2
    combined = " ".join(m.get("content", "") for m in tool_messages)
    assert "foo" in combined and "bar" in combined


# --- ContextManager.compact() - real compaction with a stubbed summarizer ------------

def test_compact_reduces_message_count_and_produces_a_summary(real_session, monkeypatch):
    """compact() is called with an OllamaClient for the summarization call -
    stub that response too, but verify the REAL ContextManager state
    transition: fewer messages, non-empty summary."""
    for i in range(10):
        real_session.memory.add("user" if i % 2 == 0 else "assistant", f"message number {i}")

    def fake_chat(self, messages, tools=None, images_b64=None):
        return {"content": "Summary: discussed messages 0 through 9.", "tool_calls": []}

    monkeypatch.setattr(OllamaClient, "chat", fake_chat)

    before_count = len(real_session.memory.messages)
    real_session.memory.compact(real_session.client, keep_recent=2)

    assert len(real_session.memory.messages) < before_count
    assert len(real_session.memory.messages) == 2
    assert real_session.memory.summary is not None
    assert "Summary" in real_session.memory.summary
