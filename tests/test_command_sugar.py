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

The one exception is ``test_client_scoped_rule_created_via_sugar_gates_dispatch``,
which uses the ``sugar_dispatch_env`` fixture instead of ``sugar_env``: it asserts
on BEHAVIOUR rather than on an emitted string, so it must run the real chain with
no probe standing in for anything.

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
from _dbfixtures import connect_with_client_name

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
        "'ROLE', 'ANALYSTS', 'EXPAND', 'PREPROC_RT.DEMO_PING_V1', 'ping', NULL, NULL)"
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
        "'USER', 'BOB', 'REWRITE', 'PREPROC_RT.DEMO_AUDIT_V1', 'tag', NULL, NULL)"
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
        "'ROLE', 'ANALYSTS', 'EXPAND', 'PREPROC_RT.DEMO_AUDIT_V1', 'tagRows', NULL, NULL)"
    )


@pytest.mark.integration
def test_add_rule_sugar_output_is_executable(sugar_env):
    """The ADD RULE expansion is a RUNNABLE EXECUTE SCRIPT call, not just a string.

    Regression guard for the argument-count class of bug: the sugar must fill
    EVERY slot of PREPROC.RULE_ADD's parameter list, so a short list raises
    "expected N script parameters but got N-1" — which the string-only assertions
    above never exercise. Both accepted arities are executed for real, because the
    11-word form and the 13-word CLIENT form emit the same number of arguments by
    two different code paths (a NULL literal vs. a quoted capture) and only one of
    them can be wrong at a time.
    """
    cases = (
        ("PREPROC_SUGAR_EXEC_TEST", "", None),
        ("PREPROC_SUGAR_EXEC_CLIENT_TEST", " CLIENT 'DbVisualizer 24.1.5'", "DbVisualizer 24.1.5"),
    )
    # BOTH commands are expanded before EITHER is executed. The probe rule lives in
    # RESOLUTION, and RULE_ADD auto-refreshes (norefresh is NULL here), which
    # rebuilds RESOLUTION from CONFIG and so deletes the probe. Executing one
    # expansion therefore disarms the sugar for any later _expand call.
    expanded = [
        (
            scope_value,
            expected_pattern,
            _expand(
                sugar_env,
                f"PREPROC ADD RULE FOR USER {scope_value} PHASE EXPAND "
                f"SCRIPT PREPROC_RT.DEMO_PING_V1 FUNCTION ping{command_tail}",
            ),
        )
        for scope_value, command_tail, expected_pattern in cases
    ]
    try:
        for scope_value, expected_pattern, expansion in expanded:
            sugar_env.execute(expansion)  # run the expanded EXECUTE SCRIPT for real
            rows = sugar_env.execute(
                "SELECT client_pattern FROM PREPROC.CONFIG WHERE scope_value = {v}",
                {"v": scope_value},
            ).fetchall()
            assert len(rows) == 1, f"the expanded RULE_ADD did not insert the rule: {expansion}"
            assert rows[0][0] == expected_pattern, (
                f"client_pattern did not persist verbatim from {expansion}"
            )
    finally:
        for scope_value, _, _ in expanded:
            sugar_env.execute(
                "DELETE FROM PREPROC.CONFIG WHERE scope_value = {v}", {"v": scope_value}
            )
        sugar_env.execute("EXECUTE SCRIPT PREPROC.REFRESH")


_CLIENT_PREFIX = (
    "PREPROC ADD RULE FOR ROLE DBA PHASE EXPAND SCRIPT PREPROC_RT.DEMO_HELP_V1 FUNCTION help_menu"
)
_CLIENT_ARGS_PREFIX = (
    "EXECUTE SCRIPT PREPROC.RULE_ADD('ROLE', 'DBA', 'EXPAND', 'PREPROC_RT.DEMO_HELP_V1', "
    "'help_menu', "
)


def _rule_add_call(client_arg, norefresh_arg):
    """The exact expansion for _CLIENT_PREFIX with the two trailing slots filled."""
    return f"{_CLIENT_ARGS_PREFIX}{client_arg}, {norefresh_arg})"


