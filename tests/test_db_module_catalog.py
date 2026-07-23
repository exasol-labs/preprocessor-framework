"""Integration tests for PREPROC.MODULE_CATALOG against a live Exasol.

One test per Scenario in the ``module/db-catalog`` spec. ``catalog_deployed``
deploys ``PREPROC.MODULE_CATALOG`` directly (Group C's ``install.py`` wiring is a
separate task), reusing the shared ``resolver_deployed`` / ``bucketfs_library_source``
fixtures from ``tests/conftest.py`` so the resolver and a staged BucketFS release
(``cast_shorthand`` v1 + ``trailing_comma`` v1) need not be re-authored here.

``PREPROC.MODULES`` is a shared, session-wide provenance table; every test that
writes a provenance row for ``cast_shorthand``/``trailing_comma`` removes it in a
``finally`` block so no state leaks into other suites (matching the DB-isolation
discipline already used by ``tests/test_module_lifecycle.py``).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from preproc.connection import connect_from_env, missing_env_vars

pytestmark = pytest.mark.integration

_REPO_ROOT = Path(__file__).parent.parent
_LUA_DIR = _REPO_ROOT / "sql" / "lua"
_CATALOG_LUA_PATH = _LUA_DIR / "module_catalog.lua"
_MODULE_REGISTER_LUA_PATH = _LUA_DIR / "module_register.lua"
_MODULE_RETIRE_LUA_PATH = _LUA_DIR / "module_retire.lua"

_CATALOG_COLUMNS = ("name", "version", "phase", "deploy_mode", "installed")

_STATE_TABLES = ("CONFIG", "SETTINGS", "RESOLUTION", "MODULES")


@pytest.fixture(scope="session")
def catalog_deployed(resolver_deployed):
    """Deploy PREPROC.MODULE_CATALOG (plus MODULE_REGISTER/MODULE_RETIRE) once.

    MODULE_REGISTER and MODULE_RETIRE are pre-existing, stable scripts (owned by
    the module lifecycle feature, unchanged here); this suite deploys them itself
    -- mirroring ``tests/test_module_lifecycle.py`` -- only to stage the
    provenance rows the ``installed?`` scenarios assert against, independently of
    Group C's ``install.py`` wiring order.
    """
    conn = resolver_deployed
    conn.execute("OPEN SCHEMA PREPROC")
    conn.execute(
        "CREATE OR REPLACE LUA SCRIPT PREPROC.MODULE_CATALOG(source) RETURNS TABLE AS\n"
        + _CATALOG_LUA_PATH.read_text(encoding="utf-8")
    )
    conn.execute(
        "CREATE OR REPLACE LUA SCRIPT PREPROC.MODULE_REGISTER("
        "name, version, source, ref, sha256, script_name, function_name, phase, deploy_mode) "
        "RETURNS TABLE AS\n" + _MODULE_REGISTER_LUA_PATH.read_text(encoding="utf-8")
    )
    conn.execute(
        "CREATE OR REPLACE LUA SCRIPT PREPROC.MODULE_RETIRE(name, version) RETURNS TABLE AS\n"
        + _MODULE_RETIRE_LUA_PATH.read_text(encoding="utf-8")
    )
    return conn


def _catalog_rows(conn, source):
    rows = conn.execute("EXECUTE SCRIPT PREPROC.MODULE_CATALOG({src})", {"src": source}).fetchall()
    return [dict(zip(_CATALOG_COLUMNS, row)) for row in rows]


def _register(
    conn,
    *,
    name,
    version,
    script_name,
    function_name,
    phase="TRANSLATE",
    deploy_mode="library-deployed",
):
    conn.execute(
        "EXECUTE SCRIPT PREPROC.MODULE_REGISTER("
        "{n}, {v}, {src}, {rf}, {sh}, {sn}, {fn}, {ph}, {dm})",
        {
            "n": name,
            "v": version,
            "src": "library",
            "rf": "v1",
            "sh": "a" * 64,
            "sn": script_name,
            "fn": function_name,
            "ph": phase,
            "dm": deploy_mode,
        },
    ).fetchall()


def _retire(conn, name, version):
    conn.execute(
        "EXECUTE SCRIPT PREPROC.MODULE_RETIRE({n}, {v})", {"n": name, "v": version}
    ).fetchall()


def _forget(conn, name, version):
    conn.execute(
        "DELETE FROM PREPROC.MODULES WHERE name = {n} AND version = {v}", {"n": name, "v": version}
    )
    conn.commit()


def _snapshot(conn):
    """Sorted rows of every framework-mutable table, for a before/after equality check."""
    return {
        "CONFIG": conn.execute("SELECT * FROM PREPROC.CONFIG ORDER BY rule_id").fetchall(),
        "SETTINGS": conn.execute("SELECT * FROM PREPROC.SETTINGS").fetchall(),
        "RESOLUTION": conn.execute(
            "SELECT * FROM PREPROC.RESOLUTION ORDER BY phase, ordinal"
        ).fetchall(),
        "MODULES": conn.execute("SELECT * FROM PREPROC.MODULES ORDER BY name, version").fetchall(),
    }


def test_catalog_lists_with_installed_state(bucketfs_library_source, catalog_deployed):
    """A staged BucketFS source's advertised modules list with each one's installed state."""
    conn = catalog_deployed
    _register(
        conn,
        name="cast_shorthand",
        version=1,
        script_name="PREPROC_RT.CAST_SHORTHAND_V1",
        function_name="cast_shorthand",
    )
    try:
        rows = _catalog_rows(conn, bucketfs_library_source.source)
    finally:
        _forget(conn, "cast_shorthand", 1)

    by_name = {row["name"]: row for row in rows}
    assert set(by_name) == {"cast_shorthand", "trailing_comma"}
    for row in rows:
        assert row["phase"] is not None
        assert row["deploy_mode"] is not None
    assert by_name["cast_shorthand"]["installed"] is True
    assert by_name["trailing_comma"]["installed"] is False


def test_catalog_read_only(bucketfs_library_source, catalog_deployed):
    """MODULE_CATALOG must not modify CONFIG, SETTINGS, RESOLUTION, or MODULES."""
    conn = catalog_deployed
    before = _snapshot(conn)

    _catalog_rows(conn, bucketfs_library_source.source)

    after = _snapshot(conn)
    for table in _STATE_TABLES:
        assert after[table] == before[table], f"PREPROC.{table} changed after MODULE_CATALOG"


def test_catalog_https_no_egress_network_error(catalog_deployed):
    """An https source whose host is unreachable fails with a network error naming it, no rows."""
    conn = catalog_deployed
    source = "https://unreachable-preproc.invalid/registry/index.json"

    with pytest.raises(Exception) as exc_info:
        _catalog_rows(conn, source)

    assert "unreachable-preproc.invalid" in str(exc_info.value)


def test_catalog_installed_excludes_retired(bucketfs_library_source, catalog_deployed):
    """A version whose only provenance row is 'retired' is reported as NOT installed."""
    conn = catalog_deployed
    _register(
        conn,
        name="cast_shorthand",
        version=1,
        script_name="PREPROC_RT.CAST_SHORTHAND_V1",
        function_name="cast_shorthand",
    )
    _retire(conn, "cast_shorthand", 1)
    try:
        rows = _catalog_rows(conn, bucketfs_library_source.source)
    finally:
        _forget(conn, "cast_shorthand", 1)

    by_name = {row["name"]: row for row in rows}
    assert by_name["cast_shorthand"]["installed"] is False


def test_catalog_non_admin_rejected(catalog_deployed):
    """A non-admin caller cannot run PREPROC.MODULE_CATALOG.

    Mirrors ``test_resolver_admin_only``: skips (rather than a false pass) where
    the env-configured account cannot mint a second login -- the admin boundary
    is also asserted structurally by the schema-layout suite.
    """
    if missing_env_vars():
        pytest.skip("no Exasol instance is configured")

    admin = catalog_deployed
    non_admin = "PREPROC_TEST_CATALOG_NOADMIN"
    password = "pw_PREPROC_catalog_1"
    other = None
    try:
        try:
            admin.execute(f"DROP USER {non_admin} CASCADE")
        except Exception:
            pass
        admin.execute(f'CREATE USER {non_admin} IDENTIFIED BY "{password}"')
        admin.execute(f"GRANT CREATE SESSION TO {non_admin}")
    except Exception as exc:
        pytest.skip(f"cannot create a non-admin user in this environment: {exc}")

    try:
        prev_user = os.environ.get("EXASOL_USER")
        prev_pw = os.environ.get("EXASOL_PASSWORD")
        os.environ["EXASOL_USER"] = non_admin
        os.environ["EXASOL_PASSWORD"] = password
        try:
            other = connect_from_env()
        finally:
            if prev_user is not None:
                os.environ["EXASOL_USER"] = prev_user
            if prev_pw is not None:
                os.environ["EXASOL_PASSWORD"] = prev_pw

        with pytest.raises(Exception):
            other.execute(
                "EXECUTE SCRIPT PREPROC.MODULE_CATALOG("
                "'bucketfs:PREPROC_TEST_BFS/preproc-lib-0.1.0.tar.gz')"
            )
    finally:
        if other is not None:
            other.close()
        try:
            admin.execute(f"DROP USER {non_admin} CASCADE")
        except Exception:
            pass
