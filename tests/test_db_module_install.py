"""Integration tests for PREPROC.MODULE_INSTALL against a live Exasol.

One test per Scenario in the ``module/db-install`` spec. Each deploys
PREPROC.MODULE_INSTALL directly (via the ``module_install_deployed`` fixture,
mirroring how the resolver UDF is deployed — Group C wires it into
``install.py`` later) and drives it over ``EXECUTE SCRIPT``.

Which tests run live versus skip cleanly (matching the repo's ``db`` skip
discipline):

* Scenarios that need a **staged source** reuse Group A's shared
  ``bucketfs_library_source`` fixture (or a test-local staging fixture). BucketFS
  READS are public but WRITES need ``EXASOL_BFS_WRITE_PASSWORD`` (a distinct
  secret), so these skip cleanly until it is set — and run the moment it is.
* ``test_install_https_no_egress_no_partial_deploy`` needs neither a staged
  source nor egress: it drives an unreachable ``https:`` source so the resolver
  signals a network error and the install aborts before any write — it runs live.
* The composite scenarios share ONE staged release (``composite_source``), so the
  BucketFS upload + auto-extract + replication wait is paid once for all seven
  composite fixtures.
* ``test_install_non_admin_rejected`` needs CREATE USER + a second login and
  skips where the env account cannot mint one (the admin boundary is also
  asserted structurally by the schema-layout suite).
* ``test_install_https_when_reachable`` needs a reachable registry base URL
  (``PREPROC_TEST_HTTPS_BASE``) and skips otherwise.
* ``test_module_install_passes_luacheck`` runs with no database at all.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path

import pytest
from _corpus import load_split_corpus, load_split_error_cases
from _dbfixtures import (
    BucketFsConfig,
    bucketfs_config,
    bucketfs_delete,
    bucketfs_put,
    build_library_tarball,
    default_artifact_text,
    library_module,
    self_deployed_module,
    standard_library_specs,
    wait_until_ok,
)
from _lua_splitter import (
    CORPUS_TABLE,
    PROBE_SCRIPT,
    db_probe_body,
    decode_probe_rows,
    lua_interpreter,
    run_local_probe,
    write_local_probe,
)

from preproc.connection import connect_from_env, missing_env_vars
from preproc.module.artifact import ArtifactSyntaxError, split_statements
from preproc.module.deploy import deploy_module
from preproc.module.registry import OBJECTS_MAX_CHARS, encode_objects, read_registry

_REPO = Path(__file__).resolve().parent.parent
_MODULE_INSTALL_LUA = _REPO / "sql" / "lua" / "module_install.lua"

_TAMPER_CONNECTION = "PREPROC_TEST_BFS_INSTALL_TAMPER"
_TAMPER_BUCKET_PATH = "preproc-lib-install-tamper-0.1.0.tar.gz"
_SELF_CONNECTION = "PREPROC_TEST_BFS_INSTALL_SELF"
_SELF_BUCKET_PATH = "preproc-lib-install-self-0.1.0.tar.gz"
_COMPOSITE_CONNECTION = "PREPROC_TEST_BFS_INSTALL_COMPOSITE"
_COMPOSITE_BUCKET_PATH = "preproc-lib-install-composite-0.1.0.tar.gz"

_CAST_SCRIPT = "PREPROC_RT.CAST_SHORTHAND_V1"
_BROKEN_SCRIPT = "PREPROC_RT.BROKEN_MODULE_V1"
_SELF_SCRIPT = "PREPROC_RT.SEMANTIC_VIEWS_V3"
_SELF_MARKER = (
    "CREATE OR REPLACE LUA SCRIPT PREPROC_RT.SEMANTIC_VIEWS_V3() AS\n"
    "-- SELF DEPLOYED MARKER (its own installer deployed this backing script)\n"
    "function semantic_views(sqltext)\n    return sqltext\nend\n"
)

# --- composite-artifact fixtures --------------------------------------------
#
# Each artifact below is built from the same three primitives so a test asserts
# on the object inventory it declares rather than on hand-written SQL. Script
# statements are wrapped in the EXAplus ``--/`` … ``/`` block markers (the shape
# a real composite artifact uses, ADR-058) and the plain DDL is ``;``-terminated,
# so one fixture exercises both halves of the grammar.
#
# ``NOSUCHTYPE`` is the deliberate mid-artifact failure: an unknown column data
# type is rejected by the parser, so the statement splits and derives its object
# cleanly (passing pre-flight) and fails only when it executes.

_BAD_COLUMN_TYPE = "NOSUCHTYPE"
_OUTSIDE_SCHEMA = "PREPROC_TEST_OUTSIDE_SCHEMA"
_INERT_SCHEMA = "PREPROC_TEST_INERT_V1"
_INERT_SQUATTER = f"{_INERT_SCHEMA}.SQUATTER_NOT_IN_INVENTORY"
_RERUN_BASE = "PREPROC_RT.RERUN_COMPOSITE_BASE_V1"

# The width of PREPROC.MODULES.objects (sql/install/22_modules_table.sql), of
# MODULE_REGISTER's CAST, and of the resolver's `objects` column.
#
# Imported, never restated: the pre-flight guard this exercises reads
# module_install.lua's own PINNED copy of the number (Lua cannot import from
# Python), so asserting the CANONICAL value appears in the refusal message makes
# the test double as a drift check on that mirror.
_OBJECTS_MAX_CHARS = OBJECTS_MAX_CHARS
_WIDE_NAME_WIDTH = 100


def _block(statement):
    """Wrap a statement in the EXAplus ``--/`` … ``/`` block markers."""
    return "--/\n" + statement.rstrip("\n") + "\n/\n"


def _terminated(statement):
    """Terminate a plain statement with the ``;`` the grammar splits on."""
    return statement.rstrip("\n") + ";\n"


def _entry_statement(upper, function, version):
    return _block(
        f"CREATE OR REPLACE LUA SCRIPT PREPROC_RT.{upper}_V{version}() AS\n"
        f"function {function}(sqltext)\n"
        "    return sqltext\n"
        "end"
    )


def _python_statement(name):
    return _block(
        f"CREATE OR REPLACE PYTHON3 SCALAR SCRIPT {name}(x VARCHAR(2000))\n"
        "RETURNS VARCHAR(2000) AS\n"
        "def run(ctx):\n"
        "    return ctx.x"
    )


def _table_statement(name):
    return _terminated(f"CREATE OR REPLACE TABLE {name} (k VARCHAR(64))")


def _broken_table_statement(name):
    return _terminated(f"CREATE OR REPLACE TABLE {name} (c {_BAD_COLUMN_TYPE})")


def _composite_spec(name, statements, objects, **kwargs):
    """A library-deployed ModuleSpec carrying a hand-built composite artifact."""
    return library_module(
        name,
        version=1,
        function=name,
        artifact_text="".join(statements),
        objects=tuple(objects),
        **kwargs,
    )


def _ok_composite():
    """5 statements / 4 objects, ordered so file order is observable.

    The VIEW (statement 4) selects from the TABLE (statement 2), so it can only
    be created if the statements ran in file order. The INSERT (statement 3)
    creates nothing, so it must consume no inventory slot and must never appear
    in the compensating-cleanup undo list.
    """
    entry = "PREPROC_RT.COMPOSITE_OK_V1"
    state = "PREPROC_RT.COMPOSITE_OK_STATE_V1"
    view = "PREPROC_RT.COMPOSITE_OK_VIEW_V1"
    helper = "PREPROC_RT.COMPOSITE_OK_HELPER_A_V1"
    statements = [
        _entry_statement("COMPOSITE_OK", "composite_ok", 1),
        _table_statement(state),
        _terminated(f"INSERT INTO {state} VALUES ('seed')"),
        _terminated(f"CREATE OR REPLACE VIEW {view} AS SELECT k FROM {state}"),
        _python_statement(helper),
    ]
    objects = [
        {"type": "LUA SCRIPT", "name": entry},
        {"type": "TABLE", "name": state},
        {"type": "VIEW", "name": view},
        {"type": "PYTHON3 SCALAR SCRIPT", "name": helper},
    ]
    return _composite_spec("composite_ok", statements, objects), objects


def _broken_composite():
    """4 statements whose LAST one is invalid DDL that passes pre-flight."""
    entry = "PREPROC_RT.BROKEN_COMPOSITE_V1"
    helper = "PREPROC_RT.BROKEN_COMPOSITE_HELPER_A_V1"
    state = "PREPROC_RT.BROKEN_COMPOSITE_STATE_V1"
    bad = "PREPROC_RT.BROKEN_COMPOSITE_BAD_V1"
    statements = [
        _entry_statement("BROKEN_COMPOSITE", "broken_composite", 1),
        _python_statement(helper),
        _table_statement(state),
        _broken_table_statement(bad),
    ]
    objects = [
        {"type": "LUA SCRIPT", "name": entry},
        {"type": "PYTHON3 SCALAR SCRIPT", "name": helper},
        {"type": "TABLE", "name": state},
        {"type": "TABLE", "name": bad},
    ]
    return _composite_spec("broken_composite", statements, objects), objects


def _outside_composite():
    """3 statements, the middle one creating a TABLE outside PREPROC_RT."""
    entry = "PREPROC_RT.OUTSIDE_COMPOSITE_V1"
    outside = f"{_OUTSIDE_SCHEMA}.OUTSIDE_TABLE_V1"
    bad = "PREPROC_RT.OUTSIDE_COMPOSITE_BAD_V1"
    statements = [
        _entry_statement("OUTSIDE_COMPOSITE", "outside_composite", 1),
        _table_statement(outside),
        _broken_table_statement(bad),
    ]
    objects = [
        {"type": "LUA SCRIPT", "name": entry},
        {"type": "TABLE", "name": outside},
        {"type": "TABLE", "name": bad},
    ]
    return _composite_spec("outside_composite", statements, objects), objects


def _inert_composite():
    """3 statements declaring a SCHEMA, so cleanup can be made to fail.

    The test seeds a table inside that schema that the inventory does not name.
    Compensating cleanup drops a SCHEMA RESTRICT, never CASCADE, so the squatter
    keeps the schema un-droppable — which is exactly the "cleanup could not drop
    every created object" precondition the inert scenario needs, and doubles as
    proof that best-effort compensation never destroys data.
    """
    entry = "PREPROC_RT.INERT_COMPOSITE_V1"
    bad = "PREPROC_RT.INERT_COMPOSITE_BAD_V1"
    statements = [
        _entry_statement("INERT_COMPOSITE", "inert_composite", 1),
        _terminated(f"CREATE SCHEMA IF NOT EXISTS {_INERT_SCHEMA}"),
        _broken_table_statement(bad),
    ]
    objects = [
        {"type": "LUA SCRIPT", "name": entry},
        {"type": "SCHEMA", "name": _INERT_SCHEMA},
        {"type": "TABLE", "name": bad},
    ]
    return _composite_spec("inert_composite", statements, objects), objects


def _rerun_composite():
    """3 statements whose LAST one fails only while a dependency is absent.

    A VIEW over a base table outside the inventory: the install fails part-way
    while the base table does not exist and converges once it does, so the SAME
    artifact at the SAME version models "re-running an install that failed
    part-way" without needing a second staged release.
    """
    entry = "PREPROC_RT.RERUN_COMPOSITE_V1"
    helper = "PREPROC_RT.RERUN_COMPOSITE_HELPER_A_V1"
    view = "PREPROC_RT.RERUN_COMPOSITE_VIEW_V1"
    statements = [
        _entry_statement("RERUN_COMPOSITE", "rerun_composite", 1),
        _python_statement(helper),
        _terminated(f"CREATE OR REPLACE VIEW {view} AS SELECT k FROM {_RERUN_BASE}"),
    ]
    objects = [
        {"type": "LUA SCRIPT", "name": entry},
        {"type": "PYTHON3 SCALAR SCRIPT", "name": helper},
        {"type": "VIEW", "name": view},
    ]
    return _composite_spec("rerun_composite", statements, objects), objects


def _wide_object_name(index):
    """A long-but-legal qualified name (Exasol allows 128 characters), fixed width.

    Padding the name to ``_WIDE_NAME_WIDTH`` is what lets the over-wide-inventory
    fixture reach the column limit in roughly half the objects a minimal name
    would need, and it models the same overflow a real subsystem module hits —
    through name LENGTH rather than object COUNT.
    """
    stem = f"PREPROC_RT.WIDE_COMPOSITE_STATE_{index:04d}_"
    return stem + "X" * (_WIDE_NAME_WIDTH - len(stem) - 3) + "_V1"


def _wide_composite():
    """A composite whose declared inventory is wider than PREPROC.MODULES.objects.

    Contract-valid in every other respect — every object carries the module's
    ``_V1`` suffix, and the artifact creates exactly what the manifest declares —
    so no check ahead of the provenance write has any reason to reject it. Grown
    one TABLE at a time until the encoded inventory passes the column width, so
    the fixture tracks the limit instead of a hard-coded object count that a
    widening would leave under it (turning the test that consumes this vacuous).

    The running length is accumulated as objects are appended rather than
    re-encoding the whole list per candidate: this fixture is built at import
    time, on every collection of this module including the runs where every
    database test skips, and at the current width re-encoding is quadratic in a
    few thousand objects. The single ``encode_objects`` call at the end keeps that
    arithmetic honest against the real wire format.
    """
    entry = "PREPROC_RT.WIDE_COMPOSITE_V1"
    objects = [{"type": "LUA SCRIPT", "name": entry}]
    encoded_length = len(encode_objects(objects))
    while encoded_length <= _OBJECTS_MAX_CHARS:
        table = {"type": "TABLE", "name": _wide_object_name(len(objects))}
        objects.append(table)
        encoded_length += len("\n") + len(table["type"]) + len("|") + len(table["name"])
    assert len(encode_objects(objects)) == encoded_length
    statements = [_entry_statement("WIDE_COMPOSITE", "wide_composite", 1)]
    statements += [_table_statement(obj["name"]) for obj in objects[1:]]
    return _composite_spec("wide_composite", statements, objects), objects


def _undeclared_composite():
    """2 statements; the index's declared inventory omits the second object."""
    entry = "PREPROC_RT.UNDECLARED_COMPOSITE_V1"
    helper = "PREPROC_RT.UNDECLARED_COMPOSITE_HELPER_A_V1"
    statements = [
        _entry_statement("UNDECLARED_COMPOSITE", "undeclared_composite", 1),
        _python_statement(helper),
    ]
    objects = [
        {"type": "LUA SCRIPT", "name": entry},
        {"type": "PYTHON3 SCALAR SCRIPT", "name": helper},
    ]
    return _composite_spec("undeclared_composite", statements, objects), objects