@pytest.mark.integration
def test_add_rule_client_clause_threads_pattern(sugar_env):
    """A CLIENT '<pattern>' clause threads client_pattern into the RULE_ADD call.

    The 13-word form. The pattern occupies the slot BETWEEN function_name and
    norefresh, matching the deployed parameter order, and the trailing norefresh
    slot is still emitted as NULL (always-pass-the-slot).
    """
    result = _expand(sugar_env, f"{_CLIENT_PREFIX} CLIENT 'DbVisualizer'")
    assert result == _rule_add_call("'DbVisualizer'", "NULL")


@pytest.mark.integration
def test_add_rule_client_value_carried_verbatim(sugar_env):
    """The CLIENT operand is NOT identifier-folded: case, spaces and quotes survive.

    client_pattern is matched as a literal SUBSTRING against the application name
    the connecting tool self-reports (`DbVisualizer 24.1.5`, `DBeaver 23.3.0`,
    `PyExasol 2.2.1`, `EXAplus`), so upper-casing it would make the rule match
    NOTHING while still resolving and still being evaluated by dispatch — a silent
    no-op. This is the second capture position exempt from folding after FUNCTION,
    and this test asserts both exemptions in one command: `help_menu` stays
    lowercase alongside the mixed-case pattern.
    """
    # Mixed case preserved — the whole point of the exemption. Also a pattern
    # containing whitespace stays ONE argument: the tokenizer keeps a quoted
    # span as a single token (the mechanism that carries FROM '<source>').
    assert _expand(sugar_env, f"{_CLIENT_PREFIX} CLIENT 'DbVisualizer 24.1.5'") == _rule_add_call(
        "'DbVisualizer 24.1.5'", "NULL"
    )
    # Not trimmed either: a deliberate trailing space is part of the pattern.
    assert _expand(sugar_env, f"{_CLIENT_PREFIX} CLIENT 'DbVisualizer '") == _rule_add_call(
        "'DbVisualizer '", "NULL"
    )
    # A doubled '' in the operand is ONE quote in the value; the emitted literal
    # re-doubles it, so the value RULE_ADD receives is O'Brien Tool.
    assert _expand(sugar_env, f"{_CLIENT_PREFIX} CLIENT 'O''Brien Tool'") == _rule_add_call(
        "'O''Brien Tool'", "NULL"
    )
    # A whitespace-only pattern is a real value, not an absent one: Exasol does not
    # collapse ' ' to NULL, so it survives transit and is carried through for the
    # shared validator to reject with its explanatory message. (The genuinely EMPTY
    # operand is the exception that is declined here instead — see
    # test_add_rule_empty_client_operand_declined.)
    assert _expand(sugar_env, f"{_CLIENT_PREFIX} CLIENT ' '") == _rule_add_call("' '", "NULL")


@pytest.mark.integration
def test_add_rule_client_composes_with_norefresh(sugar_env):
    """CLIENT and NOREFRESH are independent optional clauses in all four combinations.

    NOREFRESH is stripped BEFORE the arm parses positions, so the arm only ever
    sees 11 or 13 words and CLIENT is never read at a shifted index.
    """
    assert _expand(sugar_env, f"{_CLIENT_PREFIX} CLIENT 'DbVisualizer' NOREFRESH") == (
        _rule_add_call("'DbVisualizer'", "true")
    )
    assert _expand(sugar_env, f"{_CLIENT_PREFIX} NOREFRESH") == _rule_add_call("NULL", "true")
    assert _expand(sugar_env, f"{_CLIENT_PREFIX} CLIENT 'DbVisualizer'") == (
        _rule_add_call("'DbVisualizer'", "NULL")
    )
    assert _expand(sugar_env, _CLIENT_PREFIX) == _rule_add_call("NULL", "NULL")


