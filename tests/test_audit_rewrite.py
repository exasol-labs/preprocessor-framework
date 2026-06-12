"""Tests for the REWRITE-phase demo module DEMO_AUDIT_V1.

DEMO_AUDIT_V1.tag appends an inert SQL comment
``/* PREPROC_AUDIT user=<CURRENT_USER> */`` to a statement. Because the comment
is inert, the rewritten statement returns the SAME result as the original — so
the appended marker is NOT observable through a query result. Two observation
strategies are used:

Direct module observation (deterministic)
    A transient probe Lua script ``import``\\s DEMO_AUDIT_V1 the same way MASTER
    does and returns ``tag(<input>)`` as a table row, so the test reads the
    rewritten TEXT back directly. This is how the appended-comment and
    current-user assertions are made deterministic — far stronger than relying
    on the inert pipeline comment. The probe script is dropped on teardown.

Pipeline observation (skip-guarded)
    ``test_audit_runs_after_expand`` runs the live MASTER pipeline (EXPAND then
    REWRITE) to confirm the combined pipeline still compiles and executes, and
    uses the direct probe to confirm the audit comment lands on EXPANDED text.
    These pipeline tests skip unless the demo REWRITE rule is seeded
    (run sql/demo/seed.sql first).
"""

import shutil
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]

_AUDIT_MODULE = "PREPROC_RT.DEMO_AUDIT_V1"
_PING_MODULE = "PREPROC_RT.DEMO_PING_V1"
_PROBE_SCRIPT = "PREPROC_RT.TEST_AUDIT_PROBE"

_ACTIVATE = "ALTER SESSION SET sql_preprocessor_script = PREPROC_RT.MASTER"
_DEACTIVATE = "ALTER SESSION SET sql_preprocessor_script = ''"

_AUDIT_MARKER = "PREPROC_AUDIT"


def _skip_unless_demo_rule_seeded(connection):
    """Skip unless an enabled CONFIG rule bound to DEMO_AUDIT_V1 is present."""
    row = connection.execute(
        "SELECT 1 FROM PREPROC.CONFIG "
        "WHERE script_name = 'PREPROC_RT.DEMO_AUDIT_V1' AND enabled = TRUE"
    ).fetchone()
    if row is None:
        pytest.skip("demo REWRITE rule not seeded; run sql/demo/seed.sql first")


@pytest.fixture
def audit_probe(installed):
    """Deploy a transient script that returns DEMO_AUDIT_V1.tag(input) as a row.

    The probe imports the demo module exactly as MASTER does
    (``import(name, alias)`` then ``alias.tag(text)``), so it observes the real
    module body. The script is dropped unconditionally on teardown.
    """
    installed.execute("OPEN SCHEMA PREPROC_RT")
    installed.execute(
        f"""CREATE OR REPLACE LUA SCRIPT {_PROBE_SCRIPT}(input_text) RETURNS TABLE AS
            import("{_AUDIT_MODULE}", "audit_mod")
            local rewritten = audit_mod.tag(input_text)
            exit({{{{rewritten}}}}, "rewritten VARCHAR(2000000)")
        """
    )
    try:
        yield installed
    finally:
        installed.execute(f"DROP SCRIPT {_PROBE_SCRIPT}")


def _tag(connection, statement):
    """Return DEMO_AUDIT_V1.tag(statement) via the probe script."""
    return connection.execute(f"EXECUTE SCRIPT {_PROBE_SCRIPT}({{s}})", {"s": statement}).fetchval()


# ---------------------------------------------------------------------------
# Direct module observation
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_audit_appends_marker_comment(audit_probe):
    """tag appends a PREPROC_AUDIT SQL comment, preserving the original text."""
    original = "SELECT 1 AS X"
    rewritten = _tag(audit_probe, original)

    assert rewritten.startswith(original), (
        f"original statement text not preserved as a prefix: {rewritten!r}"
    )
    assert _AUDIT_MARKER in rewritten, f"{_AUDIT_MARKER} marker not appended: {rewritten!r}"
    assert "/*" in rewritten and "*/" in rewritten, (
        f"marker is not wrapped in an SQL comment: {rewritten!r}"
    )


