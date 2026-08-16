from __future__ import annotations
import ast
import re
import sqlite3
import threading
from pathlib import Path

import numpy as np

from .ollama_client import OllamaClient

TEXT_LIKE_EXTS = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".c", ".cpp", ".h", ".hpp",
    ".cs", ".go", ".rs", ".rb", ".php", ".swift", ".kt", ".scala", ".sh",
    ".html", ".css", ".scss", ".sql", ".yaml", ".yml", ".json", ".md",
    ".txt", ".toml", ".ini", ".cfg", ".dockerfile", ".vue",
}

# Extensions where a lightweight regex-based "does this line start a function/class"
# heuristic is worth trying before falling back to blind fixed-line chunking.
HEURISTIC_STRUCTURE_EXTS = {
    ".js", ".ts", ".tsx", ".jsx", ".java", ".c", ".cpp", ".h", ".hpp",
    ".cs", ".go", ".rs", ".rb", ".php", ".swift", ".kt", ".scala",
}

_STRUCTURE_MARKERS = [
    re.compile(r'^\s*(export\s+)?(default\s+)?(async\s+)?function\b'),          # JS/TS
    re.compile(r'^\s*(export\s+)?(default\s+)?(abstract\s+)?class\s+\w+'),      # JS/TS/Java/C#/PHP
    re.compile(r'^\s*(export\s+)?interface\s+\w+'),                             # TS
    re.compile(r'^\s*(public|private|protected|internal|static|final|virtual|'
               r'override|async|def)\s+.*\)\s*\{?\s*$'),                        # Java/C#/C++-ish method
    re.compile(r'^func\s+'),                                                    # Go
    re.compile(r'^\s*(pub\s+)?(async\s+)?fn\s+\w+'),                            # Rust
    re.compile(r'^\s*(pub\s+)?struct\s+\w+'),                                   # Rust/Go
    re.compile(r'^\s*def\s+\w+'),                                               # Ruby
    re.compile(r'^\s*(public\s+|private\s+|protected\s+|static\s+)*function\s+\w+'),  # PHP
]

# Hard ceiling on a single structure-aware chunk, in case markers are sparse
# (e.g. one giant function, or minified code) - keeps any one chunk from
# ballooning and dominating an embedding/search result.
_MAX_STRUCTURED_CHUNK_LINES = 250


def _iter_source_files(root: Path, ignore_dirs: set[str], max_kb: int):
    for p in root.rglob("*"):
        if p.is_dir():
            continue
        if any(part in ignore_dirs for part in p.parts):
            continue
        if p.suffix.lower() not in TEXT_LIKE_EXTS and p.name.lower() != "dockerfile":
            continue
        try:
            if p.stat().st_size > max_kb * 1024:
                continue
        except OSError:
            continue
        yield p


def build_file_index_text(root: Path, ignore_dirs: set[str], max_entries: int = 150) -> str:
    """The "File Index": a cheap, cached listing of what files exist and where -
    structure only, never file content. This is deliberately separate from the
    semantic CodebaseIndex below ("Project Knowledge") - this answers "what files
    exist and where", that answers "what's actually in them, relevant to this
    query". Both matter, but conflating them would mean re-running expensive
    embedding search just to answer "does a tests/ folder exist".
    """
    paths = []
    for p in root.rglob("*"):
        if p.is_dir():
            if any(part in ignore_dirs for part in p.parts):
                continue
            continue
        if any(part in ignore_dirs for part in p.parts):
            continue
        try:
            paths.append(str(p.relative_to(root)))
        except ValueError:
            continue
        if len(paths) > max_entries * 3:  # stop scanning a huge tree early
            break

    paths.sort()
    truncated = len(paths) > max_entries
    shown = paths[:max_entries]
    text = "\n".join(shown)
    if truncated:
        text += f"\n... ({len(paths)} files total, showing first {max_entries} - use list_directory or search_codebase for more)"
    return text or "(no files found)"