# name -> (ModuleSpec, declared inventory in file order)
_COMPOSITE_FIXTURES = {
    "composite_ok": _ok_composite(),
    "broken_composite": _broken_composite(),
    "outside_composite": _outside_composite(),
    "inert_composite": _inert_composite(),
    "rerun_composite": _rerun_composite(),
    "undeclared_composite": _undeclared_composite(),
    "wide_composite": _wide_composite(),
}


def _declared(module_name):
    """The declared object inventory of a composite fixture, in file order."""
    return _COMPOSITE_FIXTURES[module_name][1]


# --- deployment + staging helpers ------------------------------------------


@pytest.fixture(scope="session")
def module_install_deployed(resolver_deployed):
    """Deploy PREPROC.MODULE_INSTALL once per session (RETURNS TABLE, invoker rights).

    Depends on ``resolver_deployed`` so the resolver UDF the orchestrator drives
    is present. Group C wires this into ``install.py``; until then the install
    suite deploys it directly, mirroring how the resolver UDF is deployed.
    """
    body = _MODULE_INSTALL_LUA.read_text(encoding="utf-8")
    resolver_deployed.execute("OPEN SCHEMA PREPROC")
    resolver_deployed.execute(
        "CREATE OR REPLACE LUA SCRIPT "
        "PREPROC.MODULE_INSTALL(source, module_name, scope) RETURNS TABLE AS\n" + body
    )
    return resolver_deployed


