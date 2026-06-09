"""Offline bundle generator for the preprocessor framework.

Assembles a single ``deploy_all.sql`` file an operator can run end-to-end in an
interactive Exasol client (EXAplus / DbVisualiser). The bundle concatenates the
numbered install DDL with ``CREATE OR REPLACE`` wrappers for every Lua script
and the demo Python UDF, so deployment is one auditable file with no source
duplication.

Delimiter contract
------------------
Each statement is terminated by a ``/`` on its own line, never ``;``. Lua and
PYTHON3 script bodies contain semicolons, so a plain ``;``-split file would
break those statements. The ``/`` terminator is the documented runnable form.

Wrapper identity
---------------
The Lua and UDF wrappers are byte-identical to the ``CREATE OR REPLACE`` strings
``install.py`` sends, and the install DDL is split with the same comment-aware
splitter (``_split_statements``). The bundle and ``preproc install`` therefore
produce identical objects.

Scope
-----
The bundle is structure + code only. It MUST NOT include the activation
``ALTER SYSTEM`` statement (a deliberate DBA action) nor any demo seed data
(a separate seed script).

Path resolution mirrors ``install.py``:
``<repo>/src/preproc/bundle.py`` → ``../../..`` → ``<repo>``.

Output is deterministic: identical sources produce byte-for-byte identical
output, because install files are globbed in sorted filename order and every
other source is read in a fixed sequence.
"""

from __future__ import annotations

from pathlib import Path

from preproc.install import _split_statements

_REPO_ROOT = Path(__file__).parent.parent.parent
_SQL_INSTALL_DIR = _REPO_ROOT / "sql" / "install"
_MASTER_LUA_PATH = _REPO_ROOT / "sql" / "lua" / "master.lua"
_DEMO_PING_LUA_PATH = _REPO_ROOT / "sql" / "lua" / "demo_ping_v1.lua"
_DEMO_TRANSLATE_LUA_PATH = _REPO_ROOT / "sql" / "lua" / "demo_translate_v1.lua"
_DEMO_AUDIT_LUA_PATH = _REPO_ROOT / "sql" / "lua" / "demo_audit_v1.lua"
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
_DEMO_UDF_PATH = _REPO_ROOT / "sql" / "udf" / "demo_sqlglot_transpile_v1.sql"


def _read_source(path: Path, kind: str) -> str:
    """Read a required source file, raising a helpful error if it is missing."""
    if not path.exists():
        raise FileNotFoundError(
            f"{kind} not found at {path}. Ensure the {path.name} file exists in the repository."
        )
    return path.read_text(encoding="utf-8")


def _collect_install_statements(skip: frozenset[str] = frozenset()) -> list[str]:
    """Return install DDL statements in ascending filename order.

    Reuses install.py's comment-aware splitter so comments and trailing
    semicolons are stripped exactly as the CLI install applies them. Filenames
    in ``skip`` are omitted — used by the release bundle to drop the
    upgrade-only ``25_column_upgrades.sql`` (whose additive ALTERs error on a
    clean database where ``20_tables.sql`` already defines those columns).
    """
    sql_files = sorted(_SQL_INSTALL_DIR.glob("*.sql"))
    if not sql_files:
        raise FileNotFoundError(f"No .sql files found in {_SQL_INSTALL_DIR}")

    statements: list[str] = []
    for sql_file in sql_files:
        if sql_file.name in skip:
            continue
        sql_text = sql_file.read_text(encoding="utf-8")
        for _tolerate, statement in _split_statements(sql_text):
            statements.append(statement)
    return statements


