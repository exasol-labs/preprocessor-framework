"""Tests for the TRANSLATE-phase demo: DEMO_TRANSLATE_V1 and its companion UDF.

Two integration concerns are exercised here:

UDF contract (no pipeline needed)
    ``PREPROC_RT.DEMO_SQLGLOT_TRANSPILE_V1`` is a plain scalar UDF; it can be
    called directly via SELECT. Its contract is total: it returns the
    transpiled Exasol SQL on success and the ``ERR:<message>`` sentinel on any
    failure, never raising. These tests call it directly and assert both paths.

Module contract through the live pipeline
    ``DEMO_TRANSLATE_V1.translate`` only has observable behaviour when MASTER is
    active and a TRANSLATE rule resolves to it. Following the dispatch-suite
    pattern, the pipeline tests seed a RESOLUTION rule for CURRENT_USER, activate
    MASTER via ALTER SESSION (never ALTER SYSTEM), run a probe statement, and
    read back the only externally visible evidence — the result the rewritten
    statement produces. Each test is guarded by ``_skip_unless_demo_rule_seeded``
    so it skips cleanly unless ``sql/demo/seed.sql`` has been applied, per the
    feature's "requires manual setup" note.

The probe statements are constructed so the *translated* form is executable and
the *untranslated* form is a hard Exasol syntax error. ``SELECT TOP 1 42 AS X``
is a syntax error in Exasol; its tsql->exasol transpilation ``SELECT 42 AS X
LIMIT 1`` runs and returns 42. So a probe that returns 42 proves end-to-end
translation occurred; a probe that raises proves it did not.
"""

import subprocess
from pathlib import Path

import pyexasol
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]

_TRANSLATE_MODULE = "PREPROC_RT.DEMO_TRANSLATE_V1"
_TRANSPILE_UDF = "PREPROC_RT.DEMO_SQLGLOT_TRANSPILE_V1"

_ACTIVATE = "ALTER SESSION SET sql_preprocessor_script = PREPROC_RT.MASTER"
_DEACTIVATE = "ALTER SESSION SET sql_preprocessor_script = ''"


def _skip_unless_demo_rule_seeded(connection):
    """Skip the calling test unless the demo TRANSLATE rule is present in CONFIG.

    The pipeline tests require the demo configuration from sql/demo/seed.sql.
    Presence of an enabled CONFIG rule bound to DEMO_TRANSLATE_V1 is the agreed
    manual-setup signal.
    """
    row = connection.execute(
        "SELECT 1 FROM PREPROC.CONFIG "
        "WHERE script_name = 'PREPROC_RT.DEMO_TRANSLATE_V1' AND enabled = TRUE"
    ).fetchone()
    if row is None:
        pytest.skip("demo TRANSLATE rule not seeded; run sql/demo/seed.sql first")


def _seed_translate_rule(connection):
    """Insert a TRANSLATE RESOLUTION rule for CURRENT_USER bound to the demo module."""
    connection.execute(
        "INSERT INTO PREPROC.RESOLUTION "
        "(user_name, client_pattern, phase, script_name, function_name, ordinal) "
        "VALUES (CURRENT_USER, NULL, 'TRANSLATE', {sn}, 'translate', 1)",
        {"sn": _TRANSLATE_MODULE},
    )


def _delete_rules(connection):
    """Remove every RESOLUTION rule for CURRENT_USER."""
    connection.execute("DELETE FROM PREPROC.RESOLUTION WHERE user_name = CURRENT_USER")


@pytest.fixture
def translate_pipeline(installed):
    """Activate the demo TRANSLATE rule for CURRENT_USER, guaranteeing teardown.

    Asserts the no-rules precondition so a leftover rule cannot corrupt the
    assertions, seeds exactly one TRANSLATE rule, and on teardown unconditionally
    deactivates the preprocessor before deleting rules — in that order, each
    guarded — so a mid-test failure cannot leave the session-scoped connection
    activated or polluted.
    """
    rule_count = installed.execute(
        "SELECT COUNT(*) FROM PREPROC.RESOLUTION WHERE user_name = CURRENT_USER"
    ).fetchval()
    assert rule_count == 0, (
        f"Expected 0 RESOLUTION rules for CURRENT_USER before a pipeline test, "
        f"got {rule_count} — a leftover rule invalidates the assertions"
    )

    _seed_translate_rule(installed)
    try:
        yield installed
    finally:
        try:
            installed.execute(_DEACTIVATE)
        finally:
            _delete_rules(installed)


def _probe_scalar(connection, statement):
    """Activate MASTER, run a statement, return its scalar; always deactivate."""
    connection.execute(_ACTIVATE)
    try:
        return connection.execute(statement).fetchval()
    finally:
        connection.execute(_DEACTIVATE)


# ---------------------------------------------------------------------------
# UDF contract (direct calls, no pipeline)
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_transpile_udf_converts_tsql_top_to_limit(installed):
    """The UDF transpiles a tagged T-SQL TOP into an Exasol LIMIT, no ERR sentinel."""
    row = installed.execute(
        f"SELECT {_TRANSPILE_UDF}('tsql', 'SELECT TOP 5 col FROM t')"
    ).fetchone()
    assert row[0] == "SELECT col FROM t LIMIT 5"
    assert not row[0].startswith("ERR:"), "successful transpile must not carry ERR sentinel"


