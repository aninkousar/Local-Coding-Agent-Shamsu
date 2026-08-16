from __future__ import annotations
import platform
import subprocess
from pathlib import Path
from dataclasses import dataclass, field
import yaml

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yaml"


def _detect_available_ram_gb() -> float | None:
    """Best-effort, dependency-free detection of available system RAM in GB -
    proactive hardware awareness to complement the existing REACTIVE
    context_window fallback (which retries at half-size only after an actual
    OOM already happened). This picks a sensible starting point instead,
    without needing a new dependency like psutil, consistent with this
    project's preference for staying self-contained. Returns None if
    detection fails for any reason on any platform - callers must fall back
    to a safe hardcoded default, never let this break startup.
    """
    system = platform.system()
    try:
        if system == "Linux":
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemAvailable:"):
                        kb = int(line.split()[1])
                        return kb / (1024 * 1024)
            return None
        elif system == "Darwin":
            # sysctl reports TOTAL physical memory, not "available" the way
            # /proc/meminfo does on Linux - macOS has no equally simple single
            # figure for available memory, so this is deliberately a more
            # conservative basis (total, not free) for the tiers below.
            out = subprocess.run(["sysctl", "-n", "hw.memsize"],
                                  capture_output=True, text=True, timeout=5)
            return int(out.stdout.strip()) / (1024 ** 3)
        elif system == "Windows":
            import ctypes

            class _MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]
            stat = _MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
            return stat.ullAvailPhys / (1024 ** 3)
    except Exception:
        return None
    return None


def _suggest_context_window(available_ram_gb: float) -> int:
    """Conservative RAM-to-context_window tiers for qwen3.5:9b at Q4_K_M
    (~5-7GB for weights alone). Deliberately rough and on the cautious side -
    this is a starting point meant to avoid the worst case (picking something
    that OOMs immediately), not a precise calculation. The existing reactive
    fallback (automatic retry at half-size on an actual 500 error) remains
    the real safety net if this estimate still turns out too optimistic."""
    if available_ram_gb < 6:
        return 2048
    elif available_ram_gb < 9:
        return 4096
    elif available_ram_gb < 12:
        return 8192
    elif available_ram_gb < 20:
        return 16384
    else:
        return 32768


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
        return self.raw.get("ollama", {}).get("chat_model", "qwen3.5:9b")

    @property
    def embed_model(self) -> str:
        return self.raw.get("ollama", {}).get("embed_model", "nomic-embed-text")

    @property
    def context_window(self) -> int:
        explicit = self.raw.get("ollama", {}).get("context_window")
        if explicit is not None:
            return int(explicit)
        # Not explicitly set in config.yaml at all (distinct from being set TO
        # the fallback value) - use a hardware-aware default instead of a
        # single hardcoded number, so a fresh install doesn't need the same
        # trial-and-error tuning context_window=20000 originally needed here.
        detected_ram = _detect_available_ram_gb()
        if detected_ram is not None:
            return _suggest_context_window(detected_ram)
        return 8192  # detection failed - a conservative, safe fallback

    @property
    def context_window_source(self) -> str:
        """For startup transparency - explains WHERE context_window's value
        came from, so hardware-based auto-detection is visible, not a silent
        decision the user can't see or reason about."""
        explicit = self.raw.get("ollama", {}).get("context_window")
        if explicit is not None:
            return "explicitly set in config.yaml"
        detected_ram = _detect_available_ram_gb()
        if detected_ram is not None:
            return f"auto-detected from ~{detected_ram:.1f}GB available RAM"
        return "hardware detection failed - using a conservative fallback"

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
        return int(self.raw.get("agent", {}).get("history_soft_limit_tokens", 14000))

    @property
    def summary_max_chars(self) -> int:
        return int(self.raw.get("agent", {}).get("summary_max_chars", 1500))

    @property
    def full_detail_recent_count(self) -> int:
        return int(self.raw.get("agent", {}).get("full_detail_recent_count", 4))

    @property
    def light_compression_recent_count(self) -> int:
        return int(self.raw.get("agent", {}).get("light_compression_recent_count", 10))

    @property
    def light_compression_max_chars(self) -> int:
        return int(self.raw.get("agent", {}).get("light_compression_max_chars", 1500))

    @property
    def heavy_compression_max_chars(self) -> int:
        return int(self.raw.get("agent", {}).get("heavy_compression_max_chars", 500))

    @property
    def scratchpad_persist_path(self) -> Path:
        rel = self.raw.get("agent", {}).get("scratchpad_persist_path", ".local_agent/SCRATCHPAD.md")
        return self.project_root / rel

    @property
    def episode_log_path(self) -> Path:
        rel = self.raw.get("agent", {}).get("episode_log_path", ".local_agent/EPISODES.md")
        return self.project_root / rel

    @property
    def plan_persist_path(self) -> Path:
        rel = self.raw.get("agent", {}).get("plan_persist_path", ".local_agent/PLAN.md")
        return self.project_root / rel

    @property
    def context_overhead_budget_tokens(self) -> int:
        return int(self.raw.get("agent", {}).get("context_overhead_budget_tokens", 2000))

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

    @property
    def search_codebase_min_score(self) -> float:
        return float(self.raw.get("index", {}).get("search_codebase_min_score", 0.3))

    @property
    def file_index_max_entries(self) -> int:
        return int(self.raw.get("index", {}).get("file_index_max_entries", 100))

    @property
    def focus_files_max(self) -> int:
        return int(self.raw.get("index", {}).get("focus_files_max", 2))

    @property
    def dependency_context_max_files(self) -> int:
        return int(self.raw.get("index", {}).get("dependency_context_max_files", 4))

    @property
    def dependency_context_max_chars_per_file(self) -> int:
        return int(self.raw.get("index", {}).get("dependency_context_max_chars_per_file", 500))