@pytest.mark.integration
def test_add_rule_quoted_norefresh_pattern_not_stripped(sugar_env):
    """CLIENT 'NOREFRESH' is a pattern, not the suffix.

    The suffix check compares the LAST token to the bare keyword, and the tokenizer
    keeps a quoted span WITH its quotes, so the token ``'NOREFRESH'`` never equals
    ``NOREFRESH``. Without that property the strip would eat the operand, leave 12
    words, and the command would silently decline. The second assertion pins that
    exactly one trailing bare keyword is stripped even when the pattern is the same
    word.
    """
    assert _expand(sugar_env, f"{_CLIENT_PREFIX} CLIENT 'NOREFRESH'") == _rule_add_call(
        "'NOREFRESH'", "NULL"
    )
    assert _expand(sugar_env, f"{_CLIENT_PREFIX} CLIENT 'NOREFRESH' NOREFRESH") == (
        _rule_add_call("'NOREFRESH'", "true")
    )


@pytest.mark.integration
def test_add_rule_client_keyword_case_insensitive(sugar_env):
    """The CLIENT keyword folds case-insensitively; only the OPERAND keeps its case.

    Every PREPROC keyword is matched case-insensitively, and CLIENT is no
    exception — the exemption applies to the value it introduces, not to itself.
    """
    expected = _rule_add_call("'DbVisualizer'", "NULL")
    assert _expand(sugar_env, f"{_CLIENT_PREFIX} client 'DbVisualizer'") == expected
    assert _expand(sugar_env, f"{_CLIENT_PREFIX} Client 'DbVisualizer'") == expected
    # The whole command lower-cased: keywords still dispatch, identifiers still
    # fold UPPER, and the operand is still verbatim.
    assert (
        _expand(
            sugar_env,
            "preproc add rule for role dba phase expand "
            "script preproc_rt.demo_help_v1 function help_menu client 'DbVisualizer'",
        )
        == expected
    )


# --- end-to-end: a CLIENT-scoped rule created via the sugar gates dispatch ----
#
# Every assertion above reads the STRING the sugar emits. That is necessary but
# not sufficient: the reason the CLIENT operand is exempt from folding is a
# BEHAVIOURAL claim — a folded pattern still resolves, is still evaluated by
# dispatch, and still shows up in RULE_LIST looking correct, while matching
# nothing. Only running the whole deployed chain can falsify that claim, so this
# section drives sugar -> RULE_ADD -> CONFIG -> refresh -> RESOLUTION/profile ->
# MASTER dispatch and observes whether the rule FIRES.

_CLIENT_MODULE = "PREPROC_RT.TEST_SUGAR_CLIENT_MOD"
_CLIENT_SENTINEL = "SUGARSEED"
_CLIENT_PROBE = f"SELECT '{_CLIENT_SENTINEL}' AS PROBE"
_CLIENT_FIRED = f"{_CLIENT_SENTINEL}|FIRED"

# Measured self-reported application names. 'DbVisualizer' is a literal substring
# of the first and of neither the second nor the driver's own 'PyExasol 2.2.1'.
_MATCHING_CLIENT = "DbVisualizer 24.1.5"
_NON_MATCHING_CLIENT = "DBeaver 23.3.0"


def _deploy_client_module(connection):
    """Deploy a REWRITE module whose function tags the sentinel literal when it runs.

    ``tag_client`` appends ``|FIRED`` immediately before the closing quote of the
    ``'SUGARSEED'`` literal, so the scalar a session fetches back records whether
    the rule fired for THAT session. Its name is deliberately lowercase: FUNCTION
    is the other folding-exempt capture position, and dispatch swallows a per-rule
    failure, so a folded ``TAG_CLIENT`` would leave no tag either — this module
    therefore observes both exemptions at once.
    """
    connection.execute(
        f"""CREATE OR REPLACE LUA SCRIPT {_CLIENT_MODULE}() AS
            function tag_client(sqltext)
                local out = string.gsub(
                    sqltext, "({_CLIENT_SENTINEL}[^']*)'", "%1|FIRED'", 1
                )
                return out
            end
        """
    )


