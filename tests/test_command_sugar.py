"""Tests for the CONTROL_SUGAR_V1 convenience EXPAND module.

What this module does
---------------------
``PREPROC_RT.CONTROL_SUGAR_V1.expand(sqltext)`` parses the SQL-like ``PREPROC …``
keyword grammar and rewrites each recognised command into exactly one
``EXECUTE SCRIPT PREPROC.<SCRIPT>(...)`` call. It returns ``nil`` for any
statement that is not a recognised ``PREPROC`` command (both non-PREPROC SQL and
unrecognised subcommands — fail-open).

Why the behavioural tests are integration tests
------------------------------------------------
Exasol's embedded Lua is the only interpreter that runs this code, and there is
no standalone Lua available to the suite. To exercise ``expand`` we deploy it
into PREPROC_RT and call it through the real preprocessor pipeline — but we do
NOT depend on the Group B admin scripts (RULE_ADD, …) existing yet, nor on the
Group D install seed. Instead a tiny PROBE module wraps ``expand`` and folds its
return value into an observable ``SELECT`` literal:

    probe_expand(sqltext):
        result = sugar_mod.expand(<the real PREPROC command>)
        return "SELECT '<result>' AS PROBE"   -- or SELECT NULL when nil

The connection then fetches the expansion STRING itself as a scalar, so the
assertions compare against the exact EXECUTE SCRIPT text the module emits —
independent of whether the target admin scripts are deployed. The PROBE module
is the EXPAND rule actually seeded for CURRENT_USER; the ``PREPROC`` command it
parses is passed as the submitted statement.

Shared-state discipline
------------------------
RESOLUTION rows for CURRENT_USER and the deployed PROBE/sugar scripts are global
state; both are torn down unconditionally in the fixture, and the session
preprocessor is always deactivated, mirroring test_master_dispatch.py.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

_ACTIVATE = "ALTER SESSION SET sql_preprocessor_script = PREPROC_RT.MASTER"
_DEACTIVATE = "ALTER SESSION SET sql_preprocessor_script = ''"

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SUGAR_LUA_PATH = _REPO_ROOT / "sql" / "lua" / "control_sugar_v1.lua"

_SUGAR_SCRIPT = "PREPROC_RT.CONTROL_SUGAR_V1"
_PROBE_SCRIPT = "PREPROC_RT.TEST_SUGAR_PROBE"


def _deploy_sugar(connection):
    """Deploy CONTROL_SUGAR_V1 from its source file under its real script name."""
    body = _SUGAR_LUA_PATH.read_text()
    connection.execute(f"CREATE OR REPLACE LUA SCRIPT {_SUGAR_SCRIPT}() AS\n{body}")


def _deploy_probe(connection):
    """Deploy a PROBE EXPAND module that surfaces sugar's return value as a literal.

    ``probe_expand`` imports CONTROL_SUGAR_V1, calls its ``expand`` on the
    submitted text, and rewrites the statement to a SELECT whose scalar IS the
    expansion string (or NULL when ``expand`` declined). This lets the connection
    read back the exact EXECUTE SCRIPT text without the target admin scripts
    needing to exist.
    """
    connection.execute(
        f"""CREATE OR REPLACE LUA SCRIPT {_PROBE_SCRIPT}() AS
            import("{_SUGAR_SCRIPT}", "sugar_mod")
            function probe_expand(sqltext)
                local result = sugar_mod.expand(sqltext)
                if result == nil then
                    return "SELECT CAST(NULL AS VARCHAR(2000)) AS PROBE"
                end
                local escaped = result:gsub("'", "''")
                return "SELECT '" .. escaped .. "' AS PROBE"
            end
        """
    )


def _seed_probe_rule(connection):
    """Seed a USER-scoped EXPAND rule for CURRENT_USER pointing at the PROBE module."""
    connection.execute(
        "INSERT INTO PREPROC.RESOLUTION "
        "(user_name, client_pattern, phase, script_name, function_name, ordinal) "
        "VALUES (CURRENT_USER, NULL, 'EXPAND', {sn}, 'probe_expand', 1)",
        {"sn": _PROBE_SCRIPT},
    )


def _delete_rules(connection):
    connection.execute("DELETE FROM PREPROC.RESOLUTION WHERE user_name = CURRENT_USER")


@pytest.fixture
def sugar_env(installed):
    """Deploy sugar + probe, seed the probe EXPAND rule, guarantee teardown.

    Asserts the no-rules precondition (a leftover rule would shadow the probe),
    deploys both scripts, seeds the probe rule, and on teardown unconditionally
    deactivates the preprocessor, deletes seeded rules, and drops both scripts.
    """
    # Install now seeds a DBA-scoped control-sugar bootstrap rule and this
    # connection (SYS) holds DBA, so it resolves that rule; clear CURRENT_USER's
    # resolution so the seeded probe rule is the only EXPAND rule in play.
    _delete_rules(installed)
    rule_count = installed.execute(
        "SELECT COUNT(*) FROM PREPROC.RESOLUTION WHERE user_name = CURRENT_USER"
    ).fetchval()
    assert rule_count == 0, (
        f"Expected 0 rules for CURRENT_USER before a sugar test, got {rule_count}"
    )

    _deploy_sugar(installed)
    _deploy_probe(installed)
    _seed_probe_rule(installed)
    try:
        yield installed
    finally:
        try:
            installed.execute(_DEACTIVATE)
        finally:
            try:
                _delete_rules(installed)
            finally:
                try:
                    installed.execute(f"DROP SCRIPT {_PROBE_SCRIPT}")
                finally:
                    installed.execute(f"DROP SCRIPT {_SUGAR_SCRIPT}")


def _expand(connection, command):
    """Activate MASTER, submit ``command``, return the expansion string (or None).

    The PROBE rule rewrites ``command`` to a SELECT whose scalar is exactly what
    ``CONTROL_SUGAR_V1.expand`` returned, so the fetched value IS the expansion.
    A NULL scalar means ``expand`` declined (returned nil).
    """
    connection.execute(_ACTIVATE)
    try:
        return connection.execute(command).fetchval()
    finally:
        connection.execute(_DEACTIVATE)


@pytest.mark.integration
def test_add_rule_expands_to_rule_add(sugar_env):
    """ADD RULE FOR ROLE … expands to a single EXECUTE SCRIPT PREPROC.RULE_ADD call.

    Every captured value (scope value, phase, script, function) must be threaded
    as a single-quoted string argument in source order, and the scope_type literal
    must be the keyword the command used. The exact string proves both the
    argument order and the quoting contract.
    """
    result = _expand(
        sugar_env,
        "PREPROC ADD RULE FOR ROLE ANALYSTS PHASE EXPAND "
        "SCRIPT PREPROC_RT.DEMO_PING_V1 FUNCTION ping",
    )
    assert result == (
        "EXECUTE SCRIPT PREPROC.RULE_ADD("
        "'ROLE', 'ANALYSTS', 'EXPAND', 'PREPROC_RT.DEMO_PING_V1', 'ping', NULL)"
    )


@pytest.mark.integration
def test_add_rule_for_user_expands(sugar_env):
    """ADD RULE FOR USER … threads the USER scope_type literal, not ROLE."""
    result = _expand(
        sugar_env,
        "PREPROC ADD RULE FOR USER BOB PHASE REWRITE SCRIPT PREPROC_RT.DEMO_AUDIT_V1 FUNCTION tag",
    )
    assert result == (
        "EXECUTE SCRIPT PREPROC.RULE_ADD("
        "'USER', 'BOB', 'REWRITE', 'PREPROC_RT.DEMO_AUDIT_V1', 'tag', NULL)"
    )


@pytest.mark.integration
def test_add_rule_folds_identifiers_but_keeps_function_literal(sugar_env):
    """ADD RULE folds SQL-identifier values to Exasol case rules but keeps FUNCTION literal.

    Unquoted role/phase/script fold to UPPER (role/user always upper), while the
    FUNCTION name is a case-sensitive Lua symbol carried through verbatim — so a
    mixed-case function like ``tagRows`` is preserved, not upper-cased.
    """
    result = _expand(
        sugar_env,
        "PREPROC ADD RULE FOR ROLE analysts PHASE expand "
        "SCRIPT preproc_rt.demo_audit_v1 FUNCTION tagRows",
    )
    assert result == (
        "EXECUTE SCRIPT PREPROC.RULE_ADD("
        "'ROLE', 'ANALYSTS', 'EXPAND', 'PREPROC_RT.DEMO_AUDIT_V1', 'tagRows', NULL)"
    )


@pytest.mark.integration
def test_add_rule_sugar_output_is_executable(sugar_env):
    """The ADD RULE expansion is a RUNNABLE EXECUTE SCRIPT call, not just a string.

    Regression guard for the missing-norefresh bug: the sugar must emit the
    trailing argument so the call matches PREPROC.RULE_ADD's parameter list.
    Executing the expansion for real must insert the rule — a short argument list
    raises "expected N parameters but got N-1", which the string-only assertions
    above never exercised.
    """
    scope_value = "PREPROC_SUGAR_EXEC_TEST"
    expansion = _expand(
        sugar_env,
        f"PREPROC ADD RULE FOR USER {scope_value} PHASE EXPAND "
        "SCRIPT PREPROC_RT.DEMO_PING_V1 FUNCTION ping",
    )
    try:
        sugar_env.execute(expansion)  # run the expanded EXECUTE SCRIPT for real
        count = sugar_env.execute(
            "SELECT COUNT(*) FROM PREPROC.CONFIG WHERE scope_value = {v}",
            {"v": scope_value},
        ).fetchval()
        assert count == 1, "the expanded RULE_ADD did not insert the rule"
    finally:
        sugar_env.execute("DELETE FROM PREPROC.CONFIG WHERE scope_value = {v}", {"v": scope_value})
        sugar_env.execute("EXECUTE SCRIPT PREPROC.REFRESH")


@pytest.mark.integration
def test_command_with_leading_comment_is_recognized(sugar_env):
    """A PREPROC command preceded by -- comment lines is still expanded.

    SQL clients (and the examples/ files) submit a comment block together with the
    following statement, and the preprocessor sees that raw text. The sugar must
    skip leading comment lines rather than decline on a first token of '--'.
    """
    result = _expand(
        sugar_env,
        "-- register the ping module\n"
        "-- (edit the scope to taste)\n"
        "PREPROC ADD RULE FOR ROLE PREPROC_ADMIN PHASE EXPAND "
        "SCRIPT PREPROC_RT.DEMO_PING_V1 FUNCTION ping",
    )
    assert result == (
        "EXECUTE SCRIPT PREPROC.RULE_ADD("
        "'ROLE', 'PREPROC_ADMIN', 'EXPAND', 'PREPROC_RT.DEMO_PING_V1', 'ping', NULL)"
    )


@pytest.mark.integration
def test_drop_rule_expands_to_rule_drop(sugar_env):
    """DROP RULE 42 expands to RULE_DROP(42, NULL): rule_id UNQUOTED (numeric),
    trailing NULL for the norefresh slot so the call matches the script signature."""
    result = _expand(sugar_env, "PREPROC DROP RULE 42")
    assert result == "EXECUTE SCRIPT PREPROC.RULE_DROP(42, NULL)"


@pytest.mark.integration
def test_disable_enable_expand(sugar_env):
    """DISABLE/ENABLE RULE <id> expand to their respective control scripts.

    Both carry the rule_id unquoted; the verb selects RULE_DISABLE vs RULE_ENABLE.
    """
    assert (
        _expand(sugar_env, "PREPROC DISABLE RULE 42")
        == "EXECUTE SCRIPT PREPROC.RULE_DISABLE(42, NULL)"
    )
    assert (
        _expand(sugar_env, "PREPROC ENABLE RULE 42")
        == "EXECUTE SCRIPT PREPROC.RULE_ENABLE(42, NULL)"
    )


@pytest.mark.integration
def test_list_rules_expands(sugar_env):
    """LIST RULES expands to the parameterless EXECUTE SCRIPT PREPROC.RULE_LIST.

    A parameterless admin script must emit NO trailing parentheses — RULE_LIST is
    invoked as a bare EXECUTE SCRIPT.
    """
    assert _expand(sugar_env, "PREPROC LIST RULES") == "EXECUTE SCRIPT PREPROC.RULE_LIST"


@pytest.mark.integration
def test_refresh_status_expand(sugar_env):
    """REFRESH and STATUS expand to their parameterless admin scripts."""
    assert _expand(sugar_env, "PREPROC REFRESH") == "EXECUTE SCRIPT PREPROC.REFRESH"
    assert _expand(sugar_env, "PREPROC STATUS") == "EXECUTE SCRIPT PREPROC.STATUS"


@pytest.mark.integration
def test_set_commands_expand(sugar_env):
    """SET MODE and SET SELF_DISABLE expand to the settings scripts.

    SET MODE single-quotes its mode value; SET SELF_DISABLE passes its boolean
    literal unquoted (true/false is a boolean argument, not a string).
    """
    assert (
        _expand(sugar_env, "PREPROC SET MODE enforce")
        == "EXECUTE SCRIPT PREPROC.SET_MODE('enforce', NULL)"
    )
    assert (
        _expand(sugar_env, "PREPROC SET SELF_DISABLE true")
        == "EXECUTE SCRIPT PREPROC.SET_SELF_DISABLE(true)"
    )


@pytest.mark.integration
def test_set_sys_grouping_expands(sugar_env):
    """SET SYS_GROUPING expands to SET_SYS_GROUPING with an unquoted boolean.

    Like SET SELF_DISABLE (and unlike SET MODE), the boolean value is passed
    through unquoted since it is a boolean argument, not a string. With no
    NOREFRESH suffix the trailing slot is NULL (the script then auto-refreshes).
    """
    assert (
        _expand(sugar_env, "PREPROC SET SYS_GROUPING FALSE")
        == "EXECUTE SCRIPT PREPROC.SET_SYS_GROUPING(FALSE, NULL)"
    )
    assert (
        _expand(sugar_env, "PREPROC SET SYS_GROUPING TRUE")
        == "EXECUTE SCRIPT PREPROC.SET_SYS_GROUPING(TRUE, NULL)"
    )


@pytest.mark.integration
def test_set_sys_grouping_norefresh_threaded(sugar_env):
    """A trailing NOREFRESH on SET SYS_GROUPING threads ``true`` into the slot.

    The suffix is stripped before subcommand parsing and reappears only as the
    extra boolean argument; the value itself stays unquoted.
    """
    assert (
        _expand(sugar_env, "PREPROC SET SYS_GROUPING FALSE NOREFRESH")
        == "EXECUTE SCRIPT PREPROC.SET_SYS_GROUPING(FALSE, true)"
    )


@pytest.mark.integration
def test_norefresh_suffix_threaded(sugar_env):
    """A trailing NOREFRESH appends a ``, true`` final argument to mutating commands.

    The suffix is stripped before subcommand parsing and reappears only as the
    extra boolean argument, so the rest of the call is byte-identical to the
    refresh form. Covered for both an arg-bearing command (DROP) and the full
    ADD RULE arity to prove the threading is positional and order-stable.
    """
    assert (
        _expand(sugar_env, "PREPROC DROP RULE 42 NOREFRESH")
        == "EXECUTE SCRIPT PREPROC.RULE_DROP(42, true)"
    )
    assert (
        _expand(
            sugar_env,
            "PREPROC ADD RULE FOR ROLE X PHASE Y SCRIPT Z FUNCTION W NOREFRESH",
        )
        == "EXECUTE SCRIPT PREPROC.RULE_ADD('ROLE', 'X', 'Y', 'Z', 'W', true)"
    )


@pytest.mark.integration
def test_norefresh_is_case_insensitive_and_keyword_case_preserved(sugar_env):
    """Keywords match case-insensitively while captured values keep their case.

    Lower-cased keywords (``preproc``, ``drop``, ``norefresh``) still dispatch,
    and a mixed-case captured value in SET MODE is emitted verbatim — proving the
    module upper-cases only the tokens it dispatches on.
    """
    assert (
        _expand(sugar_env, "preproc drop rule 7 norefresh")
        == "EXECUTE SCRIPT PREPROC.RULE_DROP(7, true)"
    )
    assert (
        _expand(sugar_env, "PREPROC SET MODE MixedCase")
        == "EXECUTE SCRIPT PREPROC.SET_MODE('MixedCase', NULL)"
    )


@pytest.mark.integration
def test_trailing_semicolon_and_whitespace_tolerated(sugar_env):
    """A trailing semicolon and surrounding whitespace are stripped before parsing."""
    assert _expand(sugar_env, "   PREPROC LIST RULES ;  ") == "EXECUTE SCRIPT PREPROC.RULE_LIST"


@pytest.mark.integration
def test_non_preproc_declined(sugar_env):
    """A non-PREPROC statement is declined (expand returns nil → NULL scalar).

    The probe rewrites a nil return to ``SELECT NULL``, so a NULL fetch is the
    observable signal that the module correctly stayed out of ordinary SQL.
    """
    assert _expand(sugar_env, "SELECT 1") is None


@pytest.mark.integration
def test_unrecognised_subcommand_declined(sugar_env):
    """An unrecognised PREPROC subcommand is declined (nil), failing open.

    Returning nil lets the original ``PREPROC UNKNOWN COMMAND`` text reach the
    engine and raise a loud syntax error rather than being silently swallowed.
    """
    assert _expand(sugar_env, "PREPROC UNKNOWN COMMAND") is None


@pytest.mark.integration
def test_malformed_add_rule_declined(sugar_env):
    """A structurally invalid ADD RULE (wrong arity) is declined rather than mis-expanded.

    Dropping the FUNCTION clause leaves the command short of the fixed arity, so
    the module must return nil instead of emitting a truncated RULE_ADD call.
    """
    assert _expand(sugar_env, "PREPROC ADD RULE FOR ROLE X PHASE Y SCRIPT Z") is None


@pytest.mark.integration
def test_install_module_expands_to_module_install(sugar_env):
    """INSTALL MODULE … FROM '<source>' FOR ROLE … expands to a single MODULE_INSTALL call.

    The FROM source is carried single-quoted exactly as written (a ``bucketfs:``
    source survives intact). The module name is an object-name identifier, so the
    lowercase registry key ``cast_shorthand`` is DOUBLE-QUOTED to keep its case;
    the FOR clause folds into a ``TYPE:value`` scope with TYPE and the role name
    both upper-cased.
    """
    result = _expand(
        sugar_env,
        'PREPROC INSTALL MODULE "cast_shorthand" FROM '
        "'bucketfs:PREPROC_BFS/preproc-lib-0.1.0.tar.gz' FOR ROLE PREPROC_ADMIN",
    )
    assert result == (
        "EXECUTE SCRIPT PREPROC.MODULE_INSTALL("
        "'bucketfs:PREPROC_BFS/preproc-lib-0.1.0.tar.gz', 'cast_shorthand', "
        "'ROLE:PREPROC_ADMIN')"
    )


def test_install_module_folds_identifiers_exasol_style(sugar_env):
    """Quoting follows Exasol rules: unquoted module name folds UPPER; a "..."-quoted
    name is verbatim; the role/user value is ALWAYS upper-cased (even quoted).

    So `FOR ROLE dba`, `FOR ROLE DBA`, and `FOR ROLE "dba"` all target `DBA`, and
    an unquoted module name folds to upper (which is why a lowercase registry key
    must be quoted to resolve).
    """
    src = "'https://h/registry/index.json'"
    # Unquoted role folds to upper; quoted module preserves its lowercase case.
    assert _expand(
        sugar_env, f'PREPROC INSTALL MODULE "cast_shorthand" FROM {src} FOR ROLE dba'
    ) == (f"EXECUTE SCRIPT PREPROC.MODULE_INSTALL({src}, 'cast_shorthand', 'ROLE:DBA')")
    # A double-quoted role is STILL upper-cased (Exasol upper-cases users/roles).
    assert _expand(
        sugar_env, f'PREPROC INSTALL MODULE "cast_shorthand" FROM {src} FOR ROLE "dba"'
    ) == (f"EXECUTE SCRIPT PREPROC.MODULE_INSTALL({src}, 'cast_shorthand', 'ROLE:DBA')")
    # An UNQUOTED module name folds to UPPER CASE (regular-identifier rule).
    assert _expand(sugar_env, f"PREPROC INSTALL MODULE cast_mod FROM {src} FOR USER bob") == (
        f"EXECUTE SCRIPT PREPROC.MODULE_INSTALL({src}, 'CAST_MOD', 'USER:BOB')"
    )


@pytest.mark.integration
def test_install_module_for_user_threads_user_scope(sugar_env):
    """FOR USER BOB threads a 'USER:BOB' scope argument, TYPE matched case-insensitively."""
    result = _expand(
        sugar_env,
        'PREPROC INSTALL MODULE "cast_shorthand" FROM '
        "'bucketfs:PREPROC_BFS/preproc-lib-0.1.0.tar.gz' FOR USER BOB",
    )
    assert result == (
        "EXECUTE SCRIPT PREPROC.MODULE_INSTALL("
        "'bucketfs:PREPROC_BFS/preproc-lib-0.1.0.tar.gz', 'cast_shorthand', "
        "'USER:BOB')"
    )
    result_lower = _expand(
        sugar_env,
        'preproc install module "cast_shorthand" from '
        "'bucketfs:PREPROC_BFS/preproc-lib-0.1.0.tar.gz' for user BOB",
    )
    assert result_lower == result


@pytest.mark.integration
def test_catalog_modules_expands_to_module_catalog(sugar_env):
    """CATALOG MODULES FROM '<source>' expands to a single MODULE_CATALOG call."""
    result = _expand(
        sugar_env,
        "PREPROC CATALOG MODULES FROM 'bucketfs:PREPROC_BFS/preproc-lib-0.1.0.tar.gz'",
    )
    assert result == (
        "EXECUTE SCRIPT PREPROC.MODULE_CATALOG('bucketfs:PREPROC_BFS/preproc-lib-0.1.0.tar.gz')"
    )


# The built-in default source used when INSTALL/CATALOG omit the FROM clause —
# the public Exasol Labs library index pinned to the mutable `latest` tag. Kept
# in sync with DEFAULT_MODULE_SOURCE in sql/lua/control_sugar_v1.lua.
_DEFAULT_SOURCE = (
    "https://raw.githubusercontent.com/exasol-labs/preprocessor-library/latest/registry/index.json"
)


@pytest.mark.integration
def test_install_module_without_from_defaults_to_latest_library(sugar_env):
    """INSTALL MODULE with no FROM clause defaults the source to the Exasol Labs library@latest.

    The FOR clause sits two tokens earlier without the `FROM '<source>'` pair, so
    the parser must resolve its shifted position. The unquoted module name folds
    to UPPER CASE per the identifier rule.
    """
    result = _expand(
        sugar_env,
        "PREPROC INSTALL MODULE cast_shorthand FOR ROLE PREPROC_ADMIN",
    )
    assert result == (
        f"EXECUTE SCRIPT PREPROC.MODULE_INSTALL("
        f"'{_DEFAULT_SOURCE}', 'CAST_SHORTHAND', 'ROLE:PREPROC_ADMIN')"
    )
    # An explicit FROM still overrides the default (fork / pinned version).
    pinned = "'https://raw.githubusercontent.com/acme/fork/v1.2.3/registry/index.json'"
    assert _expand(
        sugar_env,
        f'PREPROC INSTALL MODULE "cast_shorthand" FROM {pinned} FOR USER BOB',
    ) == (f"EXECUTE SCRIPT PREPROC.MODULE_INSTALL({pinned}, 'cast_shorthand', 'USER:BOB')")


@pytest.mark.integration
def test_catalog_modules_without_from_defaults_to_latest_library(sugar_env):
    """CATALOG MODULES with no FROM clause defaults the source to the Exasol Labs library@latest."""
    assert _expand(sugar_env, "PREPROC CATALOG MODULES") == (
        f"EXECUTE SCRIPT PREPROC.MODULE_CATALOG('{_DEFAULT_SOURCE}')"
    )


@pytest.mark.integration
def test_malformed_install_module_declined(sugar_env):
    """An INSTALL MODULE command with no valid FOR clause is declined (nil), failing open.

    A malformed shape must never produce a truncated/incorrect EXECUTE SCRIPT
    string; the module must return nil so the original text reaches the engine.
    (A missing FROM is NOT malformed — it is the defaulted no-source form.)
    """
    # FROM present but no FOR clause at all.
    assert (
        _expand(
            sugar_env,
            "PREPROC INSTALL MODULE cast_shorthand FROM "
            "'bucketfs:PREPROC_BFS/preproc-lib-0.1.0.tar.gz'",
        )
        is None
    )
    # No-FROM shape whose scope type is neither ROLE nor USER.
    assert (
        _expand(
            sugar_env,
            "PREPROC INSTALL MODULE cast_shorthand FOR GROUP PREPROC_ADMIN",
        )
        is None
    )


def test_sugar_passes_luacheck():
    """The convenience module passes luacheck with zero warnings and zero errors."""
    if shutil.which("luacheck") is None:
        pytest.skip("luacheck binary is not installed")
    completed = subprocess.run(
        ["luacheck", "sql/lua/control_sugar_v1.lua"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, (
        f"luacheck failed (exit {completed.returncode}):\n{completed.stdout}\n{completed.stderr}"
    )


@pytest.mark.integration
def test_sugar_bootstrap_rule_seeded(installed):
    """After install, PREPROC.CONFIG holds enabled EXPAND sugar rules for PREPROC_ADMIN and DBA.

    The install-time bootstrap seeds two ROLE-scoped EXPAND rules pointing at
    PREPROC_RT.CONTROL_SUGAR_V1 / ``expand`` so the sugar can register further
    rules (the chicken-and-egg resolution): one for PREPROC_ADMIN (delegated
    preprocessor admins) and one for DBA (so SYS and database admins, who cannot
    be granted PREPROC_ADMIN, get the sugar via the role-grant graph). This
    asserts both seeds exist, are enabled, and are in the EXPAND phase.
    """
    rows = installed.execute(
        "SELECT scope_type, scope_value, phase, function_name, enabled "
        "FROM PREPROC.CONFIG "
        "WHERE script_name = 'PREPROC_RT.CONTROL_SUGAR_V1' "
        "ORDER BY scope_value"
    ).fetchall()
    assert len(rows) == 2, f"expected two CONFIG rules for the sugar module, got {len(rows)}"
    for scope_type, scope_value, phase, function_name, enabled in rows:
        assert scope_type == "ROLE"
        assert phase == "EXPAND"
        assert function_name == "expand"
        assert enabled is True
    assert {r[1] for r in rows} == {"DBA", "PREPROC_ADMIN"}
