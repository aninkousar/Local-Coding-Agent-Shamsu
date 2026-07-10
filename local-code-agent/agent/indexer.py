from __future__ import annotations
import ast
import json
import re
import sqlite3
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
        self.conn = sqlite3.connect(str(db_path))
        self._ensure_schema()

    def _ensure_schema(self):
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

            cur = self.conn.execute(
                "SELECT MAX(mtime) FROM chunks WHERE path = ?", (rel,)
            )
            row = cur.fetchone()
            if row and row[0] is not None and row[0] >= mtime:
                continue  # unchanged since last index

            self.conn.execute("DELETE FROM chunks WHERE path = ?", (rel,))
            chunks = _smart_chunk_file(f, chunk_lines, overlap)
            if not chunks:
                continue
            texts = [c[2] for c in chunks]
            embeddings = self.client.embed(texts)
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
        q_emb = np.array(self.client.embed([query])[0], dtype=np.float32)
        if q_emb.size == 0:
            return []
        q_norm = q_emb / (np.linalg.norm(q_emb) + 1e-8)

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
        n = self.conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        files = self.conn.execute("SELECT COUNT(DISTINCT path) FROM chunks").fetchone()[0]
        return {"chunks": n, "files": files}
