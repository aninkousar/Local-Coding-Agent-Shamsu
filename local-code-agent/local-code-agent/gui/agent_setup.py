"""Shared agent bootstrapping - factored out of gui/server.py's init_agent()
so both the Flask GUI and the new FastAPI bridge (agent/api_server.py)
build a real, fully-wired agent session without duplicating this setup.

The only thing that varies between callers is WHICH permission manager to
use: the Flask GUI needs interactive y/n prompts (GuiPermissionManager),
while the FastAPI bridge auto-approves within allowed_roots/hard_denylist
instead (agent/auto_permissions.py's AutoApprovePermissionManager) - the
bot's WSEvent contract has no PERMISSION_REQUEST case today to surface an
interactive prompt through, a decision made explicitly, not silently (see
the "Permission approvals" section of local-code-agent/README.md).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from agent.config import Config
from agent.context_manager import ContextManager
from agent.indexer import CodebaseIndex, build_file_index_text
from agent.ollama_client import OllamaClient
from agent.prompts import build_system_prompt
from agent.session_log import SessionLogger
from agent.tools import ToolRegistry


class AgentSession:
    """Bundles everything one running agent session needs - the same pieces
    gui/server.py's module-level _state dict held, but as a proper object
    any caller (Flask GUI, FastAPI bridge, tests) can hold directly rather
    than relying on shared module-level state, which wouldn't be safe once
    there's more than one kind of server that might want its own session.
    """

    def __init__(self, cfg: Config, client: OllamaClient, tools: ToolRegistry,
                 memory: ContextManager, logger: SessionLogger, project_root: Path) -> None:
        self.cfg = cfg
        self.client = client
        self.tools = tools
        self.memory = memory
        self.logger = logger
        self.project_root = project_root


def build_agent_session(project_root: Path, permission_manager_factory: Callable[[Config], Any]) -> AgentSession:
    """Builds one fully-wired agent session. Identical to what
    gui/server.py's init_agent() always did, parameterized on
    permission_manager_factory - a callable taking the loaded Config and
    returning a permission manager - so callers choose interactive
    (GuiPermissionManager) or auto-approve (AutoApprovePermissionManager)
    without duplicating everything else. A factory rather than a
    pre-built instance specifically because both permission managers need
    values (allowed_roots, hard_denylist) that only exist once Config has
    already loaded, which happens inside this function - constructing the
    manager with placeholder values and mutating it afterward would work
    today (nothing here happens to call it during setup), but is a fragile
    ordering assumption not worth relying on.
    """
    cfg = Config.load(project_root=project_root)
    permission_manager = permission_manager_factory(cfg)
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
    index = CodebaseIndex(cfg.index_db_path, client)
    index_cfg = {
        "ignore_dirs": cfg.ignore_dirs,
        "chunk_lines": cfg.chunk_lines,
        "chunk_overlap_lines": cfg.chunk_overlap_lines,
        "max_file_size_kb": cfg.max_file_size_kb,
        "search_codebase_min_score": cfg.search_codebase_min_score,
    }
    tools = ToolRegistry(project_root, permission_manager, index, index_cfg)
    stats = index.stats()
    logger = SessionLogger(project_root)
    tools.session_log_path = logger.log_path
    memory = ContextManager(
        system_prompt_base=build_system_prompt(str(project_root), stats),
        soft_limit_tokens=cfg.history_soft_limit_tokens,
        scratchpad_persist_path=cfg.scratchpad_persist_path,
        episode_log_path=cfg.episode_log_path,
        plan_persist_path=cfg.plan_persist_path,
        project_root=project_root,
        dependency_forward=tools._dep_forward,
        dependency_reverse=tools._dep_reverse,
        focus_files_max=cfg.focus_files_max,
        dependency_context_max_files=cfg.dependency_context_max_files,
        dependency_context_max_chars_per_file=cfg.dependency_context_max_chars_per_file,
        context_overhead_budget_tokens=cfg.context_overhead_budget_tokens,
        summary_max_chars=cfg.summary_max_chars,
        full_detail_recent_count=cfg.full_detail_recent_count,
        light_compression_recent_count=cfg.light_compression_recent_count,
        light_compression_max_chars=cfg.light_compression_max_chars,
        heavy_compression_max_chars=cfg.heavy_compression_max_chars,
    )
    memory.load_persisted_plan()
    memory.load_episodic_memory()
    memory.set_file_index(build_file_index_text(project_root, cfg.ignore_dirs, max_entries=cfg.file_index_max_entries))
    logger.session_start(str(project_root), cfg.chat_model)
    logger.note(f"Context window: {cfg.context_window} ({cfg.context_window_source})")

    return AgentSession(cfg, client, tools, memory, logger, project_root)