def _probe_as_client(client_name):
    """Fetch the sentinel literal as seen by a fresh session reporting ``client_name``.

    A session's CLIENT is fixed at login, so each half of the gate needs its own
    connection. MASTER is activated for the probe only and deactivated even if the
    probe raises, so a failure cannot leave a stray session preprocessor behind.
    """
    connection = connect_with_client_name(client_name)
    try:
        connection.execute(_ACTIVATE)
        try:
            return connection.execute(_CLIENT_PROBE).fetchval()
        finally:
            connection.execute(_DEACTIVATE)
    finally:
        connection.close()


@pytest.fixture
def sugar_dispatch_env(installed):
    """Arm the REAL seeded sugar rule (not the probe) and restore CONFIG afterwards.

    Three differences from ``sugar_env``, all forced by driving the real chain:

    * CONTROL_SUGAR_V1 is re-deployed from source and NOT dropped on teardown.
      ``sugar_env`` drops it, which would leave CONFIG/RESOLUTION pointing at a
      missing script for the rest of the session-scoped connection; re-deploying
      restores the post-install invariant and makes this test order-independent.
    * RESOLUTION is rebuilt from CONFIG instead of being replaced by a probe rule,
      because ``PREPROC ADD RULE`` has to reach the actual PREPROC.RULE_ADD.
    * Teardown deletes the created CONFIG row and refreshes, since RULE_ADD writes
      CONFIG for real rather than being observed as a string.

    The seeded rule's resolution is asserted up front: without it the PREPROC
    command reaches the engine as plain text and fails as a syntax error, which
    would not name its own cause.
    """
    _deploy_sugar(installed)
    installed.execute("EXECUTE SCRIPT PREPROC.REFRESH")
    seeded = installed.execute(
        "SELECT COUNT(*) FROM PREPROC.RESOLUTION "
        "WHERE user_name = CURRENT_USER AND phase = 'EXPAND' AND script_name = {sn}",
        {"sn": _SUGAR_SCRIPT},
    ).fetchval()
    assert seeded == 1, (
        f"expected the seeded control-sugar EXPAND rule to resolve for CURRENT_USER, "
        f"got {seeded} — a PREPROC command cannot expand without it"
    )

    _deploy_client_module(installed)
    try:
        yield installed
    finally:
        try:
            installed.execute(_DEACTIVATE)
        finally:
            try:
                installed.execute(
                    "DELETE FROM PREPROC.CONFIG WHERE script_name = {sn}",
                    {"sn": _CLIENT_MODULE},
                )
                installed.execute("EXECUTE SCRIPT PREPROC.REFRESH")
            finally:
                installed.execute(f"DROP SCRIPT {_CLIENT_MODULE}")


