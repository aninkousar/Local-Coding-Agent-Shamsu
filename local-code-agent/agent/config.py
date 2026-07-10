from __future__ import annotations
import os
from pathlib import Path
from dataclasses import dataclass, field
import yaml

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yaml"


@dataclass
class Config:
    raw: dict = field(default_factory=dict)
    project_root: Path = field(default_factory=lambda: Path.cwd())

    @classmethod
    def load(cls, path: Path | None = None, project_root: Path | None = None) -> "Config":
        path = path or DEFAULT_CONFIG_PATH
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        return cls(raw=raw, project_root=project_root or Path.cwd())

    # --- convenience accessors -------------------------------------------------
    @property
    def ollama_host(self) -> str:
        return self.raw.get("ollama", {}).get("host", "http://localhost:11434")

    @property
    def chat_model(self) -> str:
        return self.raw.get("ollama", {}).get("chat_model", "qwen3.5:4b")

    @property
    def embed_model(self) -> str:
        return self.raw.get("ollama", {}).get("embed_model", "nomic-embed-text")

    @property
    def context_window(self) -> int:
        return int(self.raw.get("ollama", {}).get("context_window", 8192))

    @property
    def temperature(self) -> float:
        return float(self.raw.get("ollama", {}).get("temperature", 0.3))

    @property
    def enable_thinking(self) -> bool:
        return bool(self.raw.get("ollama", {}).get("enable_thinking", False))

    @property
    def keep_alive(self):
        return self.raw.get("ollama", {}).get("keep_alive", "30m")

    @property
    def embed_batch_size(self) -> int:
        return int(self.raw.get("ollama", {}).get("embed_batch_size", 32))

    @property
    def max_tool_iterations(self) -> int:
        return int(self.raw.get("agent", {}).get("max_tool_iterations", 25))

    @property
    def history_soft_limit_tokens(self) -> int:
        return int(self.raw.get("agent", {}).get("history_soft_limit_tokens", 6000))

    @property
    def permission_mode(self) -> str:
        return self.raw.get("permissions", {}).get("mode", "ask")

    @property
    def allowed_roots(self) -> list[Path]:
        roots = self.raw.get("permissions", {}).get("allowed_roots") or []
        if not roots:
            return [self.project_root.resolve()]
        return [Path(r).resolve() for r in roots]

    @property
    def hard_denylist(self) -> list[str]:
        return self.raw.get("permissions", {}).get("hard_denylist", [])

    @property
    def index_db_path(self) -> Path:
        rel = self.raw.get("index", {}).get("db_path", ".local_agent/index.sqlite3")
        return self.project_root / rel

    @property
    def chunk_lines(self) -> int:
        return int(self.raw.get("index", {}).get("chunk_lines", 80))

    @property
    def chunk_overlap_lines(self) -> int:
        return int(self.raw.get("index", {}).get("chunk_overlap_lines", 10))

    @property
    def ignore_dirs(self) -> set[str]:
        return set(self.raw.get("index", {}).get("ignore_dirs", []))

    @property
    def max_file_size_kb(self) -> int:
        return int(self.raw.get("index", {}).get("max_file_size_kb", 512))
