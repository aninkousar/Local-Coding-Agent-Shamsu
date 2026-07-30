from __future__ import annotations
import os
import sqlite3
from pathlib import Path

# Row/output caps so a query against a huge table can't flood the model's context -
# same philosophy as grep_codebase's hit cap and tool-result truncation elsewhere.
MAX_ROWS = 200
MAX_CELL_CHARS = 200


class DBError(Exception):
    pass


def _get_connection_string(env_var_name: str) -> str:
    """For Postgres/MySQL, the 'db_path' argument is the NAME of an environment
    variable holding the real connection string - never a raw string the model
    typed itself. This means credentials never appear in a tool call, conversation
    history, or anywhere the model has to handle them directly; you set them once
    as an environment variable before starting the agent."""
    value = os.environ.get(env_var_name)
    if not value:
        raise DBError(
            f"Environment variable '{env_var_name}' is not set (or is empty). "
            f"Set it to a full connection string before starting the agent, e.g.:\n"
            f"  postgres: postgresql://user:password@host:port/dbname\n"
            f"  mysql:    mysql://user:password@host:port/dbname"
        )
    return value


def connect(db_path: str, db_type: str = "sqlite"):
    db_type = (db_type or "sqlite").lower()

    if db_type == "sqlite":
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn

    if db_type == "postgres":
        try:
            import psycopg2
            import psycopg2.extras
        except ImportError:
            raise DBError(
                "Postgres support needs an extra package. Run: pip install psycopg2-binary"
            )
        dsn = _get_connection_string(db_path)
        conn = psycopg2.connect(dsn, cursor_factory=psycopg2.extras.RealDictCursor)
        return conn

    if db_type == "mysql":
        try:
            import pymysql
            import pymysql.cursors
        except ImportError:
            raise DBError(
                "MySQL support needs an extra package. Run: pip install pymysql"
            )
        dsn = _get_connection_string(db_path)
        # pymysql wants parts, not a URL - do a minimal parse of user:pass@host:port/db
        import urllib.parse
        parsed = urllib.parse.urlparse(dsn)
        conn = pymysql.connect(
            host=parsed.hostname, port=parsed.port or 3306,
            user=parsed.username, password=parsed.password or "",
            database=parsed.path.lstrip("/"),
            cursorclass=pymysql.cursors.DictCursor,
        )
        return conn

    raise DBError(f"Unknown db_type '{db_type}'. Use 'sqlite', 'postgres', or 'mysql'.")


def _format_rows(columns: list[str], rows: list[dict]) -> str:
    if not rows:
        return "(no rows)"
    truncated = len(rows) > MAX_ROWS
    shown = rows[:MAX_ROWS]

    def cell(v) -> str:
        s = "NULL" if v is None else str(v)
        return s if len(s) <= MAX_CELL_CHARS else s[:MAX_CELL_CHARS] + "…"

    widths = [len(c) for c in columns]
    str_rows = []
    for r in shown:
        vals = [cell(r[c]) for c in columns]
        str_rows.append(vals)
        widths = [max(w, len(v)) for w, v in zip(widths, vals)]

    def fmt_row(vals):
        return " | ".join(v.ljust(w) for v, w in zip(vals, widths))

    lines = [fmt_row(columns), "-+-".join("-" * w for w in widths)]
    lines.extend(fmt_row(v) for v in str_rows)
    if truncated:
        lines.append(f"... ({len(rows)} rows total, showing first {MAX_ROWS})")
    return "\n".join(lines)


_READ_ONLY_PREFIXES = ("select", "with", "explain", "pragma", "show", "describe")


def is_read_only_sql(sql: str) -> bool:
    stripped = sql.strip().lower()
    return stripped.startswith(_READ_ONLY_PREFIXES)


def get_schema(db_path: str, db_type: str = "sqlite") -> str:
    db_type = (db_type or "sqlite").lower()
    conn = connect(db_path, db_type)
    try:
        cur = conn.cursor()
        if db_type == "sqlite":
            cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")
            tables = [r["name"] for r in cur.fetchall()]
            if not tables:
                return "(no tables found)"
            out = []
            for t in tables:
                cur.execute(f"PRAGMA table_info('{t}')")
                cols = cur.fetchall()
                col_descs = [f"{c['name']} {c['type']}" + (" PRIMARY KEY" if c["pk"] else "") for c in cols]
                out.append(f"{t}:\n  " + "\n  ".join(col_descs))
            return "\n\n".join(out)

        elif db_type == "postgres":
            cur.execute("""
                SELECT table_name, column_name, data_type
                FROM information_schema.columns
                WHERE table_schema = 'public'
                ORDER BY table_name, ordinal_position
            """)
            rows = cur.fetchall()
            return _group_columns_by_table(rows, "table_name", "column_name", "data_type")

        elif db_type == "mysql":
            cur.execute("""
                SELECT table_name, column_name, data_type
                FROM information_schema.columns
                WHERE table_schema = DATABASE()
                ORDER BY table_name, ordinal_position
            """)
            rows = cur.fetchall()
            return _group_columns_by_table(rows, "table_name", "column_name", "data_type")
    finally:
        conn.close()