def _script_statements() -> dict[str, str]:
    """Return every CREATE OR REPLACE script wrapper keyed by a short name.

    The Lua wrappers mirror install.py byte-for-byte. The UDF source already
    contains its full CREATE statement. ``run_bundle`` (full) and
    ``run_release_bundle`` (framework-only) each select and order from this map,
    so the wrapper text lives in exactly one place.
    """
    return {
        "MASTER": f"CREATE OR REPLACE LUA SCRIPT PREPROC_RT.MASTER() AS\n"
        f"{_read_source(_MASTER_LUA_PATH, 'master.lua')}",
        "DEMO_PING_V1": f"CREATE OR REPLACE LUA SCRIPT PREPROC_RT.DEMO_PING_V1() AS\n"
        f"{_read_source(_DEMO_PING_LUA_PATH, 'demo_ping_v1.lua')}",
        "DEMO_TRANSLATE_V1": f"CREATE OR REPLACE LUA SCRIPT PREPROC_RT.DEMO_TRANSLATE_V1() AS\n"
        f"{_read_source(_DEMO_TRANSLATE_LUA_PATH, 'demo_translate_v1.lua')}",
        "DEMO_AUDIT_V1": f"CREATE OR REPLACE LUA SCRIPT PREPROC_RT.DEMO_AUDIT_V1() AS\n"
        f"{_read_source(_DEMO_AUDIT_LUA_PATH, 'demo_audit_v1.lua')}",
        "CONTROL_SUGAR_V1": f"CREATE OR REPLACE LUA SCRIPT PREPROC_RT.CONTROL_SUGAR_V1() AS\n"
        f"{_read_source(_CONTROL_SUGAR_LUA_PATH, 'control_sugar_v1.lua')}",
        "REFRESH_CORE": f"CREATE OR REPLACE LUA SCRIPT PREPROC.REFRESH_CORE() AS\n"
        f"{_read_source(_REFRESH_CORE_LUA_PATH, 'refresh_core.lua')}",
        "REFRESH": f"CREATE OR REPLACE LUA SCRIPT PREPROC.REFRESH() RETURNS TABLE AS\n"
        f"{_read_source(_REFRESH_LUA_PATH, 'refresh.lua')}",
        "STATUS": f"CREATE OR REPLACE LUA SCRIPT PREPROC.STATUS() RETURNS TABLE AS\n"
        f"{_read_source(_STATUS_LUA_PATH, 'status.lua')}",
        "RULE_ADD": f"CREATE OR REPLACE LUA SCRIPT PREPROC.RULE_ADD("
        f"scope_type, scope_value, phase, script_name, function_name, norefresh) "
        f"RETURNS TABLE AS\n{_read_source(_RULE_ADD_LUA_PATH, 'rule_add.lua')}",
        "RULE_DROP": f"CREATE OR REPLACE LUA SCRIPT PREPROC.RULE_DROP(rule_id, norefresh) "
        f"RETURNS TABLE AS\n{_read_source(_RULE_DROP_LUA_PATH, 'rule_drop.lua')}",
        "RULE_ENABLE": f"CREATE OR REPLACE LUA SCRIPT PREPROC.RULE_ENABLE(rule_id, norefresh) "
        f"RETURNS TABLE AS\n{_read_source(_RULE_ENABLE_LUA_PATH, 'rule_enable.lua')}",
        "RULE_DISABLE": f"CREATE OR REPLACE LUA SCRIPT PREPROC.RULE_DISABLE(rule_id, norefresh) "
        f"RETURNS TABLE AS\n{_read_source(_RULE_DISABLE_LUA_PATH, 'rule_disable.lua')}",
        "RULE_LIST": f"CREATE OR REPLACE LUA SCRIPT PREPROC.RULE_LIST() RETURNS TABLE AS\n"
        f"{_read_source(_RULE_LIST_LUA_PATH, 'rule_list.lua')}",
        "SET_MODE": f"CREATE OR REPLACE LUA SCRIPT PREPROC.SET_MODE(mode, norefresh) "
        f"RETURNS TABLE AS\n{_read_source(_SET_MODE_LUA_PATH, 'set_mode.lua')}",
        "SET_SELF_DISABLE": f"CREATE OR REPLACE LUA SCRIPT PREPROC.SET_SELF_DISABLE(value) "
        f"RETURNS TABLE AS\n{_read_source(_SET_SELF_DISABLE_LUA_PATH, 'set_self_disable.lua')}",
        "DEMO_SQLGLOT_TRANSPILE_V1": _read_source(_DEMO_UDF_PATH, "demo_sqlglot_transpile_v1.sql"),
    }


# Full bundle: framework + demo modules + UDF (deployment order).
_FULL_SCRIPT_ORDER = [
    "MASTER",
    "DEMO_PING_V1",
    "DEMO_TRANSLATE_V1",
    "DEMO_AUDIT_V1",
    "CONTROL_SUGAR_V1",
    "REFRESH_CORE",
    "REFRESH",
    "STATUS",
    "RULE_ADD",
    "RULE_DROP",
    "RULE_ENABLE",
    "RULE_DISABLE",
    "RULE_LIST",
    "SET_MODE",
    "SET_SELF_DISABLE",
    "DEMO_SQLGLOT_TRANSPILE_V1",
]

