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
_DEMO_PING_LUA_PATH = _REPO_ROOT / "sql" / "lua" / "demo_ping_v1.lua"
_DEMO_TRANSLATE_LUA_PATH = _REPO_ROOT / "sql" / "lua" / "demo_translate_v1.lua"
_DEMO_AUDIT_LUA_PATH = _REPO_ROOT / "sql" / "lua" / "demo_audit_v1.lua"
_DEMO_SQLGLOT_UDF_PATH = _REPO_ROOT / "sql" / "udf" / "demo_sqlglot_transpile_v1.sql"
_REFRESH_LUA_PATH = _REPO_ROOT / "sql" / "lua" / "refresh.lua"
_STATUS_LUA_PATH = _REPO_ROOT / "sql" / "lua" / "status.lua"
_REFRESH_CORE_LUA_PATH = _REPO_ROOT / "sql" / "lua" / "refresh_core.lua"
_RULE_ADD_LUA_PATH = _REPO_ROOT / "sql" / "lua" / "rule_add.lua"
_RULE_DROP_LUA_PATH = _REPO_ROOT / "sql" / "lua" / "rule_drop.lua"
_RULE_ENABLE_LUA_PATH = _REPO_ROOT / "sql" / "lua" / "rule_enable.lua"
_RULE_DISABLE_LUA_PATH = _REPO_ROOT / "sql" / "lua" / "rule_disable.lua"
_RULE_LIST_LUA_PATH = _REPO_ROOT / "sql" / "lua" / "rule_list.lua"
_SET_MODE_LUA_PATH = _REPO_ROOT / "sql" / "lua" / "set_mode.lua"
_SET_SELF_DISABLE_LUA_PATH = _REPO_ROOT / "sql" / "lua" / "set_self_disable.lua"
_CONTROL_SUGAR_LUA_PATH = _REPO_ROOT / "sql" / "lua" / "control_sugar_v1.lua"

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
    """Deploy the MASTER dispatcher and all demo modules into PREPROC_RT.

    Reads the Lua bodies from sql/lua/master.lua, sql/lua/demo_ping_v1.lua,
    sql/lua/demo_translate_v1.lua, and sql/lua/demo_audit_v1.lua, wrapping each
    in a CREATE OR REPLACE LUA SCRIPT statement executed via pyexasol.

    Also deploys the companion Python3 scalar UDF
    PREPROC_RT.DEMO_SQLGLOT_TRANSPILE_V1 from sql/udf/demo_sqlglot_transpile_v1.sql.
    The UDF file contains a complete CREATE OR REPLACE PYTHON3 SCALAR SCRIPT
    statement terminated with a bare '/' line (interactive-client convention);
    that trailing '/' is stripped before execution.

    All demo scripts are deployed under their versioned names (e.g.
    PREPROC_RT.DEMO_PING_V1, never DEMO_PING) so a live module is never the
    target of an in-place replace.

    pyexasol sends the bare CREATE statement without a trailing '/' delimiter
    (that delimiter is only needed for interactive SQL clients).

    An OPEN SCHEMA PREPROC_RT is issued first because Exasol requires an
    active schema context when compiling a script that references schema objects.
    """
    for path, label in [
        (_MASTER_LUA_PATH, "master.lua"),
        (_DEMO_PING_LUA_PATH, "demo_ping_v1.lua"),
        (_DEMO_TRANSLATE_LUA_PATH, "demo_translate_v1.lua"),
        (_DEMO_AUDIT_LUA_PATH, "demo_audit_v1.lua"),
        (_DEMO_SQLGLOT_UDF_PATH, "demo_sqlglot_transpile_v1.sql"),
        (_CONTROL_SUGAR_LUA_PATH, "control_sugar_v1.lua"),
    ]:
        if not path.exists():
            raise FileNotFoundError(
                f"{label} not found at {path}. Ensure the file exists in the repository."
            )

    lua_body = _MASTER_LUA_PATH.read_text(encoding="utf-8")
    demo_ping_body = _DEMO_PING_LUA_PATH.read_text(encoding="utf-8")
    demo_translate_body = _DEMO_TRANSLATE_LUA_PATH.read_text(encoding="utf-8")
    demo_audit_body = _DEMO_AUDIT_LUA_PATH.read_text(encoding="utf-8")
    control_sugar_body = _CONTROL_SUGAR_LUA_PATH.read_text(encoding="utf-8")

    # Read the UDF SQL file and strip the trailing '/' terminator line used by
    # interactive SQL clients — pyexasol does not accept it.
    udf_sql_raw = _DEMO_SQLGLOT_UDF_PATH.read_text(encoding="utf-8")
    udf_sql = "\n".join(line for line in udf_sql_raw.splitlines() if line.strip() != "/").strip()

    if verbose:
        print(
            "Deploying MASTER, DEMO_PING_V1, DEMO_TRANSLATE_V1, DEMO_AUDIT_V1, "
            "CONTROL_SUGAR_V1 Lua scripts and DEMO_SQLGLOT_TRANSPILE_V1 UDF into PREPROC_RT ...",
            file=sys.stdout,
        )

    connection.execute("OPEN SCHEMA PREPROC_RT")
    create_script = f"CREATE OR REPLACE LUA SCRIPT PREPROC_RT.MASTER() AS\n{lua_body}"
    connection.execute(create_script)
    create_demo_ping = (
        f"CREATE OR REPLACE LUA SCRIPT PREPROC_RT.DEMO_PING_V1() AS\n{demo_ping_body}"
    )
    connection.execute(create_demo_ping)
    create_demo_translate = (
        f"CREATE OR REPLACE LUA SCRIPT PREPROC_RT.DEMO_TRANSLATE_V1() AS\n{demo_translate_body}"
    )
    connection.execute(create_demo_translate)
    create_demo_audit = (
        f"CREATE OR REPLACE LUA SCRIPT PREPROC_RT.DEMO_AUDIT_V1() AS\n{demo_audit_body}"
    )
    connection.execute(create_demo_audit)
    connection.execute(udf_sql)
    create_control_sugar = (
        f"CREATE OR REPLACE LUA SCRIPT PREPROC_RT.CONTROL_SUGAR_V1() AS\n{control_sugar_body}"
    )
    connection.execute(create_control_sugar)


