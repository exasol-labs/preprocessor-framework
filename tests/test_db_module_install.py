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
from _dbfixtures import (
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

from preproc.connection import connect_from_env, missing_env_vars
from preproc.module.deploy import deploy_module, strip_script_terminator
from preproc.module.registry import read_registry

pytestmark = pytest.mark.integration

_REPO = Path(__file__).resolve().parent.parent
_MODULE_INSTALL_LUA = _REPO / "sql" / "lua" / "module_install.lua"

_TAMPER_CONNECTION = "PREPROC_TEST_BFS_INSTALL_TAMPER"
_TAMPER_BUCKET_PATH = "preproc-lib-install-tamper-0.1.0.tar.gz"
_SELF_CONNECTION = "PREPROC_TEST_BFS_INSTALL_SELF"
_SELF_BUCKET_PATH = "preproc-lib-install-self-0.1.0.tar.gz"

_CAST_SCRIPT = "PREPROC_RT.CAST_SHORTHAND_V1"
_BROKEN_SCRIPT = "PREPROC_RT.BROKEN_MODULE_V1"
_SELF_SCRIPT = "PREPROC_RT.SEMANTIC_VIEWS_V3"
_SELF_MARKER = (
    "CREATE OR REPLACE LUA SCRIPT PREPROC_RT.SEMANTIC_VIEWS_V3() AS\n"
    "-- SELF DEPLOYED MARKER (its own installer deployed this backing script)\n"
    "function semantic_views(sqltext)\n    return sqltext\nend\n"
)


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


def _stage(connection, conn_name, bucket_path, specs, *, probe, tamper=None):
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
    build_library_tarball(work / bucket_path, specs, tamper=tamper)
    try:
        bucketfs_put(config, bucket_path, (work / bucket_path).read_bytes())
    except Exception as exc:  # noqa: BLE001 - staging is environment-dependent; skip on any failure
        pytest.skip(f"cannot stage the release tarball into BucketFS: {exc}")

    connection.execute(f"CREATE OR REPLACE CONNECTION {conn_name} TO '{config.bucketfs_address}'")
    connection.execute(f"GRANT ACCESS ON CONNECTION {conn_name} TO PREPROC_ADMIN")
    source = f"bucketfs:{conn_name}/{bucket_path}"

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
        "function_name, phase, deploy_mode, status FROM PREPROC.MODULES "
        f"WHERE name = '{module_name}' AND version = {version}"
    ).fetchall()
    return InstallState(
        script_text=_script_text(connection, script_name),
        config_rows=config_rows,
        modules_row=tuple(modules[0]) if modules else None,
    )


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


def test_install_bucketfs_parity_with_cli(
    module_install_deployed, bucketfs_library_source, tmp_path
):
    """A BucketFS install leaves an end-state byte-identical to a CLI add.

    Installs via MODULE_INSTALL, captures the script/rule/provenance end-state,
    then runs the CLI ``deploy_module`` for the same module + scope from a local
    registry built from the same sources. Because both paths converge on an
    identical end-state, the CLI deploy is a no-op — so ``after == before`` proves
    byte-identical parity (a divergence would make CREATE OR REPLACE / MERGE
    change the captured state).
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
        expected_body = strip_script_terminator(
            default_artifact_text("cast_shorthand", 1, "cast_shorthand")
        )
        assert "function cast_shorthand(sqltext)" in before.script_text
        assert expected_body.splitlines()[-1] in before.script_text
        assert before.modules_row[8] == "library-deployed"
        assert before.modules_row[4] == entry.sha256
        assert before.config_rows[0][2] == "TRANSLATE"
    finally:
        _purge(connection, module_name="cast_shorthand", version=1, script_name=_CAST_SCRIPT)


def test_install_sha_mismatch_aborts_before_write(module_install_deployed, install_tamper_source):
    """A sha256 mismatch aborts before any DB write — nothing is deployed."""
    connection = module_install_deployed
    try:
        with pytest.raises(Exception):
            _install(connection, install_tamper_source, "broken_module", "ROLE:PREPROC_ADMIN")

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

        with pytest.raises(Exception):
            other.execute(
                "EXECUTE SCRIPT PREPROC.MODULE_INSTALL("
                "'bucketfs:PREPROC_TEST_BFS/preproc-lib-0.1.0.tar.gz', "
                "'cast_shorthand', 'ROLE:PREPROC_ADMIN')"
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
