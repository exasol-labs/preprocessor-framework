"""Integration tests for PREPROC.MODULE_RESOLVER_V1 against a live Exasol.

One test per Scenario in the ``module/db-resolver`` spec. Each deploys the
resolver UDF (via the ``resolver_deployed`` fixture) and drives it over SQL.

Two environment facts shape which tests execute versus skip cleanly (matching
the repo's ``db``/``test_refresh_requires_admin`` skip discipline):

* BucketFS READS are public but WRITES need ``EXASOL_BFS_WRITE_PASSWORD`` (a
  distinct secret). The source-staging tests skip when it is unset. The resolve
  algorithm itself is proven hermetically in ``tests/test_module_resolver_udf.py``.
* The reachable-HTTPS success test needs a reachable host serving a controllable
  ``registry/index.json`` + artifact with a matching sha256; it uses
  ``PREPROC_TEST_HTTPS_BASE`` when provided and skips otherwise. The no-egress
  network-error and admin-only tests need neither and always run live.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

import pytest
from _dbfixtures import (
    EMITS_CHUNK_CHARS,
    bucketfs_config,
    bucketfs_delete,
    bucketfs_put,
    build_library_tarball,
    default_artifact_text,
    library_module,
    resolve_rows,
    self_deployed_module,
    wait_until_ok,
)

from preproc.connection import connect_from_env, missing_env_vars

pytestmark = pytest.mark.integration

_RICH_CONNECTION = "PREPROC_TEST_BFS_RICH"
_RICH_BUCKET_PATH = "preproc-lib-rich-0.1.0.tar.gz"
_BIG_ARTIFACT_TEXT = default_artifact_text("big-module", 1, "big_module") + (
    "-- " + "x" * (EMITS_CHUNK_CHARS + 500)
)


def _one(rows):
    assert len(rows) == 1, f"expected a single row, got {len(rows)}"
    return rows[0]


@dataclass
class RichSource:
    source: str
    big_artifact_text: str


@pytest.fixture(scope="session")
def bucketfs_rich_source(resolver_deployed):
    """Stage a multi-scenario release: v1+v2, self-deployed, chunked, and tampered.

    Skips when BucketFS write is unconfigured. One upload backs the latest-version,
    self-deployed, chunking, sha256-mismatch, and not-found integration checks.
    """
    connection = resolver_deployed
    config = bucketfs_config()
    if config is None:
        pytest.skip(
            "BucketFS write is not configured; set EXASOL_BFS_WRITE_PASSWORD "
            "(and EXASOL_BFS_HOST) to stage the resolver source fixtures"
        )

    specs = [
        library_module("cast-shorthand", version=1, function="cast_shorthand"),
        library_module("cast-shorthand", version=2, function="cast_shorthand"),
        library_module(
            "big-module", version=1, function="big_module", artifact_text=_BIG_ARTIFACT_TEXT
        ),
        library_module("broken-module", version=1, function="broken_module"),
        self_deployed_module(
            "semantic-views", version=3, phase="EXPAND", source=("exasol/semantic-views", "v3.0.0")
        ),
    ]
    work = Path(tempfile.mkdtemp(prefix="preproc-rich-"))
    build_library_tarball(
        work / _RICH_BUCKET_PATH, specs, tamper={"broken-module": b"\n-- tampered\n"}
    )
    try:
        bucketfs_put(config, _RICH_BUCKET_PATH, (work / _RICH_BUCKET_PATH).read_bytes())
    except Exception as exc:  # noqa: BLE001 - environment-dependent staging; skip on failure
        pytest.skip(f"cannot stage the rich release tarball into BucketFS: {exc}")

    connection.execute(f"CREATE OR REPLACE CONNECTION {_RICH_CONNECTION} TO '{config.base_url}'")
    connection.execute(f"GRANT ACCESS ON CONNECTION {_RICH_CONNECTION} TO PREPROC_ADMIN")
    source = f"bucketfs:{_RICH_CONNECTION}/{_RICH_BUCKET_PATH}"

    if not wait_until_ok(connection, source, "cast-shorthand"):
        _drop(connection, config)
        pytest.skip("BucketFS did not extract/replicate the rich release to the UDF mount in time")

    try:
        yield RichSource(source=source, big_artifact_text=_BIG_ARTIFACT_TEXT)
    finally:
        _drop(connection, config)


def _drop(connection, config):
    try:
        connection.execute(f"DROP CONNECTION IF EXISTS {_RICH_CONNECTION}")
    except Exception:
        pass
    try:
        bucketfs_delete(config, _RICH_BUCKET_PATH)
    except Exception:
        pass


def test_resolver_bucketfs_library_deployed(bucketfs_library_source, resolver_deployed):
    """A library-deployed module resolves from the BucketFS release with no egress."""
    row = _one(resolve_rows(resolver_deployed, bucketfs_library_source.source, "cast-shorthand"))

    assert row["status"] == "OK"
    assert row["script_name"] == "PREPROC_RT.CAST_SHORTHAND_V1"
    assert row["phase"] == "TRANSLATE"
    assert row["deploy_mode"] == "library-deployed"
    assert row["function_name"] == "cast_shorthand"
    assert row["chunk_count"] == 1
    assert row["artifact_chunk"] == default_artifact_text("cast-shorthand", 1, "cast_shorthand")


def test_resolver_sha256_mismatch_fails(bucketfs_rich_source, resolver_deployed):
    """A tampered artifact is a hard failure with no usable artifact text."""
    row = _one(resolve_rows(resolver_deployed, bucketfs_rich_source.source, "broken-module"))

    assert row["status"] == "ERROR"
    assert row["error_kind"] == "sha256_mismatch"
    assert row["artifact_chunk"] is None


def test_resolver_self_deployed_metadata_only(bucketfs_rich_source, resolver_deployed):
    """A self-deployed entry returns metadata with a null artifact body."""
    row = _one(resolve_rows(resolver_deployed, bucketfs_rich_source.source, "semantic-views"))

    assert row["status"] == "OK"
    assert row["deploy_mode"] == "self-deployed"
    assert row["sha256"] is None
    assert row["artifact_chunk"] is None
    assert row["chunk_count"] == 0


def test_resolver_https_fetches_and_verifies(resolver_deployed):
    """An https source is fetched + sha-verified when the cluster can reach the host.

    Needs a reachable base URL serving ``registry/index.json`` (listing
    ``cast-shorthand`` v1 with a matching sha256) and the artifact; supply it via
    ``PREPROC_TEST_HTTPS_BASE``.
    """
    base = os.environ.get("PREPROC_TEST_HTTPS_BASE")
    if not base:
        pytest.skip("set PREPROC_TEST_HTTPS_BASE to a reachable registry base URL to run this")
    index_url = base.rstrip("/") + "/registry/index.json"

    row = _one(resolve_rows(resolver_deployed, index_url, "cast-shorthand"))

    assert row["status"] == "OK"
    assert row["script_name"] == "PREPROC_RT.CAST_SHORTHAND_V1"
    assert row["artifact_chunk"] is not None


def test_resolver_https_no_egress_network_error(resolver_deployed):
    """An https source whose host is unreachable is signalled, never raised."""
    source = "https://unreachable-preproc.invalid/registry/index.json"

    row = _one(resolve_rows(resolver_deployed, source, "cast-shorthand"))

    assert row["status"] == "ERROR"
    assert row["error_kind"] == "network_error"
    assert "unreachable-preproc.invalid" in row["error_message"]
    assert row["artifact_chunk"] is None


def test_resolver_module_not_found(bucketfs_library_source, resolver_deployed):
    """A module absent from the source index is reported not found."""
    row = _one(resolve_rows(resolver_deployed, bucketfs_library_source.source, "no-such-module"))

    assert row["status"] == "ERROR"
    assert row["error_kind"] == "not_found"
    assert "no-such-module" in row["error_message"]
    assert row["artifact_chunk"] is None


def test_resolver_selects_latest_version(bucketfs_rich_source, resolver_deployed):
    """With versions 1 and 2 listed, the highest version is selected (_V2 suffix)."""
    row = _one(resolve_rows(resolver_deployed, bucketfs_rich_source.source, "cast-shorthand"))

    assert row["status"] == "OK"
    assert row["version"] == 2
    assert row["script_name"] == "PREPROC_RT.CAST_SHORTHAND_V2"


def test_resolver_chunks_large_artifact(bucketfs_rich_source, resolver_deployed):
    """An artifact exceeding one EMITS value is chunked; reassembly matches the artifact."""
    rows = resolve_rows(resolver_deployed, bucketfs_rich_source.source, "big-module")

    assert all(row["status"] == "OK" for row in rows)
    assert len(rows) >= 2
    ordered = sorted(rows, key=lambda r: r["chunk_index"])
    assert [r["chunk_index"] for r in ordered] == list(range(len(ordered)))
    assert all(len(r["artifact_chunk"]) <= EMITS_CHUNK_CHARS for r in ordered)
    assert "".join(r["artifact_chunk"] for r in ordered) == bucketfs_rich_source.big_artifact_text


def test_resolver_admin_only(resolver_deployed):
    """A non-admin user cannot execute the resolver; it is never granted to PUBLIC.

    Requires CREATE USER privilege and a second login; skips (rather than a false
    pass) where the env-configured account cannot mint one — the admin boundary is
    also asserted structurally by the schema-layout suite.
    """
    if missing_env_vars():
        pytest.skip("no Exasol instance is configured")

    admin = resolver_deployed
    non_admin = "PREPROC_TEST_RESOLVER_NOADMIN"
    password = "pw_PREPROC_resolver_1"
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
                "SELECT PREPROC.MODULE_RESOLVER_V1("
                "'bucketfs:PREPROC_TEST_BFS/preproc-lib-0.1.0.tar.gz', 'cast-shorthand')"
            )
    finally:
        if other is not None:
            other.close()
        try:
            admin.execute(f"DROP USER {non_admin} CASCADE")
        except Exception:
            pass