def _deploy_admin_scripts(connection: Any, verbose: bool) -> None:
    """Deploy the admin Lua scripts into PREPROC.

    Deploys in this order (REFRESH_CORE must come first — it is an importable
    module used by refresh.lua and the mutating admin scripts):
      1. PREPROC.REFRESH_CORE  — importable module (no RETURNS TABLE)
      2. PREPROC.REFRESH       — RETURNS TABLE
      3. PREPROC.STATUS        — RETURNS TABLE
      4. PREPROC.RULE_ADD      — RETURNS TABLE
      5. PREPROC.RULE_DROP     — RETURNS TABLE
      6. PREPROC.RULE_ENABLE   — RETURNS TABLE
      7. PREPROC.RULE_DISABLE  — RETURNS TABLE
      8. PREPROC.RULE_LIST     — RETURNS TABLE
      9. PREPROC.SET_MODE      — RETURNS TABLE
     10. PREPROC.SET_SELF_DISABLE — RETURNS TABLE

    pyexasol sends the bare CREATE statement without a trailing '/' delimiter
    (that delimiter is only needed for interactive SQL clients).

    An OPEN SCHEMA PREPROC is issued first because Exasol requires an active
    schema context when compiling a script that references schema objects.

    Note on bare-name parameters: Exasol Lua script parameters are declared as
    bare names with no SQL type annotations. Type-annotated syntax is rejected
    by the engine.
    """
    required = [
        (_REFRESH_CORE_LUA_PATH, "refresh_core.lua"),
        (_REFRESH_LUA_PATH, "refresh.lua"),
        (_STATUS_LUA_PATH, "status.lua"),
        (_RULE_ADD_LUA_PATH, "rule_add.lua"),
        (_RULE_DROP_LUA_PATH, "rule_drop.lua"),
        (_RULE_ENABLE_LUA_PATH, "rule_enable.lua"),
        (_RULE_DISABLE_LUA_PATH, "rule_disable.lua"),
        (_RULE_LIST_LUA_PATH, "rule_list.lua"),
        (_SET_MODE_LUA_PATH, "set_mode.lua"),
        (_SET_SELF_DISABLE_LUA_PATH, "set_self_disable.lua"),
    ]
    for path, label in required:
        if not path.exists():
            raise FileNotFoundError(
                f"{label} not found at {path}. "
                f"Ensure the sql/lua/{label} file exists in the repository."
            )

    refresh_core_body = _REFRESH_CORE_LUA_PATH.read_text(encoding="utf-8")
    refresh_body = _REFRESH_LUA_PATH.read_text(encoding="utf-8")
    status_body = _STATUS_LUA_PATH.read_text(encoding="utf-8")
    rule_add_body = _RULE_ADD_LUA_PATH.read_text(encoding="utf-8")
    rule_drop_body = _RULE_DROP_LUA_PATH.read_text(encoding="utf-8")
    rule_enable_body = _RULE_ENABLE_LUA_PATH.read_text(encoding="utf-8")
    rule_disable_body = _RULE_DISABLE_LUA_PATH.read_text(encoding="utf-8")
    rule_list_body = _RULE_LIST_LUA_PATH.read_text(encoding="utf-8")
    set_mode_body = _SET_MODE_LUA_PATH.read_text(encoding="utf-8")
    set_self_disable_body = _SET_SELF_DISABLE_LUA_PATH.read_text(encoding="utf-8")

    if verbose:
        print(
            "Deploying REFRESH_CORE, REFRESH, STATUS, RULE_ADD, RULE_DROP, "
            "RULE_ENABLE, RULE_DISABLE, RULE_LIST, SET_MODE, SET_SELF_DISABLE "
            "Lua scripts into PREPROC ...",
            file=sys.stdout,
        )

    connection.execute("OPEN SCHEMA PREPROC")
    # REFRESH_CORE is an importable module — plain script, no RETURNS TABLE.
    connection.execute(
        f"CREATE OR REPLACE LUA SCRIPT PREPROC.REFRESH_CORE() AS\n{refresh_core_body}"
    )
    connection.execute(
        f"CREATE OR REPLACE LUA SCRIPT PREPROC.REFRESH() RETURNS TABLE AS\n{refresh_body}"
    )
    connection.execute(
        f"CREATE OR REPLACE LUA SCRIPT PREPROC.STATUS() RETURNS TABLE AS\n{status_body}"
    )
    connection.execute(
        f"CREATE OR REPLACE LUA SCRIPT PREPROC.RULE_ADD("
        f"scope_type, scope_value, phase, script_name, function_name, norefresh) "
        f"RETURNS TABLE AS\n{rule_add_body}"
    )
    connection.execute(
        f"CREATE OR REPLACE LUA SCRIPT PREPROC.RULE_DROP(rule_id, norefresh) "
        f"RETURNS TABLE AS\n{rule_drop_body}"
    )
    connection.execute(
        f"CREATE OR REPLACE LUA SCRIPT PREPROC.RULE_ENABLE(rule_id, norefresh) "
        f"RETURNS TABLE AS\n{rule_enable_body}"
    )
    connection.execute(
        f"CREATE OR REPLACE LUA SCRIPT PREPROC.RULE_DISABLE(rule_id, norefresh) "
        f"RETURNS TABLE AS\n{rule_disable_body}"
    )
    connection.execute(
        f"CREATE OR REPLACE LUA SCRIPT PREPROC.RULE_LIST() RETURNS TABLE AS\n{rule_list_body}"
    )
    connection.execute(
        f"CREATE OR REPLACE LUA SCRIPT PREPROC.SET_MODE(mode, norefresh) "
        f"RETURNS TABLE AS\n{set_mode_body}"
    )
    connection.execute(
        f"CREATE OR REPLACE LUA SCRIPT PREPROC.SET_SELF_DISABLE(value) "
        f"RETURNS TABLE AS\n{set_self_disable_body}"
    )


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
      2. Deploy MASTER and demo modules: open PREPROC_RT schema, execute:
           - CREATE OR REPLACE LUA SCRIPT PREPROC_RT.MASTER (sql/lua/master.lua)
           - CREATE OR REPLACE LUA SCRIPT PREPROC_RT.DEMO_PING_V1
             (sql/lua/demo_ping_v1.lua — canonical EXPAND command module)
           - CREATE OR REPLACE LUA SCRIPT PREPROC_RT.DEMO_TRANSLATE_V1
             (sql/lua/demo_translate_v1.lua — TRANSLATE-phase dialect transpiler)
           - CREATE OR REPLACE LUA SCRIPT PREPROC_RT.DEMO_AUDIT_V1
             (sql/lua/demo_audit_v1.lua — REWRITE-phase audit tagger)
           - CREATE OR REPLACE PYTHON3 SCALAR SCRIPT
             PREPROC_RT.DEMO_SQLGLOT_TRANSPILE_V1
             (sql/udf/demo_sqlglot_transpile_v1.sql — companion UDF for
             DEMO_TRANSLATE_V1; the file's trailing '/' terminator is stripped
             before execution).
      2b. Deploy admin scripts: open PREPROC schema, execute CREATE OR REPLACE
          LUA SCRIPT PREPROC.REFRESH and PREPROC.STATUS (both RETURNS TABLE)
          with the bodies from sql/lua/refresh.lua and sql/lua/status.lua.
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
    _deploy_admin_scripts(connection, verbose)
    _print_activation_hint()