_PY_IMPORT_RE = re.compile(r"^\s*(?:from\s+([\w.]+)\s+import|import\s+([\w.]+))", re.MULTILINE)
_JS_IMPORT_RE = re.compile(r"""(?:import\s+.*?\s+from\s+|require\()\s*['"]([^'"]+)['"]""")


def _resolve_python_import(module: str, importing_file: Path, root: Path) -> Path | None:
    """Best-effort: only resolves imports that map to an actual file in this
    project (relative imports, or a top-level package/module name that matches a
    file/folder at the project root) - stdlib and third-party imports correctly
    resolve to nothing, since we only care about internal dependencies here."""
    parts = module.split(".")
    candidates = [
        root.joinpath(*parts).with_suffix(".py"),
        root.joinpath(*parts, "__init__.py"),
        importing_file.parent.joinpath(*parts).with_suffix(".py"),
    ]
    for c in candidates:
        if c.is_file():
            return c
    return None


def _resolve_js_import(spec: str, importing_file: Path, root: Path) -> Path | None:
    """Only resolves relative imports ('./x', '../x') to actual files - bare
    specifiers ('react', 'lodash') are npm packages, not project files."""
    if not spec.startswith("."):
        return None
    base = (importing_file.parent / spec).resolve()
    for candidate in (base, base.with_suffix(".js"), base.with_suffix(".ts"),
                      base.with_suffix(".jsx"), base.with_suffix(".tsx"),
                      base / "index.js", base / "index.ts"):
        if candidate.is_file():
            return candidate
    return None


def _assignment_target_names(target: ast.expr) -> list[str]:
    """Extracts variable name(s) from an assignment target. Handles simple
    names (x = 1) and tuple/list unpacking (x, y = 1, 2), recursively.
    Deliberately returns nothing for Attribute (obj.attr = x) or Subscript
    (d[key] = x) targets - those mutate an existing object, they don't
    define a new variable."""
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, (ast.Tuple, ast.List)):
        names = []
        for elt in target.elts:
            names.extend(_assignment_target_names(elt))
        return names
    return []


def _format_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    """Reconstructs a function/method's signature as a string, including
    parameter and return type annotations and default values if present -
    the cheap, AST-based approximation of an LSP's "type(symbol)": whatever
    type hints already exist in the source get shown, rather than performing
    genuine type inference (which would need an actual type checker like
    mypy/pyright - out of scope, see the LSP evaluation this followed).
    Never raises - falls back to a bare "(...)" placeholder on any failure,
    since a signature-formatting hiccup should never break list_symbols.
    """
    try:
        args = node.args
        parts = []
        positional = args.posonlyargs + args.args
        defaults_offset = len(positional) - len(args.defaults)
        for i, a in enumerate(positional):
            piece = a.arg
            if a.annotation is not None:
                piece += f": {ast.unparse(a.annotation)}"
            if i >= defaults_offset:
                piece += f" = {ast.unparse(args.defaults[i - defaults_offset])}"
            parts.append(piece)
        if args.vararg:
            piece = f"*{args.vararg.arg}"
            if args.vararg.annotation is not None:
                piece += f": {ast.unparse(args.vararg.annotation)}"
            parts.append(piece)
        elif args.kwonlyargs:
            parts.append("*")
        for i, a in enumerate(args.kwonlyargs):
            piece = a.arg
            if a.annotation is not None:
                piece += f": {ast.unparse(a.annotation)}"
            default = args.kw_defaults[i]
            if default is not None:
                piece += f" = {ast.unparse(default)}"
            parts.append(piece)
        if args.kwarg:
            piece = f"**{args.kwarg.arg}"
            if args.kwarg.annotation is not None:
                piece += f": {ast.unparse(args.kwarg.annotation)}"
            parts.append(piece)
        sig = f"({', '.join(parts)})"
        if node.returns is not None:
            sig += f" -> {ast.unparse(node.returns)}"
        return sig
    except Exception:
        return "(...)"