def _drop_source(connection, config, conn_name, bucket_path):
    try:
        connection.execute(f"DROP CONNECTION IF EXISTS {conn_name}")
    except Exception:
        pass
    try:
        bucketfs_delete(config, bucket_path)
    except Exception:
        pass


def _stage(connection, conn_name, bucket_path, specs, *, probe, tamper=None, objects_override=None):
    """Build + stage a release tarball into BucketFS; yield its ``source``.

    Skips cleanly (matching the ``db`` fixture) when BucketFS write is not
    configured. The operator-side CONNECTION is created by the TEST, never the
    framework, with ACCESS granted to PREPROC_ADMIN (parity with the docs).
    """
    config = bucketfs_config()
    if config is None:
        pytest.skip(
            "BucketFS write is not configured; set EXASOL_BFS_WRITE_PASSWORD "
            "(and EXASOL_BFS_HOST) to stage the install fixture"
        )
    work = Path(tempfile.mkdtemp(prefix="preproc-install-fixture-"))
    build_library_tarball(
        work / bucket_path, specs, tamper=tamper, objects_override=objects_override
    )
    try:
        bucketfs_put(config, bucket_path, (work / bucket_path).read_bytes())
    except Exception as exc:  # noqa: BLE001 - staging is environment-dependent; skip on any failure
        pytest.skip(f"cannot stage the release tarball into BucketFS: {exc}")

    connection.execute(f"CREATE OR REPLACE CONNECTION {conn_name} TO '{config.bucketfs_address}'")
    connection.execute(f"GRANT ACCESS ON CONNECTION {conn_name} TO PREPROC_ADMIN")
    source = config.bucketfs_source(bucket_path)

    if not wait_until_ok(connection, source, probe):
        _drop_source(connection, config, conn_name, bucket_path)
        pytest.skip("BucketFS did not extract/replicate the release to the UDF mount in time")

    try:
        yield source
    finally:
        _drop_source(connection, config, conn_name, bucket_path)


@pytest.fixture(scope="session")
def install_tamper_source(module_install_deployed):
    """Stage a release whose ``broken_module`` artifact is tampered (sha mismatch).

    Carries a clean ``cast_shorthand`` too, used only to poll BucketFS readiness
    (a tampered module never resolves OK, so it cannot be the readiness probe).
    """
    specs = [
        library_module("cast_shorthand", version=1, function="cast_shorthand"),
        library_module("broken_module", version=1, function="broken_module"),
    ]
    yield from _stage(
        module_install_deployed,
        _TAMPER_CONNECTION,
        _TAMPER_BUCKET_PATH,
        specs,
        probe="cast_shorthand",
        tamper={"broken_module": b"\n-- tampered\n"},
    )


@pytest.fixture(scope="session")
def composite_source(module_install_deployed):
    """Stage ONE release carrying every composite fixture this suite needs.

    All six composite modules travel in a single tarball so the suite pays the
    BucketFS upload + auto-extract + replication wait once. ``composite_ok`` is
    the readiness probe because it is the only one that resolves AND installs
    cleanly. ``undeclared_composite``'s declared inventory is overridden in the
    SERIALIZED index only — its artifact and its ``module.toml`` stay consistent,
    exactly as ``tamper`` forces a sha256 mismatch by touching only the tarball
    copy of the bytes.
    """
    specs = [spec for spec, _objects in _COMPOSITE_FIXTURES.values()]
    undeclared_objects = _COMPOSITE_FIXTURES["undeclared_composite"][1]
    yield from _stage(
        module_install_deployed,
        _COMPOSITE_CONNECTION,
        _COMPOSITE_BUCKET_PATH,
        specs,
        probe="composite_ok",
        objects_override={"undeclared_composite": [undeclared_objects[0]]},
    )


@pytest.fixture(scope="session")
def install_self_source(module_install_deployed):
    """Stage a release listing a ``self-deployed`` module (metadata only)."""
    specs = [
        self_deployed_module(
            "semantic_views", version=3, phase="EXPAND", source=("exasol/semantic_views", "v3.0.0")
        )
    ]
    yield from _stage(
        module_install_deployed,
        _SELF_CONNECTION,
        _SELF_BUCKET_PATH,
        specs,
        probe="semantic_views",
    )


# --- state capture / cleanup helpers ---------------------------------------


@dataclass(frozen=True)
class InstallState:
    """The install end-state a byte-identical comparison hinges on."""

    script_text: str | None
    config_rows: tuple
    modules_row: tuple | None


def _is_crashed_vm(exc):
    """True when the database tore the UDF VM down instead of emitting a row.

    A crashed VM surfaces below the resolver's own ``except Exception``, so no
    structured ERROR row reaches the installer and no resolver message text is
    available to assert against. The exasol/docker-db image does this where a real
    cluster returns the row -- see
    tests/test_db_resolver.py::test_resolver_https_no_egress_network_error, which
    documented the behaviour first. Callers use this to drop message assertions
    while keeping the abort-before-write assertions that hold everywhere.
    """
    return "VM crashed" in str(exc)


def _script_text(connection, script_name):
    schema, _, name = script_name.partition(".")
    rows = connection.execute(
        "SELECT script_text FROM EXA_ALL_SCRIPTS "
        f"WHERE script_schema = '{schema}' AND script_name = '{name}'"
    ).fetchall()
    return rows[0][0] if rows else None


def _capture_state(connection, script_name, scope_type, scope_value, module_name, version):
    config_rows = tuple(
        tuple(row)
        for row in connection.execute(
            "SELECT scope_type, scope_value, phase, script_name, function_name, "
            "client_pattern, enabled FROM PREPROC.CONFIG "
            f"WHERE script_name = '{script_name}' AND scope_type = '{scope_type}' "
            f"AND scope_value = '{scope_value}'"
        ).fetchall()
    )
    modules = connection.execute(
        "SELECT name, version, source_repo, source_ref, sha256, script_name, "
        "function_name, phase, deploy_mode, status, objects FROM PREPROC.MODULES "
        f"WHERE name = '{module_name}' AND version = {version}"
    ).fetchall()
    return InstallState(
        script_text=_script_text(connection, script_name),
        config_rows=config_rows,
        modules_row=tuple(modules[0]) if modules else None,
    )


