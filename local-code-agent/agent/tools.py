from __future__ import annotations
import fnmatch
import subprocess
from pathlib import Path
from dataclasses import dataclass
from typing import Callable, Any

from rich.console import Console

from .permissions import PermissionManager
from .diffs import make_unified_diff, render_diff, apply_edit
from .indexer import CodebaseIndex
from . import doc_reader
from .ollama_client import OllamaClient

console = Console()


@dataclass
class ToolResult:
    text: str
    image_b64: str | None = None


TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "list_directory",
            "description": "List files and folders under a path in the project (non-recursive unless recursive=true).",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path from project root. Use '.' for root."},
                    "recursive": {"type": "boolean", "description": "List recursively. Default false."},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the full contents of a text/code file in the project, with line numbers.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Relative path from project root."}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_document",
            "description": "Read a requirements document: pdf, docx, txt, md, json, yaml, csv. Returns extracted text.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Path to the document."}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_image",
            "description": "Load an image (screenshot, mockup, diagram, photo of a whiteboard, scanned page) so you can visually see it on your next turn. Use this before describing or reasoning about any image.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Path to the image file."}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_codebase",
            "description": "Semantic search over the indexed codebase. Finds relevant code by meaning, not just exact text. Use this to understand an existing codebase before editing it.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "What you're looking for, in plain language."},
                    "top_k": {"type": "integer", "description": "Number of results, default 6."},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grep_codebase",
            "description": "Exact text/pattern search across project files (like grep). Use for exact identifiers, e.g. a function name.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Substring or glob-like pattern to find."},
                    "file_glob": {"type": "string", "description": "Optional filename filter, e.g. '*.py'."},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create a new file or completely overwrite an existing file with new content. Shows a diff and requires user approval. Prefer edit_file for small changes to existing files.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path from project root."},
                    "content": {"type": "string", "description": "Full file content."},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Make a targeted edit to an existing file by replacing an exact, unique snippet of its current content with new content. Always read_file first so old_str matches exactly. Shows a diff and requires user approval.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path from project root."},
                    "old_str": {"type": "string", "description": "Exact existing text to replace. Must be unique in the file."},
                    "new_str": {"type": "string", "description": "Replacement text."},
                },
                "required": ["path", "old_str", "new_str"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "Run a shell command in the project directory (e.g. run tests, install a package, run a script). Always requires explicit user approval.",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string", "description": "The shell command to run."}},
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "reindex_codebase",
            "description": "Re-scan the project and refresh the semantic search index. Run this once at the start of a session, or after many files changed outside the agent.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]


class ToolRegistry:
    def __init__(self, project_root: Path, permissions: PermissionManager,
                 index: CodebaseIndex, index_cfg: dict):
        self.root = project_root.resolve()
        self.perm = permissions
        self.index = index
        self.index_cfg = index_cfg

    def _resolve(self, rel_path: str) -> Path:
        p = (self.root / rel_path).resolve()
        return p

    # -- dispatch -------------------------------------------------------------
    def execute(self, name: str, args: dict) -> ToolResult:
        handler = getattr(self, f"_tool_{name}", None)
        if handler is None:
            return ToolResult(text=f"Error: unknown tool '{name}'")
        try:
            return handler(**args)
        except TypeError as e:
            return ToolResult(text=f"Error: bad arguments for {name}: {e}")
        except Exception as e:  # keep the agent loop alive on tool errors
            return ToolResult(text=f"Error running {name}: {e}")

    # -- implementations --------------------------------------------------------
    def _tool_list_directory(self, path: str = ".", recursive: bool = False) -> ToolResult:
        target = self._resolve(path)
        if not self.perm.request_read(target):
            return ToolResult(text="Permission denied by user.")
        if not target.exists():
            return ToolResult(text=f"Path does not exist: {path}")
        entries = []
        it = target.rglob("*") if recursive else target.iterdir()
        for p in sorted(it):
            marker = "/" if p.is_dir() else ""
            entries.append(str(p.relative_to(self.root)) + marker)
        return ToolResult(text="\n".join(entries) or "(empty)")

    def _tool_read_file(self, path: str) -> ToolResult:
        target = self._resolve(path)
        if not self.perm.request_read(target):
            return ToolResult(text="Permission denied by user.")
        if not target.exists():
            return ToolResult(text=f"File does not exist: {path}")
        text = target.read_text(encoding="utf-8", errors="replace")
        numbered = "\n".join(f"{i+1:>5}\t{line}" for i, line in enumerate(text.splitlines()))
        return ToolResult(text=numbered)

    def _tool_read_document(self, path: str) -> ToolResult:
        target = self._resolve(path)
        if not self.perm.request_read(target):
            return ToolResult(text="Permission denied by user.")
        try:
            text = doc_reader.read_as_text(target)
        except doc_reader.UnsupportedFile as e:
            return ToolResult(text=str(e))
        return ToolResult(text=text[:20000])

    def _tool_read_image(self, path: str) -> ToolResult:
        target = self._resolve(path)
        if not self.perm.request_read(target):
            return ToolResult(text="Permission denied by user.")
        if not target.exists():
            return ToolResult(text=f"Image does not exist: {path}")
        b64 = OllamaClient.image_to_b64(target)
        return ToolResult(text=f"Loaded image {path}. It is now visible to you.", image_b64=b64)

    def _tool_search_codebase(self, query: str, top_k: int = 6) -> ToolResult:
        results = self.index.search(query, top_k=top_k)
        if not results:
            return ToolResult(text="No results. Try reindex_codebase first if this is a fresh project.")
        blocks = []
        for r in results:
            blocks.append(
                f"--- {r['path']} (lines {r['start_line']}-{r['end_line']}, score {r['score']:.2f}) ---\n{r['content']}"
            )
        return ToolResult(text="\n\n".join(blocks))

    def _tool_grep_codebase(self, pattern: str, file_glob: str = "*") -> ToolResult:
        hits = []
        for p in self.root.rglob(file_glob):
            if not p.is_file():
                continue
            if any(part in self.index_cfg.get("ignore_dirs", []) for part in p.parts):
                continue
            try:
                for i, line in enumerate(p.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
                    if pattern in line:
                        hits.append(f"{p.relative_to(self.root)}:{i}: {line.strip()}")
                        if len(hits) >= 200:
                            break
            except OSError:
                continue
            if len(hits) >= 200:
                break
        return ToolResult(text="\n".join(hits) or "No matches.")

    def _tool_write_file(self, path: str, content: str) -> ToolResult:
        target = self._resolve(path)
        old_text = target.read_text(encoding="utf-8", errors="replace") if target.exists() else ""
        diff = make_unified_diff(old_text, content, path)
        console.print(f"\n[bold]Proposed change to {path}:[/bold]")
        render_diff(diff)
        if not self.perm.request_write(target):
            return ToolResult(text="Permission denied by user. File not changed.")
        apply_edit(target, content)
        return ToolResult(text=f"Wrote {path} ({len(content.splitlines())} lines).")

    def _tool_edit_file(self, path: str, old_str: str, new_str: str) -> ToolResult:
        target = self._resolve(path)
        if not target.exists():
            return ToolResult(text=f"File does not exist: {path}. Use write_file to create it.")
        current = target.read_text(encoding="utf-8", errors="replace")
        count = current.count(old_str)
        if count == 0:
            return ToolResult(text="old_str not found in file. Re-read the file to get exact current text.")
        if count > 1:
            return ToolResult(text=f"old_str appears {count} times - it must be unique. Include more surrounding context.")
        new_content = current.replace(old_str, new_str, 1)
        diff = make_unified_diff(current, new_content, path)
        console.print(f"\n[bold]Proposed edit to {path}:[/bold]")
        render_diff(diff)
        if not self.perm.request_write(target):
            return ToolResult(text="Permission denied by user. File not changed.")
        apply_edit(target, new_content)
        return ToolResult(text=f"Edited {path}.")

    def _tool_run_command(self, command: str) -> ToolResult:
        if not self.perm.request_command(command):
            return ToolResult(text="Permission denied by user. Command not run.")
        try:
            proc = subprocess.run(
                command, shell=True, cwd=str(self.root),
                capture_output=True, text=True, timeout=120,
            )
            out = (proc.stdout or "") + (proc.stderr or "")
            return ToolResult(text=f"(exit code {proc.returncode})\n{out[-8000:]}")
        except subprocess.TimeoutExpired:
            return ToolResult(text="Command timed out after 120s.")

    def _tool_reindex_codebase(self) -> ToolResult:
        def progress(i, total, f):
            if i % 10 == 0 or i == total:
                console.print(f"[dim]Indexing {i}/{total}: {f.name}[/dim]")
        written = self.index.build(
            self.root,
            self.index_cfg.get("ignore_dirs", set()),
            self.index_cfg.get("chunk_lines", 80),
            self.index_cfg.get("chunk_overlap_lines", 10),
            self.index_cfg.get("max_file_size_kb", 512),
            progress_cb=progress,
        )
        stats = self.index.stats()
        return ToolResult(text=f"Indexed. {stats['files']} files, {stats['chunks']} chunks total ({written} new/updated).")
