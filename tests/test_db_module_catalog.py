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
import tempfile
from dataclasses import dataclass
from pathlib import Path

import pytest
from _dbfixtures import (
    bucketfs_config,
    bucketfs_delete,
    bucketfs_put,
    build_library_tarball,
    composite_artifact_text,
    composite_library_module,
    composite_object_specs,
    default_artifact_text,
    library_module,
    self_deployed_module,
    standard_library_specs,
    wait_until_ok,
)

from preproc.connection import connect_from_env, missing_env_vars
from preproc.module.registry import encode_objects

pytestmark = pytest.mark.integration

_REPO_ROOT = Path(__file__).parent.parent
_LUA_DIR = _REPO_ROOT / "sql" / "lua"
_CATALOG_LUA_PATH = _LUA_DIR / "module_catalog.lua"
_MODULE_REGISTER_LUA_PATH = _LUA_DIR / "module_register.lua"
_MODULE_RETIRE_LUA_PATH = _LUA_DIR / "module_retire.lua"

_CATALOG_COLUMNS = (
    "name",
    "version",
    "phase",
    "deploy_mode",
    "installed",
    "object_count",
    "objects",
)

_STATE_TABLES = ("CONFIG", "SETTINGS", "RESOLUTION", "MODULES")

# ---------------------------------------------------------------------------
# module/db-catalog: object-inventory reporting (one test per Scenario in
# specs/_plans/change-module-artifact-to-composite/module/db-catalog).
# ---------------------------------------------------------------------------

_CATALOG_INV_CONNECTION = "PREPROC_TEST_BFS_CATALOG_INV"
_CATALOG_INV_BUCKET_PATH = "preproc-lib-catalog-inventory-0.1.0.tar.gz"
_CATALOG_V1_CONNECTION = "PREPROC_TEST_BFS_CATALOG_V1"
_CATALOG_V1_BUCKET_PATH = "preproc-lib-catalog-v1-0.1.0.tar.gz"

_QUAD_MODULE_NAME = "catalog_quad_mod"
_SINGLE_MODULE_NAME = "catalog_single_mod"
_OOS_MODULE_NAME = "catalog_oos_mod"
_OOS_SCHEMA = "CATALOG_OOS_TARGET_SCHEMA"
_SELF_DEPLOYED_MODULE_NAME = "catalog_self_mod"


def _upper(name: str) -> str:
    return name.upper().replace("-", "_")


def _quad_object_specs(name: str, version: int) -> list[dict[str, str]]:
    """A 4-object inventory: composite_object_specs's 3 plus one TABLE."""
    specs = composite_object_specs(name, version)
    specs.append({"type": "TABLE", "name": f"PREPROC_RT.{_upper(name)}_STATE_V{version}"})
    return specs


def _quad_artifact_text(name: str, version: int, function: str) -> str:
    """The 4-statement artifact matching ``_quad_object_specs`` in file order."""
    text = composite_artifact_text(name, version, function)
    return text + f"CREATE TABLE PREPROC_RT.{_upper(name)}_STATE_V{version}(x INT)\n;\n"


def _oos_object_specs(name: str, version: int) -> list[dict[str, str]]:
    """A 2-object inventory whose second object lives outside PREPROC_RT/PREPROC."""
    return [
        {"type": "LUA SCRIPT", "name": f"PREPROC_RT.{_upper(name)}_V{version}"},
        {"type": "TABLE", "name": f"{_OOS_SCHEMA}.{_upper(name)}_STATE_V{version}"},
    ]


def _oos_artifact_text(name: str, version: int, function: str) -> str:
    """The 2-statement artifact matching ``_oos_object_specs`` in file order."""
    entry = default_artifact_text(name, version, function).rstrip("\n") + "\n;\n"
    return entry + f"CREATE TABLE {_OOS_SCHEMA}.{_upper(name)}_STATE_V{version}(x INT)\n;\n"


@dataclass
class CatalogInventorySource:
    """A staged release backing the object-inventory catalog scenarios."""

    source: str