def _object_exists(connection, obj):
    """Whether one inventory object is present, resolved by its declared type."""
    obj_type = obj["type"]
    name = obj["name"]
    if obj_type in ("SCHEMA", "VIRTUAL SCHEMA"):
        query = f"SELECT COUNT(*) FROM EXA_ALL_SCHEMAS WHERE UPPER(schema_name) = UPPER('{name}')"
    else:
        schema, _, local = name.partition(".")
        if obj_type.endswith("SCRIPT"):
            source = "EXA_ALL_SCRIPTS"
            columns = ("script_schema", "script_name")
        elif obj_type == "FUNCTION":
            source = "EXA_ALL_FUNCTIONS"
            columns = ("function_schema", "function_name")
        else:
            source = "EXA_ALL_OBJECTS"
            columns = ("root_name", "object_name")
        query = (
            f"SELECT COUNT(*) FROM {source} "
            f"WHERE UPPER({columns[0]}) = UPPER('{schema}') "
            f"AND UPPER({columns[1]}) = UPPER('{local}')"
        )
    return connection.execute(query).fetchall()[0][0] > 0


def _present(connection, objects):
    """The names of ``objects`` that exist, so an assertion names what survived."""
    return [obj["name"] for obj in objects if _object_exists(connection, obj)]


def _drop_objects(connection, objects):
    """Best-effort teardown of an inventory, in reverse creation order."""
    for obj in reversed(objects):
        schema, _, local = obj["name"].partition(".")
        if obj["type"].endswith("SCRIPT"):
            statement = f'DROP SCRIPT IF EXISTS "{schema}"."{local}"'
        elif obj["type"] in ("SCHEMA", "VIRTUAL SCHEMA"):
            statement = f'DROP SCHEMA IF EXISTS "{obj["name"]}" CASCADE'
        elif obj["type"] == "VIEW":
            statement = f'DROP VIEW IF EXISTS "{schema}"."{local}"'
        else:
            statement = f'DROP TABLE IF EXISTS "{schema}"."{local}"'
        try:
            connection.execute(statement)
        except Exception:
            pass


def _purge_composite(connection, module_name):
    """Full teardown for a composite fixture: rules, provenance, and every object."""
    objects = _declared(module_name)
    _purge(
        connection,
        module_name=module_name,
        version=1,
        script_name=objects[0]["name"],
        drop_script=False,
    )
    _drop_objects(connection, objects)


def _purge(connection, *, module_name, version, script_name, drop_script=True):
    """Remove a test's rules/provenance/script and refresh, for session isolation."""
    for stmt in (
        f"DELETE FROM PREPROC.CONFIG WHERE script_name = '{script_name}'",
        f"DELETE FROM PREPROC.MODULES WHERE name = '{module_name}' AND version = {version}",
        "COMMIT",
        "EXECUTE SCRIPT PREPROC.REFRESH",
    ):
        try:
            connection.execute(stmt)
        except Exception:
            pass
    if drop_script:
        try:
            connection.execute(f"DROP SCRIPT {script_name}")
        except Exception:
            pass


def _local_registry(tmp_path):
    """A local registry directory built from the shared specs (for CLI parity)."""
    tgz = tmp_path / "lib.tar.gz"
    build_library_tarball(tgz, standard_library_specs())
    regdir = tmp_path / "registry-root"
    with tarfile.open(tgz, "r:gz") as tar:
        tar.extractall(regdir, filter="data")
    return str(regdir)


def _install(connection, source, module_name, scope):
    return connection.execute(
        f"EXECUTE SCRIPT PREPROC.MODULE_INSTALL('{source}', '{module_name}', '{scope}')"
    ).fetchall()


# --- scenarios --------------------------------------------------------------


@pytest.mark.integration
def test_bucketfs_install_matches_cli_end_state(
    module_install_deployed, bucketfs_library_source, tmp_path
):
    """A BucketFS install leaves an end-state byte-identical to a CLI add.

    Installs via MODULE_INSTALL, captures the script/rule/provenance end-state,
    then runs the CLI ``deploy_module`` for the same module + scope from a local
    registry built from the same sources. Because both paths converge on an
    identical end-state, the CLI deploy is a no-op — so ``after == before`` proves
    byte-identical parity (a divergence would make CREATE OR REPLACE / MERGE
    change the captured state), and the captured state now includes the recorded
    ``objects`` inventory, so a divergence in the inventory wire form fails here.

    ``cast_shorthand`` is a single-statement artifact, and it now takes the same
    split-then-execute path a composite one does: the splitter drops the leading
    ``--/`` marker line where the old code retained it, so the database receives
    text starting at ``CREATE``. Exasol regenerates the CREATE header into
    ``EXA_ALL_SCRIPTS.SCRIPT_TEXT``, which is why that change leaves the stored
    end state untouched.
    """
    connection = module_install_deployed
    source = bucketfs_library_source.source
    scope_value = "PREPROC_TEST_PARITY_ROLE"
    try:
        result = _install(connection, source, "cast_shorthand", f"ROLE:{scope_value}")
        assert result[0][3] == "installed"

        before = _capture_state(connection, _CAST_SCRIPT, "ROLE", scope_value, "cast_shorthand", 1)
        assert before.script_text is not None
        assert len(before.config_rows) == 1
        assert before.modules_row is not None

        registry = _local_registry(tmp_path)
        index = read_registry(registry)
        entry = next(entry for entry in index.entries if entry.name == "cast_shorthand")
        deploy_module(connection, entry, registry, scope=("ROLE", scope_value))

        after = _capture_state(connection, _CAST_SCRIPT, "ROLE", scope_value, "cast_shorthand", 1)
        assert after == before

        # EXA_ALL_SCRIPTS stores the normalised CREATE (a RETURNS ROWCOUNT header)
        # around the verbatim artifact body, so parity is proven by `after ==
        # before` above; here assert the deployed body carries the module function.
        expected_body = split_statements(
            default_artifact_text("cast_shorthand", 1, "cast_shorthand")
        )[0]
        assert "function cast_shorthand(sqltext)" in before.script_text
        assert expected_body.splitlines()[-1] in before.script_text
        assert before.modules_row[8] == "library-deployed"
        assert before.modules_row[4] == entry.sha256
        assert before.config_rows[0][2] == "TRANSLATE"
    finally:
        _purge(connection, module_name="cast_shorthand", version=1, script_name=_CAST_SCRIPT)


@pytest.mark.integration
def test_sha256_mismatch_aborts_before_write(module_install_deployed, install_tamper_source):
    """A sha256 mismatch aborts before any DB write — nothing is deployed.

    The message is asserted, not just the raise: a bare ``pytest.raises(Exception)``
    is satisfied by a connection drop or a fixture fault, which would leave this
    scenario passing while the integrity gate itself was broken. The resolver's
    ``sha256_mismatch`` error kind travelling out through MODULE_INSTALL is what
    proves the abort came from the digest comparison.
    """
    connection = module_install_deployed
    try:
        with pytest.raises(Exception) as excinfo:
            _install(connection, install_tamper_source, "broken_module", "ROLE:PREPROC_ADMIN")
        message = str(excinfo.value)
        assert "sha256_mismatch" in message
        assert "broken_module" in message

        assert _script_text(connection, _BROKEN_SCRIPT) is None
        rule_count = connection.execute(
            f"SELECT COUNT(*) FROM PREPROC.CONFIG WHERE script_name = '{_BROKEN_SCRIPT}'"
        ).fetchall()[0][0]
        assert rule_count == 0
        provenance = connection.execute(
            "SELECT COUNT(*) FROM PREPROC.MODULES WHERE name = 'broken_module'"
        ).fetchall()[0][0]
        assert provenance == 0
    finally:
        _purge(connection, module_name="broken_module", version=1, script_name=_BROKEN_SCRIPT)


