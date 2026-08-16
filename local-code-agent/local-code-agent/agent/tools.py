from __future__ import annotations
import ast
import atexit
import json
import re
import shutil
import subprocess
import tempfile
import threading
import urllib.parse
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import deque, OrderedDict
from pathlib import Path
from dataclasses import dataclass

import requests
from rich.console import Console

from .permissions import PermissionManager
from .diffs import make_unified_diff, render_diff, apply_edit
from .indexer import CodebaseIndex, _STRUCTURE_MARKERS, build_dependency_graph, extract_python_symbols, find_containing_symbol, build_symbol_table
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
        text: str | None
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
                        "description": "The full current plan, in order, as plain strings - always re-send every step, not just the ones that changed. Prefix EVERY step with exactly one of: '[ ] ' (not started yet), '[~] ' (working on it now), '[x] ' (finished). Example: [\"[x] Create the Product model\", \"[~] Build the home page template\", \"[ ] Add the cart route\"]. Optional, only for genuinely multi-part tasks: a line starting with '## ' groups the steps that follow under a feature name until the next '## ' line, e.g. [\"## User Authentication\", \"[x] Create the User model\", \"[ ] Add the login route\", \"## Product Catalog\", \"[ ] Create the Product model\"]. Skip this entirely for anything simple - it's for organizing a plan that genuinely spans multiple features, not a requirement.",
                    },
                },
                "required": ["steps"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_scratchpad",
            "description": "Save short, durable notes for yourself - facts worth remembering not just for the rest of this session but for FUTURE sessions on this project too (e.g. 'the DB connection env var is DATABASE_URL', 'user wants tabs not spaces', 'the API base path is /api/v2'). These are saved to disk and reloaded automatically the next time the agent runs on this project, even after this process closes. Call this any time you learn something worth not forgetting. Replaces the full scratchpad each time - re-send everything still worth keeping, not just the new note.",
            "parameters": {
                "type": "object",
                "properties": {
                    "notes": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "The full current set of notes, as short plain strings. Keep each one to one sentence.",
                    },
                },
                "required": ["notes"],
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
            "description": "Read a text/code file in the project, with line numbers. For a large file, pass start_line/end_line to read just the relevant section instead of the whole thing - cheaper and faster than reading everything. If you already read this exact file in full earlier this session and it has since changed, a full read shows what changed instead of the whole file again - pass full=true if you need the complete current content regardless.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path from project root."},
                    "start_line": {"type": "integer", "description": "Optional: first line to read (1-indexed)."},
                    "end_line": {"type": "integer", "description": "Optional: last line to read (inclusive)."},
                    "full": {"type": "boolean", "description": "Optional: force the complete current content even if only what changed since your last full read would normally be shown. Defaults to false."},
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
            "description": "Get a quick, near-instant map of a file without reading the whole thing: its function/class/method names with line numbers, module-level variables/constants (Python only), a one-line summary (from its docstring or leading comment, if any), and what it imports plus what imports it (if a reindex has been run - shows what else might be affected by changing this file). Use this before deciding what to read_file in full, or before changing a file to see what depends on it.",
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
            "name": "get_symbol",
            "description": "Python only. Fetch the exact source of ONE named function, method, or class from a file - not the whole file, not a guessed line range. Use this after list_symbols (or after a symbol map auto-surfaced under RELATED above) has told you what's in a file and where, when you only need one specific piece of it, rather than reading the whole file. Includes decorators if present.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path from project root."},
                    "name": {"type": "string", "description": "The function, method, or class name to fetch, exactly as it appears in list_symbols."},
                },
                "required": ["path", "name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_definition",
            "description": "Python only, requires reindex_codebase to have been run first. Find which file(s) and line(s) define a function or class by name, project-wide - use this when you don't already know which file something lives in. This is name-based, not a full semantic resolution: it can't distinguish two unrelated classes that happen to define a same-named method, so check the results make sense rather than trusting a single match blindly. If it returns more than one location, that's telling you the name isn't unique in this project.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "The function or class name to find."},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_document",
            "description": "Read a requirements document: pdf, docx, txt, md, json, yaml, csv. Returns extracted text, capped per call to avoid overflowing your context window. If the result says there's more, call this again with offset set to the value it gives you to continue reading - calling it again with the SAME offset returns the SAME text, it does not advance on its own.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to the document."},
                    "offset": {"type": "integer", "description": "Character position to start reading from. Defaults to 0 (the beginning). Use the offset given in a previous result to continue from where it left off."},
                },
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
            "description": "Semantic search over the indexed codebase. Finds relevant code by meaning, not just exact text - use this when you don't know which file something lives in, or want to search by topic/concept rather than an exact name. Results below a minimum relevance score are filtered out automatically rather than forcing out weak matches just to fill top_k - if nothing meets the bar, that's told to you plainly instead of handing back irrelevant results as if they were useful.",
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
            "description": "Exact text/pattern search across project files (like grep). Use for exact identifiers, e.g. a function name, or to approximate 'find all references to X' - matches by substring, not scope, so a name that's also a substring of something unrelated (e.g. 'login' inside 'login_page') will show up too; check results make sense rather than trusting every hit as a true usage.",
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
            "name": "recall_history",
            "description": "Search this session's full conversation log for a keyword or phrase - use this when you need something that was said or decided earlier but no longer appears in your current context (it may have been summarized away). This searches the complete record on disk, not just what's currently visible to you, so it can find things a compacted summary lost detail on.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Keyword or phrase to search for, e.g. a filename, decision, or topic mentioned earlier."},
                },
                "required": ["query"],
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
    into the {"description", "status", "feature"} dicts the CLI panel and GUI
    checklist render. Tolerant of a missing/malformed marker - defaults to
    "pending" rather than erroring, since a small model won't always get the
    prefix exactly right.

    Optional Hierarchical Planning: a line starting with '##' is a feature-group
    header, not a step - everything after it (until the next '##' line) gets
    tagged with that feature name in its "feature" key. This is entirely
    optional and backward compatible - a simple plan with no '##' lines at all
    just has every step's "feature" as None, identical to before this was
    added. Deliberately NOT a new schema field or a rigid required hierarchy -
    a small model shouldn't be forced through extra structure for a one-step
    task, so this stays available only when actually useful for a genuinely
    multi-part one.
    """
    parsed = []
    current_feature: str | None = None
    for raw in raw_steps:
        raw = (raw or "").strip()
        if not raw:
            continue
        if raw.startswith("##"):
            current_feature = raw.lstrip("#").strip() or None
            continue
        if raw.startswith("[x]"):
            status, desc = "completed", raw[3:].strip()
        elif raw.startswith("[~]"):
            status, desc = "in_progress", raw[3:].strip()
        elif raw.startswith("[ ]"):
            status, desc = "pending", raw[3:].strip()
        else:
            status, desc = "pending", raw
        if desc:
            parsed.append({"description": desc, "status": status, "feature": current_feature})
    return parsed


def format_plan_text(parsed_steps: list[dict]) -> str:
    """Renders parsed plan steps as readable text - shared by the tool's own
    result text and the text shown to the model in context, so both stay
    consistent. Groups by feature ONLY if at least one step actually has one;
    otherwise renders the identical flat numbered list as before Hierarchical
    Planning grouping was added, so a simple plan looks exactly the same as
    it always has."""
    if not any(s.get("feature") for s in parsed_steps):
        lines = []
        for i, s in enumerate(parsed_steps, 1):
            marker = {"completed": "[x]", "in_progress": "[~]"}.get(s["status"], "[ ]")
            lines.append(f"{marker} {i}. {s['description']}")
        return "\n".join(lines)

    lines = []
    seen_feature = object()  # sentinel - never equals a real feature name or None
    step_num = 0
    for s in parsed_steps:
        feature = s.get("feature")
        if feature != seen_feature:
            seen_feature = feature
            lines.append(f"## {feature}" if feature else "## (ungrouped)")
        step_num += 1
        marker = {"completed": "[x]", "in_progress": "[~]"}.get(s["status"], "[ ]")
        lines.append(f"  {marker} {step_num}. {s['description']}")
    return "\n".join(lines)


ALL_TOOL_NAMES: set[str] = {schema["function"]["name"] for schema in TOOL_SCHEMAS}

_INTENT_PHRASES = (
    "i will use", "i'll use", "let me use", "i am going to use", "i'm going to use",
    "i will call", "i'll call", "let me call", "going to call",
    "i will now use", "i'll now use", "let me now use",
)

# Tier 2 exists because real-world narration rarely names a tool explicitly - "I am
# reading the file" never says "read_document", so Tier 1 alone misses it entirely.
# Built from (verb, tense-prefix) combinations rather than bare verb stems, so past
# tense ("I already read the document") doesn't false-positive: "read" alone would
# match that too, but "i am read" / "i will read" / "let me read" etc. don't.
_ACTION_VERBS_PRESENT = (
    "reading", "opening", "analyzing", "analysing", "reviewing", "checking",
    "retrieving", "fetching", "loading", "processing", "examining", "inspecting",
    "parsing", "scanning", "going through",
)
_ACTION_VERBS_BASE = (
    "read", "open", "analyze", "analyse", "review", "check", "retrieve", "fetch",
    "load", "process", "examine", "inspect", "parse", "scan", "go through",
)
_PRESENT_CONTINUOUS_PREFIXES = ("i am ", "i'm ", "now ")
_FUTURE_PREFIXES = ("i will ", "i'll ", "let me ", "going to ", "i'm going to ", "i am going to ")

_ACTION_PHRASES: set[str] = set()
for _v in _ACTION_VERBS_PRESENT:
    for _p in _PRESENT_CONTINUOUS_PREFIXES:
        _ACTION_PHRASES.add(_p + _v)
for _v in _ACTION_VERBS_BASE:
    for _p in _FUTURE_PREFIXES:
        _ACTION_PHRASES.add(_p + _v)

_FILE_REFERENCE_RE = re.compile(
    r"(the file|the document|that file|that document|the doc\b|this file|this document|"
    r"[\w./\\-]+\.(?:docx|doc|pdf|txt|md|csv|json|ya?ml|xlsx?|png|jpe?g))",
    re.IGNORECASE,
)


def find_announced_but_uncalled_tool(content: str) -> str | None:
    """Detects a specific, real failure mode of smaller local models: narrating an
    intention or an in-progress action in plain text ("I will use read_document to...",
    or just "I am reading the file...") without actually emitting a tool call in the
    same response - so nothing happens, but it reads as if something did.

    Two tiers: Tier 1 catches an explicit tool name named right after an intent
    phrase (precise). Tier 2 catches natural narration near a reference to "the
    file"/"the document"/an actual filename that never names a tool at all, using
    present/future-tense phrase templates specifically so past-tense mentions
    ("I already read the document") don't false-positive - a bare verb-stem search
    would incorrectly match those too.

    Returns a description of what was seemingly promised (a specific tool name for
    Tier 1, a generic description for Tier 2, since the text alone doesn't say
    which read tool was meant), or None. Deliberately conservative - a miss just
    means no nudge happens, but a false positive injects a confusing correction
    into a turn that didn't need one.
    """
    if not content:
        return None
    lowered = content.lower()

    for phrase in _INTENT_PHRASES:
        idx = lowered.find(phrase)
        if idx == -1:
            continue
        window = lowered[idx + len(phrase): idx + len(phrase) + 60]
        for name in ALL_TOOL_NAMES:
            if name in window:
                return name

    if _FILE_REFERENCE_RE.search(content):
        for phrase in _ACTION_PHRASES:
            if phrase in lowered:
                return "a file-reading tool (read_file / read_document / read_image)"

    return None


class _BoundedHybridCache:
    """A small LRU/LFU hybrid cache, modeled directly on how Linux's actual page
    reclaim works: entries start on an "inactive" list; a second reference
    promotes them to "active", which is protected from eviction (unless active
    itself grows past its own share of the total capacity, in which case the
    oldest active entries get demoted back to inactive rather than evicted
    outright). Eviction always comes from inactive first, oldest entry first.

    The point of this over plain LRU: a file referenced only once, long ago,
    gets evicted before anything else - but a file referenced repeatedly
    throughout a long session stays cached even if it hasn't been touched
    recently, which pure recency-based eviction would evict just as readily as
    a one-off. Built specifically to fix _list_symbols_cache's unbounded growth
    (it used to be a plain dict with no cap at all) without losing the value of
    caching a file that keeps coming back into play.
    """

    def __init__(self, max_size: int = 200, max_active_fraction: float = 0.5):
        self.max_size = max_size
        self.max_active = max(1, int(max_size * max_active_fraction))
        self._active: OrderedDict = OrderedDict()
        self._inactive: OrderedDict = OrderedDict()

    def get(self, key):
        if key in self._active:
            self._active.move_to_end(key)
            return self._active[key]
        if key in self._inactive:
            value = self._inactive.pop(key)
            self._active[key] = value  # second reference - promote
            self._active.move_to_end(key)
            self._rebalance()
            return value
        return None

    def set(self, key, value) -> None:
        if key in self._active:
            self._active[key] = value
            self._active.move_to_end(key)
            return
        if key in self._inactive:
            self._inactive.pop(key)
            self._active[key] = value  # being set again is a re-reference too - promote
            self._active.move_to_end(key)
            self._rebalance()
            return
        self._inactive[key] = value
        self._inactive.move_to_end(key)
        self._evict_if_needed()

    def _rebalance(self) -> None:
        while len(self._active) > self.max_active:
            k, v = self._active.popitem(last=False)  # oldest active, demoted not dropped
            self._inactive[k] = v
        self._evict_if_needed()

    def _evict_if_needed(self) -> None:
        while len(self._active) + len(self._inactive) > self.max_size:
            if self._inactive:
                self._inactive.popitem(last=False)
            elif self._active:
                self._active.popitem(last=False)  # only if inactive is somehow empty
            else:
                break

    def clear(self) -> None:
        self._active.clear()
        self._inactive.clear()

    def __len__(self) -> int:
        return len(self._active) + len(self._inactive)

    def __contains__(self, key) -> bool:
        return key in self._active or key in self._inactive


class ToolRegistry:
    def __init__(self, project_root: Path, permissions: PermissionManager,
                 index: CodebaseIndex, index_cfg: dict):
        self.root = project_root.resolve()
        self.perm = permissions
        self.index = index
        self.index_cfg = index_cfg
        self.search_codebase_min_score = index_cfg.get("search_codebase_min_score", 0.3)
        self._processes: dict[str, _RunningProcess] = {}
        atexit.register(self._cleanup_processes_on_exit)
        self._process_counter = 0
        self._current_plan: list[dict] = []
        self._scratchpad: list[str] = []
        self._dep_forward: dict[str, set[str]] = {}
        self._dep_reverse: dict[str, set[str]] = {}
        self._symbol_table: dict[str, list[tuple[str, int, str]]] = {}
        self._list_symbols_cache = _BoundedHybridCache(max_size=200)
        # Differential Memory: tracks the full content last shown for each path
        # via a full-file read_file call, so a later re-read of a changed file
        # can show what changed instead of the whole file again. Same bounded
        # cache design as above, for the same reason - no unbounded growth.
        self._read_file_cache = _BoundedHybridCache(max_size=200)
        # Set post-construction by main.py/gui/server.py once the SessionLogger
        # exists (ToolRegistry is built first) - powers recall_history, letting
        # the model page back in conversation detail that's been compacted out
        # of active context, rather than that detail being genuinely lost.
        self.session_log_path: Path | None = None

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

    # Tools that never show a permission prompt at all - always safe to run
    # concurrently regardless of session trust state.
    _ALWAYS_PARALLEL_SAFE_TOOLS = {"search_codebase", "grep_codebase", "recall_history"}

    # Tools gated by request_read() - safe to run concurrently ONLY once the
    # session has already granted read trust (self.perm._session_allow_all_reads),
    # since request_read() then returns instantly with no prompting at all. If
    # that trust isn't established yet, concurrent execution risks two threads
    # trying to show an interactive permission prompt at the same time, which
    # could genuinely garble a terminal - so this stays conservative rather than
    # trying to parallelize while an actual prompt might still occur.
    _READ_GATED_PARALLEL_SAFE_TOOLS = {
        "read_file", "read_document", "read_files", "list_symbols",
        "list_directory", "db_schema", "db_query",
    }

    def _batch_is_parallel_safe(self, calls: list[tuple[str, dict]]) -> bool:
        """Decides whether a batch of tool calls from the same model turn can run
        concurrently. Deliberately conservative: write/execute-type tools (anything
        not in the two sets above) always force the whole batch to run
        sequentially, one at a time, exactly as before - they have side effects,
        ordering can matter, and each needs its own uninterrupted permission
        prompt. Only a batch made ENTIRELY of read-only tools, where any
        permission-gated ones are already covered by session-wide read trust,
        gets to run in parallel."""
        if len(calls) < 2:
            return False
        names = {name for name, _ in calls}
        safe_names = self._ALWAYS_PARALLEL_SAFE_TOOLS | self._READ_GATED_PARALLEL_SAFE_TOOLS
        if not names.issubset(safe_names):
            return False
        needs_read_trust = bool(names & self._READ_GATED_PARALLEL_SAFE_TOOLS)
        if needs_read_trust and not self.perm._session_allow_all_reads:
            return False
        return True

    def execute_batch(self, calls: list[tuple[str, dict]]) -> list[ToolResult]:
        """Executes several tool calls from one model turn. Runs them concurrently
        via threads ONLY when _batch_is_parallel_safe() confirms it - otherwise
        falls back to the exact same one-at-a-time behavior as calling execute()
        in a loop. Threads give a real speedup here despite Python's GIL, because
        the actual work involved - disk I/O in read_file/read_document, numpy
        cosine-similarity math in search_codebase - releases the GIL during the
        expensive part, so it genuinely overlaps even on a single CPU. Results are
        returned in the same order the calls were given, regardless of which one
        actually finished first.
        """
        if not self._batch_is_parallel_safe(calls):
            return [self.execute(name, args) for name, args in calls]

        results: list[ToolResult | None] = [None] * len(calls)
        with ThreadPoolExecutor(max_workers=min(len(calls), 8)) as pool:
            future_to_index = {pool.submit(self.execute, name, args): i
                                for i, (name, args) in enumerate(calls)}
            for future in as_completed(future_to_index):
                i = future_to_index[future]
                try:
                    results[i] = future.result()
                except Exception as e:  # a thread's own exception should never kill the turn
                    name = calls[i][0]
                    results[i] = ToolResult(text=f"Error running {name} concurrently: {e}")
        return results

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
        return ToolResult(text=format_plan_text(parsed))

    def _tool_update_scratchpad(self, notes: list[str] | None = None) -> ToolResult:
        if isinstance(notes, str):
            notes = [notes]  # tolerate a bare string instead of a list
        cleaned = [n.strip() for n in (notes or []) if n and n.strip()]
        self._scratchpad = cleaned
        if not cleaned:
            return ToolResult(text="Scratchpad cleared (no notes given).")
        return ToolResult(text="Scratchpad updated:\n" + "\n".join(f"- {n}" for n in cleaned))

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

    def _tool_read_file(self, path: str, start_line: int | None = None, end_line: int | None = None,
                         full: bool = False) -> ToolResult:
        target = self._resolve(path)
        if not self.perm.request_read(target):
            return ToolResult(text="Permission denied by user.")
        if not target.exists():
            return ToolResult(text=f"File does not exist: {path}")
        text = target.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        total = len(lines)
        is_partial_read = bool(start_line or end_line)
        cache_key = str(target)

        # Differential Memory: only for full-file reads (a line-range slice is
        # already "showing less than everything", so diffing it against a
        # prior full read would be comparing two different things). If this
        # exact file was read in full earlier this session, show what changed
        # since then instead of the whole file again - more compact AND more
        # informative, since the change is usually what's actually relevant.
        # full=True is the escape hatch when complete current content is
        # genuinely needed regardless of what changed.
        if not is_partial_read and not full:
            previous = self._read_file_cache.get(cache_key)
            if previous is not None:
                if previous == text:
                    self._read_file_cache.set(cache_key, text)
                    return ToolResult(text=f"(unchanged since your last full read of {path} - {total} lines, nothing new to show)")
                diff = make_unified_diff(previous, text, path)
                self._read_file_cache.set(cache_key, text)
                if diff.strip():
                    return ToolResult(
                        text=f"(showing what changed in {path} since your last full read, not the whole file - "
                             f"pass full=true to read_file if you need the complete current content)\n\n{diff}"
                    )
                # difflib found no line-level difference despite the raw text
                # differing (e.g. only trailing-newline changes) - fall through
                # to a normal full read below rather than showing an empty diff

        s = max(1, start_line) if start_line else 1
        e = min(total, end_line) if end_line else total
        if s > total:
            return ToolResult(text=f"start_line {s} is past the end of the file ({total} lines total).")
        numbered = "\n".join(f"{i:>5}\t{lines[i - 1]}" for i in range(s, e + 1))
        header = f"(showing lines {s}-{e} of {total} total)\n" if (s != 1 or e != total) else ""
        if not is_partial_read:
            self._read_file_cache.set(cache_key, text)
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

        # File Memory: cache the computed result keyed by (path, mtime) - a
        # syntax error result is deliberately NOT cached, since re-parsing a
        # broken file is cheap anyway and caching an error risks it going stale
        # the instant the file is fixed. Permission checking above always runs
        # regardless of the cache - only the parsing/computation is skipped.
        try:
            mtime = target.stat().st_mtime
        except OSError:
            mtime = None
        cache_key = str(target)
        if mtime is not None:
            cached = self._list_symbols_cache.get(cache_key)
            if cached is not None and cached[0] == mtime:
                return ToolResult(text=cached[1])

        rel = str(target.relative_to(self.root)) if self._path_in_root(target) else path
        sections = []

        # -- dependency info (needs a reindex to have been run at least once) --
        imports = sorted(self._dep_forward.get(rel, set()))
        imported_by = sorted(self._dep_reverse.get(rel, set()))
        if imports or imported_by:
            dep_lines = []
            if imports:
                dep_lines.append("Imports: " + ", ".join(imports))
            if imported_by:
                dep_lines.append("Imported by (would be affected by changes here): " + ", ".join(imported_by))
            sections.append("\n".join(dep_lines))

        if target.suffix.lower() == ".py":
            try:
                source = target.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                return ToolResult(text="Could not read file.")
            parsed = extract_python_symbols(source)
            if parsed is None:
                try:
                    ast.parse(source)  # re-parse just to get the precise error location for the message
                except SyntaxError as e:
                    return ToolResult(text=f"Could not parse (syntax error at line {e.lineno}): {e.msg}")
                # In practice unreachable - extract_python_symbols only ever returns None when
                # ast.parse(source) itself raises, and the re-parse above is the identical,
                # deterministic call, so it will always raise the same SyntaxError and return
                # above. Kept as an explicit, safe fallback rather than relying on that as an
                # implicit invariant between two separate functions.
                return ToolResult(text="Could not parse this file (syntax error).")

            if parsed["summary"]:
                sections.append("Summary: " + parsed["summary"])

            lines_out = [f"{ln}: {label}" for ln, label in parsed["entries"]]
            sections.append("\n".join(lines_out) or "(no functions or classes found)")

            if parsed["variables"]:
                sections.append("Module-level variables:\n" +
                                 "\n".join(f"{ln}: {name}" for ln, name in parsed["variables"]))

            final_text = "\n\n".join(sections)
            if mtime is not None:
                self._list_symbols_cache.set(cache_key, (mtime, final_text))
            return ToolResult(text=final_text)

        try:
            lines = target.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            return ToolResult(text="Could not read file.")

        # leading comment block as a best-effort summary for non-Python files
        leading_comment = []
        for line in lines[:15]:
            stripped = line.strip()
            if stripped.startswith(("//", "/*", "*", "#")):
                leading_comment.append(stripped.lstrip("/*# ").strip())
            elif stripped:
                break
        if leading_comment:
            sections.append("Summary: " + " ".join(leading_comment)[:200])

        hits = [f"{i + 1}: {line.strip()}" for i, line in enumerate(lines) if any(p.match(line) for p in _STRUCTURE_MARKERS)]
        sections.append("\n".join(hits) or
                         "No recognizable function/class definitions found this way for this file type - try read_file instead.")
        final_text = "\n\n".join(sections)
        if mtime is not None:
            self._list_symbols_cache.set(cache_key, (mtime, final_text))
        return ToolResult(text=final_text)

    def _tool_get_symbol(self, path: str, name: str) -> ToolResult:
        """Precise, AST-based extraction of exactly one function/method/class's
        source - the two-step workaround this replaces was list_symbols (to
        find a starting line) followed by read_file with a GUESSED end line,
        since nothing told the model where a function actually ends. Python's
        AST has provided end_lineno since 3.8 - exact boundaries are
        computable, not guessed, which removes a real source of the model
        either cutting a function off mid-body or reading more than it needed.
        """
        target = self._resolve(path)
        if not self.perm.request_read(target):
            return ToolResult(text="Permission denied by user.")
        if not target.exists():
            return ToolResult(text=f"File does not exist: {path}")
        if target.suffix.lower() != ".py":
            return ToolResult(text=f"get_symbol only supports Python files currently - use read_file with a line range for {path}.")

        try:
            source = target.read_text(encoding="utf-8", errors="ignore")
            tree = ast.parse(source)
        except SyntaxError as e:
            return ToolResult(text=f"Could not parse (syntax error at line {e.lineno}): {e.msg}")

        lines = source.splitlines()
        matches = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == name:
                start = node.lineno
                if node.decorator_list:
                    start = min(start, min(d.lineno for d in node.decorator_list))
                end = getattr(node, "end_lineno", None) or node.lineno
                kind = "class" if isinstance(node, ast.ClassDef) else "function"
                matches.append((start, end, kind))

        if not matches:
            return ToolResult(text=f"No function, method, or class named '{name}' found in {path}. Use list_symbols to see what's actually there.")

        matches.sort(key=lambda m: m[0])
        start, end, kind = matches[0]
        prefix = ""
        if len(matches) > 1:
            other_lines = ", ".join(f"line {s}" for s, e, k in matches[1:])
            prefix = (f"(note: '{name}' appears {len(matches)} times in this file - showing the first "
                      f"match at line {start}; also found at {other_lines} - use read_file with a line "
                      f"range if you need one of those instead)\n\n")

        end = min(end, len(lines))
        snippet = "\n".join(f"{i:>5}\t{lines[i - 1]}" for i in range(start, end + 1))
        return ToolResult(text=f"{prefix}{kind} {name} in {path} (lines {start}-{end}):\n{snippet}")

    def _tool_find_definition(self, name: str) -> ToolResult:
        """Queries the project-wide symbol table built during reindex - a
        cheap, deterministic approximation of an LSP's "go to definition."
        Purely a lookup against a precomputed dict, not a re-parse of the
        project - the actual work happened once, during reindex_codebase.
        """
        if not self._symbol_table:
            return ToolResult(text="No symbol table available yet - run reindex_codebase first.")
        locations = self._symbol_table.get(name)
        if not locations:
            return ToolResult(text=f"No function or class named '{name}' found in the indexed project. "
                                    f"Try grep_codebase if it might be a variable, or reindex_codebase if "
                                    f"the project has changed since the last index.")
        lines = [f"{kind} {name} defined in {rel_path}, line {line}" for rel_path, line, kind in locations]
        header = ""
        if len(locations) > 1:
            header = (f"'{name}' is defined in {len(locations)} places - this is a name-based lookup, "
                      f"not scope-aware, so more than one match doesn't necessarily mean a conflict, "
                      f"just that the name isn't unique in this project:\n")
        return ToolResult(text=header + "\n".join(lines))

    def _tool_read_document(self, path: str, offset: int = 0) -> ToolResult:
        target = self._resolve(path)
        if not self.perm.request_read(target):
            return ToolResult(text="Permission denied by user.")
        try:
            text = doc_reader.read_as_text(target)
        except doc_reader.UnsupportedFile as e:
            return ToolResult(text=str(e))

        MAX_CHARS_PER_CALL = 20000
        total_len = len(text)
        offset = max(0, offset)
        if offset >= total_len and total_len > 0:
            return ToolResult(text=f"(offset {offset} is past the end of the document - it's only {total_len} characters long)")

        chunk = text[offset: offset + MAX_CHARS_PER_CALL]
        end = offset + len(chunk)

        if offset > 0 or end < total_len:
            header = f"(showing characters {offset}-{end} of {total_len} total"
            if end < total_len:
                header += f" - call read_document again with offset={end} to continue reading"
            header += ")\n\n"
            return ToolResult(text=header + chunk)
        return ToolResult(text=chunk)

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

        relevant = [r for r in results if r.get("score", 0) >= self.search_codebase_min_score]
        if not relevant:
            best = max(r.get("score", 0) for r in results)
            return ToolResult(
                text=f"No results met the relevance threshold ({self.search_codebase_min_score:.2f} - "
                     f"best match scored only {best:.2f}). The codebase likely doesn't contain anything "
                     f"closely related to this query, rather than the search having failed outright - "
                     f"try grep_codebase for an exact identifier, or rephrase if you were guessing at "
                     f"terminology the code might not actually use."
            )

        blocks = []
        for r in relevant:
            blocks.append(
                f"--- {r['path']} (lines {r['start_line']}-{r['end_line']}, score {r['score']:.2f}) ---\n{r['content']}"
            )
        omitted = len(results) - len(relevant)
        text = "\n\n".join(blocks)
        if omitted:
            text += f"\n\n({omitted} additional result(s) omitted - below the relevance threshold, likely not actually useful)"
        return ToolResult(text=text)

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

    def _tool_recall_history(self, query: str) -> ToolResult:
        """The 'page back in' half of this project's context-as-virtual-memory
        design: compact() pages OLD chat detail out of active context into a
        lossy summary, but the full, un-summarized detail still exists in the
        session log on disk - this searches that log directly so specific
        earlier detail can be recovered deliberately, rather than being
        genuinely gone once it's been compacted away."""
        if not self.session_log_path or not self.session_log_path.exists():
            return ToolResult(text="No session log available to search yet.")
        try:
            text = self.session_log_path.read_text(encoding="utf-8", errors="ignore")
        except OSError as e:
            return ToolResult(text=f"Could not read the session log: {e}")

        blocks = [b.strip() for b in text.split("\n\n") if b.strip()]
        query_lower = query.lower()
        matches = [b for b in blocks if query_lower in b.lower()]

        if not matches:
            return ToolResult(text=f"No mention of '{query}' found in this session's history so far.")

        max_shown = 5
        shown = matches[-max_shown:]  # most recent matches are usually the most relevant
        header = f"Found {len(matches)} matching entr{'y' if len(matches) == 1 else 'ies'} in this session's history"
        if len(matches) > max_shown:
            header += f" (showing the {max_shown} most recent)"
        return ToolResult(text=header + ":\n\n" + "\n\n---\n\n".join(shown))

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
            result += "\n" + self._enrich_with_error_context(check_report, known_file=target)
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
            result += "\n" + self._enrich_with_error_context(check_report, known_file=target)
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
            truncated = out[-8000:]
            if proc.returncode != 0:
                truncated = self._enrich_with_error_context(truncated)
            return ToolResult(text=f"(exit code {proc.returncode})\n{truncated}")
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
        new_forward, new_reverse = build_dependency_graph(
            self.root,
            self.index_cfg.get("ignore_dirs", set()),
            self.index_cfg.get("max_file_size_kb", 512),
        )
        # Mutate in place, don't reassign - anything holding a reference to
        # these same dict objects (e.g. ContextManager's dependency_reverse,
        # used for multi-resolution context) sees the update automatically,
        # with no separate "refresh" call needed anywhere else.
        self._dep_forward.clear()
        self._dep_forward.update(new_forward)
        self._dep_reverse.clear()
        self._dep_reverse.update(new_reverse)
        # also drop any cached list_symbols results - dependencies may have
        # changed even for files whose own content (and mtime) didn't
        self._list_symbols_cache.clear()
        new_symbol_table = build_symbol_table(
            self.root,
            self.index_cfg.get("ignore_dirs", set()),
            self.index_cfg.get("max_file_size_kb", 512),
        )
        # Mutate in place - same reasoning as the dependency graph above.
        self._symbol_table.clear()
        self._symbol_table.update(new_symbol_table)
        stats = self.index.stats()
        return ToolResult(text=f"Indexed. {stats['files']} files, {stats['chunks']} chunks total ({written} new/updated). "
                                f"Dependency graph rebuilt: {len(self._dep_forward)} file(s) with detected imports. "
                                f"Symbol table: {len(self._symbol_table)} unique name(s).")

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
                enriched = self._enrich_with_error_context(check_report, known_file=target)
                reports.append(f"{path}:\n  " + enriched.replace("\n", "\n  "))

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

    def _cleanup_processes_on_exit(self) -> None:
        """Registered with atexit at construction time - terminates any
        background process still running via start_dev_server when the agent
        itself exits (normal exit, Ctrl+C, or an uncaught exception
        unwinding). Without this, a dev server started but never explicitly
        stopped would become an orphaned process, continuing to run and hold
        its port with nothing telling the user it's still alive - a real,
        common annoyance for a coding agent that starts dev servers, not a
        hypothetical one. Best-effort throughout: never raises, since an
        error here should never block the interpreter from actually exiting.
        """
        for process_id, rp in list(self._processes.items()):
            if rp.proc.poll() is not None:
                continue  # already exited on its own
            try:
                rp.proc.terminate()
                rp.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                try:
                    rp.proc.kill()
                except Exception:
                    pass
            except Exception:
                pass

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

    _PY_TRACEBACK_RE = re.compile(r'File "([^"]+)", line (\d+)')
    _PYFLAKES_RE = re.compile(r'^([^\s:]+\.py):(\d+):', re.MULTILINE)
    _LINE_ONLY_RE = re.compile(r'^\s*(?:-\s*)?line (\d+):', re.MULTILINE)
    _SYNTAX_ERROR_LINE_RE = re.compile(r'\bat line (\d+)\b')

    def _enrich_with_error_context(self, output: str, known_file: Path | None = None) -> str:
        """Two-hop, Python-only error-context enrichment, appended to
        test/check output that contains failures. Hop 1: parse file:line
        references out of the output and find the exact function/method/class
        containing each line via find_containing_symbol, showing its precise
        source - or, when that's not possible (see below), a raw line-range
        window instead. Hop 2: for the file the error is in, show a compact
        SYMBOL MAP (not a guessed specific function) of whatever it directly
        depends on - deliberately not trying to resolve which exact call was
        involved, since that's the same precision problem a full call graph
        has; a symbol map lets the model make that judgment call itself,
        safely.

        known_file: when the caller already knows which file was just checked
        (write_file/edit_file/scaffold_files all do), pass it here - this
        project's own pyflakes wrapper reports "line N: ..." without a
        filename (there's only ever one file in that context), which a
        filename-based regex can't recover on its own. run_tests and
        run_command, where output can span multiple files or come from
        anything, leave this as None and rely on the file:line references
        actually present in the output instead.
        """
        locations: list[tuple[Path, int]] = []
        seen: set[tuple[str, int]] = set()

        def _add(p: Path, line: int) -> bool:
            try:
                p = p.resolve()
                p.relative_to(self.root)
            except (OSError, ValueError):
                return False
            if not p.exists() or p.suffix != ".py":
                return False
            key = (str(p), line)
            if key in seen:
                return False
            seen.add(key)
            locations.append((p, line))
            return len(locations) >= 3

        if known_file is not None:
            for pattern in (self._LINE_ONLY_RE, self._SYNTAX_ERROR_LINE_RE):
                done = False
                for m in pattern.finditer(output):
                    if _add(known_file, int(m.group(1))):
                        done = True
                        break
                if done or len(locations) >= 3:
                    break
        else:
            for pattern in (self._PY_TRACEBACK_RE, self._PYFLAKES_RE):
                done = False
                for m in pattern.finditer(output):
                    raw_path, line_str = m.group(1), m.group(2)
                    try:
                        line = int(line_str)
                    except ValueError:
                        continue
                    p = Path(raw_path)
                    p = p if p.is_absolute() else (self.root / p)
                    if _add(p, line):
                        done = True
                        break
                if done or len(locations) >= 3:
                    break

        if not locations:
            return output

        blocks = []
        for file_path, line in locations:
            rel = str(file_path.relative_to(self.root))
            try:
                source = file_path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            src_lines = source.splitlines()
            found = find_containing_symbol(source, line)
            if found is not None:
                kind, name, start, end = found
                snippet = "\n".join(f"{i:>5}\t{src_lines[i - 1]}" for i in range(start, min(end, len(src_lines)) + 1))
                block = f"--- {rel}:{line} -> {kind} {name} (lines {start}-{end}) ---\n{snippet}"
            else:
                # AST-based extraction isn't possible - two different reasons,
                # worth distinguishing rather than guessing: either the file
                # has a genuine syntax error (doesn't parse at all - the
                # deepest, AST-based tooling in this project structurally
                # can't analyze broken code), or the reported line is
                # legitimately module-level code with no containing function
                # (e.g. an "if __name__ == '__main__':" block) - not an
                # error at all, just outside what a function-boundary lookup
                # can describe.
                try:
                    ast.parse(source)
                    reason = "module-level code, not inside any function/class"
                except SyntaxError:
                    reason = "the file has a syntax error and can't be parsed"
                window = 4
                s = max(1, line - window)
                e = min(len(src_lines), line + window)
                if s > len(src_lines):
                    continue
                snippet = "\n".join(f"{i:>5}\t{src_lines[i - 1]}" for i in range(s, e + 1))
                block = f"--- {rel}:{line} (lines {s}-{e}, approximate window - {reason}) ---\n{snippet}"

            # Hop 2: a compact symbol map of what this file directly depends
            # on, not a guessed specific function - see docstring above.
            deps = sorted(self._dep_forward.get(rel, set()))[:2]
            for dep_rel in deps:
                dep_path = self.root / dep_rel
                try:
                    dep_source = dep_path.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue
                dep_symbols = extract_python_symbols(dep_source)
                if dep_symbols is None:
                    continue
                dep_lines = [f"    (dependency: {dep_rel})"]
                for ln, label in dep_symbols["entries"]:
                    dep_lines.append(f"      {ln}: {label}")
                if not dep_symbols["entries"]:
                    dep_lines.append("      (no functions or classes)")
                block += "\n" + "\n".join(dep_lines)
            blocks.append(block)

        if not blocks:
            return output
        return output + "\n\nRelevant source (auto-extracted from the error above):\n\n" + "\n\n".join(blocks)

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
            # No -v: measured directly (not assumed) that verbose mode adds a
            # PASSED line per test that conveys nothing beyond what the final
            # "N passed" summary count already says, while every failure still
            # gets its full traceback and name in the FAILURES section either
            # way - -v costs real, scaling tokens for zero information gain
            # about what actually needs fixing.
            cmd = ["python3", "-m", "pytest", str(scope), "--tb=short"]
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
        truncated = output[-6000:]
        if proc.returncode != 0:
            truncated = self._enrich_with_error_context(truncated)
        return ToolResult(text=f"(exit code {proc.returncode})\n{truncated}")