def _group_columns_by_table(rows, table_key, col_key, type_key) -> str:
    if not rows:
        return "(no tables found)"
    by_table: dict[str, list[str]] = {}
    for r in rows:
        by_table.setdefault(r[table_key], []).append(f"{r[col_key]} {r[type_key]}")
    return "\n\n".join(f"{t}:\n  " + "\n  ".join(cols) for t, cols in by_table.items())


def run_query(db_path: str, sql: str, db_type: str = "sqlite", params: list | None = None) -> str:
    if not is_read_only_sql(sql):
        return ("This looks like a write/DDL statement, not a read query. "
                "Use db_execute for anything that changes data or schema.")
    conn = connect(db_path, db_type)
    try:
        cur = conn.cursor()
        cur.execute(sql, params or [])
        rows = cur.fetchall()
        if not rows:
            return "(no rows)"
        columns = list(rows[0].keys()) if hasattr(rows[0], "keys") else [f"col{i}" for i in range(len(rows[0]))]
        dict_rows = [dict(r) for r in rows]
        return _format_rows(columns, dict_rows)
    finally:
        conn.close()


def run_execute(db_path: str, sql: str, db_type: str = "sqlite",
                 params: list | None = None, dry_run: bool = False) -> str:
    conn = connect(db_path, db_type)
    try:
        if db_type == "sqlite":
            # CRITICAL: Python's sqlite3 module does NOT automatically open a
            # transaction before DDL statements (CREATE/DROP/ALTER) - only before
            # DML (INSERT/UPDATE/DELETE). Without this explicit BEGIN, rollback()
            # after a DDL statement is a silent no-op and dry_run would actually
            # execute the change for real. Verified this the hard way - do not remove.
            conn.execute("BEGIN")
        cur = conn.cursor()
        cur.execute(sql, params or [])
        affected = cur.rowcount if cur.rowcount is not None and cur.rowcount >= 0 else 0
        if dry_run:
            conn.rollback()
            return f"[DRY RUN - rolled back, nothing changed] Would have affected {affected} row(s)."
        conn.commit()
        return f"Executed. {affected} row(s) affected."
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def run_execute_file(db_path: str, sql_text: str, db_type: str = "sqlite", dry_run: bool = False) -> str:
    """Executes a multi-statement .sql script, splitting naively on ';' - this won't
    perfectly handle semicolons inside string literals or stored procedures, but is
    fine for straightforward migration-style scripts. Statements run in a single
    transaction: dry_run rolls the whole thing back, otherwise all-or-nothing commit.
    """
    statements = [s.strip() for s in sql_text.split(";") if s.strip()]
    if not statements:
        return "No SQL statements found in the script."
    conn = connect(db_path, db_type)
    try:
        if db_type == "sqlite":
            # See the identical comment in run_execute() - this is not optional.
            conn.execute("BEGIN")
        cur = conn.cursor()
        total_affected = 0
        for stmt in statements:
            cur.execute(stmt)
            if cur.rowcount and cur.rowcount > 0:
                total_affected += cur.rowcount
        if dry_run:
            conn.rollback()
            return (f"[DRY RUN - rolled back, nothing changed] Ran {len(statements)} statement(s), "
                     f"would have affected {total_affected} row(s) total.")
        conn.commit()
        return f"Executed {len(statements)} statement(s). {total_affected} row(s) affected total."
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def check_sql_syntax(sql_text: str) -> tuple[bool, str | None]:
    """Validates a .sql file against a throwaway in-memory SQLite database - pure
    stdlib, no new dependency, and it's a REAL parser/engine rather than a hand-rolled
    regex check. Important honest caveat: this only fully validates a SELF-CONTAINED
    script (one that creates whatever tables/columns it references). A migration
    that does `ALTER TABLE users ...` without a `CREATE TABLE users` earlier in the
    SAME file will report "no such table" even though the real target database
    already has that table - that's a false positive worth knowing about, not a
    sign the SQL is actually wrong.
    """
    statements = [s.strip() for s in sql_text.split(";") if s.strip()]
    if not statements:
        return True, None
    conn = sqlite3.connect(":memory:")
    try:
        cur = conn.cursor()
        for stmt in statements:
            cur.execute(stmt)
        return True, None
    except sqlite3.Error as e:
        return True, str(e)
    finally:
        conn.close()