@pytest.mark.integration
def test_composite_installs_all_objects_in_order(module_install_deployed, composite_source):
    """A composite module's statements execute in file order and every object lands.

    File order is asserted by construction, not by observation: the artifact's
    VIEW selects from the TABLE an earlier statement creates, so the install can
    only succeed if the statements ran in order. The rule is registered only after
    the LAST statement succeeded, which the mid-artifact-failure scenario proves
    from the other side (a failure leaves no rule at all).
    """
    connection = module_install_deployed
    objects = _declared("composite_ok")
    scope_value = "PREPROC_TEST_COMPOSITE_ROLE"
    try:
        result = _install(connection, composite_source, "composite_ok", f"ROLE:{scope_value}")
        assert result[0][3] == "installed"

        assert _present(connection, objects) == [obj["name"] for obj in objects]

        rule = connection.execute(
            "SELECT phase, function_name FROM PREPROC.CONFIG "
            f"WHERE script_name = '{objects[0]['name']}' AND scope_type = 'ROLE' "
            f"AND scope_value = '{scope_value}'"
        ).fetchall()
        assert len(rule) == 1
        assert rule[0][0] == "TRANSLATE"

        # The recorded inventory is the undo list reclaim will later use, so it
        # must carry every object in file order, encoded as 'TYPE|NAME' lines.
        recorded = connection.execute(
            "SELECT objects FROM PREPROC.MODULES WHERE name = 'composite_ok' AND version = 1"
        ).fetchall()[0][0]
        assert recorded == "\n".join(f"{obj['type']}|{obj['name']}" for obj in objects)

        # The INSERT created nothing, so it consumed no inventory slot; its row
        # is proof the non-DDL statement really executed.
        seeded = connection.execute(
            "SELECT COUNT(*) FROM PREPROC_RT.COMPOSITE_OK_STATE_V1 WHERE k = 'seed'"
        ).fetchall()[0][0]
        assert seeded == 1
    finally:
        _purge_composite(connection, "composite_ok")


@pytest.mark.integration
def test_preflight_aborts_before_any_ddl(module_install_deployed, composite_source):
    """An artifact creating an undeclared object fails before any statement runs."""
    connection = module_install_deployed
    objects = _declared("undeclared_composite")
    undeclared = objects[1]
    try:
        with pytest.raises(Exception) as excinfo:
            _install(connection, composite_source, "undeclared_composite", "ROLE:PREPROC_ADMIN")
        assert undeclared["name"] in str(excinfo.value)

        # No statement of the artifact ran: not even the FIRST one, which is
        # valid DDL on its own and would have succeeded had execution started.
        assert _present(connection, objects) == []

        rules = connection.execute(
            f"SELECT COUNT(*) FROM PREPROC.CONFIG WHERE script_name = '{objects[0]['name']}'"
        ).fetchall()[0][0]
        assert rules == 0
        provenance = connection.execute(
            "SELECT COUNT(*) FROM PREPROC.MODULES WHERE name = 'undeclared_composite'"
        ).fetchall()[0][0]
        assert provenance == 0
    finally:
        _purge_composite(connection, "undeclared_composite")


@pytest.mark.integration
def test_over_wide_inventory_aborts_before_write(module_install_deployed, composite_source):
    """An inventory too wide for PREPROC.MODULES.objects is refused before any DDL.

    The provenance row is written LAST — after every CREATE and after RULE_ENSURE
    — so an over-wide inventory discovered at MODULE_REGISTER would leave the
    module live and DISPATCHING with no provenance row: un-retirable and
    un-reclaimable, the exact inverse of "a partial install is inert". Nothing
    else in the pipeline would have stopped it; this manifest is contract-valid,
    so the CLI accepts it too.

    Both the limit and the actual size are asserted, because an operator's only
    remedy is to split the module or shorten its names and the message is what
    tells them by how much they are over.
    """
    connection = module_install_deployed
    objects = _declared("wide_composite")
    try:
        with pytest.raises(Exception) as excinfo:
            _install(connection, composite_source, "wide_composite", "ROLE:PREPROC_ADMIN")
        message = str(excinfo.value)
        assert str(_OBJECTS_MAX_CHARS) in message
        assert str(len(encode_objects(objects))) in message

        # Refused during pre-flight: not even the FIRST statement ran, which is
        # valid DDL on its own and would have succeeded had execution started.
        assert _present(connection, objects) == []

        rules = connection.execute(
            f"SELECT COUNT(*) FROM PREPROC.CONFIG WHERE script_name = '{objects[0]['name']}'"
        ).fetchall()[0][0]
        assert rules == 0
        provenance = connection.execute(
            "SELECT COUNT(*) FROM PREPROC.MODULES WHERE name = 'wide_composite'"
        ).fetchall()[0][0]
        assert provenance == 0
    finally:
        _purge_composite(connection, "wide_composite")


@pytest.mark.integration
def test_midartifact_failure_compensating_cleanup(module_install_deployed, composite_source):
    """A failure at statement k drops statements 1..k-1's objects in reverse."""
    connection = module_install_deployed
    objects = _declared("broken_composite")
    created = objects[:3]
    try:
        with pytest.raises(Exception) as excinfo:
            _install(connection, composite_source, "broken_composite", "ROLE:PREPROC_ADMIN")
        message = str(excinfo.value)

        # The ORIGINAL error leads and is not masked by the cleanup outcome.
        # Exasol's parse error does not echo the offending type name, so the
        # database's own diagnostic is what proves the cause survived.
        assert "statement 4 of 4 failed" in message
        assert objects[3]["name"] in message
        assert "syntax error" in message.lower()

        # Cleanup is reported, in reverse creation order.
        assert "dropped 3 object(s) in reverse" in message
        reversed_labels = [f"{obj['type']} {obj['name']}" for obj in reversed(created)]
        assert ", ".join(reversed_labels) in message

        assert _present(connection, objects) == []

        rules = connection.execute(
            f"SELECT COUNT(*) FROM PREPROC.CONFIG WHERE script_name = '{objects[0]['name']}'"
        ).fetchall()[0][0]
        assert rules == 0
        provenance = connection.execute(
            "SELECT COUNT(*) FROM PREPROC.MODULES WHERE name = 'broken_composite'"
        ).fetchall()[0][0]
        assert provenance == 0
    finally:
        _purge_composite(connection, "broken_composite")