@pytest.fixture(scope="session")
def bucketfs_catalog_inventory_source(resolver_deployed):
    """Stage a release covering the reported-inventory catalog scenarios.

    ``catalog_quad_mod`` declares a 4-object composite inventory (1 Lua entry
    script + 2 Python UDFs + 1 table, matching the plan's "declares four
    objects" example); ``catalog_single_mod`` is an ordinary single-statement
    module (1 declared object); ``catalog_oos_mod`` declares one object
    outside PREPROC_RT; ``catalog_self_mod`` is self-deployed (no artifact
    travels through the registry, so its index entry's ``objects`` is NULL --
    the one shape ``objects`` being the LAST column of MODULE_CATALOG's result
    row risks truncating). Skips when BucketFS write is unconfigured, matching
    every other staging fixture in this suite.
    """
    connection = resolver_deployed
    config = bucketfs_config()
    if config is None:
        pytest.skip(
            "BucketFS write is not configured; set EXASOL_BFS_WRITE_PASSWORD "
            "(and EXASOL_BFS_HOST) to stage the catalog inventory fixture"
        )

    specs = [
        composite_library_module(
            _QUAD_MODULE_NAME,
            version=1,
            function=_QUAD_MODULE_NAME,
            artifact_text=_quad_artifact_text(_QUAD_MODULE_NAME, 1, _QUAD_MODULE_NAME),
            objects=tuple(_quad_object_specs(_QUAD_MODULE_NAME, 1)),
        ),
        library_module(_SINGLE_MODULE_NAME, version=1, function=_SINGLE_MODULE_NAME),
        composite_library_module(
            _OOS_MODULE_NAME,
            version=1,
            function=_OOS_MODULE_NAME,
            artifact_text=_oos_artifact_text(_OOS_MODULE_NAME, 1, _OOS_MODULE_NAME),
            objects=tuple(_oos_object_specs(_OOS_MODULE_NAME, 1)),
        ),
        self_deployed_module(
            _SELF_DEPLOYED_MODULE_NAME,
            version=1,
            phase="EXPAND",
            source=("exasol/catalog-self-mod", "v1"),
        ),
    ]
    work = Path(tempfile.mkdtemp(prefix="preproc-catalog-inventory-"))
    build_library_tarball(work / _CATALOG_INV_BUCKET_PATH, specs)
    try:
        bucketfs_put(
            config, _CATALOG_INV_BUCKET_PATH, (work / _CATALOG_INV_BUCKET_PATH).read_bytes()
        )
    except Exception as exc:  # noqa: BLE001 - environment-dependent staging; skip on failure
        pytest.skip(f"cannot stage the catalog inventory release into BucketFS: {exc}")

    connection.execute(
        f"CREATE OR REPLACE CONNECTION {_CATALOG_INV_CONNECTION} TO '{config.bucketfs_address}'"
    )
    connection.execute(f"GRANT ACCESS ON CONNECTION {_CATALOG_INV_CONNECTION} TO PREPROC_ADMIN")
    source = f"bucketfs:{_CATALOG_INV_CONNECTION}/{_CATALOG_INV_BUCKET_PATH}"

    if not wait_until_ok(connection, source, _QUAD_MODULE_NAME):
        _drop_catalog_inventory(connection, config)
        pytest.skip("BucketFS did not extract/replicate the catalog inventory release in time")

    try:
        yield CatalogInventorySource(source=source)
    finally:
        _drop_catalog_inventory(connection, config)


def _drop_catalog_inventory(connection, config):
    try:
        connection.execute(f"DROP CONNECTION IF EXISTS {_CATALOG_INV_CONNECTION}")
    except Exception:
        pass
    try:
        bucketfs_delete(config, _CATALOG_INV_BUCKET_PATH)
    except Exception:
        pass


@pytest.fixture(scope="session")
def bucketfs_catalog_schema_v1_source(resolver_deployed):
    """Stage a genuine ``schema_version: 1`` index (no ``objects`` key on any entry).

    Skips when BucketFS write is unconfigured, matching every other staging
    fixture in this suite.
    """
    connection = resolver_deployed
    config = bucketfs_config()
    if config is None:
        pytest.skip(
            "BucketFS write is not configured; set EXASOL_BFS_WRITE_PASSWORD "
            "(and EXASOL_BFS_HOST) to stage the catalog schema-version-1 fixture"
        )

    work = Path(tempfile.mkdtemp(prefix="preproc-catalog-v1-"))
    build_library_tarball(
        work / _CATALOG_V1_BUCKET_PATH, standard_library_specs(), schema_version=1
    )
    try:
        bucketfs_put(config, _CATALOG_V1_BUCKET_PATH, (work / _CATALOG_V1_BUCKET_PATH).read_bytes())
    except Exception as exc:  # noqa: BLE001 - environment-dependent staging; skip on failure
        pytest.skip(f"cannot stage the schema-version-1 release into BucketFS: {exc}")

    connection.execute(
        f"CREATE OR REPLACE CONNECTION {_CATALOG_V1_CONNECTION} TO '{config.bucketfs_address}'"
    )
    connection.execute(f"GRANT ACCESS ON CONNECTION {_CATALOG_V1_CONNECTION} TO PREPROC_ADMIN")
    source = f"bucketfs:{_CATALOG_V1_CONNECTION}/{_CATALOG_V1_BUCKET_PATH}"

    if not wait_until_ok(connection, source, "cast_shorthand"):
        _drop_catalog_schema_v1(connection, config)
        pytest.skip("BucketFS did not extract/replicate the schema-version-1 release in time")

    try:
        yield source
    finally:
        _drop_catalog_schema_v1(connection, config)