def extract_python_symbols(source: str) -> dict | None:
    """Extracts a lightweight structural summary from Python source via AST:
    a one-line docstring summary, classes/functions/methods with line
    numbers, and module-level variables. Returns None on a syntax error.

    Shared by list_symbols (agent/tools.py) and dependency-graph-based
    Project Knowledge (agent/context_manager.py) so both stay consistent and
    there's exactly one AST-walking implementation to maintain, not two that
    could quietly drift apart from each other over time.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None

    docstring = ast.get_docstring(tree)
    summary = docstring.strip().splitlines()[0][:200] if docstring else None

    entries: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            entries.append((node.lineno, f"class {node.name}"))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
            entries.append((node.lineno, f"{prefix} {node.name}{_format_signature(node)}"))
    entries.sort(key=lambda t: t[0])

    # Module-level variables only - iterating tree.body directly, NOT
    # ast.walk(), so local variables inside function bodies are never
    # captured as if they were meaningful project structure.
    variables: list[tuple[int, str]] = []
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                for name in _assignment_target_names(target):
                    variables.append((stmt.lineno, name))
        elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            variables.append((stmt.lineno, stmt.target.id))
    variables.sort(key=lambda t: t[0])

    return {"summary": summary, "entries": entries, "variables": variables}


def find_containing_symbol(source: str, line: int) -> tuple[str, str, int, int] | None:
    """Finds the innermost function/method/class containing a given line
    number - the precise link from an error's file:line reference straight to
    the exact code it happened in, without the model needing to guess
    boundaries. Returns (kind, name, start_line, end_line), or None if the
    line isn't inside any function/class (e.g. module-level code) or the
    source doesn't parse.

    "Innermost" matters for correctness: a line inside a method inside a
    class is contained by BOTH the method and the class, and the method (the
    smaller range) is the more useful, specific answer.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    best: tuple[str, str, int, int] | None = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            start = node.lineno
            end = getattr(node, "end_lineno", None) or node.lineno
            if start <= line <= end:
                if best is None or (end - start) < (best[3] - best[2]):
                    kind = "class" if isinstance(node, ast.ClassDef) else "function"
                    best = (kind, node.name, start, end)
    return best


def build_symbol_table(root: Path, ignore_dirs: set[str], max_file_kb: int = 512) -> dict[str, list[tuple[str, int, str]]]:
    """Builds a project-wide symbol table: name -> [(rel_path, line, kind), ...]
    - the cheap, deterministic approximation of an LSP's "go to definition",
    scoped to Python only (where this project's AST tooling goes deepest).
    Built once during reindex, alongside the dependency graph, then queried
    instantly by find_definition rather than re-parsing the whole project on
    every lookup.

    Honest limitation, stated plainly: this is a name-based index, not a real
    semantic resolution - it can't distinguish two different classes that
    happen to define a same-named method, or correctly handle shadowing the
    way a real language server would. It answers "where is a symbol named X
    defined" across the project, not "where is THIS specific X, in THIS
    specific scope, actually defined" - a real, useful, but bounded amount
    of value for near-zero infrastructure cost.
    """
    table: dict[str, list[tuple[str, int, str]]] = {}
    for py_file in root.rglob("*.py"):
        if any(part in ignore_dirs for part in py_file.parts):
            continue
        try:
            if py_file.stat().st_size > max_file_kb * 1024:
                continue
            source = py_file.read_text(encoding="utf-8", errors="ignore")
            tree = ast.parse(source)
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue
        rel = str(py_file.relative_to(root))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                table.setdefault(node.name, []).append((rel, node.lineno, "class"))
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                table.setdefault(node.name, []).append((rel, node.lineno, "function"))
    return table