@pytest.mark.integration
def test_partial_install_is_inert_no_rule(module_install_deployed, composite_source):
    """A partial install whose cleanup could not finish is inert, not half-active.

    The squatter table makes the declared SCHEMA un-droppable, so compensating
    cleanup genuinely fails for one object and the module really is left partially
    present — the precondition the scenario needs. It must still be INERT: no
    rule, nothing in the caller's own MY_PIPELINE, and no provenance row.
    """
    connection = module_install_deployed
    objects = _declared("inert_composite")
    schema_object = objects[1]
    caller = connection.execute("SELECT CURRENT_USER").fetchall()[0][0]
    try:
        connection.execute(f"CREATE SCHEMA IF NOT EXISTS {_INERT_SCHEMA}")
        connection.execute(f"CREATE OR REPLACE TABLE {_INERT_SQUATTER} (k VARCHAR(64))")

        with pytest.raises(Exception) as excinfo:
            _install(connection, composite_source, "inert_composite", f"USER:{caller}")
        message = str(excinfo.value)
        assert "compensating cleanup could NOT drop" in message
        assert schema_object["name"] in message
        assert "inert (no rule was registered)" in message

        # The precondition really held: the schema survived cleanup...
        assert _object_exists(connection, schema_object)
        # ...and RESTRICT never destroyed the data inside it.
        assert connection.execute(f"SELECT COUNT(*) FROM {_INERT_SQUATTER}").fetchall()[0][0] == 0

        rules = connection.execute(
            f"SELECT COUNT(*) FROM PREPROC.CONFIG WHERE script_name = '{objects[0]['name']}'"
        ).fetchall()[0][0]
        assert rules == 0
        pipeline = connection.execute(
            "SELECT COUNT(*) FROM PREPROC_RT.MY_PIPELINE "
            f"WHERE script_name = '{objects[0]['name']}'"
        ).fetchall()[0][0]
        assert pipeline == 0
        provenance = connection.execute(
            "SELECT COUNT(*) FROM PREPROC.MODULES WHERE name = 'inert_composite'"
        ).fetchall()[0][0]
        assert provenance == 0

        # A statement the module would have transformed passes through untouched.
        assert connection.execute("SELECT 1 AS untransformed").fetchall()[0][0] == 1
    finally:
        _purge_composite(connection, "inert_composite")
        try:
            connection.execute(f"DROP SCHEMA IF EXISTS {_INERT_SCHEMA} CASCADE")
        except Exception:
            pass


@pytest.mark.integration
def test_failed_install_rerun_converges(module_install_deployed, composite_source):
    """Re-running an install that failed part-way converges rather than colliding.

    The first run fails at the VIEW statement because its base table is absent.
    Before the second run the base table is created and the entry script is
    hand-restored with a sentinel body — modelling an object a partial cleanup
    could not drop. The second run must succeed and must REPLACE the sentinel,
    which is what "every artifact statement uses CREATE OR REPLACE" buys.
    """
    connection = module_install_deployed
    objects = _declared("rerun_composite")
    entry = objects[0]["name"]
    scope_value = "PREPROC_TEST_RERUN_ROLE"
    sentinel = "-- SENTINEL LEFT BY AN INCOMPLETE COMPENSATING CLEANUP"
    try:
        with pytest.raises(Exception) as excinfo:
            _install(connection, composite_source, "rerun_composite", f"ROLE:{scope_value}")
        assert "statement 3 of 3 failed" in str(excinfo.value)
        assert _present(connection, objects) == []
        assert (
            connection.execute(
                f"SELECT COUNT(*) FROM PREPROC.CONFIG WHERE script_name = '{entry}'"
            ).fetchall()[0][0]
            == 0
        )

        connection.execute("OPEN SCHEMA PREPROC_RT")
        connection.execute(f"CREATE OR REPLACE TABLE {_RERUN_BASE} (k VARCHAR(64))")
        connection.execute(
            f"CREATE OR REPLACE LUA SCRIPT {entry}() AS\n{sentinel}\n"
            "function rerun_composite(sqltext)\n    return sqltext\nend\n"
        )
        assert sentinel in _script_text(connection, entry)

        result = _install(connection, composite_source, "rerun_composite", f"ROLE:{scope_value}")
        assert result[0][3] == "installed"
        assert _present(connection, objects) == [obj["name"] for obj in objects]
        assert sentinel not in _script_text(connection, entry)

        rules = connection.execute(
            f"SELECT COUNT(*) FROM PREPROC.CONFIG WHERE script_name = '{entry}' "
            f"AND scope_type = 'ROLE' AND scope_value = '{scope_value}'"
        ).fetchall()[0][0]
        assert rules == 1
        provenance = connection.execute(
            "SELECT COUNT(*) FROM PREPROC.MODULES WHERE name = 'rerun_composite' AND version = 1"
        ).fetchall()[0][0]
        assert provenance == 1
    finally:
        _purge_composite(connection, "rerun_composite")
        try:
            connection.execute(f"DROP TABLE IF EXISTS {_RERUN_BASE}")
        except Exception:
            pass


@pytest.mark.integration
def test_out_of_schema_objects_created_and_cleaned(module_install_deployed, composite_source):
    """An object outside PREPROC_RT is created, and compensating cleanup reaches it."""
    connection = module_install_deployed
    objects = _declared("outside_composite")
    outside = objects[1]
    assert not outside["name"].startswith("PREPROC_RT.")
    try:
        connection.execute(f"CREATE SCHEMA IF NOT EXISTS {_OUTSIDE_SCHEMA}")

        with pytest.raises(Exception) as excinfo:
            _install(connection, composite_source, "outside_composite", "ROLE:PREPROC_ADMIN")
        message = str(excinfo.value)

        # Cleanup resolved the object by its declared type and qualified name,
        # so it reached across the schema boundary.
        assert "dropped 2 object(s) in reverse" in message
        assert f"{outside['type']} {outside['name']}" in message
        assert "compensating cleanup could NOT drop" not in message

        assert _present(connection, objects) == []
        # The schema the object lived in is NOT the module's, so cleanup left it.
        assert _object_exists(connection, {"type": "SCHEMA", "name": _OUTSIDE_SCHEMA})
    finally:
        _purge_composite(connection, "outside_composite")
        try:
            connection.execute(f"DROP SCHEMA IF EXISTS {_OUTSIDE_SCHEMA} CASCADE")
        except Exception:
            pass


@pytest.mark.integration
def test_install_self_deployed_register_only(module_install_deployed, install_self_source):
    """A self-deployed module registers rule + provenance without any CREATE."""
    connection = module_install_deployed
    connection.execute("OPEN SCHEMA PREPROC_RT")
    connection.execute(_SELF_MARKER)
    marker_before = _script_text(connection, _SELF_SCRIPT)
    try:
        result = _install(
            connection, install_self_source, "semantic_views", "USER:PREPROC_TEST_SELF"
        )
        assert result[0][3] == "registered-only"

        modules = connection.execute(
            "SELECT deploy_mode, sha256 FROM PREPROC.MODULES WHERE name = 'semantic_views'"
        ).fetchall()
        assert modules[0][0] == "self-deployed"
        assert modules[0][1] is None

        rule = connection.execute(
            "SELECT scope_type, scope_value FROM PREPROC.CONFIG "
            f"WHERE script_name = '{_SELF_SCRIPT}'"
        ).fetchall()
        assert rule[0][0] == "USER"
        assert rule[0][1] == "PREPROC_TEST_SELF"

        assert _script_text(connection, _SELF_SCRIPT) == marker_before
    finally:
        _purge(connection, module_name="semantic_views", version=3, script_name=_SELF_SCRIPT)


@pytest.mark.integration
def test_install_scope_activates_rule(module_install_deployed, bucketfs_library_source):
    """The scope argument targets rule activation: the in-scope user resolves it."""
    connection = module_install_deployed
    source = bucketfs_library_source.source
    in_scope = "PREPROC_TEST_BOB"
    out_of_scope = "PREPROC_TEST_CAROL"
    try:
        _install(connection, source, "cast_shorthand", f"USER:{in_scope}")

        in_rows = connection.execute(
            "SELECT script_name FROM PREPROC.RESOLUTION "
            f"WHERE user_name = '{in_scope}' AND script_name = '{_CAST_SCRIPT}'"
        ).fetchall()
        assert in_rows, "the in-scope user must receive the module in RESOLUTION"

        out_rows = connection.execute(
            "SELECT script_name FROM PREPROC.RESOLUTION "
            f"WHERE user_name = '{out_of_scope}' AND script_name = '{_CAST_SCRIPT}'"
        ).fetchall()
        assert not out_rows, "a user outside the scope must not receive the module"
    finally:
        _purge(connection, module_name="cast_shorthand", version=1, script_name=_CAST_SCRIPT)


