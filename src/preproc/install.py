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
allowed to fail when the error message contains "already exists" or "conflicts
with another user or role".  Detection is by message substring only — Exasol
phrases duplicate-object errors differently per object type, so no single error
code covers all cases.  All other errors propagate immediately.
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


def _split_statements(sql_text: str) -> list[tuple[bool, str]]:
    """Split a SQL file into (tolerate_exists, statement) pairs.

    Line-based: ``--`` comments are stripped BEFORE semicolon splitting, so
    comments may freely contain semicolons. The DDL contract still forbids
    semicolons and ``--`` sequences inside string literals (none of the
    install files need them).

    The tolerate flag is True when the ``--@tolerate-exists`` marker line
    appears before the statement's first content line.
    """
    results: list[tuple[bool, str]] = []
    pending_tolerate = False  # marker seen, waiting for its statement
    buf: list[str] = []  # content lines of the statement in progress
    flag = False  # tolerate flag of the statement in progress

    def _flush() -> None:
        nonlocal buf
        stmt = "\n".join(buf).strip()
        buf = []
        if stmt:
            results.append((flag, stmt))

    for line in sql_text.splitlines():
        if line.strip() == _TOLERATE_MARKER:
            pending_tolerate = True
            continue
        code = re.sub(r"--.*", "", line).rstrip()
        if not code.strip():
            continue
        if not buf:
            # First content line of a new statement claims the pending marker.
            flag = pending_tolerate
            pending_tolerate = False
        # A line may close one statement and open the next (rare but legal).
        while ";" in code:
            head, code = code.split(";", 1)
            buf.append(head)
            _flush()
            flag = False  # a same-line follow-up statement has no marker
        if code.strip():
            buf.append(code)
    _flush()  # trailing statement without a terminating semicolon

    return results


def _is_already_exists_error(exc: Exception) -> bool:
    """Return True if exc represents an Exasol 'object already exists' error.

    Detection is by message-substring matching only — not by error code.
    Exasol phrases duplicate-object errors differently per object type
    (e.g. "already exists" for tables/schemas/scripts, "conflicts with another
    user or role" for roles/users), so a single code cannot cover all cases.
    Substring matching is intentionally narrow: it applies only to statements
    explicitly marked --@tolerate-exists, which bounds the risk of accidentally
    swallowing unrelated errors.
    """
    msg = str(exc).lower()
    return _ALREADY_EXISTS_MSG in msg or "conflicts with another user or role" in msg


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
            "Ensure the sql/lua/master.lua file exists in the repository."
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
         Each file is split using a comment-aware line-based splitter: ``--``
         comments are stripped before semicolon splitting, so comments may
         freely contain semicolons.  Statements preceded by --@tolerate-exists
         tolerate "already exists" (and equivalent) errors.
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
