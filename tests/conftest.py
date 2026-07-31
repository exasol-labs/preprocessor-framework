"""Pytest fixtures for integration tests against an env-configured Exasol instance.

Connection parameters are resolved from the environment by ``preproc.connection``
so that the same suite runs unchanged against an ephemeral ``exasol/docker-db``
container in CI or any live instance locally. When the required variables are
unset the database fixtures skip cleanly rather than erroring.
"""

import tempfile
from dataclasses import dataclass
from pathlib import Path

import pytest
from _dbfixtures import (
    bucketfs_config,
    bucketfs_delete,
    bucketfs_put,
    build_library_tarball,
    deploy_resolver_udf,
    standard_library_specs,
    wait_until_ok,
)

from preproc.connection import connect_from_env, missing_env_vars
from preproc.install import run_install
from preproc.module.registry import RegistryIndex

_TEST_CONNECTION = "PREPROC_TEST_BFS"
_RELEASE_BUCKET_PATH = "preproc-lib-0.1.0.tar.gz"


@pytest.fixture(scope="session")
def db():
    """Yield an open pyexasol connection to the env-configured instance.

    Skips the requesting test when the connection environment is not configured,
    naming the missing variables, and closes the connection on teardown.
    """
    missing = missing_env_vars()
    if missing:
        pytest.skip("no Exasol instance is configured; set " + ", ".join(missing))

    connection = connect_from_env()
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture(scope="session")
def installed(db):
    """Run the idempotent CLI install once per session before layout assertions."""
    run_install(db)
    return db


@pytest.fixture(scope="session")
def resolver_deployed(installed):
    """Deploy PREPROC.MODULE_RESOLVER_V1 once per session.

    Group C wires the resolver into ``install.py``; until then the in-DB
    install/catalog suites deploy it directly here, mirroring how the demo UDF
    is deployed. Returns the admin (session) connection.
    """
    deploy_resolver_udf(installed)
    return installed


@dataclass
class BucketFsLibrarySource:
    """A BucketFS-staged release tarball plus what a test needs to assert on it."""

    source: str
    connection: str
    bucket_path: str
    index: RegistryIndex


@pytest.fixture(scope="session")
def bucketfs_library_source(resolver_deployed):
    """Build + stage a release tarball into BucketFS and expose its ``source``.

    Shared by the resolver, install, and catalog suites. Skips cleanly (matching
    the ``db`` fixture) when BucketFS write is not configured — BucketFS reads are
    public but writes need ``EXASOL_BFS_WRITE_PASSWORD`` (a distinct secret). The
    operator-side CONNECTION is created by the TEST, never the framework, and
    ACCESS is granted to PREPROC_ADMIN (parity with the documented setup) — but
    the ``source`` string built below never names that CONNECTION. It is the
    plain BucketFS location ``bucketfs:<service>/<bucket>/<path>``, built through
    ``BucketFsConfig.bucketfs_source``: the CONNECTION stays purely a credential
    grant, demonstrated here by keeping it while the source no longer needs it.
    """
    connection = resolver_deployed
    config = bucketfs_config()
    if config is None:
        pytest.skip(
            "BucketFS write is not configured; set EXASOL_BFS_WRITE_PASSWORD "
            "(and EXASOL_BFS_HOST) to stage the release fixture"
        )

    work = Path(tempfile.mkdtemp(prefix="preproc-fixture-"))
    built = build_library_tarball(work / _RELEASE_BUCKET_PATH, standard_library_specs())
    try:
        bucketfs_put(config, _RELEASE_BUCKET_PATH, (work / _RELEASE_BUCKET_PATH).read_bytes())
    except Exception as exc:  # noqa: BLE001 - staging is environment-dependent; skip on any failure
        pytest.skip(f"cannot stage the release tarball into BucketFS: {exc}")

    connection.execute(
        f"CREATE OR REPLACE CONNECTION {_TEST_CONNECTION} TO '{config.bucketfs_address}'"
    )
    connection.execute(f"GRANT ACCESS ON CONNECTION {_TEST_CONNECTION} TO PREPROC_ADMIN")
    source = config.bucketfs_source(_RELEASE_BUCKET_PATH)

    if not wait_until_ok(connection, source, "cast_shorthand"):
        _drop_bucketfs_source(connection, config)
        pytest.skip("BucketFS did not extract/replicate the release to the UDF mount in time")

    try:
        yield BucketFsLibrarySource(
            source=source,
            connection=_TEST_CONNECTION,
            bucket_path=_RELEASE_BUCKET_PATH,
            index=built.index,
        )
    finally:
        _drop_bucketfs_source(connection, config)


def _drop_bucketfs_source(connection, config):
    try:
        connection.execute(f"DROP CONNECTION IF EXISTS {_TEST_CONNECTION}")
    except Exception:
        pass
    try:
        bucketfs_delete(config, _RELEASE_BUCKET_PATH)
    except Exception:
        pass