@pytest.mark.integration
def test_install_idempotent_reinstall(module_install_deployed, bucketfs_library_source):
    """Re-installing the identical version is idempotent (no duplicate rule/row)."""
    connection = module_install_deployed
    source = bucketfs_library_source.source
    scope = "ROLE:PREPROC_TEST_IDEMPOTENT"
    try:
        first = _install(connection, source, "cast_shorthand", scope)
        assert first[0][3] == "installed"

        second = _install(connection, source, "cast_shorthand", scope)
        assert second[0][3] == "already-current"

        rule_count = connection.execute(
            f"SELECT COUNT(*) FROM PREPROC.CONFIG WHERE script_name = '{_CAST_SCRIPT}' "
            "AND scope_type = 'ROLE' AND scope_value = 'PREPROC_TEST_IDEMPOTENT'"
        ).fetchall()[0][0]
        assert rule_count == 1

        provenance = connection.execute(
            "SELECT COUNT(*) FROM PREPROC.MODULES WHERE name = 'cast_shorthand' AND version = 1"
        ).fetchall()[0][0]
        assert provenance == 1
    finally:
        _purge(connection, module_name="cast_shorthand", version=1, script_name=_CAST_SCRIPT)


@pytest.mark.integration
def test_install_non_admin_rejected(module_install_deployed):
    """A non-admin caller cannot execute MODULE_INSTALL; it is never PUBLIC.

    Requires CREATE USER + a second login; skips (rather than a false pass) where
    the env account cannot mint one — the admin boundary is also asserted
    structurally by the schema-layout suite.
    """
    if missing_env_vars():
        pytest.skip("no Exasol instance is configured")

    admin = module_install_deployed
    non_admin = "PREPROC_TEST_INSTALL_NOADMIN"
    password = "pw_PREPROC_install_1"
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

        source = BucketFsConfig(
            host="", port="", bucket="default", write_password="", service="bfsdefault"
        ).bucketfs_source("preproc-lib-0.1.0.tar.gz")
        with pytest.raises(Exception):
            other.execute(
                "EXECUTE SCRIPT PREPROC.MODULE_INSTALL("
                f"'{source}', 'cast_shorthand', 'ROLE:PREPROC_ADMIN')"
            )

        provenance = admin.execute(
            "SELECT COUNT(*) FROM PREPROC.MODULES WHERE name = 'cast_shorthand'"
        ).fetchall()[0][0]
        assert provenance == 0
    finally:
        if other is not None:
            other.close()
        try:
            admin.execute(f"DROP USER {non_admin} CASCADE")
        except Exception:
            pass


@pytest.mark.integration
def test_install_malformed_bucketfs_source_aborts_before_write(module_install_deployed):
    """A malformed (two-segment) bucketfs source aborts before any database write.

    'bucketfs:PREPROC_BFS/preproc-lib-0.1.0.tar.gz' is the pre-delta, two-segment
    form under deliberate rejection here -- not a literal left behind by oversight.
    """
    connection = module_install_deployed
    source = "bucketfs:PREPROC_BFS/preproc-lib-0.1.0.tar.gz"

    with pytest.raises(Exception) as excinfo:
        _install(connection, source, "cast_shorthand", "ROLE:PREPROC_ADMIN")

    # The abort-before-write assertions below are the point of this test and hold
    # on every environment. The resolver's MESSAGE only reaches the installer
    # where the UDF is allowed to emit its structured row: on docker-db the VM is
    # torn down instead (see
    # test_db_resolver.py::test_resolver_unreadable_location_reports_credential_hint),
    # which still aborts the install but carries no grammar text to match.
    if not _is_crashed_vm(excinfo.value):
        assert "bucketfs:<service>/<bucket>/<path>" in str(excinfo.value)

    assert _script_text(connection, _CAST_SCRIPT) is None
    rule_count = connection.execute(
        f"SELECT COUNT(*) FROM PREPROC.CONFIG WHERE script_name = '{_CAST_SCRIPT}'"
    ).fetchall()[0][0]
    assert rule_count == 0
    provenance = connection.execute(
        "SELECT COUNT(*) FROM PREPROC.MODULES WHERE name = 'cast_shorthand'"
    ).fetchall()[0][0]
    assert provenance == 0


@pytest.mark.integration
def test_install_unreadable_location_reports_credential_hint(module_install_deployed):
    """An unreadable-but-well-formed location surfaces the credential hint, no write.

    Targets a release absent from the always-public ``default`` bucket, so the
    failure is purely "no such release" -- proving the installer surfaces the
    resolver's full ``source_unreadable`` message, including the CONNECTION
    grant prerequisite, with no staged fixture needed.
    """
    connection = module_install_deployed
    config = BucketFsConfig(
        host="", port="", bucket="default", write_password="", service="bfsdefault"
    )
    source = config.bucketfs_source("no-such-release.tar.gz")

    with pytest.raises(Exception) as excinfo:
        _install(connection, source, "cast_shorthand", "ROLE:PREPROC_ADMIN")
    message = str(excinfo.value)
    # See _is_crashed_vm: on docker-db there is no structured row to surface, so
    # the hint cannot be asserted there. The no-write checks below still can.
    if not _is_crashed_vm(excinfo.value):
        assert "source_unreadable" in message
        assert "CONNECTION TO 'bucketfs:bfsdefault/default' IDENTIFIED BY" in message

    assert _script_text(connection, _CAST_SCRIPT) is None
    provenance = connection.execute(
        "SELECT COUNT(*) FROM PREPROC.MODULES WHERE name = 'cast_shorthand'"
    ).fetchall()[0][0]
    assert provenance == 0


@pytest.mark.integration
def test_install_https_when_reachable(module_install_deployed):
    """An https source installs when the cluster can reach the host (no toggle).

    Needs a reachable base URL serving ``registry/index.json`` (listing
    ``cast_shorthand`` v1 with a matching sha256) and the artifact; supply it via
    ``PREPROC_TEST_HTTPS_BASE``. The end-state matches the BucketFS-path install.
    """
    base = os.environ.get("PREPROC_TEST_HTTPS_BASE")
    if not base:
        pytest.skip("set PREPROC_TEST_HTTPS_BASE to a reachable registry base URL to run this")
    connection = module_install_deployed
    index_url = base.rstrip("/") + "/registry/index.json"
    scope_value = "PREPROC_TEST_HTTPS_ROLE"
    try:
        result = _install(connection, index_url, "cast_shorthand", f"ROLE:{scope_value}")
        assert result[0][3] == "installed"
        assert _script_text(connection, _CAST_SCRIPT) is not None
        provenance = connection.execute(
            "SELECT deploy_mode FROM PREPROC.MODULES WHERE name = 'cast_shorthand' AND version = 1"
        ).fetchall()
        assert provenance[0][0] == "library-deployed"
    finally:
        _purge(connection, module_name="cast_shorthand", version=1, script_name=_CAST_SCRIPT)


