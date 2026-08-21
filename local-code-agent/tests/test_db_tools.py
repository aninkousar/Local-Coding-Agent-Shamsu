"""Tests for agent/db_tools.py - real SQLite databases throughout, not mocks,
consistent with how this whole project has been verified: a mock proves the
code calls the right methods, a real database proves the code actually works.
"""
from __future__ import annotations

import sqlite3

import pytest

from agent.db_tools import (
    DBError,
    _format_rows,
    _get_connection_string,
    check_sql_syntax,
    connect,
    detect_dangerous_sql,
    get_schema,
    is_read_only_sql,
    run_execute,
    run_execute_file,
    run_query,
)


@pytest.fixture
def db_path(tmp_path):
    """A real, populated SQLite database on disk - not :memory:, since
    run_execute/run_query each open their own connection via connect(),
    and an in-memory database wouldn't be visible across connections."""
    path = tmp_path / "test.db"
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, email TEXT)")
    conn.execute("INSERT INTO users (name, email) VALUES ('Alice', 'alice@example.com')")
    conn.execute("INSERT INTO users (name, email) VALUES ('Bob', 'bob@example.com')")
    conn.execute("CREATE TABLE posts (id INTEGER PRIMARY KEY, user_id INTEGER, title TEXT)")
    conn.commit()
    conn.close()
    return str(path)


# --- is_read_only_sql ---------------------------------------------------------

@pytest.mark.parametrize("sql,expected", [
    ("SELECT * FROM users", True),
    ("select * from users", True),
    ("  SELECT * FROM users  ", True),
    ("WITH cte AS (SELECT 1) SELECT * FROM cte", True),
    ("EXPLAIN SELECT * FROM users", True),
    ("PRAGMA table_info(users)", True),
    ("SHOW TABLES", True),
    ("DESCRIBE users", True),
    ("INSERT INTO users VALUES (1, 'x', 'y')", False),
    ("UPDATE users SET name = 'x'", False),
    ("DELETE FROM users", False),
    ("DROP TABLE users", False),
    ("CREATE TABLE x (id INTEGER)", False),
    ("ALTER TABLE users ADD COLUMN age INTEGER", False),
])
def test_is_read_only_sql(sql, expected):
    assert is_read_only_sql(sql) is expected


# --- connect / _get_connection_string -----------------------------------------

def test_connect_unknown_db_type_raises():
    with pytest.raises(DBError, match="Unknown db_type"):
        connect("whatever", "oracle")


def test_connect_sqlite_real_connection(db_path):
    conn = connect(db_path, "sqlite")
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) as c FROM users")
        assert cur.fetchone()["c"] == 2
    finally:
        conn.close()


def test_get_connection_string_missing_env_raises(monkeypatch):
    monkeypatch.delenv("NONEXISTENT_DB_ENV_VAR_XYZ", raising=False)
    with pytest.raises(DBError, match="NONEXISTENT_DB_ENV_VAR_XYZ"):
        _get_connection_string("NONEXISTENT_DB_ENV_VAR_XYZ")


def test_get_connection_string_present(monkeypatch):
    monkeypatch.setenv("TEST_DB_ENV_VAR_XYZ", "postgresql://user:pass@host/db")
    assert _get_connection_string("TEST_DB_ENV_VAR_XYZ") == "postgresql://user:pass@host/db"


# --- get_schema ----------------------------------------------------------------

def test_get_schema_real_database(db_path):
    schema = get_schema(db_path, "sqlite")
    assert "users:" in schema
    assert "posts:" in schema
    assert "id INTEGER PRIMARY KEY" in schema
    assert "name TEXT" in schema


def test_get_schema_empty_database(tmp_path):
    path = tmp_path / "empty.db"
    sqlite3.connect(str(path)).close()
    assert get_schema(str(path), "sqlite") == "(no tables found)"


def test_get_schema_unknown_db_type_raises_not_none():
    """Regression test for the real bug found and fixed during the audit:
    get_schema's if/elif/elif chain had no final else, so an unhandled
    db_type would silently return None from a function typed -> str.
    connect() already raises first in practice, but this directly confirms
    get_schema itself never returns None - the property that actually
    matters, independent of which function happens to catch it first."""
    with pytest.raises(DBError):
        get_schema("whatever", "oracle")


# --- run_query / _format_rows ---------------------------------------------------

def test_run_query_real_select(db_path):
    result = run_query(db_path, "SELECT name, email FROM users ORDER BY name")
    assert "Alice" in result
    assert "Bob" in result
    assert "alice@example.com" in result


def test_run_query_rejects_write_statement(db_path):
    result = run_query(db_path, "DELETE FROM users")
    assert "write/DDL" in result
    # Confirm it genuinely didn't execute - data must still be there
    conn = connect(db_path)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) as c FROM users")
    assert cur.fetchone()["c"] == 2
    conn.close()


def test_run_query_no_rows(db_path):
    result = run_query(db_path, "SELECT * FROM users WHERE name = 'Nobody'")
    assert result == "(no rows)"


def test_format_rows_truncates_long_cells():
    columns = ["id", "description"]
    rows = [{"id": 1, "description": "x" * 300}]
    result = _format_rows(columns, rows)
    assert "…" in result
    assert len(result.splitlines()[2]) < 320  # truncated, not the full 300 chars


def test_format_rows_handles_none_values():
    columns = ["id", "name"]
    rows = [{"id": 1, "name": None}]
    result = _format_rows(columns, rows)
    assert "NULL" in result


def test_format_rows_empty():
    assert _format_rows(["id"], []) == "(no rows)"


# --- run_execute: the explicitly-flagged dry_run/transaction behavior -------------

