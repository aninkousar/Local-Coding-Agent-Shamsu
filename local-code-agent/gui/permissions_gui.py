from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path

from . import events


@dataclass
class GuiPermissionManager:
    """Duck-types the same interface as agent.permissions.PermissionManager, so
    ToolRegistry works identically whether it's holding this or the CLI one.
    Nothing here ever assumes consent - every method either checks a prior
    explicit grant from *this session* or blocks on a real answer from the UI.
    """
    allowed_roots: list[Path]
    hard_denylist: list[str]

    _session_allow_paths: set = field(default_factory=set)
    _session_allow_commands: set = field(default_factory=set)
    _session_allow_all_reads: bool = False
    _session_allow_all_writes: bool = False

    def _within_allowed_roots(self, path: Path) -> bool:
        rp = path.resolve()
        for root in self.allowed_roots:
            try:
                rp.relative_to(root)
                return True
            except ValueError:
                continue
        return False

    def _ask(self, kind: str, message: str, danger: str = "", diff: str | None = None) -> str:
        request_id = events.new_request_id()
        events.push_event({
            "type": "permission_request",
            "id": request_id,
            "kind": kind,
            "message": message,
            "danger": danger,
            "diff": diff,
        })
        return events.wait_for_permission(request_id)

    # -- file reads -------------------------------------------------------
    def request_read(self, path: Path) -> bool:
        path = Path(path)
        if not self._within_allowed_roots(path):
            events.push_event({"type": "blocked", "message": f"{path} is outside the allowed project directory."})
            return False
        if self._session_allow_all_reads or str(path) in self._session_allow_paths:
            return True
        choice = self._ask("read", f"Read file: {path}")
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
    def _write_pre_allowed(self, path: Path) -> bool:
        return self._session_allow_all_writes or f"write:{path}" in self._session_allow_paths

    def request_write(self, path: Path, preview: str = "") -> bool:
        path = Path(path)
        if not self._within_allowed_roots(path):
            events.push_event({"type": "blocked", "message": f"{path} is outside the allowed project directory."})
            return False
        if self._write_pre_allowed(path):
            return True
        exists = path.exists()
        verb = "Modify" if exists else "Create"
        choice = self._ask(
            "write", f"{verb} file: {path}",
            danger="This will change a file on your disk." if exists else "",
            diff=preview,
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

    def request_write_batch(self, paths: list[Path], diffs: list[str] | None = None) -> bool:
        for p in paths:
            if not self._within_allowed_roots(p):
                events.push_event({"type": "blocked", "message": f"{p} is outside the allowed project directory."})
                return False
        if self._session_allow_all_writes:
            return True
        combined_diff = "\n\n".join(diffs) if diffs else None
        choice = self._ask(
            "write_batch", f"Create/modify these {len(paths)} files",
            danger="This will write multiple files to disk.",
            diff=combined_diff,
        )
        if choice == "y":
            return True
        if choice in ("always", "session"):
            self._session_allow_all_writes = True
            return True
        return False

    # -- shell commands -------------------------------------------------------
    def request_command(self, command: str) -> bool:
        for bad in self.hard_denylist:
            if bad in command:
                events.push_event({"type": "blocked", "message": "Command matches a hard-denied pattern - blocked outright."})
                return False
        if command in self._session_allow_commands:
            return True
        choice = self._ask(
            "command", f"Run shell command:\n{command}",
            danger="Shell commands can modify or delete files, install packages, or access the network.",
        )
        if choice == "y":
            return True
        if choice in ("always", "session"):
            self._session_allow_commands.add(command)
            return True
        return False

    # -- misc one-off actions --------------------------------------------------
    def request_action(self, description: str) -> bool:
        choice = self._ask("action", description)
        return choice in ("y", "always", "session")