@pytest.mark.integration
def test_install_https_no_egress_no_partial_deploy(module_install_deployed):
    """An unreachable https source fails with a network error and deploys nothing."""
    connection = module_install_deployed
    source = "https://unreachable-preproc.invalid/registry/index.json"

    with pytest.raises(Exception) as excinfo:
        _install(connection, source, "cast_shorthand", "ROLE:PREPROC_ADMIN")
    assert "unreachable-preproc.invalid" in str(excinfo.value)

    assert _script_text(connection, _CAST_SCRIPT) is None
    rule_count = connection.execute(
        f"SELECT COUNT(*) FROM PREPROC.CONFIG WHERE script_name = '{_CAST_SCRIPT}'"
    ).fetchall()[0][0]
    assert rule_count == 0
    provenance = connection.execute(
        "SELECT COUNT(*) FROM PREPROC.MODULES WHERE name = 'cast_shorthand'"
    ).fetchall()[0][0]
    assert provenance == 0


@pytest.mark.integration
def test_install_result_set_reports_outcome(module_install_deployed, bucketfs_library_source):
    """The result set reports (module, version, phase, status, library_version)."""
    connection = module_install_deployed
    source = bucketfs_library_source.source
    scope_value = "PREPROC_TEST_RESULT_ROLE"
    try:
        statement = connection.execute(
            f"EXECUTE SCRIPT PREPROC.MODULE_INSTALL('{source}', 'cast_shorthand', "
            f"'ROLE:{scope_value}')"
        )
        names = [name.lower() for name in statement.column_names()]
        rows = statement.fetchall()
        assert names == ["module", "version", "phase", "status", "library_version"]
        assert len(rows) == 1
        assert rows[0][0] == "cast_shorthand"
        assert int(rows[0][1]) == 1
        assert rows[0][2] == "TRANSLATE"
        assert rows[0][3] in ("installed", "already-current")
    finally:
        _purge(connection, module_name="cast_shorthand", version=1, script_name=_CAST_SCRIPT)


@pytest.mark.integration
def test_install_missing_module_rejected(module_install_deployed, bucketfs_library_source):
    """A module absent from the source is rejected before any write, naming it."""
    connection = module_install_deployed
    source = bucketfs_library_source.source

    with pytest.raises(Exception) as excinfo:
        _install(connection, source, "no-such-module", "ROLE:PREPROC_ADMIN")
    assert "no-such-module" in str(excinfo.value)

    provenance = connection.execute(
        "SELECT COUNT(*) FROM PREPROC.MODULES WHERE name = 'no-such-module'"
    ).fetchall()[0][0]
    assert provenance == 0


# --- the shared statement-split conformance corpus --------------------------
#
# The Lua splitter exists only inside the database, so it is driven the only way
# it can be: the source extracted verbatim from module_install.lua is deployed as
# a probe script over a staged corpus. Staging the text CHUNKED and reassembling
# it inside the probe mirrors the resolver's own VARCHAR(2000000) chunking, so
# the corpus case that straddles a chunk boundary is split after reassembly
# exactly as MODULE_INSTALL splits it.

_CORPUS_CHUNK_CHARS = 500000


def _sql_text_literal(text):
    return "'" + text.replace("'", "''") + "'"


def _deploy_corpus_probe(connection):
    connection.execute(
        f"CREATE OR REPLACE TABLE {CORPUS_TABLE} (case_name VARCHAR(128), "
        "chunk_index DECIMAL(9,0), chunk_text VARCHAR(2000000))"
    )
    connection.execute(
        f"CREATE OR REPLACE LUA SCRIPT {PROBE_SCRIPT}(case_name) RETURNS TABLE AS\n"
        + db_probe_body()
    )


def _stage_corpus_case(connection, name, text):
    """Stage one case as ordered chunks. An empty text stages no row and reads back as ''."""
    for offset in range(0, len(text), _CORPUS_CHUNK_CHARS):
        connection.execute(
            f"INSERT INTO {CORPUS_TABLE} VALUES ({_sql_text_literal(name)}, "
            f"{offset // _CORPUS_CHUNK_CHARS}, "
            f"{_sql_text_literal(text[offset : offset + _CORPUS_CHUNK_CHARS])})"
        )
    connection.execute("COMMIT")


def _run_corpus_probe(connection, case_name):
    rows = connection.execute(
        f"EXECUTE SCRIPT {PROBE_SCRIPT}({_sql_text_literal(case_name)})"
    ).fetchall()
    return decode_probe_rows(rows)


def _drop_corpus_probe(connection):
    for statement in (
        f"DROP SCRIPT IF EXISTS {PROBE_SCRIPT}",
        f"DROP TABLE IF EXISTS {CORPUS_TABLE}",
    ):
        try:
            connection.execute(statement)
        except Exception:
            pass


@pytest.mark.integration
def test_python_and_lua_splitters_agree_on_corpus(installed):
    """Both splitters yield the corpus's expected split for every case, and reject every error case.

    Each side is asserted against the corpus AND against the other: agreeing with
    the corpus is what pins the grammar, and agreeing with each other is what the
    byte-identical end-state guarantee between the CLI path and the in-database
    path actually rests on.
    """
    connection = installed
    cases = load_split_corpus()
    error_cases = load_split_error_cases()
    try:
        _deploy_corpus_probe(connection)
        for case in [*cases, *error_cases]:
            _stage_corpus_case(connection, case.name, case.text)

        for case in cases:
            lua = _run_corpus_probe(connection, case.name)
            assert lua.error is None, f"{case.name}: {lua.error}"
            python_statements = split_statements(case.text)
            assert python_statements == case.statements, case.name
            assert lua.statements == case.statements, case.name
            assert lua.statements == python_statements, case.name

        for case in error_cases:
            lua = _run_corpus_probe(connection, case.name)
            assert lua.statements is None, f"the Lua splitter must reject {case.name}"
            assert lua.error, f"the Lua splitter must name why it rejects {case.name}"
            with pytest.raises(ArtifactSyntaxError):
                split_statements(case.text)
    finally:
        _drop_corpus_probe(connection)


def test_lua_splitter_matches_corpus_under_a_local_interpreter(tmp_path):
    """The Lua splitter reproduces the corpus with no database in the loop.

    The conformance scenario above can only reach the in-database splitter through
    a database. This runs the very same extracted source through a local Lua
    interpreter, so a Lua-side grammar regression is caught where no instance is
    configured. It skips when no interpreter is available (the discipline
    ``test_module_install_passes_luacheck`` uses for a missing ``luacheck``);
    ``PREPROC_TEST_LUA`` names one explicitly.
    """
    interpreter = lua_interpreter()
    if interpreter is None:
        pytest.skip("no Lua interpreter is available; set PREPROC_TEST_LUA to run this")
    program = write_local_probe(tmp_path)

    for case in load_split_corpus():
        result = run_local_probe(interpreter, program, case.text)
        assert result.error is None, f"{case.name}: {result.error}"
        assert result.statements == case.statements, case.name

    for case in load_split_error_cases():
        result = run_local_probe(interpreter, program, case.text)
        assert result.statements is None, f"{case.name} must be rejected"
        assert result.error, f"{case.name} must be rejected with a message"


def test_module_install_passes_luacheck():
    """luacheck reports zero warnings and zero errors for sql/lua/."""
    if shutil.which("luacheck") is None:
        pytest.skip("luacheck binary is not installed")
    completed = subprocess.run(
        ["luacheck", "sql/lua/"],
        cwd=str(_REPO),
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