@pytest.mark.integration
def test_transpile_udf_returns_err_sentinel_on_garbage(installed):
    """The UDF returns the ERR: sentinel on unparseable input rather than raising."""
    row = installed.execute(f"SELECT {_TRANSPILE_UDF}('tsql', '%%%INVALID%%%')").fetchone()
    assert row[0].startswith("ERR:"), f"expected ERR: sentinel, got {row[0]!r}"


# ---------------------------------------------------------------------------
# Module contract through the live pipeline (skip-guarded)
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_marked_statement_translated_through_pipeline(translate_pipeline):
    """A --!dialect:tsql TOP statement is rewritten to runnable Exasol LIMIT SQL.

    Skips unless a TRANSLATE rule is seeded (run sql/demo/seed.sql first).

    ``SELECT TOP 1 42 AS X`` is a hard Exasol syntax error; only if MASTER
    strips the marker and transpiles it to ``SELECT 42 AS X LIMIT 1`` does the
    statement compile and return 42. A returned 42 therefore proves the marker
    was removed and the TOP was rewritten to LIMIT end-to-end.
    """
    _skip_unless_demo_rule_seeded(translate_pipeline)

    result = _probe_scalar(translate_pipeline, "--!dialect:tsql\nSELECT TOP 1 42 AS X")
    assert result == 42, (
        "marked statement was not translated to runnable Exasol SQL "
        f"(got {result!r}); marker strip or TOP->LIMIT rewrite failed"
    )


@pytest.mark.integration
def test_unmarked_statement_passes_through_unchanged(translate_pipeline):
    """An ordinary statement with no dialect marker runs unchanged under the rule.

    Skips unless a TRANSLATE rule is seeded (run sql/demo/seed.sql first).
    """
    _skip_unless_demo_rule_seeded(translate_pipeline)

    result = _probe_scalar(translate_pipeline, "SELECT 99 AS X")
    assert result == 99, f"unmarked statement was altered by the TRANSLATE rule (got {result!r})"


@pytest.mark.integration
def test_failed_transpile_fails_closed(translate_pipeline):
    """A marked statement whose body cannot be transpiled fails closed.

    Skips unless a TRANSLATE rule is seeded (run sql/demo/seed.sql first).

    The module must return the ORIGINAL text on a UDF ERR sentinel and never
    thread the sentinel forward. The garbage body ``%%%INVALID%%%`` cannot be
    transpiled, so the original (marker-bearing) text reaches the engine and
    raises a NORMAL Exasol syntax error. The error must NOT mention the ERR:
    sentinel — that would prove the corrupted sentinel text was threaded into
    the statement instead of the original.
    """
    _skip_unless_demo_rule_seeded(translate_pipeline)

    with pytest.raises(pyexasol.ExaQueryError) as exc:
        _probe_scalar(translate_pipeline, "--!dialect:tsql\n%%%INVALID%%%")

    message = str(exc.value)
    assert "ERR:" not in message, (
        "fail-closed violated: the ERR: sentinel was threaded into the "
        f"statement text reaching the engine:\n{message}"
    )


# ---------------------------------------------------------------------------
# Deployment and lint
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_translate_module_and_udf_deployed(installed):
    """Install deploys both the module and the UDF, executable by PUBLIC."""
    module_row = installed.execute(
        "SELECT SCRIPT_NAME FROM EXA_ALL_SCRIPTS "
        "WHERE SCRIPT_SCHEMA = 'PREPROC_RT' AND SCRIPT_NAME = 'DEMO_TRANSLATE_V1'"
    ).fetchone()
    assert module_row is not None, "PREPROC_RT.DEMO_TRANSLATE_V1 not found"

    udf_row = installed.execute(
        "SELECT SCRIPT_NAME FROM EXA_ALL_SCRIPTS "
        "WHERE SCRIPT_SCHEMA = 'PREPROC_RT' AND SCRIPT_NAME = 'DEMO_SQLGLOT_TRANSPILE_V1'"
    ).fetchone()
    assert udf_row is not None, "PREPROC_RT.DEMO_SQLGLOT_TRANSPILE_V1 not found"

    grant_row = installed.execute(
        "SELECT PRIVILEGE FROM EXA_DBA_OBJ_PRIVS "
        "WHERE OBJECT_NAME = 'PREPROC_RT' AND OBJECT_TYPE = 'SCHEMA' "
        "AND GRANTEE = 'PUBLIC' AND PRIVILEGE = 'EXECUTE'"
    ).fetchone()
    assert grant_row is not None, "EXECUTE on PREPROC_RT not granted to PUBLIC"


@pytest.mark.integration
def test_translate_module_passes_luacheck(installed):
    """The TRANSLATE module source passes luacheck with zero warnings/errors."""
    result = subprocess.run(
        ["luacheck", "sql/lua/demo_translate_v1.lua"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, (
        f"luacheck failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