def _drop_catalog_schema_v1(connection, config):
    try:
        connection.execute(f"DROP CONNECTION IF EXISTS {_CATALOG_V1_CONNECTION}")
    except Exception:
        pass
    try:
        bucketfs_delete(config, _CATALOG_V1_BUCKET_PATH)
    except Exception:
        pass


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
        "name, version, source, ref, sha256, script_name, function_name, phase, deploy_mode, "
        "source_url, library_version, objects) "
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
        "{n}, {v}, {src}, {rf}, {sh}, {sn}, {fn}, {ph}, {dm}, NULL, NULL, NULL)",
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


def test_catalog_reports_object_inventory(bucketfs_catalog_inventory_source, catalog_deployed):
    """A composite module's row reports its full object inventory; a single-statement
    module's row reports one.

    ``catalog_quad_mod`` declares 4 objects (1 Lua entry script + 2 Python UDFs +
    1 table); its row's ``object_count`` MUST be 4 and its ``objects`` MUST expose
    every declared object's type and fully-qualified name. ``catalog_single_mod``'s
    row MUST report an object count of 1.
    """
    conn = catalog_deployed
    rows = _catalog_rows(conn, bucketfs_catalog_inventory_source.source)
    by_name = {row["name"]: row for row in rows}

    quad = by_name[_QUAD_MODULE_NAME]
    assert quad["object_count"] == 4
    assert quad["objects"] == encode_objects(_quad_object_specs(_QUAD_MODULE_NAME, 1))

    single = by_name[_SINGLE_MODULE_NAME]
    assert single["object_count"] == 1
    assert single["objects"] == encode_objects(
        [{"type": "LUA SCRIPT", "name": f"PREPROC_RT.{_upper(_SINGLE_MODULE_NAME)}_V1"}]
    )


def test_catalog_reveals_out_of_schema_objects(bucketfs_catalog_inventory_source, catalog_deployed):
    """An object outside PREPROC_RT is named, schema and all, without being installed.

    ``catalog_oos_mod`` declares one object in ``CATALOG_OOS_TARGET_SCHEMA`` (a
    schema the module never installed). The catalog MUST report it by its
    fully-qualified name, and MUST NOT have created it or its schema.
    """
    conn = catalog_deployed
    rows = _catalog_rows(conn, bucketfs_catalog_inventory_source.source)
    by_name = {row["name"]: row for row in rows}

    oos = by_name[_OOS_MODULE_NAME]
    assert oos["object_count"] == 2
    expected_objects = _oos_object_specs(_OOS_MODULE_NAME, 1)
    assert oos["objects"] == encode_objects(expected_objects)
    assert f"{_OOS_SCHEMA}." in oos["objects"]

    schema_exists = conn.execute(
        "SELECT COUNT(*) FROM EXA_ALL_OBJECTS WHERE OBJECT_TYPE = 'SCHEMA' AND OBJECT_NAME = {s}",
        {"s": _OOS_SCHEMA},
    ).fetchall()[0][0]
    assert schema_exists == 0, "MODULE_CATALOG must never create an object it merely reports"


def test_catalog_reports_null_objects_for_a_self_deployed_entry(
    bucketfs_catalog_inventory_source, catalog_deployed
):
    """A self-deployed entry's row reports objects NULL / object_count 0, undamaged.

    ``catalog_self_mod`` carries no artifact, so its resolver row's ``objects``
    column (r[19]) is SQL NULL, which the Lua UDF may receive as either Lua
    ``nil`` or the ``null`` sentinel. ``objects`` sits in the LAST position of
    MODULE_CATALOG's result row: an uncoerced Lua ``nil`` there would shorten the
    row's table constructor and break the declared 7-column result contract
    (module_catalog.lua's ``objects_out`` coercion is what prevents that,
    mirroring module_install.lua's own trailing-nullable-column coercion). Every
    other column on this row is asserted too, so a truncation that silently
    shifted values rather than raising would still be caught.
    """
    conn = catalog_deployed
    rows = _catalog_rows(conn, bucketfs_catalog_inventory_source.source)
    by_name = {row["name"]: row for row in rows}

    self_deployed = by_name[_SELF_DEPLOYED_MODULE_NAME]
    assert self_deployed["phase"] == "EXPAND"
    assert self_deployed["deploy_mode"] == "self-deployed"
    assert self_deployed["installed"] is False
    assert self_deployed["object_count"] == 0
    assert self_deployed["objects"] is None


def test_catalog_v1_entry_single_object(bucketfs_catalog_schema_v1_source, catalog_deployed):
    """A schema_version-1 index entry (no ``objects`` key) reports a single-object inventory.

    Each entry's row MUST report an object count of 1, naming the entry's own
    ``script_name`` as the single declared object.
    """
    conn = catalog_deployed
    rows = _catalog_rows(conn, bucketfs_catalog_schema_v1_source)
    by_name = {row["name"]: row for row in rows}

    for module_name in ("cast_shorthand", "trailing_comma"):
        row = by_name[module_name]
        assert row["object_count"] == 1
        expected_script = f"PREPROC_RT.{_upper(module_name)}_V1"
        assert row["objects"] == encode_objects([{"type": "LUA SCRIPT", "name": expected_script}])
