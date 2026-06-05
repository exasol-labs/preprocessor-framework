"""Behaviour integration tests for the MASTER preprocessor skeleton.

These tests verify the safety contract of ``PREPROC_RT.MASTER`` against a live
Exasol instance: installation, byte-for-byte pass-through, the ALTER guard, and
fail-open on a broken pipeline lookup. They activate the preprocessor with
SESSION scope only — never ALTER SYSTEM — and unconditionally restore session
and schema state in ``finally`` so a failure cannot poison the session-scoped
connection shared with the rest of the suite.

The luacheck test needs no database and runs wherever the ``luacheck`` binary is
available; it skips cleanly otherwise.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]

_ACTIVATE = "ALTER SESSION SET sql_preprocessor_script = PREPROC_RT.MASTER"
_DEACTIVATE = "ALTER SESSION SET sql_preprocessor_script = ''"


def _activate(connection):
    """Activate MASTER as the session preprocessor."""
    connection.execute(_ACTIVATE)


def _deactivate(connection):
    """Clear the session preprocessor; safe to call unconditionally."""
    connection.execute(_DEACTIVATE)


@pytest.mark.integration
def test_master_script_installed(installed):
    """After install, PREPROC_RT.MASTER is present in the script catalogue."""
    rows = installed.execute(
        "SELECT SCRIPT_NAME FROM EXA_ALL_SCRIPTS "
        "WHERE SCRIPT_SCHEMA = 'PREPROC_RT' AND SCRIPT_NAME = 'MASTER'"
    ).fetchall()
    assert rows == [("MASTER",)]


@pytest.mark.integration
def test_normal_statement_passthrough(installed):
    """An ordinary SELECT executes unchanged while MASTER is active."""
    _activate(installed)
    try:
        value = installed.execute("SELECT 987654321 AS PROBE").fetchval()
        assert value == 987654321
    finally:
        _deactivate(installed)


@pytest.mark.integration
def test_alter_statements_guarded(installed):
    """ALTER SESSION is guarded (returns early) and the session stays usable.

    The guard must return before any pipeline lookup, so the ALTER itself
    succeeds and a following ordinary statement still works. If the guard were
    absent the ALTER could provoke lookup/recursion noise; asserting both the
    ALTER and a subsequent SELECT succeed pins the guarded behaviour.
    """
    _activate(installed)
    try:
        installed.execute("ALTER SESSION SET QUERY_TIMEOUT = 600")
        value = installed.execute("SELECT 424242 AS PROBE").fetchval()
        assert value == 424242
    finally:
        try:
            installed.execute("ALTER SESSION SET QUERY_TIMEOUT = 0")
        finally:
            _deactivate(installed)


@pytest.mark.integration
def test_lookup_failure_fails_open(installed):
    """A broken pipeline lookup fails open: statements still execute.

    The pipeline view is renamed out from under MASTER so its sole data access
    (``SELECT ... FROM PREPROC_RT.MY_PIPELINE``) raises. MASTER wraps that read
    in pcall and returns the statement unchanged, so ``SELECT 1`` must still
    return 1. The view is renamed back unconditionally; the preprocessor is
    deactivated unconditionally even if the restore raises.
    """
    installed.execute("RENAME VIEW PREPROC_RT.MY_PIPELINE TO MY_PIPELINE_BROKEN_TEST")
    try:
        _activate(installed)
        try:
            value = installed.execute("SELECT 1").fetchval()
            assert value == 1
        finally:
            _deactivate(installed)
    finally:
        installed.execute("RENAME VIEW PREPROC_RT.MY_PIPELINE_BROKEN_TEST TO MY_PIPELINE")


def test_master_passes_luacheck():
    """The Lua sources pass luacheck with the project configuration."""
    if shutil.which("luacheck") is None:
        pytest.skip("luacheck binary is not installed")

    try:
        result = subprocess.run(
            ["luacheck", "sql/lua/", "--config", ".luacheckrc"],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        pytest.skip(f"luacheck is present on PATH but not executable: {exc}")

    assert result.returncode == 0, result.stdout + result.stderr