@pytest.mark.integration
def test_client_scoped_rule_created_via_sugar_gates_dispatch(sugar_dispatch_env):
    """A rule created THROUGH THE SUGAR fires for a matching client and not otherwise.

    The one test that closes the loop. ``PREPROC ADD RULE … CLIENT 'DbVisualizer'``
    is submitted as a statement to a MASTER-active session, so the seeded sugar
    rule expands it, PREPROC.RULE_ADD validates and persists the pattern, and the
    auto-refresh materialises it into RESOLUTION and the baked profile — none of it
    stubbed. Two fresh sessions then submit the same sentinel SELECT under two
    controlled client names and must disagree:

        DBeaver 23.3.0      -> 'SUGARSEED'         (rule does not fire)
        DbVisualizer 24.1.5 -> 'SUGARSEED|FIRED'   (rule fires)

    Why this cannot be replaced by a string assertion: upper-casing the CLIENT
    operand anywhere on this path produces a rule that resolves, is evaluated by
    dispatch, and reads correctly in ``PREPROC LIST RULES`` — while matching
    NOTHING, because 'DBVISUALIZER' is not a substring of 'DbVisualizer 24.1.5'.
    The whole failure is the ABSENCE of a tag, so only the positive half above can
    detect it. The negative half is what stops the positive half from being
    satisfied by a rule that fires for everyone.

    The rule is scoped FOR USER CURRENT_USER so the test does not depend on the
    ``group_sys_with_dba`` setting to resolve.
    """
    current_user = sugar_dispatch_env.execute("SELECT CURRENT_USER").fetchval()

    sugar_dispatch_env.execute(_ACTIVATE)
    try:
        allocated = sugar_dispatch_env.execute(
            f"PREPROC ADD RULE FOR USER {current_user} PHASE REWRITE "
            f"SCRIPT {_CLIENT_MODULE} FUNCTION tag_client CLIENT 'DbVisualizer'"
        ).fetchval()
    finally:
        sugar_dispatch_env.execute(_DEACTIVATE)

    # The sugar reached RULE_ADD, which reported an allocated operator-space id.
    assert allocated is not None, "the PREPROC command did not expand into RULE_ADD"
    created = sugar_dispatch_env.execute(
        "SELECT rule_id FROM PREPROC.CONFIG WHERE script_name = {sn}",
        {"sn": _CLIENT_MODULE},
    ).fetchall()
    assert [(allocated,)] == created, (
        "RULE_ADD must have inserted exactly the rule it reported, "
        f"reported {allocated}, CONFIG holds {created}"
    )

    # Negative first: a non-matching client must leave the sentinel untagged. This
    # is deliberately NOT asserted on the persisted pattern — the gate has to be
    # proven by what dispatch does, not by what CONFIG says.
    assert _probe_as_client(_NON_MATCHING_CLIENT) == _CLIENT_SENTINEL

    # Positive: the literal substring is present, so the rule fires.
    assert _probe_as_client(_MATCHING_CLIENT) == _CLIENT_FIRED


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
        "'ROLE', 'PREPROC_ADMIN', 'EXPAND', 'PREPROC_RT.DEMO_PING_V1', 'ping', NULL, NULL)"
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
        == "EXECUTE SCRIPT PREPROC.RULE_ADD('ROLE', 'X', 'Y', 'Z', 'W', NULL, true)"
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
def test_malformed_add_rule_client_clause_declined(sugar_env):
    """Every malformed CLIENT clause declines fail-open (nil), never a wrong pattern.

    A malformed clause must NOT produce an EXECUTE SCRIPT string, because the only
    alternatives to declining are creating a rule with the wrong pattern or with no
    pattern — both silent. Returning nil sends the original text to the engine,
    which raises a loud syntax error.

    Note what a NULL fetch proves: the probe rewrites only a genuine nil return to
    SELECT NULL. If ``expand`` instead THREW, MASTER's pcall would skip the rule,
    the original PREPROC text would reach the engine, and this test would error on
    a SQL syntax error rather than read None. So each assertion below distinguishes
    "declined" from "crashed".
    """
    # Keyword with no operand: 12 words, neither accepted count.
    assert _expand(sugar_env, f"{_CLIENT_PREFIX} CLIENT") is None
    # ... and the NOREFRESH strip must not rescue it into an accepted count.
    assert _expand(sugar_env, f"{_CLIENT_PREFIX} CLIENT NOREFRESH") is None
    # Unquoted operand: a bare word is not a string literal.
    assert _expand(sugar_env, f"{_CLIENT_PREFIX} CLIENT DbVisualizer") is None
    # Double-quoted operand: "..." is the delimited-IDENTIFIER form; the CLIENT
    # operand is a SQL string literal and must use '...'.
    assert _expand(sugar_env, f'{_CLIENT_PREFIX} CLIENT "DbVisualizer"') is None
    # Unterminated span: the tokenizer runs an unterminated span to end-of-text and
    # cannot fail, so the closing quote is NOT implied by the token existing.
    assert _expand(sugar_env, f"{_CLIENT_PREFIX} CLIENT 'DbVisualizer") is None
    # A lone quote is a one-character token, not an empty literal ('' is).
    assert _expand(sugar_env, f"{_CLIENT_PREFIX} CLIENT '") is None
    # The subtle one: ''' is an opening quote plus an ESCAPED quote and no
    # terminator. It starts and ends with ' and is >= 2 chars, so a shape test like
    # ^'.*'$ accepts it and unescapes it to a stray quote. Only scanning the span
    # under the doubling rule reveals that it never closes.
    assert _expand(sugar_env, f"{_CLIENT_PREFIX} CLIENT '''") is None
    # An unrecognised word in the CLIENT keyword position.
    assert _expand(sugar_env, f"{_CLIENT_PREFIX} BOGUS 'DbVisualizer'") is None
    # A trailing extra word: 14 words, neither accepted count.
    assert _expand(sugar_env, f"{_CLIENT_PREFIX} CLIENT 'DbVisualizer' EXTRA") is None


