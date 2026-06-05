"""Tests for the project bootstrap scaffolding.

Covers: uv environment, connection fixture, skip behaviour, luacheck, and ruff.
No test here must fail when the Exasol env vars are absent — integration tests
use the ``db`` fixture (which skips cleanly) and pure-unit tests monkeypatch env.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from preproc.connection import missing_env_vars

_REPO_ROOT = Path(__file__).resolve().parents[1]


def test_uv_sync_environment():
    """``uv sync`` must exit 0, indicating a healthy, reproducible environment."""
    result = subprocess.run(
        ["uv", "sync"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, (
        f"uv sync failed.\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )


@pytest.mark.integration
def test_connection_fixture_targets_env(db):
    """The ``db`` fixture must yield a working connection to the env-configured instance.

    Verifies by running a trivial query and checking the DSN matches the env var.
    """
    value = db.execute("SELECT CURRENT_USER").fetchval()
    assert value is not None, "SELECT CURRENT_USER returned None"

    expected_dsn = os.environ.get("EXASOL_DSN", "")
    assert expected_dsn, "EXASOL_DSN must be set for this test"
    # pyexasol keeps the original connect kwargs in the public `options` dict.
    actual_dsn = db.options.get("dsn")
    assert actual_dsn == expected_dsn, (
        f"Connection DSN {actual_dsn!r} does not match EXASOL_DSN {expected_dsn!r}"
    )


def test_integration_skips_without_instance(monkeypatch):
    """``missing_env_vars`` must return all three names when env is scrubbed
    and an empty list when all three are set to dummy values.
    """
    for var in ("EXASOL_DSN", "EXASOL_USER", "EXASOL_PASSWORD"):
        monkeypatch.delenv(var, raising=False)

    missing = missing_env_vars()
    assert set(missing) == {"EXASOL_DSN", "EXASOL_USER", "EXASOL_PASSWORD"}, (
        f"Expected all three vars missing, got: {missing}"
    )

    monkeypatch.setenv("EXASOL_DSN", "localhost:8563")
    monkeypatch.setenv("EXASOL_USER", "dummy")
    monkeypatch.setenv("EXASOL_PASSWORD", "dummy")

    assert missing_env_vars() == [], "Expected no missing vars when all are set"


def test_luacheck_passes():
    """Lua sources under sql/lua/ must pass luacheck with the project config."""
    if shutil.which("luacheck") is None:
        pytest.skip("luacheck binary is not installed")

    result = subprocess.run(
        ["luacheck", "sql/lua/", "--config", ".luacheckrc"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_ruff_clean():
    """``uv run ruff check .`` must exit 0 (no lint violations)."""
    result = subprocess.run(
        ["uv", "run", "ruff", "check", "."],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, (
        f"ruff found violations.\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
