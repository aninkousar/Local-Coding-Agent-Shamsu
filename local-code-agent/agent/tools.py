from __future__ import annotations
import fnmatch
import subprocess
import threading
import webbrowser
from collections import deque
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


@dataclass
class _RunningProcess:
    proc: subprocess.Popen
    output: deque
    command: str


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
    {
        "type": "function",
        "function": {
            "name": "scaffold_files",
            "description": "Create or overwrite several files at once as a single reviewed batch - use this when setting up a new project's structure (e.g. a web app's initial HTML/CSS/JS or Flask files) instead of many separate write_file calls. Shows all diffs together and asks for one approval covering the whole batch.",
            "parameters": {
                "type": "object",
                "properties": {
                    "files": {
                        "type": "array",
                        "description": "List of files to create/overwrite.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "path": {"type": "string", "description": "Relative path from project root."},
                                "content": {"type": "string", "description": "Full file content."},
                            },
                            "required": ["path", "content"],
                        },
                    },
                },
                "required": ["files"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "start_dev_server",
            "description": "Start a long-running background process that does NOT exit on its own - a dev server, file watcher, or similar (e.g. 'flask run', 'npm start', 'python -m http.server'). Use this instead of run_command for anything that keeps running. Returns a process_id to check logs or stop it later.",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string", "description": "The shell command to run in the background."}},
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_process_output",
            "description": "Check the recent log output and running/exited status of a background process started with start_dev_server.",
            "parameters": {
                "type": "object",
                "properties": {"process_id": {"type": "string", "description": "The process_id returned by start_dev_server."}},
                "required": ["process_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "stop_process",
            "description": "Stop a background process previously started with start_dev_server.",
            "parameters": {
                "type": "object",
                "properties": {"process_id": {"type": "string", "description": "The process_id returned by start_dev_server."}},
                "required": ["process_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_in_browser",
            "description": "Open a local HTML file (or a URL, e.g. a running dev server's address) in the user's default web browser, so they can see the rendered result. Use this after building or changing a web page/UI.",
            "parameters": {
                "type": "object",
                "properties": {"path_or_url": {"type": "string", "description": "A relative file path, or a full http(s) URL."}},
                "required": ["path_or_url"],
            },
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
        self._processes: dict[str, _RunningProcess] = {}
        self._process_counter = 0

    def shutdown(self) -> None:
        """Stop any background dev servers still running when the agent exits."""
        for pid, rp in self._processes.items():
            if rp.proc.poll() is None:
                try:
                    rp.proc.terminate()
                except OSError:
                    pass

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

    def _tool_scaffold_files(self, files: list[dict]) -> ToolResult:
        if not files:
            return ToolResult(text="No files given.")
        previews = []
        for f in files:
            path = f.get("path")
            content = f.get("content", "")
            if not path:
                return ToolResult(text="Each entry needs a 'path'.")
            target = self._resolve(path)
            old_text = target.read_text(encoding="utf-8", errors="replace") if target.exists() else ""
            diff = make_unified_diff(old_text, content, path)
            previews.append((target, path, content, diff))

        console.print(f"\n[bold]Proposed project scaffold - {len(previews)} file(s):[/bold]")
        for target, path, content, diff in previews:
            console.print(f"\n[cyan]{path}[/cyan]")
            render_diff(diff)

        if not self.perm.request_write_batch([t for t, _, _, _ in previews]):
            return ToolResult(text="Permission denied by user. No files changed.")

        for target, path, content, _ in previews:
            apply_edit(target, content)
        return ToolResult(text=f"Created/updated {len(previews)} files: " +
                                ", ".join(p for _, p, _, _ in previews))

    def _tool_start_dev_server(self, command: str) -> ToolResult:
        if not self.perm.request_command(command):
            return ToolResult(text="Permission denied by user. Server not started.")
        try:
            proc = subprocess.Popen(
                command, shell=True, cwd=str(self.root),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1,
            )
        except OSError as e:
            return ToolResult(text=f"Failed to start '{command}': {e}")

        self._process_counter += 1
        process_id = f"proc{self._process_counter}"
        output: deque = deque(maxlen=300)
        self._processes[process_id] = _RunningProcess(proc=proc, output=output, command=command)

        def _reader():
            try:
                for line in proc.stdout:
                    output.append(line.rstrip())
            except (ValueError, OSError):
                pass  # pipe closed when the process is stopped/exits

        threading.Thread(target=_reader, daemon=True).start()
        return ToolResult(
            text=f"Started '{command}' in the background as process_id='{process_id}'. "
                 f"It keeps running - use check_process_output(process_id='{process_id}') to see logs, "
                 f"and stop_process(process_id='{process_id}') when done."
        )

    def _tool_check_process_output(self, process_id: str) -> ToolResult:
        rp = self._processes.get(process_id)
        if not rp:
            return ToolResult(text=f"No such process_id: {process_id}")
        status = "running" if rp.proc.poll() is None else f"exited (code {rp.proc.returncode})"
        log = "\n".join(rp.output) or "(no output yet)"
        return ToolResult(text=f"[{process_id}] '{rp.command}' - {status}\n{log}")

    def _tool_stop_process(self, process_id: str) -> ToolResult:
        rp = self._processes.get(process_id)
        if not rp:
            return ToolResult(text=f"No such process_id: {process_id}")
        if rp.proc.poll() is None:
            rp.proc.terminate()
            return ToolResult(text=f"Stopped {process_id} ('{rp.command}').")
        return ToolResult(text=f"{process_id} had already exited (code {rp.proc.returncode}).")

    def _tool_open_in_browser(self, path_or_url: str) -> ToolResult:
        if path_or_url.startswith("http://") or path_or_url.startswith("https://"):
            target_desc = path_or_url
            url = path_or_url
        else:
            target = self._resolve(path_or_url)
            if not target.exists():
                return ToolResult(text=f"File does not exist: {path_or_url}")
            target_desc = str(target.relative_to(self.root))
            url = target.as_uri()

        if not self.perm.request_action(f"Open [cyan]{target_desc}[/cyan] in your default browser?"):
            return ToolResult(text="Permission denied by user.")
        webbrowser.open(url)
        return ToolResult(text=f"Opened {target_desc} in your default browser.")
