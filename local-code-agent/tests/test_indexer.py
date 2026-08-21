"""Tests for the AST-based project-intelligence functions in agent/indexer.py:
extract_python_symbols, find_containing_symbol, build_symbol_table, and
build_dependency_graph. All pure/deterministic given source text or a real
directory tree - tested against real files on disk via tmp_path, not
synthetic ASTs, so a test failure means the actual behavior changed, not
just that a mock's expectations drifted from reality.
"""
from __future__ import annotations

from agent.indexer import (
    build_dependency_graph,
    build_symbol_table,
    extract_python_symbols,
    find_containing_symbol,
)


# --- extract_python_symbols --------------------------------------------------------

def test_extract_python_symbols_basic():
    source = '''"""Module docstring summary line."""

def simple(a, b):
    pass

class Foo:
    def method(self):
        pass
'''
    result = extract_python_symbols(source)
    assert result is not None
    assert result["summary"] == "Module docstring summary line."
    labels = [label for _, label in result["entries"]]
    assert "def simple(a, b)" in labels
    assert "class Foo" in labels
    assert "def method(self)" in labels


def test_extract_python_symbols_syntax_error_returns_none():
    assert extract_python_symbols("def broken(:\n    pass") is None


def test_extract_python_symbols_no_docstring_summary_is_none():
    result = extract_python_symbols("def f():\n    pass\n")
    assert result["summary"] is None


def test_extract_python_symbols_signatures_include_type_hints():
    source = "def typed(user: str, password: str) -> dict:\n    pass\n"
    result = extract_python_symbols(source)
    label = result["entries"][0][1]
    assert label == "def typed(user: str, password: str) -> dict"


def test_extract_python_symbols_signature_with_defaults_and_varargs():
    source = "def mixed(a, b: str = 'x', *args: int, c: bool, **kwargs: str) -> list:\n    pass\n"
    result = extract_python_symbols(source)
    label = result["entries"][0][1]
    assert label == "def mixed(a, b: str = 'x', *args: int, c: bool, **kwargs: str) -> list"


def test_extract_python_symbols_async_function_prefix():
    result = extract_python_symbols("async def fetch(url: str) -> dict:\n    pass\n")
    assert result["entries"][0][1].startswith("async def fetch")


def test_extract_python_symbols_module_level_variables_only():
    """Module-level variables are captured; variables local to a function
    body must NOT be, since they aren't meaningful project structure."""
    source = '''
CONFIG_VALUE = 42

def f():
    local_var = "not project structure"
    return local_var
'''
    result = extract_python_symbols(source)
    var_names = [name for _, name in result["variables"]]
    assert "CONFIG_VALUE" in var_names
    assert "local_var" not in var_names


def test_extract_python_symbols_annotated_module_variable():
    result = extract_python_symbols("PORT: int = 8080\n")
    var_names = [name for _, name in result["variables"]]
    assert "PORT" in var_names


def test_extract_python_symbols_entries_sorted_by_line():
    source = "class B:\n    pass\n\ndef a():\n    pass\n"
    result = extract_python_symbols(source)
    lines = [ln for ln, _ in result["entries"]]
    assert lines == sorted(lines)


# --- find_containing_symbol --------------------------------------------------------

def test_find_containing_symbol_inside_function():
    source = "def outer():\n    x = 1\n    y = 2\n    return x + y\n"
    result = find_containing_symbol(source, line=3)
    assert result is not None
    kind, name, start, end = result
    assert kind == "function"
    assert name == "outer"


def test_find_containing_symbol_innermost_wins():
    """A line inside a method inside a class is contained by both - the
    method (smaller range) must be returned, not the class."""
    source = '''class Foo:
    def method(self):
        x = 1
        return x
'''
    result = find_containing_symbol(source, line=3)
    kind, name, _, _ = result
    assert kind == "function"
    assert name == "method"


def test_find_containing_symbol_module_level_line_returns_none():
    source = "x = 1\ny = 2\n"
    assert find_containing_symbol(source, line=1) is None


def test_find_containing_symbol_syntax_error_returns_none():
    assert find_containing_symbol("def broken(:\n", line=1) is None


def test_find_containing_symbol_line_outside_any_range():
    source = "def f():\n    pass\n\n\n\n\n\nx = 1\n"
    assert find_containing_symbol(source, line=8) is None


# --- build_symbol_table -------------------------------------------------------------

def test_build_symbol_table_finds_definitions(tmp_path):
    (tmp_path / "auth.py").write_text("def login(user, password):\n    pass\n\nclass SessionManager:\n    pass\n")
    table = build_symbol_table(tmp_path, ignore_dirs=set())
    assert table["login"] == [("auth.py", 1, "function")]
    assert table["SessionManager"] == [("auth.py", 4, "class")]