def build_dependency_graph(root: Path, ignore_dirs: set[str], max_file_kb: int = 512):
    """Static-analysis dependency graph - Python (via `ast`, accurate) and JS/TS
    (via regex, same honest limitation as this project's other JS heuristics).
    No LLM calls, same cheap/cached lifecycle as the File Index. Returns
    (forward, reverse): forward[file] = set of files it imports; reverse[file] =
    set of files that import it - the second is usually the more actionable
    question ("what would break if I change this") for a coding agent.
    """
    forward: dict[str, set[str]] = {}
    for f in _iter_source_files(root, ignore_dirs, max_file_kb):
        rel = str(f.relative_to(root))
        deps: set[str] = set()
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue

        if f.suffix.lower() == ".py":
            try:
                tree = ast.parse(text)
            except SyntaxError:
                tree = None
            if tree is not None:
                for node in ast.walk(tree):
                    if isinstance(node, ast.ImportFrom) and node.module:
                        resolved = _resolve_python_import(node.module, f, root)
                        if resolved:
                            deps.add(str(resolved.relative_to(root)))
                    elif isinstance(node, ast.Import):
                        for alias in node.names:
                            resolved = _resolve_python_import(alias.name, f, root)
                            if resolved:
                                deps.add(str(resolved.relative_to(root)))
        elif f.suffix.lower() in (".js", ".ts", ".jsx", ".tsx"):
            for spec in _JS_IMPORT_RE.findall(text):
                resolved = _resolve_js_import(spec, f, root)
                if resolved:
                    try:
                        deps.add(str(resolved.relative_to(root)))
                    except ValueError:
                        pass

        if deps:
            forward[rel] = deps

    reverse: dict[str, set[str]] = {}
    for src, targets in forward.items():
        for t in targets:
            reverse.setdefault(t, set()).add(src)

    return forward, reverse


def _chunk_file_fixed(path: Path, chunk_lines: int, overlap: int):
    """Blind fixed-size line-window chunking. Used as the fallback for anything
    the structure-aware chunkers below don't recognize or can't parse."""
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return
    lines = text.splitlines()
    if not lines:
        return
    step = max(1, chunk_lines - overlap)
    for start in range(0, len(lines), step):
        end = min(start + chunk_lines, len(lines))
        chunk = "\n".join(lines[start:end])
        if chunk.strip():
            yield start + 1, end, chunk
        if end == len(lines):
            break


def _chunk_python_ast(path: Path):
    """Chunk a Python file by top-level function/class definitions using the stdlib
    `ast` module - each chunk is one complete, semantically meaningful unit instead
    of an arbitrary line window. Returns None (signalling "fall back") if the file
    doesn't parse or has no top-level def/class structure to key off of.
    """
    try:
        source = path.read_text(encoding="utf-8", errors="ignore")
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError):
        return None

    lines = source.splitlines()
    if not lines:
        return None

    top_level = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
    if not top_level:
        return None  # e.g. a plain script with no def/class - let the fixed-line chunker handle it

    chunks = []
    first_start = top_level[0].lineno
    if first_start > 1:
        preamble = "\n".join(lines[:first_start - 1]).strip()
        if preamble:
            chunks.append((1, first_start - 1, preamble))

    for node in top_level:
        start = node.lineno
        end = getattr(node, "end_lineno", None) or start
        segment = "\n".join(lines[start - 1:end])
        if segment.strip():
            chunks.append((start, end, segment))

    return chunks


