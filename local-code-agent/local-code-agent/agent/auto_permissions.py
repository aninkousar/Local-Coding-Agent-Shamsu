"""Auto-approve permission manager for bot-driven sessions - decision 2(a)
from the adapter design: auto-approve within allowed_roots + hard_denylist,
no interactive prompt. Telegram's ALLOWED_USER_IDS whitelist becomes the
real gate (only a whitelisted human can reach the bot at all, and thus
reach this agent), not a per-action y/n like the CLI/GUI sessions use.

Deliberately NOT "approve everything" - every method still enforces the
exact same structural safety boundary agent.permissions.PermissionManager
and gui/permissions_gui.py's GuiPermissionManager do: a path outside
allowed_roots, or a command matching hard_denylist, is refused just as
firmly as it always was. What's different is skipping the INTERACTIVE
step for everything that already passes those checks, not loosening the
checks themselves.

A clean seam for the full-parity option (2b) described in the task: a
PERMISSION_REQUEST WS event plus a bot-side inline keyboard for real y/n
approval over Telegram. That's a materially bigger feature (new WSEvent
type, new bot handler, new endpoint) and wasn't built here - see the
"Permission approvals" section of local-code-agent/README.md for exactly
what's needed if it's wanted later. Nothing about this class's interface
would need to change for that - it would become a new implementation,
swapped in via the same permission_manager_factory used everywhere else.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional


@dataclass
class AutoApprovePermissionManager:
    """Duck-types the same interface as agent.permissions.PermissionManager
    and gui/permissions_gui.py's GuiPermissionManager, so ToolRegistry works
    identically no matter which one it's holding. on_action, if given, is
    called with a short human-readable description of every auto-approved
    action - the api bridge wires this to append a log entry visible via
    the bot's own /project/{id}/logs, so "no interactive prompt" doesn't
    also mean "no visibility into what happened".
    """
    allowed_roots: list[Path]
    hard_denylist: list[str]
    on_action: Optional[Callable[[str], None]] = None

    def _within_allowed_roots(self, path: Path) -> bool:
        rp = Path(path).resolve()
        for root in self.allowed_roots:
            try:
                rp.relative_to(root)
                return True
            except ValueError:
                continue
        return False

    def _log(self, description: str) -> None:
        if self.on_action:
            self.on_action(description)

    # -- file reads -------------------------------------------------------
    def request_read(self, path: Path) -> bool:
        path = Path(path)
        if not self._within_allowed_roots(path):
            self._log(f"Blocked read outside allowed roots: {path}")
            return False
        self._log(f"Read {path}")
        return True

    def request_read_batch(self, paths: list[Path]) -> bool:
        for p in paths:
            if not self._within_allowed_roots(p):
                self._log(f"Blocked batch read outside allowed roots: {p}")
                return False
        self._log(f"Read {len(paths)} files")
        return True

    # -- file writes/edits --------------------------------------------------
    def request_write(self, path: Path, preview: str = "") -> bool:
        path = Path(path)
        if not self._within_allowed_roots(path):
            self._log(f"Blocked write outside allowed roots: {path}")
            return False
        verb = "Modified" if path.exists() else "Created"
        self._log(f"{verb} {path}")
        return True

    def request_write_batch(self, paths: list[Path], diffs: list[str] | None = None) -> bool:
        for p in paths:
            if not self._within_allowed_roots(p):
                self._log(f"Blocked batch write outside allowed roots: {p}")
                return False
        self._log(f"Wrote {len(paths)} files")
        return True

    # -- shell commands -------------------------------------------------------
    def request_command(self, command: str) -> bool:
        for bad in self.hard_denylist:
            if bad in command:
                self._log(f"Blocked command matching hard denylist: {command}")
                return False
        self._log(f"Ran command: {command}")
        return True

    # -- misc one-off actions --------------------------------------------------
    def request_action(self, description: str) -> bool:
        self._log(description)
        return True

    def request_db_write(self, description: str, sql_preview: str = "", danger_warnings: list[str] | None = None) -> bool:
        self._log(f"Database write: {description}")
        return True