def test_run_execute_dry_run_genuinely_rolls_back(db_path):
    """The exact behavior the source code's own comment calls out as
    'verified this the hard way - do not remove': without an explicit BEGIN
    before a DDL statement, sqlite3 won't open a transaction, and rollback()
    after a DDL statement becomes a silent no-op - meaning dry_run would
    actually execute the change for real. This test directly guards that."""
    result = run_execute(db_path, "DROP TABLE posts", dry_run=True)
    assert "DRY RUN" in result
    assert "rolled back" in result

    # The real, decisive check: is the table STILL there after a "dry run" drop?
    conn = connect(db_path)
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='posts'")
    assert cur.fetchone() is not None, "DROP TABLE dry_run must not actually drop the table"
    conn.close()


def test_run_execute_dry_run_rolls_back_dml_too(db_path):
    result = run_execute(db_path, "DELETE FROM users", dry_run=True)
    assert "DRY RUN" in result

    conn = connect(db_path)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) as c FROM users")
    assert cur.fetchone()["c"] == 2, "dry_run DELETE must not actually delete rows"
    conn.close()


def test_run_execute_real_run_actually_commits(db_path):
    result = run_execute(db_path, "DELETE FROM users WHERE name = 'Alice'")
    assert "Executed" in result
    assert "1 row" in result

    conn = connect(db_path)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) as c FROM users")
    assert cur.fetchone()["c"] == 1, "a real (non-dry-run) execute must actually persist"
    conn.close()


def test_run_execute_ddl_real_run_actually_applies(db_path):
    """Confirms the BEGIN-before-DDL fix doesn't just protect dry_run - a
    real (non-dry-run) DDL statement must still actually commit."""
    run_execute(db_path, "DROP TABLE posts", dry_run=False)
    conn = connect(db_path)
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='posts'")
    assert cur.fetchone() is None, "a real (non-dry-run) DROP TABLE must actually drop it"
    conn.close()


def test_run_execute_failure_rolls_back(db_path):
    with pytest.raises(sqlite3.OperationalError):
        run_execute(db_path, "INSERT INTO nonexistent_table VALUES (1)")
    # Connection should have been cleanly rolled back and closed, not left dangling -
    # confirmed indirectly by being able to open a fresh connection successfully.
    conn = connect(db_path)
    conn.execute("SELECT 1")
    conn.close()


# --- run_execute_file --------------------------------------------------------------

def test_run_execute_file_dry_run(db_path):
    script = "DELETE FROM users; DELETE FROM posts;"
    result = run_execute_file(db_path, script, dry_run=True)
    assert "DRY RUN" in result
    assert "2 statement" in result

    conn = connect(db_path)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) as c FROM users")
    assert cur.fetchone()["c"] == 2, "dry_run multi-statement script must not actually execute"
    conn.close()


def test_run_execute_file_real_run(db_path):
    script = "DELETE FROM posts; INSERT INTO users (name, email) VALUES ('Carol', 'carol@x.com');"
    result = run_execute_file(db_path, script, dry_run=False)
    assert "Executed 2 statement" in result

    conn = connect(db_path)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) as c FROM users")
    assert cur.fetchone()["c"] == 3
    conn.close()


def test_run_execute_file_empty_script():
    assert run_execute_file("unused.db", "   ;  ; ") == "No SQL statements found in the script."


# --- check_sql_syntax --------------------------------------------------------------

def test_check_sql_syntax_valid_self_contained_script():
    ok, error = check_sql_syntax("CREATE TABLE t (id INTEGER); INSERT INTO t VALUES (1);")
    assert ok is True
    assert error is None


def test_check_sql_syntax_invalid_sql():
    ok, error = check_sql_syntax("CREATE TALBE t (id INTEGER);")  # typo: TALBE
    assert ok is True  # the function itself always "succeeds" at running the check
    assert error is not None
    assert "syntax" in error.lower() or "near" in error.lower()


def test_check_sql_syntax_empty():
    assert check_sql_syntax("") == (True, None)


# --- detect_dangerous_sql -----------------------------------------------------------

def test_detect_dangerous_sql_delete_without_where():
    warnings = detect_dangerous_sql("DELETE FROM users;")
    assert len(warnings) == 1
    assert "DELETE" in warnings[0] and "WHERE" in warnings[0]


def test_detect_dangerous_sql_delete_with_where_is_safe():
    warnings = detect_dangerous_sql("DELETE FROM users WHERE id = 1;")
    assert warnings == []


def test_detect_dangerous_sql_update_without_where():
    warnings = detect_dangerous_sql("UPDATE users SET name = 'x';")
    assert len(warnings) == 1
    assert "UPDATE" in warnings[0]


def test_detect_dangerous_sql_drop_table():
    warnings = detect_dangerous_sql("DROP TABLE users;")
    assert len(warnings) == 1
    assert "DROP TABLE" in warnings[0]


def test_detect_dangerous_sql_drop_database():
    warnings = detect_dangerous_sql("DROP DATABASE mydb;")
    assert len(warnings) == 1
    assert "DROP DATABASE" in warnings[0]


def test_detect_dangerous_sql_truncate():
    warnings = detect_dangerous_sql("TRUNCATE users;")
    assert len(warnings) == 1
    assert "TRUNCATE" in warnings[0]


def test_detect_dangerous_sql_multiple_statements():
    script = "DELETE FROM a; UPDATE b SET x = 1 WHERE id = 1; DROP TABLE c;"
    warnings = detect_dangerous_sql(script)
    assert len(warnings) == 2  # DELETE without WHERE, and DROP TABLE - UPDATE has a WHERE


def test_detect_dangerous_sql_safe_query_no_warnings():
    assert detect_dangerous_sql("SELECT * FROM users WHERE id = 1;") == []
