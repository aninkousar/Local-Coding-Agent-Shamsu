from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
from rich.console import Console
from rich.prompt import Prompt

console = Console()


class PermissionDenied(Exception):
    pass


@dataclass
class PermissionManager:
    """Nothing in this agent touches disk or a shell without going through here first.

    Default posture: ask every single time. The user can widen trust interactively
    (per-path or per-session), but the agent never assumes consent on its own.
    """
    allowed_roots: list[Path]
    hard_denylist: list[str]
    mode: str = "ask"  # "ask" | "ask_once_per_session"

    _session_allow_paths: set[str] = field(default_factory=set)
    _session_allow_commands: set[str] = field(default_factory=set)
    _session_allow_all_reads: bool = False
    _session_allow_all_writes: bool = False

    # -------------------------------------------------------------------
    def _within_allowed_roots(self, path: Path) -> bool:
        rp = path.resolve()
        for root in self.allowed_roots:
            try:
                rp.relative_to(root)
                return True
            except ValueError:
                continue
        return False

    def _ask(self, question: str, danger: str = "") -> str:
        console.print(f"\n[bold yellow]Permission required[/bold yellow]")
        if danger:
            console.print(f"[red]{danger}[/red]")
        console.print(question)
        choice = Prompt.ask(
            "Allow?",
            choices=["y", "n", "always", "session"],
            default="n",
        )
        return choice

    # -- file reads -------------------------------------------------------
    def request_read(self, path: Path) -> bool:
        path = Path(path)
        if not self._within_allowed_roots(path):
            console.print(f"[red]Blocked:[/red] {path} is outside the allowed project directory.")
            return False
        if self._session_allow_all_reads or str(path) in self._session_allow_paths:
            return True
        choice = self._ask(f"Read file: [cyan]{path}[/cyan]?")
        if choice == "y":
            return True
        if choice == "always":
            self._session_allow_paths.add(str(path))
            return True
        if choice == "session":
            self._session_allow_all_reads = True
            return True
        return False

    # -- file writes/edits --------------------------------------------------
    def request_write(self, path: Path, preview: str = "") -> bool:
        path = Path(path)
        if not self._within_allowed_roots(path):
            console.print(f"[red]Blocked:[/red] {path} is outside the allowed project directory.")
            return False
        if self._write_pre_allowed(path):
            return True
        exists = path.exists()
        verb = "Modify" if exists else "Create"
        if preview:
            console.print(preview)
        choice = self._ask(
            f"{verb} file: [cyan]{path}[/cyan]?",
            danger="This will change a file on your disk." if exists else "",
        )
        if choice == "y":
            return True
        if choice == "always":
            self._session_allow_paths.add(f"write:{path}")
            return True
        if choice == "session":
            self._session_allow_all_writes = True
            return True
        return False

    def _write_pre_allowed(self, path: Path) -> bool:
        return self._session_allow_all_writes or f"write:{path}" in self._session_allow_paths

    # -- shell commands -------------------------------------------------------
    def request_command(self, command: str) -> bool:
        for bad in self.hard_denylist:
            if bad in command:
                console.print(f"[red]Blocked outright:[/red] command matches a hard-denied pattern.")
                return False
        if command in self._session_allow_commands:
            return True
        choice = self._ask(
            f"Run shell command:\n  [cyan]{command}[/cyan]",
            danger="Shell commands can modify or delete files, install packages, or access the network.",
        )
        if choice == "y":
            return True
        if choice in ("always", "session"):
            self._session_allow_commands.add(command)
            return True
        return False
