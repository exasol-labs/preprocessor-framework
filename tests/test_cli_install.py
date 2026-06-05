"""Tests for the ``preproc install`` CLI verb.

Integration tests (marked ``integration``) require an env-configured Exasol
instance. Unit tests run without any database. The subprocess tests invoke the
CLI via ``uv run preproc install`` so they exercise the installed entry point
rather than import-time resolution.
"""

import os
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _run_cli(*extra_args, env=None, timeout=30):
    """Run ``uv run preproc install`` as a subprocess, returning CompletedProcess."""
    return subprocess.run(
        ["uv", "run", "preproc", "install", *extra_args],
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


@pytest.mark.integration
def test_install_clean_database(installed):
    """After install, spot-check one object from each install file.

    The ``installed`` fixture already ran install; we just verify the resulting
    state covers schema, table, view, grant, and MASTER script.
    """
    # Schema (10_schemas.sql)
    row = installed.execute(
        "SELECT SCHEMA_NAME FROM EXA_ALL_SCHEMAS WHERE SCHEMA_NAME = 'PREPROC'"
    ).fetchone()
    assert row is not None, "PREPROC schema not found"

    # Table (20_tables.sql)
    row = installed.execute(
        "SELECT TABLE_NAME FROM EXA_ALL_TABLES "
        "WHERE TABLE_SCHEMA = 'PREPROC' AND TABLE_NAME = 'SETTINGS'"
    ).fetchone()
    assert row is not None, "PREPROC.SETTINGS table not found"

    # View (40_views.sql)
    row = installed.execute(
        "SELECT VIEW_NAME FROM EXA_ALL_VIEWS "
        "WHERE VIEW_SCHEMA = 'PREPROC_RT' AND VIEW_NAME = 'MY_PIPELINE'"
    ).fetchone()
    assert row is not None, "PREPROC_RT.MY_PIPELINE view not found"

    # Grant (50_grants.sql) — SELECT on PREPROC_RT granted to PUBLIC.
    # Schema-level grants: OBJECT_SCHEMA is NULL, the schema is the OBJECT_NAME.
    row = installed.execute(
        "SELECT PRIVILEGE FROM EXA_DBA_OBJ_PRIVS "
        "WHERE OBJECT_NAME = 'PREPROC_RT' AND OBJECT_TYPE = 'SCHEMA' "
        "AND GRANTEE = 'PUBLIC' AND PRIVILEGE = 'SELECT'"
    ).fetchone()
    assert row is not None, "SELECT on PREPROC_RT not granted to PUBLIC"

    # MASTER script (deploy_master step)
    row = installed.execute(
        "SELECT SCRIPT_NAME FROM EXA_ALL_SCRIPTS "
        "WHERE SCRIPT_SCHEMA = 'PREPROC_RT' AND SCRIPT_NAME = 'MASTER'"
    ).fetchone()
    assert row is not None, "PREPROC_RT.MASTER script not found"


@pytest.mark.integration
def test_install_prints_not_activates(installed):
    """CLI must print the ALTER SYSTEM statement but not execute it.

    The system preprocessor setting is captured before and after the CLI run;
    it must be unchanged.
    """
    # Capture the current system preprocessor value.
    before_row = installed.execute(
        "SELECT SYSTEM_VALUE FROM EXA_PARAMETERS WHERE PARAMETER_NAME = 'SQL_PREPROCESSOR_SCRIPT'"
    ).fetchone()
    assert before_row is not None, (
        "SQL_PREPROCESSOR_SCRIPT not found in EXA_PARAMETERS — parameter name assumption broken"
    )
    before_value = before_row[0]

    result = _run_cli(env=os.environ.copy())

    assert result.returncode == 0, (
        f"CLI exited with {result.returncode}.\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "ALTER SYSTEM SET sql_preprocessor_script" in result.stdout, (
        "Expected activation statement in stdout"
    )

    # System setting must be unchanged.
    after_row = installed.execute(
        "SELECT SYSTEM_VALUE FROM EXA_PARAMETERS WHERE PARAMETER_NAME = 'SQL_PREPROCESSOR_SCRIPT'"
    ).fetchone()
    assert after_row is not None, (
        "SQL_PREPROCESSOR_SCRIPT not found in EXA_PARAMETERS after install — "
        "parameter name assumption broken"
    )
    after_value = after_row[0]
    assert before_value == after_value, (
        f"System preprocessor changed: was {before_value!r}, now {after_value!r}"
    )


@pytest.mark.integration
def test_install_rerun_idempotent(installed):
    """Running the CLI twice must both exit 0 with no error output."""
    for run_n in (1, 2):
        result = _run_cli(env=os.environ.copy())
        assert result.returncode == 0, (
            f"Run {run_n}: CLI exited {result.returncode}.\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )


def test_missing_env_fails_fast(monkeypatch):
    """CLI must exit 2 and name all missing variables when env is empty.

    No database connection is attempted because the missing-env check happens
    before any connection code runs.
    """
    scrubbed = {
        k: v
        for k, v in os.environ.items()
        if k not in ("EXASOL_DSN", "EXASOL_USER", "EXASOL_PASSWORD")
    }
    result = subprocess.run(
        ["uv", "run", "preproc", "install"],
        cwd=_REPO_ROOT,
        env=scrubbed,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 2, (
        f"Expected exit 2, got {result.returncode}.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    combined = result.stdout + result.stderr
    for var in ("EXASOL_DSN", "EXASOL_USER", "EXASOL_PASSWORD"):
        assert var in combined, f"Missing variable {var!r} not mentioned in output"


def test_unreachable_instance_errors():
    """CLI must exit 1 and mention connection failure for an unreachable DSN.

    Uses 127.0.0.1:9 (a closed loopback port) to force an immediate refusal.
    """
    env = os.environ.copy()
    env["EXASOL_DSN"] = "127.0.0.1:9"
    env["EXASOL_USER"] = "testuser"
    env["EXASOL_PASSWORD"] = "testpassword"

    result = subprocess.run(
        ["uv", "run", "preproc", "install"],
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 1, (
        f"Expected exit 1, got {result.returncode}.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    combined = result.stdout + result.stderr
    assert any(kw in combined.lower() for kw in ("connection", "connect", "error")), (
        f"Expected connection error mention in output:\n{combined}"
    )


@pytest.mark.integration
def test_install_deploys_demo_modules_and_udf(installed):
    """Install deploys the three demo objects into PREPROC_RT.

    The demo TRANSLATE/REWRITE modules and the companion transpile UDF are part
    of the install deltas; all three must land in the runtime schema.
    """
    expected = ("DEMO_TRANSLATE_V1", "DEMO_AUDIT_V1", "DEMO_SQLGLOT_TRANSPILE_V1")
    for script_name in expected:
        row = installed.execute(
            "SELECT SCRIPT_NAME FROM EXA_ALL_SCRIPTS "
            "WHERE SCRIPT_SCHEMA = 'PREPROC_RT' AND SCRIPT_NAME = {n}",
            {"n": script_name},
        ).fetchone()
        assert row is not None, f"PREPROC_RT.{script_name} not deployed by install"


@pytest.mark.integration
def test_demo_modules_and_udf_public_executable(installed):
    """The schema-level EXECUTE grant to PUBLIC covers the new demo objects.

    The demo modules and UDF rely on the existing schema-level grant rather than
    per-object grants, so EXECUTE ON SCHEMA PREPROC_RT TO PUBLIC must be present.
    """
    row = installed.execute(
        "SELECT PRIVILEGE FROM EXA_DBA_OBJ_PRIVS "
        "WHERE OBJECT_NAME = 'PREPROC_RT' AND OBJECT_TYPE = 'SCHEMA' "
        "AND GRANTEE = 'PUBLIC' AND PRIVILEGE = 'EXECUTE'"
    ).fetchone()
    assert row is not None, "EXECUTE on PREPROC_RT not granted to PUBLIC"


@pytest.mark.integration
def test_install_deploys_control_layer(installed):
    """After install, all control scripts must exist in EXA_ALL_SCRIPTS.

    Verifies that _deploy_admin_scripts deployed the full set of PREPROC admin
    scripts and that _deploy_master deployed CONTROL_SUGAR_V1 into PREPROC_RT.
    """
    preproc_scripts = (
        "REFRESH_CORE",
        "RULE_ADD",
        "RULE_DROP",
        "RULE_ENABLE",
        "RULE_DISABLE",
        "RULE_LIST",
        "SET_MODE",
        "SET_SELF_DISABLE",
    )
    for script_name in preproc_scripts:
        row = installed.execute(
            "SELECT SCRIPT_NAME FROM EXA_ALL_SCRIPTS "
            "WHERE SCRIPT_SCHEMA = 'PREPROC' AND SCRIPT_NAME = {n}",
            {"n": script_name},
        ).fetchone()
        assert row is not None, f"PREPROC.{script_name} not deployed by install"

    row = installed.execute(
        "SELECT SCRIPT_NAME FROM EXA_ALL_SCRIPTS "
        "WHERE SCRIPT_SCHEMA = 'PREPROC_RT' AND SCRIPT_NAME = 'CONTROL_SUGAR_V1'"
    ).fetchone()
    assert row is not None, "PREPROC_RT.CONTROL_SUGAR_V1 not deployed by install"


@pytest.mark.integration
def test_install_seeds_sugar_rule(installed):
    """After install, PREPROC.CONFIG must contain the CONTROL_SUGAR_V1 bootstrap rule.

    Verifies that 70_control_sugar_seed.sql seeded an enabled EXPAND rule for
    the PREPROC_ADMIN role pointing at PREPROC_RT.CONTROL_SUGAR_V1 with
    function_name 'expand'.
    """
    row = installed.execute(
        "SELECT rule_id, scope_type, scope_value, phase, function_name, enabled "
        "FROM PREPROC.CONFIG "
        "WHERE script_name = 'PREPROC_RT.CONTROL_SUGAR_V1'"
    ).fetchone()
    assert row is not None, "No CONFIG rule for PREPROC_RT.CONTROL_SUGAR_V1 found after install"
    _rule_id, scope_type, scope_value, phase, function_name, enabled = row
    assert scope_type == "ROLE", f"Expected scope_type='ROLE', got {scope_type!r}"
    assert scope_value == "PREPROC_ADMIN", (
        f"Expected scope_value='PREPROC_ADMIN', got {scope_value!r}"
    )
    assert phase == "EXPAND", f"Expected phase='EXPAND', got {phase!r}"
    assert function_name == "expand", f"Expected function_name='expand', got {function_name!r}"
    assert enabled is True, f"Expected enabled=TRUE, got {enabled!r}"