@pytest.mark.integration
def test_add_rule_empty_client_operand_declined(sugar_env):
    """CLIENT '' is declined, because Exasol collapses '' to NULL in transit.

    This is the one decline whose cause is NOT malformed syntax: `''` is a
    perfectly well-formed empty string literal. It is refused for where it would
    LAND. Exasol treats the empty string as NULL (`SELECT CASE WHEN '' IS NULL`
    answers YES), so an emitted `''` argument arrives at PREPROC.RULE_ADD as the
    `null` sentinel — indistinguishable from an omitted clause. The shared
    validator that exists to reject an empty pattern is never reached, and the
    command that READS as "restrict this rule to a client" silently creates a
    rule matching EVERY client. Declining keeps that from being the single silent
    outcome in a grammar where every other malformed input is loud.
    """
    assert _expand(sugar_env, f"{_CLIENT_PREFIX} CLIENT ''") is None
    # The NOREFRESH strip runs first, so the arm still sees the empty operand.
    assert _expand(sugar_env, f"{_CLIENT_PREFIX} CLIENT '' NOREFRESH") is None
    # The policy is on the OPERAND's value, not on how the keyword was cased.
    assert _expand(sugar_env, f"{_CLIENT_PREFIX} client ''") is None
    # Whitespace-only is NOT empty to Exasol (' ' IS NULL answers NO), so it
    # survives transit as a real value and reaches the validator, which rejects it
    # with an explanatory message. It must therefore still be carried through.
    assert _expand(sugar_env, f"{_CLIENT_PREFIX} CLIENT ' '") == _rule_add_call("' '", "NULL")
    # The boundary the check must not over-reach past: '''' is a FOUR-character
    # token whose unescaped value is one apostrophe, not the empty string. A test
    # on the token text (`#token == 2`, or a `^''` prefix match) would wrongly
    # decline it; the check belongs on the unescaped VALUE.
    assert _expand(sugar_env, f"{_CLIENT_PREFIX} CLIENT ''''") == _rule_add_call("''''", "NULL")


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
        "'bucketfs:bfsdefault/default/preproc-lib-0.1.0.tar.gz' FOR ROLE PREPROC_ADMIN",
    )
    assert result == (
        "EXECUTE SCRIPT PREPROC.MODULE_INSTALL("
        "'bucketfs:bfsdefault/default/preproc-lib-0.1.0.tar.gz', 'cast_shorthand', "
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
        "'bucketfs:bfsdefault/default/preproc-lib-0.1.0.tar.gz' FOR USER BOB",
    )
    assert result == (
        "EXECUTE SCRIPT PREPROC.MODULE_INSTALL("
        "'bucketfs:bfsdefault/default/preproc-lib-0.1.0.tar.gz', 'cast_shorthand', "
        "'USER:BOB')"
    )
    result_lower = _expand(
        sugar_env,
        'preproc install module "cast_shorthand" from '
        "'bucketfs:bfsdefault/default/preproc-lib-0.1.0.tar.gz' for user BOB",
    )
    assert result_lower == result


@pytest.mark.integration
def test_catalog_modules_expands_to_module_catalog(sugar_env):
    """CATALOG MODULES FROM '<source>' expands to a single MODULE_CATALOG call."""
    result = _expand(
        sugar_env,
        "PREPROC CATALOG MODULES FROM 'bucketfs:bfsdefault/default/preproc-lib-0.1.0.tar.gz'",
    )
    assert result == (
        "EXECUTE SCRIPT PREPROC.MODULE_CATALOG("
        "'bucketfs:bfsdefault/default/preproc-lib-0.1.0.tar.gz')"
    )