def _chunk_by_markers(path: Path, chunk_lines: int):
    """Regex-heuristic structure-aware chunking for non-Python languages: split at
    lines that look like a function/class/method definition. Not a real parser, so
    it can misfire on unusual formatting - falls back to fixed-line chunking (via
    returning None) if no markers are found at all.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    lines = text.splitlines()
    if not lines:
        return None

    marker_idxs = [i for i, line in enumerate(lines) if any(p.match(line) for p in _STRUCTURE_MARKERS)]
    if not marker_idxs:
        return None

    cap = max(chunk_lines, _MAX_STRUCTURED_CHUNK_LINES)
    chunks = []
    if marker_idxs[0] > 0:
        pre = "\n".join(lines[:marker_idxs[0]]).strip()
        if pre:
            chunks.append((1, marker_idxs[0], pre))

    for idx, start in enumerate(marker_idxs):
        natural_end = marker_idxs[idx + 1] if idx + 1 < len(marker_idxs) else len(lines)
        end = min(natural_end, start + cap)
        segment = "\n".join(lines[start:end])
        if segment.strip():
            chunks.append((start + 1, end, segment))

    return chunks


def _smart_chunk_file(path: Path, chunk_lines: int, overlap: int):
    """Dispatch to the best available chunker for this file type, falling back to
    blind fixed-line chunking for anything without recognizable code structure
    (plain text, JSON/YAML/config, markup, or a parse failure)."""
    ext = path.suffix.lower()
    if ext == ".py":
        result = _chunk_python_ast(path)
        if result is not None:
            return result
    elif ext in HEURISTIC_STRUCTURE_EXTS:
        result = _chunk_by_markers(path, chunk_lines)
        if result is not None:
            return result
    return list(_chunk_file_fixed(path, chunk_lines, overlap))


class CodebaseIndex:
    def __init__(self, db_path: Path, client: OllamaClient):
        self.db_path = db_path
        self.client = client
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False + an explicit lock: the CLI is single-threaded so this
        # is a no-op there, but the GUI's Flask server handles requests on a thread pool,
        # and a bare sqlite3 connection isn't safe to touch from more than one thread.
        self.conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._lock = threading.Lock()
        self._ensure_schema()

    def _ensure_schema(self):
        with self._lock:
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS chunks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    path TEXT NOT NULL,
                    start_line INTEGER,
                    end_line INTEGER,
                    content TEXT,
                    embedding BLOB,
                    mtime REAL
                )
            """)
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_path ON chunks(path)")
            self.conn.commit()

    def build(self, root: Path, ignore_dirs: set[str], chunk_lines: int,
              overlap: int, max_file_kb: int, progress_cb=None) -> int:
        """(Re)index every file under root whose mtime changed. Returns #chunks written."""
        files = list(_iter_source_files(root, ignore_dirs, max_file_kb))
        written = 0
        for i, f in enumerate(files):
            if progress_cb:
                progress_cb(i + 1, len(files), f)
            mtime = f.stat().st_mtime
            rel = str(f.relative_to(root))

            with self._lock:
                cur = self.conn.execute(
                    "SELECT MAX(mtime) FROM chunks WHERE path = ?", (rel,)
                )
                row = cur.fetchone()
            if row and row[0] is not None and row[0] >= mtime:
                continue  # unchanged since last index

            chunks = _smart_chunk_file(f, chunk_lines, overlap)
            if not chunks:
                with self._lock:
                    self.conn.execute("DELETE FROM chunks WHERE path = ?", (rel,))
                    self.conn.commit()
                continue

            texts = [c[2] for c in chunks]
            embeddings = self.client.embed(texts)  # network call - deliberately outside the lock

            with self._lock:
                self.conn.execute("DELETE FROM chunks WHERE path = ?", (rel,))
                for (start, end, content), emb in zip(chunks, embeddings):
                    vec = np.array(emb, dtype=np.float32).tobytes()
                    self.conn.execute(
                        "INSERT INTO chunks (path, start_line, end_line, content, embedding, mtime) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (rel, start, end, content, vec, mtime),
                    )
                    written += 1
                self.conn.commit()
        return written

    def search(self, query: str, top_k: int = 8) -> list[dict]:
        q_emb = np.array(self.client.embed([query])[0], dtype=np.float32)  # network call, outside the lock
        if q_emb.size == 0:
            return []
        q_norm = q_emb / (np.linalg.norm(q_emb) + 1e-8)

        with self._lock:
            rows = self.conn.execute(
                "SELECT path, start_line, end_line, content, embedding FROM chunks"
            ).fetchall()

        scored = []
        for path, start, end, content, emb_blob in rows:
            vec = np.frombuffer(emb_blob, dtype=np.float32)
            if vec.size != q_emb.size:
                continue
            v_norm = vec / (np.linalg.norm(vec) + 1e-8)
            score = float(np.dot(q_norm, v_norm))
            scored.append({
                "path": path, "start_line": start, "end_line": end,
                "content": content, "score": score,
            })
        scored.sort(key=lambda x: x["score"], reverse=True)
        return scored[:top_k]

    def stats(self) -> dict:
        with self._lock:
            n = self.conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
            files = self.conn.execute("SELECT COUNT(DISTINCT path) FROM chunks").fetchone()[0]
        return {"chunks": n, "files": files}
