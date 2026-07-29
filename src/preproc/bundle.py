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

Sources are resolved by ``preproc.resources``, the same way ``install.py``
resolves them, so the bundle and the CLI install read one identical tree whether
the framework runs from a source checkout or an installed distribution.

Output is deterministic: identical sources produce byte-for-byte identical
output, because install files are globbed in sorted filename order and every
other source is read in a fixed sequence.
"""

from __future__ import annotations

from pathlib import Path

from preproc import resources
from preproc.install import _split_statements

# Sources this module assembles, named locally so every wrapper below reads as a
# constant. Where they are is preproc.resources' business, not this module's.
_SQL_INSTALL_DIR = resources.SQL_INSTALL_DIR
_MASTER_LUA_PATH = resources.MASTER_LUA_PATH
_DEMO_PING_LUA_PATH = resources.DEMO_PING_LUA_PATH
_DEMO_TRANSLATE_LUA_PATH = resources.DEMO_TRANSLATE_LUA_PATH
_DEMO_AUDIT_LUA_PATH = resources.DEMO_AUDIT_LUA_PATH
_REFRESH_LUA_PATH = resources.REFRESH_LUA_PATH
_STATUS_LUA_PATH = resources.STATUS_LUA_PATH
_REFRESH_CORE_LUA_PATH = resources.REFRESH_CORE_LUA_PATH
_RULE_ADD_LUA_PATH = resources.RULE_ADD_LUA_PATH
_RULE_DROP_LUA_PATH = resources.RULE_DROP_LUA_PATH
_RULE_ENABLE_LUA_PATH = resources.RULE_ENABLE_LUA_PATH
_RULE_DISABLE_LUA_PATH = resources.RULE_DISABLE_LUA_PATH
_RULE_LIST_LUA_PATH = resources.RULE_LIST_LUA_PATH
_SET_MODE_LUA_PATH = resources.SET_MODE_LUA_PATH
_SET_SELF_DISABLE_LUA_PATH = resources.SET_SELF_DISABLE_LUA_PATH
_SET_SYS_GROUPING_LUA_PATH = resources.SET_SYS_GROUPING_LUA_PATH
_CONTROL_SUGAR_LUA_PATH = resources.CONTROL_SUGAR_LUA_PATH
_RULE_ENSURE_LUA_PATH = resources.RULE_ENSURE_LUA_PATH
_MODULE_REGISTER_LUA_PATH = resources.MODULE_REGISTER_LUA_PATH
_MODULE_RETIRE_LUA_PATH = resources.MODULE_RETIRE_LUA_PATH
_MODULE_RECLAIM_LUA_PATH = resources.MODULE_RECLAIM_LUA_PATH
_MODULE_INSTALL_LUA_PATH = resources.MODULE_INSTALL_LUA_PATH
_MODULE_CATALOG_LUA_PATH = resources.MODULE_CATALOG_LUA_PATH
_DEMO_UDF_PATH = resources.DEMO_SQLGLOT_UDF_PATH
_MODULE_RESOLVER_UDF_PATH = resources.MODULE_RESOLVER_UDF_PATH


def _read_source(path: Path, kind: str) -> str:
    """Read a required source file, raising a helpful error if it is missing."""
    return resources.require_resource(path, kind).read_text(encoding="utf-8")


def _collect_install_statements(skip: frozenset[str] = frozenset()) -> list[str]:
    """Return install DDL statements in ascending filename order.

    Reuses install.py's comment-aware splitter so comments and trailing
    semicolons are stripped exactly as the CLI install applies them. Filenames
    in ``skip`` are omitted — used by the release bundle to drop the
    upgrade-only ``25_column_upgrades.sql`` (whose additive ALTERs error on a
    clean database where ``20_tables.sql`` already defines those columns).
    """
    statements: list[str] = []
    for sql_file in resources.require_install_ddl(_SQL_INSTALL_DIR):
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
        "RULE_ENSURE": f"CREATE OR REPLACE LUA SCRIPT PREPROC.RULE_ENSURE("
        f"scope_type, scope_value, phase, script_name, function_name, client_pattern, norefresh) "
        f"RETURNS TABLE AS\n{_read_source(_RULE_ENSURE_LUA_PATH, 'rule_ensure.lua')}",
        "MODULE_REGISTER": f"CREATE OR REPLACE LUA SCRIPT PREPROC.MODULE_REGISTER("
        f"name, version, source, ref, sha256, script_name, function_name, phase, deploy_mode, "
        f"source_url, library_version, objects) "
        f"RETURNS TABLE AS\n{_read_source(_MODULE_REGISTER_LUA_PATH, 'module_register.lua')}",
        "MODULE_RETIRE": f"CREATE OR REPLACE LUA SCRIPT PREPROC.MODULE_RETIRE(name, version) "
        f"RETURNS TABLE AS\n{_read_source(_MODULE_RETIRE_LUA_PATH, 'module_retire.lua')}",
        "MODULE_RECLAIM": f"CREATE OR REPLACE LUA SCRIPT PREPROC.MODULE_RECLAIM(name, version) "
        f"RETURNS TABLE AS\n{_read_source(_MODULE_RECLAIM_LUA_PATH, 'module_reclaim.lua')}",
        "SET_MODE": f"CREATE OR REPLACE LUA SCRIPT PREPROC.SET_MODE(mode, norefresh) "
        f"RETURNS TABLE AS\n{_read_source(_SET_MODE_LUA_PATH, 'set_mode.lua')}",
        "SET_SELF_DISABLE": f"CREATE OR REPLACE LUA SCRIPT PREPROC.SET_SELF_DISABLE(value) "
        f"RETURNS TABLE AS\n{_read_source(_SET_SELF_DISABLE_LUA_PATH, 'set_self_disable.lua')}",
        "SET_SYS_GROUPING": f"CREATE OR REPLACE LUA SCRIPT PREPROC.SET_SYS_GROUPING("
        f"value, norefresh) "
        f"RETURNS TABLE AS\n{_read_source(_SET_SYS_GROUPING_LUA_PATH, 'set_sys_grouping.lua')}",
        "DEMO_SQLGLOT_TRANSPILE_V1": _read_source(_DEMO_UDF_PATH, "demo_sqlglot_transpile_v1.sql"),
        "MODULE_RESOLVER_V1": _read_source(_MODULE_RESOLVER_UDF_PATH, "module_resolver_v1.sql"),
        "MODULE_INSTALL": "CREATE OR REPLACE LUA SCRIPT "
        "PREPROC.MODULE_INSTALL(source, module_name, scope) RETURNS TABLE AS\n"
        f"{_read_source(_MODULE_INSTALL_LUA_PATH, 'module_install.lua')}",
        "MODULE_CATALOG": "CREATE OR REPLACE LUA SCRIPT PREPROC.MODULE_CATALOG(source) "
        f"RETURNS TABLE AS\n{_read_source(_MODULE_CATALOG_LUA_PATH, 'module_catalog.lua')}",
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
    "RULE_ENSURE",
    "MODULE_REGISTER",
    "MODULE_RETIRE",
    "MODULE_RECLAIM",
    "SET_MODE",
    "SET_SELF_DISABLE",
    "SET_SYS_GROUPING",
    "DEMO_SQLGLOT_TRANSPILE_V1",
    "MODULE_RESOLVER_V1",
    "MODULE_INSTALL",
    "MODULE_CATALOG",
]

# Release bundle: framework + control surface only — no demo modules, no demo
# UDF. The in-database module installer (resolver UDF + MODULE_INSTALL +
# MODULE_CATALOG) IS a product feature, not a demo, so — unlike
# DEMO_SQLGLOT_TRANSPILE_V1 — all three ship here too.
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
    "RULE_ENSURE",
    "MODULE_REGISTER",
    "MODULE_RETIRE",
    "MODULE_RECLAIM",
    "SET_MODE",
    "SET_SELF_DISABLE",
    "SET_SYS_GROUPING",
    "MODULE_RESOLVER_V1",
    "MODULE_INSTALL",
    "MODULE_CATALOG",
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
-- Run the whole file in EXAplus (recommended). Plain SQL statements end with
-- `;`; each Lua/Python script body is wrapped between a `--/` line and a lone
-- `/` line (the EXAplus block delimiter) so the semicolons inside the body are
-- not taken as statement terminators.
--
-- WARNING — if you run this file through a GUI SQL client that treats `:name`
-- as a bind-variable placeholder (DbVisualiser, DBeaver, and similar JDBC
-- tools), DISABLE parameterized SQL FIRST. With it enabled the client
-- substitutes on the colons inside the Lua/Python script bodies (`'bucketfs:'`,
-- `if …:`, `def …:`) and SILENTLY CORRUPTS the deployed scripts (symptoms:
-- `object <NAME> not found`, `SyntaxError: expected ':'`). DbVisualiser:
-- SQL Commander → Parameterized SQL → off. EXAplus does not substitute and
-- needs no change.
--
-- Clean-install only: CREATE ROLE/SCHEMA are unguarded and the upgrade-only
-- column ALTERs are omitted — use `preproc install` for idempotent re-runs /
-- column upgrades.
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


def _strip_terminator(statement: str) -> str:
    """Return the statement text without any trailing ``;`` or bare ``/`` line."""
    text = statement.rstrip()
    if text.endswith("/") and (text == "/" or text[-2] == "\n"):
        text = text[:-1].rstrip()
    if text.endswith(";"):
        text = text[:-1].rstrip()
    return text


def _emit_sql(statement: str) -> str:
    """Format a plain SQL statement terminated by ``;``."""
    return f"{_strip_terminator(statement)};\n\n"


def _emit_script(statement: str) -> str:
    """Format a script CREATE wrapped in the EXAplus ``--/`` ... ``/`` block.

    A Lua/PYTHON3 script body contains semicolons, so a ``;`` terminator would
    split it mid-body. EXAplus treats everything between a ``--/`` line and a
    lone ``/`` line as a single statement, ignoring the inner semicolons.
    """
    return f"--/\n{_strip_terminator(statement)}\n/\n\n"


def run_release_bundle(output_path: Path) -> int:
    """Generate the framework-only single-file release at output_path.

    The release is the deploy artifact for a CLEAN database: install DDL (minus
    the upgrade-only column ALTERs), the MASTER dispatcher, the PREPROC command
    sugar, and the full admin control surface — but NO demo modules and NO UDF
    (those ship as opt-in files under examples/). It ends with
    ``EXECUTE SCRIPT PREPROC.REFRESH`` so the seeded control-sugar rule resolves
    immediately. Returns the number of statements written (the header comment is
    not counted).

    Delimiters follow the EXAplus convention: plain SQL (install DDL, the final
    REFRESH) is terminated by ``;``; each Lua script CREATE is wrapped between a
    ``--/`` line and a lone ``/`` line so its body's semicolons are not treated
    as terminators.

    Raises FileNotFoundError if any required source is missing.
    """
    scripts = _script_statements()
    install_statements = _collect_install_statements(skip=frozenset({"25_column_upgrades.sql"}))
    script_statements = [scripts[name] for name in _RELEASE_SCRIPT_ORDER]

    body = _RELEASE_HEADER
    body += "".join(_emit_sql(statement) for statement in install_statements)
    body += "".join(_emit_script(statement) for statement in script_statements)
    body += _emit_sql("EXECUTE SCRIPT PREPROC.REFRESH")

    output_path.write_text(body, encoding="utf-8")
    return len(install_statements) + len(script_statements) + 1
