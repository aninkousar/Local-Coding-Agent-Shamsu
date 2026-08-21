from __future__ import annotations
from datetime import datetime
from pathlib import Path


class SessionLogger:
    """Writes a plain-text log of one session's activity to disk. Every other
    piece of state in this project (conversation, plan, scratchpad) lives only in
    memory and is gone the moment the process closes - this is the one thing that
    survives, specifically so there's a durable record of what happened after the
    fact, without needing to keep the terminal/GUI open or scroll back through it.

    Flushes after every write rather than buffering, so an abrupt exit (Ctrl+C,
    a crash, closing the GUI window) doesn't lose whatever was written so far.
    """

    def __init__(self, project_root: Path, log_dir: Path | None = None):
        self.log_dir = log_dir or (project_root / ".local_agent" / "sessions")
        self.log_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        self.log_path = self.log_dir / f"session_{timestamp}.log"
        self._file = open(self.log_path, "a", encoding="utf-8")

    def _write(self, text: str) -> None:
        try:
            self._file.write(text)
            self._file.flush()
        except (OSError, ValueError):
            pass  # a logging failure should never take down the actual session

    @staticmethod
    def _now() -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    @staticmethod
    def _time() -> str:
        return datetime.now().strftime("%H:%M:%S")

    def session_start(self, project_root: str, model: str) -> None:
        self._write(f"=== Session started {self._now()} ===\nProject: {project_root}\nModel: {model}\n\n")

    def user_message(self, text: str) -> None:
        self._write(f"--- User ({self._time()}) ---\n{text}\n\n")

    def assistant_message(self, text: str) -> None:
        if text:
            self._write(f"--- Agent ({self._time()}) ---\n{text}\n\n")

    def tool_call(self, name: str, args: dict) -> None:
        self._write(f"  [tool call] {name}({args})\n")

    def tool_result(self, name: str, text: str) -> None:
        shown = text if len(text) < 2000 else text[:2000] + "\n  ...(truncated in log)"
        self._write(f"  [tool result] {name} -> {shown}\n\n")

    def note(self, text: str) -> None:
        """For miscellaneous session events outside the normal chat flow (a manual
        reindex, a startup warning) - keeps the log readable as one linear record
        of everything that happened, not just the back-and-forth conversation."""
        self._write(f"  [note {self._time()}] {text}\n")

    def token_usage(self, prompt_tokens: int | None, completion_tokens: int | None) -> None:
        """Real per-call token counts from Ollama's own response - not the
        char-based estimate used elsewhere in this project for budget decisions,
        the actual number Ollama computed. Logged after every model call so
        there's a genuine, reviewable record of what things really cost, rather
        than needing to manually instrument the code to find out (which is how
        this data was checked throughout this project's own development)."""
        if prompt_tokens is None and completion_tokens is None:
            return
        parts = []
        if prompt_tokens is not None:
            parts.append(f"prompt={prompt_tokens}")
        if completion_tokens is not None:
            parts.append(f"completion={completion_tokens}")
        self._write(f"  [tokens {self._time()}] {', '.join(parts)}\n")

    def session_end(self) -> None:
        self._write(f"=== Session ended {self._now()} ===\n")
        try:
            self._file.close()
        except (OSError, ValueError):
            pass