def test_build_symbol_table_honestly_discloses_name_collisions(tmp_path):
    (tmp_path / "auth.py").write_text("def login(user):\n    pass\n")
    (tmp_path / "other.py").write_text("def login(admin):\n    pass\n")
    table = build_symbol_table(tmp_path, ignore_dirs=set())
    assert len(table["login"]) == 2
    files = {rel for rel, _, _ in table["login"]}
    assert files == {"auth.py", "other.py"}


def test_build_symbol_table_respects_ignore_dirs(tmp_path):
    (tmp_path / "real.py").write_text("def real_func():\n    pass\n")
    ignored_dir = tmp_path / "vendor"
    ignored_dir.mkdir()
    (ignored_dir / "third_party.py").write_text("def vendored_func():\n    pass\n")

    table = build_symbol_table(tmp_path, ignore_dirs={"vendor"})
    assert "real_func" in table
    assert "vendored_func" not in table


def test_build_symbol_table_skips_files_with_syntax_errors(tmp_path):
    (tmp_path / "broken.py").write_text("def broken(:\n")
    (tmp_path / "fine.py").write_text("def fine():\n    pass\n")
    table = build_symbol_table(tmp_path, ignore_dirs=set())
    assert "fine" in table
    assert "broken" not in table  # no crash, just skipped


def test_build_symbol_table_empty_project(tmp_path):
    assert build_symbol_table(tmp_path, ignore_dirs=set()) == {}


def test_build_symbol_table_respects_max_file_size(tmp_path):
    big_file = tmp_path / "huge.py"
    big_file.write_text("def huge_func():\n    pass\n" + "# padding\n" * 2000)
    assert big_file.stat().st_size > 1024  # confirm the file is genuinely over 1KB
    table = build_symbol_table(tmp_path, ignore_dirs=set(), max_file_kb=1)
    assert "huge_func" not in table


# --- build_dependency_graph ----------------------------------------------------------

def test_build_dependency_graph_simple_import(tmp_path):
    (tmp_path / "main.py").write_text("import utils\n")
    (tmp_path / "utils.py").write_text("def helper():\n    pass\n")

    forward, reverse = build_dependency_graph(tmp_path, ignore_dirs=set())
    assert "utils.py" in forward.get("main.py", set())
    assert "main.py" in reverse.get("utils.py", set())


def test_build_dependency_graph_from_import(tmp_path):
    (tmp_path / "main.py").write_text("from utils import helper\n")
    (tmp_path / "utils.py").write_text("def helper():\n    pass\n")

    forward, _ = build_dependency_graph(tmp_path, ignore_dirs=set())
    assert "utils.py" in forward.get("main.py", set())


def test_build_dependency_graph_package_import(tmp_path):
    pkg = tmp_path / "mypkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "helpers.py").write_text("def h():\n    pass\n")
    (tmp_path / "main.py").write_text("from mypkg.helpers import h\n")

    forward, _ = build_dependency_graph(tmp_path, ignore_dirs=set())
    assert "mypkg/helpers.py" in forward.get("main.py", set())


def test_build_dependency_graph_no_imports_means_no_entry(tmp_path):
    (tmp_path / "standalone.py").write_text("def f():\n    pass\n")
    forward, _ = build_dependency_graph(tmp_path, ignore_dirs=set())
    assert "standalone.py" not in forward


def test_build_dependency_graph_external_import_ignored(tmp_path):
    """An import of a real stdlib/third-party module (not resolvable within
    the project) should not appear in the dependency graph at all - only
    project-internal files are tracked."""
    (tmp_path / "main.py").write_text("import os\nimport json\n")
    forward, _ = build_dependency_graph(tmp_path, ignore_dirs=set())
    # main.py has imports, but none resolve to project files, so no entry (or an empty one)
    assert not forward.get("main.py")


def test_build_dependency_graph_reverse_correctly_inverted(tmp_path):
    (tmp_path / "a.py").write_text("import shared\n")
    (tmp_path / "b.py").write_text("import shared\n")
    (tmp_path / "shared.py").write_text("X = 1\n")

    forward, reverse = build_dependency_graph(tmp_path, ignore_dirs=set())
    assert reverse["shared.py"] == {"a.py", "b.py"}


def test_build_dependency_graph_syntax_error_file_skipped_not_crashed(tmp_path):
    (tmp_path / "broken.py").write_text("import utils\ndef broken(:\n")
    (tmp_path / "utils.py").write_text("X = 1\n")
    # must not raise
    forward, reverse = build_dependency_graph(tmp_path, ignore_dirs=set())
    assert isinstance(forward, dict)
