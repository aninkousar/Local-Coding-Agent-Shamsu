from __future__ import annotations
import difflib
from pathlib import Path
from rich.console import Console
from rich.syntax import Syntax

console = Console()


def make_unified_diff(old_text: str, new_text: str, filename: str) -> str:
    old_lines = old_text.splitlines(keepends=True)
    new_lines = new_text.splitlines(keepends=True)
    diff = difflib.unified_diff(
        old_lines, new_lines,
        fromfile=f"a/{filename}", tofile=f"b/{filename}",
    )
    return "".join(diff)


def render_diff(diff_text: str) -> None:
    if not diff_text.strip():
        console.print("[dim](no changes)[/dim]")
        return
    syntax = Syntax(diff_text, "diff", theme="ansi_dark", word_wrap=True)
    console.print(syntax)


def apply_edit(path: Path, new_text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(new_text, encoding="utf-8")