@pytest.mark.integration
def test_audit_marker_carries_current_user(audit_probe):
    """The appended marker carries user=<CURRENT_USER>, derived inside the module."""
    current_user = audit_probe.execute("SELECT CURRENT_USER").fetchval()
    rewritten = _tag(audit_probe, "SELECT 1 AS X")

    assert f"user={current_user}" in rewritten, (
        f"marker does not carry the current user {current_user!r}: {rewritten!r}"
    )


# ---------------------------------------------------------------------------
# Pipeline observation (skip-guarded)
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_audit_runs_after_expand(audit_probe):
    """REWRITE annotates the EXPANDED statement, and the combined pipeline runs.

    Skips unless the demo REWRITE rule is seeded (run sql/demo/seed.sql first).

    Two facts are asserted: (1) the audit comment lands on already-expanded SQL
    (DEMO_PING_V1's expansion ``SELECT 'PONG'``) — observed via the direct probe;
    (2) running PING through the live MASTER pipeline with both an EXPAND
    (DEMO_PING_V1) and a REWRITE (DEMO_AUDIT_V1) rule still compiles and returns
    PONG, proving the appended comment did not break the expanded statement.
    """
    _skip_unless_demo_rule_seeded(audit_probe)

    expanded = "SELECT 'PONG'"
    rewritten_expanded = _tag(audit_probe, expanded)
    assert rewritten_expanded.startswith(expanded), (
        f"audit comment did not append to the expanded statement: {rewritten_expanded!r}"
    )
    assert _AUDIT_MARKER in rewritten_expanded

    # Install now seeds a DBA-scoped control-sugar bootstrap rule and this
    # connection (SYS) holds DBA, so it resolves that rule; clear CURRENT_USER's
    # resolution to start from a clean pipeline before seeding the audit rules.
    audit_probe.execute("DELETE FROM PREPROC.RESOLUTION WHERE user_name = CURRENT_USER")
    rule_count = audit_probe.execute(
        "SELECT COUNT(*) FROM PREPROC.RESOLUTION WHERE user_name = CURRENT_USER"
    ).fetchval()
    assert rule_count == 0, (
        f"Expected 0 RESOLUTION rules for CURRENT_USER, got {rule_count} "
        f"— a leftover rule invalidates the pipeline assertion"
    )
    audit_probe.execute(
        "INSERT INTO PREPROC.RESOLUTION "
        "(user_name, client_pattern, phase, script_name, function_name, ordinal) "
        "VALUES (CURRENT_USER, NULL, 'EXPAND', {sn}, 'ping', 1)",
        {"sn": _PING_MODULE},
    )
    audit_probe.execute(
        "INSERT INTO PREPROC.RESOLUTION "
        "(user_name, client_pattern, phase, script_name, function_name, ordinal) "
        "VALUES (CURRENT_USER, NULL, 'REWRITE', {sn}, 'tag', 1)",
        {"sn": _AUDIT_MODULE},
    )
    try:
        audit_probe.execute(_ACTIVATE)
        try:
            result = audit_probe.execute("PING").fetchval()
        finally:
            audit_probe.execute(_DEACTIVATE)
        assert result == "PONG", (
            f"expanded + audited PING did not run to PONG (got {result!r}); "
            f"the appended audit comment may have broken the expanded statement"
        )
    finally:
        audit_probe.execute("DELETE FROM PREPROC.RESOLUTION WHERE user_name = CURRENT_USER")


# ---------------------------------------------------------------------------
# Deployment and lint
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_audit_module_deployed(installed):
    """Install deploys the audit module into PREPROC_RT."""
    row = installed.execute(
        "SELECT SCRIPT_NAME FROM EXA_ALL_SCRIPTS "
        "WHERE SCRIPT_SCHEMA = 'PREPROC_RT' AND SCRIPT_NAME = 'DEMO_AUDIT_V1'"
    ).fetchone()
    assert row is not None, "PREPROC_RT.DEMO_AUDIT_V1 not found"


@pytest.mark.integration
def test_audit_module_passes_luacheck(installed):
    """The audit module source passes luacheck with zero warnings/errors."""
    if shutil.which("luacheck") is None:
        pytest.skip("luacheck binary is not installed")
    result = subprocess.run(
        ["luacheck", "sql/lua/demo_audit_v1.lua"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, (
        f"luacheck failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
