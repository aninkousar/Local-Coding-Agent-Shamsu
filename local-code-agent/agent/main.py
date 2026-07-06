from __future__ import annotations
import sys
from pathlib import Path

from rich.console import Console
from rich.panel import Panel

from .config import Config
from .ollama_client import OllamaClient
from .permissions import PermissionManager
from .indexer import CodebaseIndex
from .tools import ToolRegistry
from .memory import ConversationMemory
from .tool_loop import AgentLoop
from .prompts import build_system_prompt

console = Console()


def startup_checks(client: OllamaClient, cfg: Config) -> bool:
    console.print("[dim]Checking local Ollama server...[/dim]")
    if not client.ping():
        console.print(Panel(
            "Can't reach Ollama at " + cfg.ollama_host + ".\n\n"
            "Start it first:\n  ollama serve\n\n"
            "(If you haven't installed Ollama yet, see README.md.)",
            title="Local model server not running", style="red",
        ))
        return False

    for model, label in [(cfg.chat_model, "chat/coding model"), (cfg.embed_model, "embedding model")]:
        if not client.has_model(model):
            console.print(Panel(
                f"Model '{model}' ({label}) is not pulled yet.\n\n"
                f"Run:\n  ollama pull {model}\n\n"
                "This only needs to happen once - after that everything runs fully offline.",
                title="Missing local model", style="red",
            ))
            return False
    console.print("[green]Ollama and required models are ready.[/green]")
    return True


def main():
    project_root = Path.cwd()
    cfg = Config.load(project_root=project_root)

    client = OllamaClient(
        host=cfg.ollama_host,
        chat_model=cfg.chat_model,
        embed_model=cfg.embed_model,
        context_window=cfg.context_window,
        temperature=cfg.temperature,
        enable_thinking=cfg.enable_thinking,
    )

    if not startup_checks(client, cfg):
        sys.exit(1)

    permissions = PermissionManager(
        allowed_roots=cfg.allowed_roots,
        hard_denylist=cfg.hard_denylist,
        mode=cfg.permission_mode,
    )
    index = CodebaseIndex(cfg.index_db_path, client)
    index_cfg = {
        "ignore_dirs": cfg.ignore_dirs,
        "chunk_lines": cfg.chunk_lines,
        "chunk_overlap_lines": cfg.chunk_overlap_lines,
        "max_file_size_kb": cfg.max_file_size_kb,
    }
    tools = ToolRegistry(project_root, permissions, index, index_cfg)

    stats = index.stats()
    console.print(Panel(
        f"Project: {project_root}\n"
        f"Model: {cfg.chat_model} (local, via Ollama)\n"
        f"Index: {stats['files']} files / {stats['chunks']} chunks\n\n"
        "Every file read, file write, and shell command will ask for your approval first.\n"
        "Type 'reindex' to (re)scan this project, 'exit' to quit.",
        title="Local Code Agent", style="cyan",
    ))

    memory = ConversationMemory(
        system_prompt=build_system_prompt(str(project_root), stats),
        soft_limit_tokens=cfg.history_soft_limit_tokens,
    )
    loop = AgentLoop(client, tools, memory, max_iterations=cfg.max_tool_iterations)

    while True:
        try:
            user_input = console.input("\n[bold cyan]you>[/bold cyan] ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]bye[/dim]")
            break

        if not user_input:
            continue
        if user_input.lower() in ("exit", "quit"):
            break
        if user_input.lower() == "reindex":
            result = tools.execute("reindex_codebase", {})
            console.print(result.text)
            continue

        console.print("\n[bold magenta]agent>[/bold magenta]")
        loop.run_turn(user_input)


if __name__ == "__main__":
    main()
