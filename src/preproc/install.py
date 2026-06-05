"""Install applier for the preprocessor framework.

Applies SQL DDL files from sql/install/ in sorted filename order, then deploys
the MASTER Lua script from sql/lua/master.lua into PREPROC_RT.

Path resolution
---------------
The SQL and Lua sources live at the repository root under sql/ and are NOT
shipped inside the Python wheel.  The path is resolved relative to this module
file's location:  <repo>/src/preproc/install.py → ../../..  → <repo>.

This works correctly for:
  - Development installs (``uv pip install -e .``)
  - Direct invocations from the repo tree

If the package is ever moved out of the repository (e.g. installed from PyPI),
the sql/ directory must be included as package data and the path logic updated.

Autocommit note
---------------
pyexasol opens connections with autocommit=True by default.  This is preserved
here.  Each DDL statement is committed by the server immediately.  If explicit
transaction control is needed later, set autocommit=False on the connection and
add an explicit connection.commit() call at the end of run_install().

Tolerate-exists contract
------------------------
A SQL statement whose preceding uncommented line is ``--@tolerate-exists`` is
allowed to fail with an Exasol "already exists" error (error code 42505 or the
message substring "already exists").  All other errors propagate immediately.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).parent.parent.parent
_SQL_INSTALL_DIR = _REPO_ROOT / "sql" / "install"
_MASTER_LUA_PATH = _REPO_ROOT / "sql" / "lua" / "master.lua"

_TOLERATE_MARKER = "--@tolerate-exists"
_ALREADY_EXISTS_MSG = "already exists"


def _strip_line_comments(sql: str) -> str:
    """Remove -- line comments while preserving newlines for line-count stability."""
    return re.sub(r"--[^\n]*", "", sql)


def _split_statements(sql_text: str) -> list[tuple[bool, str]]:
    """Split a SQL file into (tolerate_exists, statement) pairs.

    The tolerate flag is True when the --@tolerate-exists marker appears on
    the last non-empty line before the statement's opening token.

    Strategy:
      1. Walk the raw lines to track the --@tolerate-exists marker.
      2. Split on semicolons (safe per DDL contract: no semicolons inside
         literals, comments, or Lua bodies in these files).
      3. Associate each resulting statement with the flag active at the time
         the first non-whitespace token of that statement appeared.
    """
    results: list[tuple[bool, str]] = []
    lines = sql_text.splitlines(keepends=True)

    # Build a per-character flag array: is char i tolerate-flagged?
    # We walk lines; when a --@tolerate-exists line is seen, the next
    # non-empty non-comment content line's statement gets the flag.
    pending_tolerate = False
    char_tolerate: list[bool] = []
    for line in lines:
        stripped = line.strip()
        if stripped == _TOLERATE_MARKER:
            pending_tolerate = True
            char_tolerate.extend([False] * len(line))
            continue
        if stripped and not stripped.startswith("--"):
            # This line has real SQL content; assign the pending flag.
            char_tolerate.extend([pending_tolerate] * len(line))
            pending_tolerate = False
        else:
            char_tolerate.extend([False] * len(line))

    full_text = "".join(lines)
    # Split on semicolons; include position tracking.
    parts = full_text.split(";")
    pos = 0
    for part in parts:
        part_len = len(part)
        # Determine tolerate flag: use the flag of the first non-whitespace char.
        flag = False
        for i, ch in enumerate(part):
            if not ch.isspace():
                abs_idx = pos + i
                if abs_idx < len(char_tolerate):
                    flag = char_tolerate[abs_idx]
                break
        content = re.sub(r"--[^\n]*", "", part).strip()
        if content:
            results.append((flag, part.strip()))
        pos += part_len + 1  # +1 for the semicolon itself

    return results


def _is_already_exists_error(exc: Exception) -> bool:
    """Return True if exc represents an Exasol 'object already exists' error."""
    msg = str(exc).lower()
    return _ALREADY_EXISTS_MSG in msg


def _execute_statement(connection: Any, statement: str, tolerate: bool, verbose: bool) -> None:
    """Execute one SQL statement, honouring the tolerate-exists flag."""
    if verbose:
        preview = statement[:120].replace("\n", " ")
        print(f"  [SQL] {preview}", file=sys.stdout)
    try:
        connection.execute(statement)
    except Exception as exc:
        if tolerate and _is_already_exists_error(exc):
            if verbose:
                print(f"  [SKIP] tolerated: {exc}", file=sys.stdout)
            return
        raise


def _apply_sql_file(connection: Any, path: Path, verbose: bool) -> None:
    """Read and apply all statements from a single .sql file."""
    if verbose:
        print(f"Applying {path.name} ...", file=sys.stdout)
    sql_text = path.read_text(encoding="utf-8")
    for tolerate, statement in _split_statements(sql_text):
        _execute_statement(connection, statement, tolerate, verbose)


def _deploy_master(connection: Any, verbose: bool) -> None:
    """Deploy the MASTER Lua script into PREPROC_RT.

    Reads the Lua body from sql/lua/master.lua and wraps it in a
    CREATE OR REPLACE LUA SCRIPT statement executed via pyexasol.

    pyexasol sends the bare CREATE statement without a trailing '/' delimiter
    (that delimiter is only needed for interactive SQL clients).

    An OPEN SCHEMA PREPROC_RT is issued first because Exasol requires an
    active schema context when compiling a script that references schema objects.
    """
    if not _MASTER_LUA_PATH.exists():
        raise FileNotFoundError(
            f"master.lua not found at {_MASTER_LUA_PATH}. "
            "Ensure the sql/lua/master.lua file has been created (task 3.1)."
        )

    lua_body = _MASTER_LUA_PATH.read_text(encoding="utf-8")

    if verbose:
        print("Deploying MASTER Lua script into PREPROC_RT ...", file=sys.stdout)

    connection.execute("OPEN SCHEMA PREPROC_RT")
    create_script = f"CREATE OR REPLACE LUA SCRIPT PREPROC_RT.MASTER() AS\n{lua_body}"
    connection.execute(create_script)


def _print_activation_hint() -> None:
    """Print the activation statement and explain that a DBA must run it."""
    print(
        "\nInstallation complete.",
        file=sys.stdout,
    )
    print(
        "To activate the preprocessor, a DBA must run the following statement:",
        file=sys.stdout,
    )
    print(
        "\n    ALTER SYSTEM SET sql_preprocessor_script = PREPROC_RT.MASTER;\n",
        file=sys.stdout,
    )
    print(
        "This statement is intentionally NOT executed by this tool — "
        "activating the preprocessor is a system-wide change and must be "
        "a deliberate DBA decision.",
        file=sys.stdout,
    )


def run_install(connection: Any, verbose: bool = False) -> None:
    """Apply the full two-schema layout and deploy MASTER.

    Steps:
      1. Apply all .sql files under sql/install/ in sorted filename order.
         Each file is split into statements on ';'.  Statements preceded by
         --@tolerate-exists tolerate "already exists" errors.
      2. Deploy MASTER: open PREPROC_RT schema, execute CREATE OR REPLACE LUA
         SCRIPT PREPROC_RT.MASTER with the body from sql/lua/master.lua.
      3. Print (do NOT execute) the activation ALTER SYSTEM statement.

    This function is idempotent: re-running against an already-installed
    database completes successfully and changes nothing.

    Args:
        connection: An open pyexasol connection.
        verbose: When True, print each SQL statement before execution.
    """
    sql_files = sorted(_SQL_INSTALL_DIR.glob("*.sql"))
    if not sql_files:
        raise FileNotFoundError(f"No .sql files found in {_SQL_INSTALL_DIR}")

    for sql_file in sql_files:
        _apply_sql_file(connection, sql_file, verbose)

    _deploy_master(connection, verbose)
    _print_activation_hint()