# Release bundle: framework + control surface only — no demo modules, no UDF.
_RELEASE_SCRIPT_ORDER = [
    "MASTER",
    "CONTROL_SUGAR_V1",
    "REFRESH_CORE",
    "REFRESH",
    "STATUS",
    "RULE_ADD",
    "RULE_DROP",
    "RULE_ENABLE",
    "RULE_DISABLE",
    "RULE_LIST",
    "SET_MODE",
    "SET_SELF_DISABLE",
]

_RELEASE_HEADER = """\
-- =============================================================================
-- preprocessor-framework — release.sql   (single-file framework deploy)
-- =============================================================================
-- GENERATED by `preproc bundle --release`; built by CI on every push. Do not
-- hand-edit — change the sources under sql/ instead.
--
-- Deploys the COMPLETE framework into a CLEAN Exasol instance: the
-- PREPROC_ADMIN role, the PREPROC and PREPROC_RT schemas, all tables, the
-- MY_PIPELINE view, schema grants, the MASTER dispatcher, the REFRESH/STATUS
-- engine, the full rule/mode admin control surface, and the PREPROC command
-- sugar (CONTROL_SUGAR_V1). A final REFRESH resolves the seeded control-sugar
-- rule. Deploys NO demo/example preprocessors — those live in examples/.
--
-- Run the whole file in EXAplus or DbVisualiser; the `/` on its own line is the
-- statement delimiter (Lua bodies contain semicolons). Clean-install only:
-- CREATE ROLE/SCHEMA are unguarded and the upgrade-only column ALTERs are
-- omitted — use `preproc install` for idempotent re-runs / column upgrades.
-- Does NOT activate the preprocessor (a deliberate DBA action):
--   ALTER SYSTEM SET sql_preprocessor_script = PREPROC_RT.MASTER;
-- =============================================================================

"""


def _collect_script_statements() -> list[str]:
    """Return the full-bundle script wrappers in deployment order (incl. demos)."""
    scripts = _script_statements()
    return [scripts[name] for name in _FULL_SCRIPT_ORDER]


def _emit(statement: str) -> str:
    """Format one statement as its text, a bare ``/`` line, and a blank line.

    The generator owns the single ``/`` terminator, so any delimiter the source
    already carries is stripped first: a trailing ``;`` (the DDL terminator) or
    a trailing bare ``/`` line (some script sources, e.g. the demo UDF, ship one
    for direct EXAplus use). Stripping both keeps the output to exactly one
    terminator per statement and deterministic regardless of source convention.
    """
    text = statement.rstrip()
    if text.endswith("/") and (text == "/" or text[-2] == "\n"):
        text = text[:-1].rstrip()
    if text.endswith(";"):
        text = text[:-1].rstrip()
    return f"{text}\n/\n\n"


def run_bundle(output_path: Path) -> int:
    """Generate the single-file deploy bundle at output_path.

    Assembles the sorted install DDL, then the Lua and UDF CREATE OR REPLACE
    wrappers, each terminated by ``/`` on its own line. Returns the number of
    statements written. Excludes activation and demo seed data.

    Raises FileNotFoundError if any required source is missing.
    """
    statements = _collect_install_statements() + _collect_script_statements()
    body = "".join(_emit(statement) for statement in statements)
    output_path.write_text(body, encoding="utf-8")
    return len(statements)


def run_release_bundle(output_path: Path) -> int:
    """Generate the framework-only single-file release at output_path.

    The release is the deploy artifact for a CLEAN database: install DDL (minus
    the upgrade-only column ALTERs), the MASTER dispatcher, the PREPROC command
    sugar, and the full admin control surface — but NO demo modules and NO UDF
    (those ship as opt-in files under examples/). It ends with
    ``EXECUTE SCRIPT PREPROC.REFRESH`` so the seeded control-sugar rule resolves
    immediately. Returns the number of statements written (the header comment is
    not counted).

    Raises FileNotFoundError if any required source is missing.
    """
    scripts = _script_statements()
    statements = (
        _collect_install_statements(skip=frozenset({"25_column_upgrades.sql"}))
        + [scripts[name] for name in _RELEASE_SCRIPT_ORDER]
        + ["EXECUTE SCRIPT PREPROC.REFRESH"]
    )
    body = _RELEASE_HEADER + "".join(_emit(statement) for statement in statements)
    output_path.write_text(body, encoding="utf-8")
    return len(statements)