def test_module_source_token_carried_verbatim_at_any_depth(sugar_env):
    """The FROM source token is carried through opaque and unparsed, at any path depth.

    R9: the sugar never parses or validates a ``bucketfs:`` source -- it is a
    single quoted span threaded straight into the EXECUTE SCRIPT call, so the
    location form's extra ``/`` separators (service/bucket/multi-segment path)
    disturb nothing.
    """
    deep_source = "bucketfs:bfsdefault/default/nested/path/segments/preproc-lib-0.1.0.tar.gz"

    install_result = _expand(
        sugar_env,
        f"PREPROC INSTALL MODULE \"cast_shorthand\" FROM '{deep_source}' FOR ROLE PREPROC_ADMIN",
    )
    assert install_result == (
        f"EXECUTE SCRIPT PREPROC.MODULE_INSTALL('{deep_source}', 'cast_shorthand', "
        "'ROLE:PREPROC_ADMIN')"
    )

    catalog_result = _expand(sugar_env, f"PREPROC CATALOG MODULES FROM '{deep_source}'")
    assert catalog_result == f"EXECUTE SCRIPT PREPROC.MODULE_CATALOG('{deep_source}')"


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
            "'bucketfs:bfsdefault/default/preproc-lib-0.1.0.tar.gz'",
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


@pytest.mark.integration
def test_client_clause_lands_in_v1_in_place(installed):
    """The CLIENT grammar change is an in-place V1 edit under the pre-release exception.

    ADR-038: the project is pre-first-release, so no session can bind a stale
    CONTROL_SUGAR_V1 body and the never-CREATE-OR-REPLACE-a-live-module discipline
    is suspended. That exception must not be mistaken for the rule, so the header
    has to keep requiring CONTROL_SUGAR_V2 for a POST-release grammar change — and
    the seeded rules must keep resolving to V1 with no repointing.
    """
    source = _SUGAR_LUA_PATH.read_text()
    header = source.split("-- luacheck:")[0]

    # No V2 module was spawned and the install seed was not repointed.
    seed = (_REPO_ROOT / "sql" / "install" / "70_control_sugar_seed.sql").read_text()
    assert not (_REPO_ROOT / "sql" / "lua" / "control_sugar_v2.lua").exists()
    assert "CONTROL_SUGAR_V2" not in seed
    assert seed.count(f"'{_SUGAR_SCRIPT}'") >= 2, "both seeded rules must still target V1"

    # The header carries the new grammar and both halves of the versioning rule.
    assert "[CLIENT '<pattern>']" in header, "the header grammar line must show the clause"
    assert "CONTROL_SUGAR_V2" in header
    assert "post-release" in header.lower(), (
        "the header must keep stating that a POST-release grammar change requires V2"
    )
    # The section-10 folding-exemption paragraph itself must survive, not just
    # incidental mentions of the words CLIENT/FUNCTION elsewhere in the header
    # (both occur repeatedly for unrelated reasons, e.g. the grammar line and the
    # NOREFRESH-threading discussion). This phrase exists ONLY in that paragraph,
    # so deleting it -- the exact silent-failure mode this plan exists to
    # prevent, where a folded CLIENT operand resolves and dispatches but never
    # matches -- fails this assertion.
    assert "the SECOND capture position deliberately" in header
    assert "dispatch still evaluates it, it just never fires" in header

    # The deploy path was not repointed either: nothing anywhere emits or seeds a V2.
    for module in ("install.py", "bundle.py", "resources.py"):
        assert "CONTROL_SUGAR_V2" not in (_REPO_ROOT / "src" / "preproc" / module).read_text(), (
            f"{module} must still deploy CONTROL_SUGAR_V1 only"
        )

    # The seeded bootstrap rules in CONFIG still point at V1, unrepointed and enabled.
    rows = installed.execute(
        "SELECT script_name, enabled FROM PREPROC.CONFIG WHERE function_name = 'expand'"
    ).fetchall()
    assert rows, "the install seed must still hold the sugar bootstrap rules"
    assert {(r[0], r[1]) for r in rows} == {(_SUGAR_SCRIPT, True)}


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
