from __future__ import annotations
import ast
import fnmatch
import json
import re
import shutil
import subprocess
import tempfile
import threading
import urllib.parse
import webbrowser
from collections import deque
from pathlib import Path
from dataclasses import dataclass
from typing import Callable, Any

import requests
from rich.console import Console

from .permissions import PermissionManager
from .diffs import make_unified_diff, render_diff, apply_edit
from .indexer import CodebaseIndex, _STRUCTURE_MARKERS
from . import doc_reader
from . import db_tools
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


def _check_syntax(path: Path) -> tuple[bool, str | None]:
    """Best-effort, zero-dependency syntax check run right after a write/edit, so a
    broken change surfaces in the SAME turn instead of waiting for the user to
    notice and report it next message. Returns (checked, error):
    checked=False means this file type isn't verified this way (nothing to report).
    checked=True, error=None means it parsed cleanly.
    checked=True, error=<msg> means a real problem was found.
    """
    ext = path.suffix.lower()
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False, None

    if ext == ".py":
        try:
            ast.parse(text)
            return True, None
        except SyntaxError as e:
            return True, f"Python syntax error at line {e.lineno}: {e.msg}"

    if ext == ".json":
        try:
            json.loads(text)
            return True, None
        except json.JSONDecodeError as e:
            return True, f"JSON syntax error at line {e.lineno}: {e.msg}"

    if ext in (".js", ".mjs", ".cjs"):
        node = shutil.which("node")
        if not node:
            return False, None
        try:
            proc = subprocess.run([node, "--check", str(path)], capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            return False, None
        if proc.returncode != 0:
            return True, f"JS syntax error: {proc.stderr.strip()[:300]}"
        return True, None

    if ext == ".rb":
        ruby = shutil.which("ruby")
        if not ruby:
            return False, None
        try:
            proc = subprocess.run([ruby, "-c", str(path)], capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            return False, None
        if proc.returncode != 0:
            return True, f"Ruby syntax error: {(proc.stderr or proc.stdout).strip()[:300]}"
        return True, None

    if ext == ".php":
        php = shutil.which("php")
        if not php:
            return False, None
        try:
            proc = subprocess.run([php, "-l", str(path)], capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            return False, None
        if proc.returncode != 0:
            return True, f"PHP syntax error: {(proc.stdout or proc.stderr).strip()[:300]}"
        return True, None

    return False, None


def _tool_check(binary: str, cmd: list[str], cwd: Path | None = None, timeout: int = 20) -> tuple[bool, bool, str]:
    """Generic runner for external checker tools. Returns (available, passed, output):
    available=False means the binary wasn't found on PATH - callers should report
    nothing rather than a false pass/fail. Otherwise passed=(exit code 0) with the
    combined stdout+stderr, trimmed by the caller as needed.
    """
    if not shutil.which(binary):
        return False, False, ""
    try:
        proc = subprocess.run(cmd, cwd=str(cwd) if cwd else None, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        return True, False, str(e)
    output = ((proc.stdout or "") + (proc.stderr or "")).strip()
    return True, proc.returncode == 0, output


def _check_c_family(path: Path, compiler: str, project_root: Path) -> str:
    """gcc/g++ -fsyntax-only: a real, standard compiler feature that checks syntax
    AND semantics (undeclared identifiers, type mismatches) without producing an
    object file - the single tool invocation covers what pyflakes covers for Python.
    """
    available, passed, output = _tool_check(
        compiler, [compiler, "-fsyntax-only", "-I", str(project_root), str(path)], timeout=20,
    )
    if not available:
        return ""
    if passed:
        return f"Code check: OK ({compiler} -fsyntax-only found no errors)"
    return f"⚠ Code check FAILED ({compiler} -fsyntax-only):\n{output[:1500]}\nFix these before moving on."


def _check_go(path: Path, project_root: Path) -> str:
    """go vet catches both syntax errors and real semantic issues (unused imports
    are a compile ERROR in Go, not just a lint warning) in one pass."""
    available, passed, output = _tool_check("go", ["go", "vet", str(path)], cwd=project_root, timeout=30)
    if not available:
        return ""
    if passed:
        return "Code check: OK (go vet found no issues)"
    return f"⚠ Code check FAILED (go vet):\n{output[:1500]}\nFix these before moving on."


def _check_rust(path: Path, project_root: Path) -> str:
    """cargo check type-checks the whole crate without producing a binary. Only
    runs if a Cargo.toml is present - without one there's no sensible crate to
    check, and cargo would just fail with an unrelated "no such file" error."""
    if not (project_root / "Cargo.toml").exists():
        return ""
    available, passed, output = _tool_check(
        "cargo", ["cargo", "check", "--message-format=short"], cwd=project_root, timeout=60,
    )
    if not available:
        return ""
    if passed:
        return "Code check: OK (cargo check found no issues in the project)"
    lines = output.splitlines()
    relevant = [l for l in lines if path.name in l] or lines[:20]
    return "⚠ Code check FAILED (cargo check):\n" + "\n".join(relevant[:20]) + "\nFix these before moving on."


def _check_java(path: Path, project_root: Path) -> str:
    """javac actually compiles (to a throwaway temp dir), which - like gcc/go/cargo -
    catches syntax AND semantic errors in one pass. Sibling classes in the project
    are made available via -sourcepath/-cp so this doesn't misfire on same-project
    references, though anything outside the project (external jars) isn't resolvable
    this way and could produce a false failure - review before trusting a FAILED
    result blindly if the project uses external dependencies."""
    out_dir = tempfile.mkdtemp(prefix="agent_javac_")
    available, passed, output = _tool_check(
        "javac",
        ["javac", "-d", out_dir, "-cp", str(project_root), "-sourcepath", str(project_root), str(path)],
        timeout=30,
    )
    if not available:
        return ""
    if passed:
        return "Code check: OK (javac compiled with no errors)"
    return f"⚠ Code check FAILED (javac):\n{output[:1500]}\nFix these before moving on (note: unresolved external/third-party imports can cause a false failure here)."


def _check_typescript(path: Path, project_root: Path) -> str:
    """tsc --noEmit type-checks without emitting output. Only runs if a tsconfig.json
    exists - without a real TS project configured, per-file checking produces mostly
    noise about unresolvable imports rather than useful signal."""
    tsconfig = project_root / "tsconfig.json"
    if not tsconfig.exists():
        return ""
    tsc_bin = shutil.which("tsc")
    if not tsc_bin:
        local_tsc = project_root / "node_modules" / ".bin" / "tsc"
        tsc_bin = str(local_tsc) if local_tsc.exists() else None
    if not tsc_bin:
        return ""
    try:
        proc = subprocess.run([tsc_bin, "--noEmit", "-p", str(tsconfig)],
                               cwd=str(project_root), capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    output = ((proc.stdout or "") + (proc.stderr or "")).strip()
    if proc.returncode == 0:
        return "Code check: OK (tsc --noEmit found no type errors in the project)"
    lines = output.splitlines()
    relevant = [l for l in lines if path.name in l]
    shown = relevant if relevant else lines[:20]
    note = "" if relevant else " (showing the first errors found project-wide - none specifically named this file)"
    return f"⚠ Code check found type errors (tsc){note}:\n" + "\n".join(shown[:20]) + "\nFix these before moving on."


def _lint_js_eslint(path: Path) -> list[str] | None:
    """Deeper-than-syntax check for JS/TS via eslint, if it's installed - mirrors
    what pyflakes does for Python (undefined variables, unused variables), using a
    minimal inline ruleset rather than requiring the project to have its own
    eslint config. Returns None if eslint isn't available (not checked)."""
    eslint_bin = shutil.which("eslint")
    if not eslint_bin:
        return None
    try:
        proc = subprocess.run(
            [eslint_bin, "--no-eslintrc",
             "--parser-options=ecmaVersion:2022,sourceType:module",
             "--env", "es2021,node,browser",
             "--rule", '{"no-undef":"error","no-unused-vars":"warn"}',
             "--format", "json", str(path)],
            capture_output=True, text=True, timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    try:
        results = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError:
        return None
    issues = []
    for file_result in results:
        for msg in file_result.get("messages", []):
            issues.append(f"line {msg.get('line', '?')}: {msg.get('message', '')}")
    return issues


def _lint_php_phpstan(path: Path) -> list[str] | None:
    """Deeper-than-syntax check for PHP via PHPStan, if it's installed - undefined
    variables/functions, type errors, and similar real bugs beyond what `php -l`
    (syntax only) catches. Uses level 0 (PHPStan's most basic rule set) so it works
    without a project-specific configuration file. Returns None if PHPStan isn't
    available (not checked)."""
    phpstan_bin = shutil.which("phpstan")
    if not phpstan_bin:
        return None
    try:
        proc = subprocess.run(
            [phpstan_bin, "analyse", "--no-progress", "--error-format=raw", "--level=0", str(path)],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode == 0:
        return []
    issues = []
    for line in (proc.stdout or "").splitlines():
        # raw format: path:line:message
        parts = line.split(":", 2)
        if len(parts) == 3:
            issues.append(f"line {parts[1]}: {parts[2].strip()}")
    return issues if issues else (["PHPStan reported an issue but output could not be parsed - run it directly for details."] if proc.stdout else None)


def _format_code(path: Path, text: str) -> tuple[bool, str | None, str | None]:
    """Runs the standard formatter for this file type, if one is available.
    Returns (available, formatted_text, error):
    available=False - no formatter for this type, or the tool isn't installed - say nothing.
    available=True, error=None - formatted_text holds the result (may be identical to
      the input if it was already well-formatted).
    available=True, error=<msg> - the formatter is installed but failed (e.g. the file
      doesn't parse) - formatted_text is None.
    """
    ext = path.suffix.lower()

    if ext == ".py":
        try:
            import black
        except ImportError:
            return False, None, None
        try:
            formatted = black.format_str(text, mode=black.Mode())
            return True, formatted, None
        except Exception as e:
            return True, None, str(e)

    if ext in (".js", ".jsx", ".ts", ".tsx", ".css", ".scss", ".html", ".json", ".md", ".yaml", ".yml"):
        prettier = shutil.which("prettier")
        if not prettier:
            return False, None, None
        try:
            proc = subprocess.run([prettier, "--stdin-filepath", str(path)],
                                   input=text, capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.TimeoutExpired):
            return False, None, None
        if proc.returncode != 0:
            return True, None, proc.stderr.strip()[:500]
        return True, proc.stdout, None

    if ext == ".go":
        gofmt = shutil.which("gofmt")
        if not gofmt:
            return False, None, None
        try:
            proc = subprocess.run([gofmt], input=text, capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.TimeoutExpired):
            return False, None, None
        if proc.returncode != 0:
            return True, None, proc.stderr.strip()[:500]
        return True, proc.stdout, None

    if ext == ".rs":
        rustfmt = shutil.which("rustfmt")
        if not rustfmt:
            return False, None, None
        try:
            proc = subprocess.run([rustfmt, "--emit", "stdout"],
                                   input=text, capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.TimeoutExpired):
            return False, None, None
        if proc.returncode != 0:
            return True, None, proc.stderr.strip()[:500]
        return True, proc.stdout, None

    if ext in (".c", ".h", ".cpp", ".cc", ".cxx", ".hpp", ".hh"):
        clang_format = shutil.which("clang-format")
        if not clang_format:
            return False, None, None
        try:
            proc = subprocess.run([clang_format], input=text, capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.TimeoutExpired):
            return False, None, None
        if proc.returncode != 0:
            return True, None, proc.stderr.strip()[:500]
        return True, proc.stdout, None

    if ext == ".sql":
        formatted = db_tools.format_sql(text)
        if formatted is None:
            return False, None, None
        return True, formatted, None

    if ext == ".php":
        if shutil.which("php-cs-fixer"):
            cmd_template = ["php-cs-fixer", "fix", None, "--rules=@PSR12", "--quiet"]
        elif shutil.which("phpcbf"):
            # phpcbf's exit code isn't pass/fail in the usual sense (1 = "fixed
            # successfully", 2 = "partially fixed", 3 = processing error) - we don't
            # branch on it at all, just read the file back either way, same as
            # php-cs-fixer above.
            cmd_template = ["phpcbf", "--standard=PSR12", None]
        else:
            return False, None, None

        tmp_index = cmd_template.index(None)
        tmp = tempfile.NamedTemporaryFile(suffix=".php", mode="w", delete=False, encoding="utf-8")
        try:
            tmp.write(text)
            tmp.close()
            cmd = cmd_template.copy()
            cmd[tmp_index] = tmp.name
            subprocess.run(cmd, capture_output=True, text=True, timeout=20)
            formatted = Path(tmp.name).read_text(encoding="utf-8", errors="replace")
        except (OSError, subprocess.TimeoutExpired):
            return False, None, None
        finally:
            Path(tmp.name).unlink(missing_ok=True)
        return True, formatted, None

    return False, None, None


def _lint_python(text: str, filename: str) -> list[str] | None:
    """Deeper-than-syntax correctness check for Python: undefined names, unused
    imports/variables, redefinition, and similar real bugs that still PARSE fine
    (so _check_syntax alone would miss them). Uses pyflakes if it's installed -
    a well-established, low-false-positive static analyzer, not a hand-rolled
    checker that would risk crying wolf on correct code.

    This necessarily runs on the WHOLE file, not an isolated snippet - resolving
    whether a name is "undefined" requires seeing the file's imports and other
    definitions, so checking a function/class in isolation would misfire constantly.
    Running it on the whole file after every edit still means whatever function or
    class you just added gets checked in its real context, which is what matters.

    Returns None if pyflakes isn't installed (not checked, so callers should NOT
    report "OK" - that would be a false claim). Otherwise a list of "line N: ..."
    strings; empty list means checked and clean.
    """
    try:
        from pyflakes.checker import Checker
    except ImportError:
        return None
    try:
        tree = ast.parse(text)
        checker = Checker(tree, filename=filename)
    except Exception:
        # if it doesn't even parse, _check_syntax already reported that -
        # nothing useful to add here, and pyflakes internals can be fragile
        # on some malformed trees, so fail safe rather than crash the tool.
        return None
    issues = []
    for msg in checker.messages:
        try:
            rendered = msg.message % msg.message_args
        except Exception:
            rendered = str(msg)
        issues.append(f"line {msg.lineno}: {rendered}")
    return issues


def _format_check_result(path: Path, project_root: Path) -> str:
    """The single combined syntax + code-quality report appended after every
    write_file/edit_file/scaffold_files call, so the model finds out about a
    broken or suspect change in the SAME turn instead of the user catching it.
    Dispatches by extension since different languages need different tools -
    for compiled/typed languages (C/C++/Go/Rust/Java/TS) one compiler invocation
    covers both syntax and semantics at once; for Python/JS it's a two-step
    syntax-then-deeper-lint, matching how those ecosystems' tools actually work.
    """
    ext = path.suffix.lower()

    # -- compiled/typed languages: one tool call covers syntax + semantics -----
    if ext in (".c", ".h"):
        return _check_c_family(path, "gcc", project_root)
    if ext in (".cpp", ".cc", ".cxx", ".hpp", ".hh"):
        return _check_c_family(path, "g++", project_root)
    if ext == ".go":
        return _check_go(path, project_root)
    if ext == ".rs":
        return _check_rust(path, project_root)
    if ext == ".java":
        return _check_java(path, project_root)
    if ext == ".sql":
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return ""
        ok, error = db_tools.check_sql_syntax(text)
        if error:
            return (f"⚠ SQL check FAILED: {error}\nNote: this validates the script against an empty "
                     "in-memory database - a statement referencing a table/column that already exists "
                     "in your real target database (but isn't created earlier in this same file) can "
                     "show as a false 'no such table' error. Review before assuming it's wrong.")
        return "SQL check: OK (parses and runs cleanly against a fresh database)"
    if ext in (".ts", ".tsx"):
        return _check_typescript(path, project_root)

    # -- everything else: syntax check first, then an optional deeper pass -----
    checked, syntax_error = _check_syntax(path)
    if not checked:
        return ""  # this file type isn't verified this way - say nothing, claim nothing

    if syntax_error:
        return f"⚠ Syntax check FAILED: {syntax_error} - fix this before moving on."

    lines = ["Syntax check: OK"]

    if ext == ".py":
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            text = None
        issues = _lint_python(text, str(path)) if text is not None else None
        if issues is None:
            pass  # pyflakes not installed - no code-check claim either way
        elif issues:
            shown = issues[:10]
            extra = f"\n  ...and {len(issues) - 10} more" if len(issues) > 10 else ""
            lines.append(
                f"⚠ Code check found {len(issues)} potential issue(s):\n"
                + "\n".join(f"  - {i}" for i in shown) + extra
                + "\nReview these - fix real bugs, but a name that's clearly defined "
                  "dynamically, via a wildcard import, or a false positive can be noted "
                  "and left if you're confident it's not actually wrong."
            )
        else:
            lines.append("Code check: OK (no undefined names, unused imports, or similar issues found)")

    elif ext in (".js", ".mjs", ".cjs"):
        issues = _lint_js_eslint(path)
        if issues is None:
            pass  # eslint not installed - no code-check claim either way
        elif issues:
            shown = issues[:10]
            extra = f"\n  ...and {len(issues) - 10} more" if len(issues) > 10 else ""
            lines.append(
                f"⚠ Code check found {len(issues)} potential issue(s):\n"
                + "\n".join(f"  - {i}" for i in shown) + extra
                + "\nReview these before moving on."
            )
        else:
            lines.append("Code check: OK (eslint found no undefined/unused-variable issues)")

    elif ext == ".php":
        issues = _lint_php_phpstan(path)
        if issues is None:
            pass  # PHPStan not installed - no code-check claim either way
        elif issues:
            shown = issues[:10]
            extra = f"\n  ...and {len(issues) - 10} more" if len(issues) > 10 else ""
            lines.append(
                f"⚠ Code check found {len(issues)} potential issue(s):\n"
                + "\n".join(f"  - {i}" for i in shown) + extra
                + "\nReview these before moving on."
            )
        else:
            lines.append("Code check: OK (PHPStan level 0 found no issues)")

    return "\n".join(lines)


TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "update_plan",
            "description": "Record or update your step-by-step plan for the current task. Call this FIRST, before any other tool, for any request that will take more than one small action - break the work into a short list of concrete, single-purpose segments (one file, one function, one feature at a time - not broad steps like 'build the app'). Call it again (re-sending the FULL list) whenever you finish a step or the plan needs to change. Skip this entirely for simple one-step requests (answering a question, reading one file, a single small edit).",
            "parameters": {
                "type": "object",
                "properties": {
                    "steps": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "The full current plan, in order, as plain strings - always re-send every step, not just the ones that changed. Prefix EVERY step with exactly one of: '[ ] ' (not started yet), '[~] ' (working on it now), '[x] ' (finished). Example: [\"[x] Create the Product model\", \"[~] Build the home page template\", \"[ ] Add the cart route\"]",
                    },
                },
                "required": ["steps"],
            },
        },
    },
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
            "description": "Read a text/code file in the project, with line numbers. For a large file, pass start_line/end_line to read just the relevant section instead of the whole thing - cheaper and faster than reading everything.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path from project root."},
                    "start_line": {"type": "integer", "description": "Optional: first line to read (1-indexed)."},
                    "end_line": {"type": "integer", "description": "Optional: last line to read (inclusive)."},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_files",
            "description": "Read several files in one batch instead of separate read_file calls - use this when you need to look at multiple related files before making a coordinated change. One approval covers the whole batch.",
            "parameters": {
                "type": "object",
                "properties": {
                    "paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Relative paths from project root.",
                    },
                },
                "required": ["paths"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_symbols",
            "description": "Quickly list the function/class/method names and line numbers in a file, without reading the whole thing - near-instant, no search index needed. Use this to get a map of an unfamiliar or large file before deciding what to read_file in full.",
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
    {
        "type": "function",
        "function": {
            "name": "db_schema",
            "description": "List the tables and columns in a database. Use this before writing any query or migration against an unfamiliar database.",
            "parameters": {
                "type": "object",
                "properties": {
                    "db_path": {"type": "string", "description": "For sqlite: a relative file path to the .db/.sqlite file. For postgres/mysql: the NAME of an environment variable holding the connection string (never a raw connection string - credentials must be set as an env var before the agent starts)."},
                    "db_type": {"type": "string", "enum": ["sqlite", "postgres", "mysql"], "description": "Defaults to sqlite."},
                },
                "required": ["db_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "db_query",
            "description": "Run a read-only SQL query (SELECT/EXPLAIN/PRAGMA/SHOW) and see the results. Rejected if it looks like a write - use db_execute for those.",
            "parameters": {
                "type": "object",
                "properties": {
                    "db_path": {"type": "string", "description": "For sqlite: a relative file path. For postgres/mysql: an environment variable name holding the connection string."},
                    "sql": {"type": "string", "description": "The SELECT query to run."},
                    "db_type": {"type": "string", "enum": ["sqlite", "postgres", "mysql"], "description": "Defaults to sqlite."},
                },
                "required": ["db_path", "sql"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "db_execute",
            "description": "Run a write/DDL SQL statement (INSERT, UPDATE, DELETE, CREATE, ALTER, DROP) against a database. Always requires explicit user approval, shown with the exact SQL. Prefer dry_run=true first to see what would happen without actually changing anything, especially for anything destructive or unfamiliar.",
            "parameters": {
                "type": "object",
                "properties": {
                    "db_path": {"type": "string", "description": "For sqlite: a relative file path. For postgres/mysql: an environment variable name holding the connection string."},
                    "sql": {"type": "string", "description": "The write/DDL statement to run."},
                    "db_type": {"type": "string", "enum": ["sqlite", "postgres", "mysql"], "description": "Defaults to sqlite."},
                    "dry_run": {"type": "boolean", "description": "If true, runs inside a transaction and rolls back - reports what would have happened without changing anything. Defaults to false."},
                },
                "required": ["db_path", "sql"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "db_execute_file",
            "description": "Run a multi-statement .sql migration/script file against a database, as a single all-or-nothing transaction. Always requires explicit user approval. Prefer dry_run=true first for anything destructive or unfamiliar.",
            "parameters": {
                "type": "object",
                "properties": {
                    "db_path": {"type": "string", "description": "For sqlite: a relative file path. For postgres/mysql: an environment variable name holding the connection string."},
                    "sql_file": {"type": "string", "description": "Relative path to the .sql script to run."},
                    "db_type": {"type": "string", "enum": ["sqlite", "postgres", "mysql"], "description": "Defaults to sqlite."},
                    "dry_run": {"type": "boolean", "description": "If true, runs inside a transaction and rolls back. Defaults to false."},
                },
                "required": ["db_path", "sql_file"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_local_server",
            "description": "Send a real HTTP request to YOUR OWN running dev server (started with start_dev_server) to verify an endpoint actually works - not a static check, an actual runtime call. Use this after wiring frontend and backend together, to confirm they're actually connected rather than just assuming it from reading the code. Only works against localhost/127.0.0.1 - refuses any external URL, since this agent otherwise makes no network calls beyond your local Ollama server.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Must be a localhost/127.0.0.1 URL, e.g. http://localhost:5000/api/users."},
                    "method": {"type": "string", "enum": ["GET", "POST", "PUT", "DELETE", "PATCH"], "description": "Defaults to GET."},
                    "expected_status": {"type": "integer", "description": "Optional - if given, flags a mismatch if the actual status differs."},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_api_routes",
            "description": "Scan the project for backend route definitions (Flask/Express-style) and frontend fetch/axios calls, shown as two separate lists side by side. Use this when wiring a frontend to a backend, to visually spot-check that what the frontend calls actually matches what the backend defines - this does NOT auto-diff or judge matches/mismatches for you (routes with path parameters like /users/<id> won't string-match exactly), it just surfaces both lists so you can compare them yourself.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "format_file",
            "description": "Auto-format a file with the standard formatter for its language (black for Python, prettier for JS/TS/CSS/HTML/JSON/YAML/Markdown if installed, gofmt for Go, rustfmt for Rust, clang-format for C/C++). Shows a diff and requires approval like any edit. Use this instead of manually fussing over indentation/spacing - let the real tool handle style so you can focus on logic. If no formatter is available for the file type or installed on this machine, says so rather than silently doing nothing.",
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
            "name": "run_tests",
            "description": "Detect and run this project's test suite (pytest for Python, npm test for Node, go test for Go, cargo test for Rust) and report the results. This is real behavior verification, going beyond static syntax/correctness checks - use it after making a change to code that has tests, to confirm it actually still works rather than just looking right. Requires approval like any command.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Optional subdirectory to scope the test run to. Defaults to the whole project."}},
            },
        },
    },
]


def parse_plan_steps(raw_steps: list[str]) -> list[dict]:
    """Parses update_plan's flat string-array format ('[ ] ...', '[~] ...', '[x] ...')
    into the {"description", "status"} dicts the CLI panel and GUI checklist render.
    Tolerant of a missing/malformed marker - defaults to "pending" rather than
    erroring, since a small model won't always get the prefix exactly right."""
    parsed = []
    for raw in raw_steps:
        raw = (raw or "").strip()
        if raw.startswith("[x]"):
            status, desc = "completed", raw[3:].strip()
        elif raw.startswith("[~]"):
            status, desc = "in_progress", raw[3:].strip()
        elif raw.startswith("[ ]"):
            status, desc = "pending", raw[3:].strip()
        else:
            status, desc = "pending", raw
        if desc:
            parsed.append({"description": desc, "status": status})
    return parsed


class ToolRegistry:
    def __init__(self, project_root: Path, permissions: PermissionManager,
                 index: CodebaseIndex, index_cfg: dict):
        self.root = project_root.resolve()
        self.perm = permissions
        self.index = index
        self.index_cfg = index_cfg
        self._processes: dict[str, _RunningProcess] = {}
        self._process_counter = 0
        self._current_plan: list[dict] = []

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
            return ToolResult(
                text=f"You called {name} with a missing or incorrect argument ({e}). "
                     f"This just means the arguments were wrong this time - re-check what {name} "
                     f"requires and call it again with the correct arguments. This is NOT a sign "
                     f"the tool is broken, unavailable, or misconfigured - it works fine, just retry it correctly."
            )
        except Exception as e:  # keep the agent loop alive on tool errors
            return ToolResult(text=f"Error running {name}: {e}")

    # -- implementations --------------------------------------------------------
    def _tool_update_plan(self, steps: list[str] | None = None) -> ToolResult:
        if isinstance(steps, str):
            steps = [steps]  # tolerate a bare string instead of a list
        if not steps:
            return ToolResult(
                text="update_plan needs a 'steps' argument: a list of at least one string, "
                     "each prefixed with '[ ] ', '[~] ', or '[x] '. Example: "
                     "steps=[\"[ ] First thing to do\", \"[~] Second thing\"]. "
                     "Call update_plan again with that argument included - the tool itself is fine, "
                     "it just needs the argument this time."
            )
        parsed = parse_plan_steps(steps)
        if not parsed:
            return ToolResult(text="No usable steps given - a plan needs at least one non-empty step.")
        self._current_plan = parsed
        lines = []
        for i, s in enumerate(parsed, 1):
            marker = {"completed": "[x]", "in_progress": "[~]"}.get(s["status"], "[ ]")
            lines.append(f"{marker} {i}. {s['description']}")
        return ToolResult(text="\n".join(lines))

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

    def _tool_read_file(self, path: str, start_line: int | None = None, end_line: int | None = None) -> ToolResult:
        target = self._resolve(path)
        if not self.perm.request_read(target):
            return ToolResult(text="Permission denied by user.")
        if not target.exists():
            return ToolResult(text=f"File does not exist: {path}")
        lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
        total = len(lines)
        s = max(1, start_line) if start_line else 1
        e = min(total, end_line) if end_line else total
        if s > total:
            return ToolResult(text=f"start_line {s} is past the end of the file ({total} lines total).")
        numbered = "\n".join(f"{i:>5}\t{lines[i - 1]}" for i in range(s, e + 1))
        header = f"(showing lines {s}-{e} of {total} total)\n" if (s != 1 or e != total) else ""
        return ToolResult(text=header + numbered)

    def _tool_read_files(self, paths: list[str]) -> ToolResult:
        if not paths:
            return ToolResult(text="No paths given.")
        targets = [self._resolve(p) for p in paths]
        if not self.perm.request_read_batch(targets):
            return ToolResult(text="Permission denied by user.")
        blocks = []
        for p, target in zip(paths, targets):
            if not target.exists():
                blocks.append(f"--- {p} ---\n(does not exist)")
                continue
            text = target.read_text(encoding="utf-8", errors="replace")
            numbered = "\n".join(f"{i + 1:>5}\t{line}" for i, line in enumerate(text.splitlines()))
            blocks.append(f"--- {p} ---\n{numbered}")
        return ToolResult(text="\n\n".join(blocks))

    def _tool_list_symbols(self, path: str) -> ToolResult:
        target = self._resolve(path)
        if not self.perm.request_read(target):
            return ToolResult(text="Permission denied by user.")
        if not target.exists():
            return ToolResult(text=f"File does not exist: {path}")

        if target.suffix.lower() == ".py":
            try:
                source = target.read_text(encoding="utf-8", errors="ignore")
                tree = ast.parse(source)
            except SyntaxError as e:
                return ToolResult(text=f"Could not parse (syntax error at line {e.lineno}): {e.msg}")
            entries = []
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef):
                    entries.append((node.lineno, f"class {node.name}"))
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
                    entries.append((node.lineno, f"{prefix} {node.name}(...)"))
            entries.sort(key=lambda t: t[0])
            lines_out = [f"{ln}: {label}" for ln, label in entries]
            return ToolResult(text="\n".join(lines_out) or "(no functions or classes found)")

        try:
            lines = target.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            return ToolResult(text="Could not read file.")
        hits = [f"{i + 1}: {line.strip()}" for i, line in enumerate(lines) if any(p.match(line) for p in _STRUCTURE_MARKERS)]
        return ToolResult(text="\n".join(hits) or
                           "No recognizable function/class definitions found this way for this file type - try read_file instead.")

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
        if not self.perm.request_write(target, preview=diff):
            return ToolResult(text="Permission denied by user. File not changed.")
        apply_edit(target, content)
        result = f"Wrote {path} ({len(content.splitlines())} lines)."
        check_report = _format_check_result(target, self.root)
        if check_report:
            result += "\n" + check_report
        return ToolResult(text=result)

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
        if not self.perm.request_write(target, preview=diff):
            return ToolResult(text="Permission denied by user. File not changed.")
        apply_edit(target, new_content)
        result = f"Edited {path}."
        check_report = _format_check_result(target, self.root)
        if check_report:
            result += "\n" + check_report
        return ToolResult(text=result)

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

        if not self.perm.request_write_batch([t for t, _, _, _ in previews],
                                              diffs=[d for _, _, _, d in previews]):
            return ToolResult(text="Permission denied by user. No files changed.")

        for target, path, content, _ in previews:
            apply_edit(target, content)

        reports = []
        for target, path, content, _ in previews:
            check_report = _format_check_result(target, self.root)
            if check_report:
                reports.append(f"{path}:\n  " + check_report.replace("\n", "\n  "))

        result = f"Created/updated {len(previews)} files: " + ", ".join(p for _, p, _, _ in previews)
        if reports:
            result += "\n\n" + "\n\n".join(reports)
        return ToolResult(text=result)

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

    # -- database tools --------------------------------------------------------
    def _resolve_db_path(self, db_path: str, db_type: str) -> tuple[str, str | None]:
        """For sqlite, db_path is a real project-relative file - resolve and
        boundary-check it like any other file. For postgres/mysql it's just an
        env var name, not a filesystem path, so there's nothing to resolve.
        Returns (path_to_use, display_name_or_None_if_blocked)."""
        if db_type == "sqlite":
            target = self._resolve(db_path)
            return str(target), str(target.relative_to(self.root)) if self._path_in_root(target) else None
        return db_path, db_path

    def _path_in_root(self, target: Path) -> bool:
        try:
            target.resolve().relative_to(self.root.resolve())
            return True
        except ValueError:
            return False

    def _tool_db_schema(self, db_path: str, db_type: str = "sqlite") -> ToolResult:
        db_type = db_type or "sqlite"
        resolved, display = self._resolve_db_path(db_path, db_type)
        if db_type == "sqlite":
            target = Path(resolved)
            if not self._path_in_root(target):
                return ToolResult(text=f"Blocked: {db_path} is outside the allowed project directory.")
            if not target.exists():
                return ToolResult(text=f"Database file does not exist: {db_path}")
            if not self.perm.request_read(target):
                return ToolResult(text="Permission denied by user.")
        else:
            if not self.perm.request_action(f"Read the schema of the {db_type} database configured by env var '{db_path}'?"):
                return ToolResult(text="Permission denied by user.")
        try:
            return ToolResult(text=db_tools.get_schema(resolved, db_type))
        except db_tools.DBError as e:
            return ToolResult(text=str(e))
        except Exception as e:
            return ToolResult(text=f"Error reading schema: {e}")

    def _tool_db_query(self, db_path: str, sql: str, db_type: str = "sqlite") -> ToolResult:
        db_type = db_type or "sqlite"
        resolved, display = self._resolve_db_path(db_path, db_type)
        if db_type == "sqlite":
            target = Path(resolved)
            if not self._path_in_root(target):
                return ToolResult(text=f"Blocked: {db_path} is outside the allowed project directory.")
            if not target.exists():
                return ToolResult(text=f"Database file does not exist: {db_path}")
            if not self.perm.request_read(target):
                return ToolResult(text="Permission denied by user.")
        else:
            if not self.perm.request_action(f"Run this read-only query against the {db_type} database configured by env var '{db_path}'?\n{sql}"):
                return ToolResult(text="Permission denied by user.")
        try:
            return ToolResult(text=db_tools.run_query(resolved, sql, db_type))
        except db_tools.DBError as e:
            return ToolResult(text=str(e))
        except Exception as e:
            return ToolResult(text=f"Query error: {e}")

    def _tool_db_execute(self, db_path: str, sql: str, db_type: str = "sqlite", dry_run: bool = False) -> ToolResult:
        db_type = db_type or "sqlite"
        resolved, display = self._resolve_db_path(db_path, db_type)
        if db_type == "sqlite":
            target = Path(resolved)
            if not self._path_in_root(target):
                return ToolResult(text=f"Blocked: {db_path} is outside the allowed project directory.")
            label = f"{'[DRY RUN] ' if dry_run else ''}Run this SQL against {db_path}?"
        else:
            label = f"{'[DRY RUN] ' if dry_run else ''}Run this SQL against the {db_type} database configured by env var '{db_path}'?"
        warnings = db_tools.detect_dangerous_sql(sql)
        if not self.perm.request_db_write(label, sql_preview=sql, danger_warnings=warnings):
            return ToolResult(text="Permission denied by user. Database not changed.")
        try:
            return ToolResult(text=db_tools.run_execute(resolved, sql, db_type, dry_run=dry_run))
        except db_tools.DBError as e:
            return ToolResult(text=str(e))
        except Exception as e:
            return ToolResult(text=f"Execute error: {e}")

    def _tool_db_execute_file(self, db_path: str, sql_file: str, db_type: str = "sqlite", dry_run: bool = False) -> ToolResult:
        db_type = db_type or "sqlite"
        script_path = self._resolve(sql_file)
        if not self._path_in_root(script_path):
            return ToolResult(text=f"Blocked: {sql_file} is outside the allowed project directory.")
        if not script_path.exists():
            return ToolResult(text=f"SQL file does not exist: {sql_file}")
        if not self.perm.request_read(script_path):
            return ToolResult(text="Permission denied by user.")
        sql_text = script_path.read_text(encoding="utf-8", errors="replace")

        resolved, display = self._resolve_db_path(db_path, db_type)
        if db_type == "sqlite":
            target = Path(resolved)
            if not self._path_in_root(target):
                return ToolResult(text=f"Blocked: {db_path} is outside the allowed project directory.")
            label = f"{'[DRY RUN] ' if dry_run else ''}Run {sql_file} ({len(sql_text.splitlines())} lines) against {db_path}?"
        else:
            label = f"{'[DRY RUN] ' if dry_run else ''}Run {sql_file} against the {db_type} database configured by env var '{db_path}'?"
        warnings = db_tools.detect_dangerous_sql(sql_text)
        if not self.perm.request_db_write(label, sql_preview=sql_text[:2000], danger_warnings=warnings):
            return ToolResult(text="Permission denied by user. Database not changed.")
        try:
            return ToolResult(text=db_tools.run_execute_file(resolved, sql_text, db_type, dry_run=dry_run))
        except db_tools.DBError as e:
            return ToolResult(text=str(e))
        except Exception as e:
            return ToolResult(text=f"Execute error: {e}")

    # -- full-stack integration verification -------------------------------------
    _LOCAL_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1"}

    def _tool_check_local_server(self, url: str, method: str = "GET", expected_status: int | None = None) -> ToolResult:
        parsed = urllib.parse.urlparse(url)
        if parsed.hostname not in self._LOCAL_HOSTS:
            return ToolResult(
                text=f"Refused: '{parsed.hostname}' is not a local address. This tool only checks "
                     f"your own dev server on localhost/127.0.0.1 - this agent otherwise makes no "
                     f"network calls beyond your local Ollama server, and this tool doesn't change that."
            )
        if not self.perm.request_action(f"Send an HTTP {method.upper()} request to {url} (your own local dev server)?"):
            return ToolResult(text="Permission denied by user.")
        try:
            resp = requests.request(method.upper(), url, timeout=8)
        except requests.RequestException as e:
            return ToolResult(text=f"Request failed: {e}\n(is the dev server actually running? check_process_output can confirm.)")

        status_note = ""
        if expected_status is not None and resp.status_code != expected_status:
            status_note = f"\n⚠ Expected status {expected_status}, got {resp.status_code}."
        body_preview = resp.text[:1000] if resp.text else "(empty body)"
        return ToolResult(text=f"{method.upper()} {url} -> HTTP {resp.status_code}{status_note}\n\nResponse body (first 1000 chars):\n{body_preview}")

    _ROUTE_PATTERNS = [
        re.compile(r"""@\w+\.route\(\s*['"]([^'"]+)['"]"""),                              # Flask
        re.compile(r"""\b(?:app|router)\.(?:get|post|put|delete|patch)\(\s*['"]([^'"]+)['"]"""),  # Express
    ]
    _CALL_PATTERNS = [
        re.compile(r"""fetch\(\s*['"]([^'"]+)['"]"""),                                     # fetch('...')
        re.compile(r"""fetch\(\s*`([^`$]*)"""),                                            # fetch(`...${x}`) - static prefix only
        re.compile(r"""axios\.(?:get|post|put|delete|patch)\(\s*['"]([^'"]+)['"]"""),       # axios('...')
        re.compile(r"""axios\.(?:get|post|put|delete|patch)\(\s*`([^`$]*)"""),              # axios(`...${x}`)
    ]
    _SCAN_EXTS = {".py", ".js", ".ts", ".jsx", ".tsx"}

    def _tool_list_api_routes(self) -> ToolResult:
        routes: dict[str, list[str]] = {}
        calls: dict[str, list[str]] = {}
        ignore_dirs = self.index_cfg.get("ignore_dirs", set())

        for p in self.root.rglob("*"):
            if p.is_dir() or p.suffix.lower() not in self._SCAN_EXTS:
                continue
            if any(part in ignore_dirs for part in p.parts):
                continue
            try:
                text = p.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            rel = str(p.relative_to(self.root))
            for pat in self._ROUTE_PATTERNS:
                for m in pat.findall(text):
                    routes.setdefault(m, []).append(rel)
            for pat in self._CALL_PATTERNS:
                for m in pat.findall(text):
                    if m.startswith("/") or m.startswith("http://localhost") or m.startswith("http://127.0.0.1"):
                        calls.setdefault(m, []).append(rel)

        if not routes and not calls:
            return ToolResult(text="No Flask/Express-style routes or fetch/axios calls found in this project.")

        lines = ["Backend routes found:"]
        if routes:
            for path, files in sorted(routes.items()):
                lines.append(f"  {path}  (in {', '.join(sorted(set(files)))})")
        else:
            lines.append("  (none found)")

        lines.append("\nFrontend calls found:")
        if calls:
            for path, files in sorted(calls.items()):
                lines.append(f"  {path}  (in {', '.join(sorted(set(files)))})")
        else:
            lines.append("  (none found)")

        lines.append(
            "\nNote: these lists are NOT auto-compared - routes with path parameters "
            "(e.g. /users/<id> vs a frontend call to /users/42) won't string-match exactly "
            "even when they're actually the same endpoint. Review both lists yourself."
        )
        return ToolResult(text="\n".join(lines))

    # -- formatting and test running --------------------------------------------------
    def _tool_format_file(self, path: str) -> ToolResult:
        target = self._resolve(path)
        if not target.exists():
            return ToolResult(text=f"File does not exist: {path}")
        if not self.perm.request_read(target):
            return ToolResult(text="Permission denied by user.")

        original = target.read_text(encoding="utf-8", errors="replace")
        available, formatted, error = _format_code(target, original)

        if not available:
            return ToolResult(text=f"No formatter available for {target.suffix or '(no extension)'} files "
                                     f"(either this file type has none wired up, or the tool isn't installed).")
        if error:
            return ToolResult(text=f"Formatting failed: {error}")
        if formatted == original:
            return ToolResult(text=f"{path} is already formatted - no changes needed.")

        diff = make_unified_diff(original, formatted, path)
        console.print(f"\n[bold]Proposed formatting change to {path}:[/bold]")
        render_diff(diff)
        if not self.perm.request_write(target, preview=diff):
            return ToolResult(text="Permission denied by user. File not changed.")
        apply_edit(target, formatted)
        return ToolResult(text=f"Formatted {path}.")

    def _tool_run_tests(self, path: str = ".") -> ToolResult:
        scope = self._resolve(path) if path and path != "." else self.root
        if not self._path_in_root(scope):
            return ToolResult(text=f"Blocked: {path} is outside the allowed project directory.")

        if (self.root / "Cargo.toml").exists():
            cmd = ["cargo", "test"]
        elif (self.root / "go.mod").exists():
            cmd = ["go", "test", "./..."]
        elif (self.root / "package.json").exists():
            cmd = ["npm", "test"]
        elif (self.root / "composer.json").exists():
            local_phpunit = self.root / "vendor" / "bin" / "phpunit"
            phpunit_bin = str(local_phpunit) if local_phpunit.exists() else "phpunit"
            # unlike pytest, phpunit does NOT auto-discover tests with no arguments -
            # it needs an explicit file/directory target or it just prints its help text.
            cmd = [phpunit_bin, str(scope)]
        elif next(self.root.rglob("test_*.py"), None) or next(self.root.rglob("*_test.py"), None) \
                or (self.root / "pytest.ini").exists() or (self.root / "tests").is_dir():
            cmd = ["python3", "-m", "pytest", str(scope), "-v", "--tb=short"]
        else:
            return ToolResult(text="No recognized test setup found (looked for pytest-style Python tests, "
                                     "package.json, composer.json, go.mod, Cargo.toml). If tests exist under "
                                     "a different convention, run them with run_command instead.")

        if not self.perm.request_command(" ".join(cmd)):
            return ToolResult(text="Permission denied by user. Tests not run.")
        try:
            proc = subprocess.run(cmd, cwd=str(self.root), capture_output=True, text=True, timeout=120)
        except subprocess.TimeoutExpired:
            return ToolResult(text="Tests timed out after 120s.")
        except OSError as e:
            return ToolResult(text=f"Could not run tests: {e}")
        output = (proc.stdout or "") + (proc.stderr or "")
        return ToolResult(text=f"(exit code {proc.returncode})\n{output[-6000:]}")
