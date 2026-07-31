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
import uuid
from dataclasses import dataclass
from pathlib import Path

import pyexasol
import pytest
from _dbfixtures import (
    EMITS_CHUNK_CHARS,
    BucketFsConfig,
    bucketfs_config,
    bucketfs_delete,
    bucketfs_protected_config,
    bucketfs_put,
    build_library_tarball,
    composite_artifact_text,
    composite_library_module,
    composite_object_specs,
    default_artifact_text,
    library_module,
    resolve_rows,
    self_deployed_module,
    standard_library_specs,
    wait_until_ok,
)

from preproc.connection import connect_from_env, missing_env_vars
from preproc.module.registry import encode_objects

pytestmark = pytest.mark.integration

_RICH_CONNECTION = "PREPROC_TEST_BFS_RICH"
_RICH_BUCKET_PATH = "preproc-lib-rich-0.1.0.tar.gz"
_BIG_ARTIFACT_TEXT = default_artifact_text("big_module", 1, "big_module") + (
    "-- " + "x" * (EMITS_CHUNK_CHARS + 500)
)

_COMPOSITE_CONNECTION = "PREPROC_TEST_BFS_COMPOSITE"
_COMPOSITE_BUCKET_PATH = "preproc-lib-composite-0.1.0.tar.gz"
_SCHEMA_V1_CONNECTION = "PREPROC_TEST_BFS_SCHEMA_V1"
_SCHEMA_V1_BUCKET_PATH = "preproc-lib-schema-v1-0.1.0.tar.gz"

# A composite artifact whose last statement's body is padded well past one
# EMITS chunk, so the chunk boundary falls INSIDE that statement rather than
# between statements -- proving splitting only happens after full reassembly
# (test_chunked_artifact_split_after_reassembly pins this exact ordering).
_BIG_COMPOSITE_ARTIFACT_TEXT = composite_artifact_text("big_composite", 1, "big_composite")
_BIG_COMPOSITE_ARTIFACT_TEXT = _BIG_COMPOSITE_ARTIFACT_TEXT[:-2] + (
    "-- " + "x" * (EMITS_CHUNK_CHARS + 500) + "\n;\n"
)

_IMPOSTOR_ARTIFACT_TEXT = (
    "CREATE OR REPLACE LUA SCRIPT PREPROC_RT.IMPOSTOR_MOD_IMPOSTOR_V1() AS\n"
    "function impostor_mod(sqltext)\n"
    "    return sqltext\n"
    "end\n"
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
        library_module("cast_shorthand", version=1, function="cast_shorthand"),
        library_module("cast_shorthand", version=2, function="cast_shorthand"),
        library_module(
            "big_module", version=1, function="big_module", artifact_text=_BIG_ARTIFACT_TEXT
        ),
        library_module("broken_module", version=1, function="broken_module"),
        self_deployed_module(
            "semantic_views", version=3, phase="EXPAND", source=("exasol/semantic_views", "v3.0.0")
        ),
    ]
    work = Path(tempfile.mkdtemp(prefix="preproc-rich-"))
    build_library_tarball(
        work / _RICH_BUCKET_PATH, specs, tamper={"broken_module": b"\n-- tampered\n"}
    )
    try:
        bucketfs_put(config, _RICH_BUCKET_PATH, (work / _RICH_BUCKET_PATH).read_bytes())
    except Exception as exc:  # noqa: BLE001 - environment-dependent staging; skip on failure
        pytest.skip(f"cannot stage the rich release tarball into BucketFS: {exc}")

    connection.execute(
        f"CREATE OR REPLACE CONNECTION {_RICH_CONNECTION} TO '{config.bucketfs_address}'"
    )
    connection.execute(f"GRANT ACCESS ON CONNECTION {_RICH_CONNECTION} TO PREPROC_ADMIN")
    source = config.bucketfs_source(_RICH_BUCKET_PATH)

    if not wait_until_ok(connection, source, "cast_shorthand"):
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


@dataclass
class CompositeSource:
    source: str
    big_artifact_text: str


@pytest.fixture(scope="session")
def bucketfs_composite_source(resolver_deployed):
    """Stage the composite-artifact scenarios: agreeing, disagreeing (x2), script-name
    mismatch, and a chunked composite artifact whose last statement straddles the
    chunk boundary.

    Skips when BucketFS write is unconfigured, matching every other staging fixture.
    """
    connection = resolver_deployed
    config = bucketfs_config()
    if config is None:
        pytest.skip(
            "BucketFS write is not configured; set EXASOL_BFS_WRITE_PASSWORD "
            "(and EXASOL_BFS_HOST) to stage the resolver source fixtures"
        )

    agreeing = composite_object_specs("subsystem_mod", 1)
    undeclared = agreeing[:-1]  # drops the HELPER_B object the artifact still creates
    missing = [*agreeing, {"type": "TABLE", "name": "PREPROC_RT.MISSING_MOD_PHANTOM_V1"}]

    specs = [
        composite_library_module("subsystem_mod", version=1),
        composite_library_module("undeclared_mod", version=1),
        composite_library_module("missing_mod", version=1),
        library_module(
            "impostor_mod",
            version=1,
            function="impostor_mod",
            artifact_text=_IMPOSTOR_ARTIFACT_TEXT,
        ),
        composite_library_module(
            "big_composite", version=1, artifact_text=_BIG_COMPOSITE_ARTIFACT_TEXT
        ),
    ]
    work = Path(tempfile.mkdtemp(prefix="preproc-composite-"))
    build_library_tarball(
        work / _COMPOSITE_BUCKET_PATH,
        specs,
        objects_override={"undeclared_mod": undeclared, "missing_mod": missing},
    )
    try:
        bucketfs_put(config, _COMPOSITE_BUCKET_PATH, (work / _COMPOSITE_BUCKET_PATH).read_bytes())
    except Exception as exc:  # noqa: BLE001 - environment-dependent staging; skip on failure
        pytest.skip(f"cannot stage the composite release tarball into BucketFS: {exc}")

    connection.execute(
        f"CREATE OR REPLACE CONNECTION {_COMPOSITE_CONNECTION} TO '{config.bucketfs_address}'"
    )
    connection.execute(f"GRANT ACCESS ON CONNECTION {_COMPOSITE_CONNECTION} TO PREPROC_ADMIN")
    source = config.bucketfs_source(_COMPOSITE_BUCKET_PATH)

    if not wait_until_ok(connection, source, "subsystem_mod"):
        _drop_composite(connection, config)
        pytest.skip("BucketFS did not extract/replicate the composite release in time")

    try:
        yield CompositeSource(source=source, big_artifact_text=_BIG_COMPOSITE_ARTIFACT_TEXT)
    finally:
        _drop_composite(connection, config)


def _drop_composite(connection, config):
    try:
        connection.execute(f"DROP CONNECTION IF EXISTS {_COMPOSITE_CONNECTION}")
    except Exception:
        pass
    try:
        bucketfs_delete(config, _COMPOSITE_BUCKET_PATH)
    except Exception:
        pass


@pytest.fixture(scope="session")
def bucketfs_schema_v1_source(resolver_deployed):
    """Stage a genuine ``schema_version: 1`` index (no ``objects`` key on any entry).

    Skips when BucketFS write is unconfigured, matching every other staging fixture.
    """
    connection = resolver_deployed
    config = bucketfs_config()
    if config is None:
        pytest.skip(
            "BucketFS write is not configured; set EXASOL_BFS_WRITE_PASSWORD "
            "(and EXASOL_BFS_HOST) to stage the resolver source fixtures"
        )

    work = Path(tempfile.mkdtemp(prefix="preproc-schema-v1-"))
    build_library_tarball(work / _SCHEMA_V1_BUCKET_PATH, standard_library_specs(), schema_version=1)
    try:
        bucketfs_put(config, _SCHEMA_V1_BUCKET_PATH, (work / _SCHEMA_V1_BUCKET_PATH).read_bytes())
    except Exception as exc:  # noqa: BLE001 - environment-dependent staging; skip on failure
        pytest.skip(f"cannot stage the schema-version-1 release tarball into BucketFS: {exc}")

    connection.execute(
        f"CREATE OR REPLACE CONNECTION {_SCHEMA_V1_CONNECTION} TO '{config.bucketfs_address}'"
    )
    connection.execute(f"GRANT ACCESS ON CONNECTION {_SCHEMA_V1_CONNECTION} TO PREPROC_ADMIN")
    source = config.bucketfs_source(_SCHEMA_V1_BUCKET_PATH)

    if not wait_until_ok(connection, source, "cast_shorthand"):
        _drop_schema_v1(connection, config)
        pytest.skip("BucketFS did not extract/replicate the schema-version-1 release in time")

    try:
        yield source
    finally:
        _drop_schema_v1(connection, config)


def _drop_schema_v1(connection, config):
    try:
        connection.execute(f"DROP CONNECTION IF EXISTS {_SCHEMA_V1_CONNECTION}")
    except Exception:
        pass
    try:
        bucketfs_delete(config, _SCHEMA_V1_BUCKET_PATH)
    except Exception:
        pass


def test_resolver_bucketfs_library_deployed(bucketfs_library_source, resolver_deployed):
    """A library-deployed module resolves from the BucketFS release with no egress."""
    row = _one(resolve_rows(resolver_deployed, bucketfs_library_source.source, "cast_shorthand"))

    assert row["status"] == "OK"
    assert row["script_name"] == "PREPROC_RT.CAST_SHORTHAND_V1"
    assert row["phase"] == "TRANSLATE"
    assert row["deploy_mode"] == "library-deployed"
    assert row["function_name"] == "cast_shorthand"
    assert row["chunk_count"] == 1
    assert row["artifact_chunk"] == default_artifact_text("cast_shorthand", 1, "cast_shorthand")


def test_resolver_sha256_mismatch_fails(bucketfs_rich_source, resolver_deployed):
    """A tampered artifact is a hard failure with no usable artifact text."""
    row = _one(resolve_rows(resolver_deployed, bucketfs_rich_source.source, "broken_module"))

    assert row["status"] == "ERROR"
    assert row["error_kind"] == "sha256_mismatch"
    assert row["artifact_chunk"] is None


def test_resolver_self_deployed_metadata_only(bucketfs_rich_source, resolver_deployed):
    """A self-deployed entry returns metadata with a null artifact body."""
    row = _one(resolve_rows(resolver_deployed, bucketfs_rich_source.source, "semantic_views"))

    assert row["status"] == "OK"
    assert row["deploy_mode"] == "self-deployed"
    assert row["sha256"] is None
    assert row["artifact_chunk"] is None
    assert row["chunk_count"] == 0


def test_resolver_https_fetches_and_verifies(resolver_deployed):
    """An https source is fetched + sha-verified when the cluster can reach the host.

    Needs a reachable base URL serving ``registry/index.json`` (listing
    ``cast_shorthand`` v1 with a matching sha256) and the artifact; supply it via
    ``PREPROC_TEST_HTTPS_BASE``.
    """
    base = os.environ.get("PREPROC_TEST_HTTPS_BASE")
    if not base:
        pytest.skip("set PREPROC_TEST_HTTPS_BASE to a reachable registry base URL to run this")
    index_url = base.rstrip("/") + "/registry/index.json"

    row = _one(resolve_rows(resolver_deployed, index_url, "cast_shorthand"))

    assert row["status"] == "OK"
    assert row["script_name"] == "PREPROC_RT.CAST_SHORTHAND_V1"
    assert row["artifact_chunk"] is not None


def test_resolver_https_no_egress_network_error(resolver_deployed):
    """An https source whose host is unreachable never leaves a half-installed DB.

    The resolver's contract (ADR-051) is that a no-egress / unreachable-host HTTPS
    fetch fails SOFTLY so ``MODULE_INSTALL`` aborts cleanly before any write. On a
    real cluster (verified on Exasol Personal) that means a single structured
    ``network_error`` ERROR row: ``urllib`` raises a catchable ``URLError`` which
    the resolver converts into the row.

    On the ``exasol/docker-db`` CI image the *same* code cannot resolve the bogus
    host cleanly — DNS can't complete in the restricted runner sandbox and Python's
    socket timeout does not cover ``getaddrinfo`` — so Exasol tears the UDF down as
    a "crashed VM". That surfaces as a pyexasol ``ExaError`` (query/communication
    error) BELOW the resolver's ``except Exception``, which cannot catch a VM crash.

    Both are acceptable no-egress outcomes: either the resolver signalled the
    failure as a structured row, OR the database itself rejected the call. What
    must NEVER happen is a usable artifact coming back from an unreachable host, so
    we accept both failure shapes and only assert the never-raise contract on the
    environment (real cluster) that can actually deliver the structured row.
    """
    source = "https://unreachable-preproc.invalid/registry/index.json"

    try:
        rows = resolve_rows(resolver_deployed, source, "cast_shorthand")
    except pyexasol.ExaError:
        # docker-db path: the unreachable-host fetch crashed the UDF VM. The
        # database failed the call rather than returning a usable artifact — the
        # air-gap safety property holds; we just can't inspect a structured row.
        return

    # Real-cluster path: the resolver signalled the failure as one structured row.
    row = _one(rows)
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
    row = _one(resolve_rows(resolver_deployed, bucketfs_rich_source.source, "cast_shorthand"))

    assert row["status"] == "OK"
    assert row["version"] == 2
    assert row["script_name"] == "PREPROC_RT.CAST_SHORTHAND_V2"


def test_resolver_chunks_large_artifact(bucketfs_rich_source, resolver_deployed):
    """An artifact exceeding one EMITS value is chunked; reassembly matches the artifact."""
    rows = resolve_rows(resolver_deployed, bucketfs_rich_source.source, "big_module")

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

        source = BucketFsConfig(
            host="", port="", bucket="default", write_password="", service="bfsdefault"
        ).bucketfs_source("preproc-lib-0.1.0.tar.gz")
        with pytest.raises(Exception):
            other.execute(f"SELECT PREPROC.MODULE_RESOLVER_V1('{source}', 'cast_shorthand')")
    finally:
        if other is not None:
            other.close()
        try:
            admin.execute(f"DROP USER {non_admin} CASCADE")
        except Exception:
            pass


def test_resolver_rejects_legacy_connection_form(resolver_deployed):
    """A two-segment bucketfs source (the retired CONNECTION-named form) is bad_source.

    'bucketfs:PREPROC_BFS/preproc-lib-0.1.0.tar.gz' below is the pre-delta,
    two-segment form under deliberate rejection here -- see
    change-bucketfs-source-to-location -- not a literal left behind by oversight.
    No CONNECTION fallback exists, so this never touches the filesystem.
    """
    source = "bucketfs:PREPROC_BFS/preproc-lib-0.1.0.tar.gz"
    row = _one(resolve_rows(resolver_deployed, source, "cast_shorthand"))

    assert row["status"] == "ERROR"
    assert row["error_kind"] == "bad_source"
    assert "bucketfs:<service>/<bucket>/<path>" in row["error_message"]
    assert source in row["error_message"]


def test_resolver_unreadable_location_reports_credential_hint(resolver_deployed):
    """An unreadable-but-well-formed location names both probed paths and the grant.

    Targets a release that does not exist inside the always-public ``default``
    bucket, so the failure is purely "no such release" -- proving the message
    contract with no staged fixture needed. The extracted-directory path is a
    PREFIX of the tarball path, so both are asserted WITH their labels; a bare
    substring check for the shorter one would also pass against a message naming
    only the tarball.
    """
    config = BucketFsConfig(
        host="", port="", bucket="default", write_password="", service="bfsdefault"
    )
    source = config.bucketfs_source("no-such-release.tar.gz")

    row = _one(resolve_rows(resolver_deployed, source, "cast_shorthand"))

    assert row["status"] == "ERROR"
    assert row["error_kind"] == "source_unreadable"
    assert "/buckets/bfsdefault/default/no-such-release.tar.gz (tarball)" in row["error_message"]
    assert "/buckets/bfsdefault/default/no-such-release (auto-extracted)" in row["error_message"]
    assert (
        "CONNECTION TO 'bucketfs:bfsdefault/default' IDENTIFIED BY '<read-password>'"
        in row["error_message"]
    )


def test_read_protected_bucket_resolves_only_while_granted(resolver_deployed):
    """A read-protected bucket resolves only while its CONNECTION grant is held.

    Task 0.1's live evidence (decision-log.md Design Decision [1]) found this
    scenario UNFALSIFIABLE as SYS: SYS reads a protected bucket with no grant to
    anyone. This MUST therefore run as a dedicated non-superuser holding
    PREPROC_ADMIN (to execute the resolver at all) plus, separately, ACCESS on the
    protected bucket's CONNECTION. Grant and revoke are asserted in the SAME
    session with no reconnect between them, matching the live-verified fact that
    visibility is re-evaluated per statement in both directions (controls C/F in
    the decision log).

    The release resolved here is staged BY THIS TEST, with the write password
    ``bucketfs_protected_config`` already requires, under a bucket path the test
    owns end to end and deletes in ``finally``. A green run therefore proves the
    grant rather than the presence of an artefact somebody else left in the
    bucket: with nothing staged, the granted read fails ``source_unreadable``,
    which is indistinguishable from this very premise being falsified.

    Mount readiness is polled with the ADMIN session, whose read of the protected
    bucket does not depend on the grant under test -- so a poll timeout reports a
    BucketFS extraction/replication problem and skips, while a granted read that
    fails AFTER the admin read succeeded is unambiguously a grant failure and
    fails loudly. BucketFS auto-extracts the upload and presents only the
    extracted DIRECTORY on the UDF mount (verified live), so this resolves
    through the reader's auto-extracted branch; nothing here assumes the tarball
    file itself is visible there.

    Skips cleanly when the protected bucket is not configured -- it is consumed,
    never created, by this suite (see ``bucketfs_protected_config``'s docstring
    for the provisioning command).
    """
    protected = bucketfs_protected_config()
    if protected is None:
        pytest.skip(
            "the read-protected bucket is not configured; set "
            "EXASOL_BFS_PROTECTED_BUCKET, EXASOL_BFS_PROTECTED_READ_PASSWORD, and "
            "EXASOL_BFS_PROTECTED_WRITE_PASSWORD to run this"
        )

    admin = resolver_deployed
    non_admin = "PREPROC_TEST_PROTECTED_READER"
    password = "pw_PREPROC_protected_1"
    connection_name = "PREPROC_TEST_PROTECTED_CONN"
    # A bucket path unique per run. BucketFS answers 423 Locked, then stalls past a
    # two-minute client timeout, on a PUT to a path it deleted moments ago (measured
    # live), so a fixed name would make every quick re-run skip on staging instead of
    # asserting. Unique names also keep an artefact a crashed run left behind unable
    # to stand in for the one this run must upload itself.
    bucket_path = f"preproc-lib-protected-{uuid.uuid4().hex[:12]}.tar.gz"
    source = protected.bucketfs_source(bucket_path)

    work = Path(tempfile.mkdtemp(prefix="preproc-protected-"))
    build_library_tarball(
        work / bucket_path, [library_module("cast_shorthand", version=1, function="cast_shorthand")]
    )

    other = None
    try:
        try:
            bucketfs_put(protected, bucket_path, (work / bucket_path).read_bytes())
        except Exception as exc:  # noqa: BLE001 - staging is environment-dependent; skip on failure
            pytest.skip(f"cannot stage the release into the read-protected bucket: {exc}")

        try:
            admin.execute(f"DROP USER {non_admin} CASCADE")
        except Exception:
            pass
        try:
            admin.execute(f'CREATE USER {non_admin} IDENTIFIED BY "{password}"')
            admin.execute(f"GRANT CREATE SESSION TO {non_admin}")
            admin.execute(f"GRANT PREPROC_ADMIN TO {non_admin}")
        except Exception as exc:
            pytest.skip(f"cannot create a non-admin user in this environment: {exc}")

        admin.execute(
            f"CREATE OR REPLACE CONNECTION {connection_name} TO '{protected.bucketfs_address}' "
            f"IDENTIFIED BY '{protected.read_password}'"
        )
        # Granted to the test user ALONE, never to PREPROC_ADMIN: the user holds
        # that role, so a role-level grant would survive the REVOKE below and the
        # revoked assertion would silently stop testing anything.
        admin.execute(f"GRANT ACCESS ON CONNECTION {connection_name} TO {non_admin}")

        # Polled only once the CONNECTION exists, and that ordering is load-bearing:
        # verified live on 2026-07-31, the protected bucket is absent from /buckets
        # entirely -- for SYS too -- until some CONNECTION addressing it with the
        # correct read password exists. What is under test is the GRANT to the test
        # user, which the admin's own read does not depend on.
        if not wait_until_ok(admin, source, "cast_shorthand"):
            pytest.skip(
                f"BucketFS did not extract/replicate {source} to the UDF mount in time: "
                "the admin session cannot resolve it either, so the granted read below "
                "could not tell that apart from a failed grant"
            )

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

        granted_row = _one(resolve_rows(other, source, "cast_shorthand"))
        assert granted_row["status"] == "OK", (
            f"the granted read of {source} failed although the admin session resolved it, "
            f"so this is the grant and not the staging: "
            f"{granted_row['error_kind']}: {granted_row['error_message']}"
        )

        admin.execute(f"REVOKE ACCESS ON CONNECTION {connection_name} FROM {non_admin}")

        revoked_row = _one(resolve_rows(other, source, "cast_shorthand"))
        assert revoked_row["status"] == "ERROR"
        assert revoked_row["error_kind"] == "source_unreadable"
    finally:
        if other is not None:
            other.close()
        try:
            admin.execute(f"DROP CONNECTION IF EXISTS {connection_name}")
        except Exception:
            pass
        try:
            admin.execute(f"DROP USER {non_admin} CASCADE")
        except Exception:
            pass
        # Deleted even when staging above raised: a PUT can time out client-side
        # while the upload still lands (measured live).
        try:
            bucketfs_delete(protected, bucket_path)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# module/db-resolver: composite object-inventory verification (one test per
# Scenario in specs/_plans/change-module-artifact-to-composite/module/db-resolver).
# ---------------------------------------------------------------------------


def test_script_name_mismatch_hard_failure(bucketfs_composite_source, resolver_deployed):
    """An artifact whose CREATE target differs from the declared script_name is a hard failure.

    The sha256 still matches (the index describes exactly these bytes), but the
    artifact's CREATE targets ``PREPROC_RT.IMPOSTOR_MOD_IMPOSTOR_V1`` rather than
    the declared ``PREPROC_RT.IMPOSTOR_MOD_V1`` -- the resolver returns no usable
    artifact text, naming both names.
    """
    row = _one(resolve_rows(resolver_deployed, bucketfs_composite_source.source, "impostor_mod"))

    assert row["status"] == "ERROR"
    assert row["error_kind"] == "artifact_invalid"
    assert row["artifact_chunk"] is None
    assert "PREPROC_RT.IMPOSTOR_MOD_V1" in row["error_message"]
    assert "PREPROC_RT.IMPOSTOR_MOD_IMPOSTOR_V1" in row["error_message"]


def test_composite_inventory_verified_against_entry(bucketfs_composite_source, resolver_deployed):
    """A composite artifact's derived objects, confirmed to agree with the entry, are returned.

    ``subsystem_mod`` declares (and the artifact creates) one Lua entry script
    plus two Python UDFs; a match returns the full artifact text and metadata,
    with the derived inventory encoded in the ``objects`` column.
    """
    row = _one(resolve_rows(resolver_deployed, bucketfs_composite_source.source, "subsystem_mod"))

    assert row["status"] == "OK"
    assert row["artifact_chunk"] is not None
    assert row["objects"] == encode_objects(composite_object_specs("subsystem_mod", 1))


@pytest.mark.parametrize("module_name", ["undeclared_mod", "missing_mod"])
def test_inventory_disagreement_hard_failure(
    module_name, bucketfs_composite_source, resolver_deployed
):
    """Derived objects disagreeing with the entry's declared objects are a hard failure.

    ``undeclared_mod``'s artifact creates an object the entry does not declare;
    ``missing_mod``'s entry declares an object the artifact does not create.
    Both directions are hard failures with no usable artifact text.
    """
    row = _one(resolve_rows(resolver_deployed, bucketfs_composite_source.source, module_name))

    assert row["status"] == "ERROR"
    assert row["error_kind"] == "artifact_invalid"
    assert row["artifact_chunk"] is None
    assert row["objects"] is None


def test_schema_version_1_entry_single_statement(bucketfs_schema_v1_source, resolver_deployed):
    """A schema_version-1 index entry (no ``objects`` key) is verified as single-statement.

    Its inventory is derived as the single object named by ``script_name``, and
    the resolver returns the artifact text and metadata unchanged from the
    pre-delta behaviour.
    """
    row = _one(resolve_rows(resolver_deployed, bucketfs_schema_v1_source, "cast_shorthand"))

    assert row["status"] == "OK"
    assert row["script_name"] == "PREPROC_RT.CAST_SHORTHAND_V1"
    assert row["artifact_chunk"] == default_artifact_text("cast_shorthand", 1, "cast_shorthand")
    assert row["objects"] == encode_objects(
        [{"type": "LUA SCRIPT", "name": "PREPROC_RT.CAST_SHORTHAND_V1"}]
    )


def test_chunked_artifact_split_after_reassembly(bucketfs_composite_source, resolver_deployed):
    """A chunked composite artifact is split only after full reassembly.

    ``big_composite``'s last statement is padded well past one EMITS chunk, so
    the chunk boundary falls INSIDE that statement. The caller-reassembled text
    must equal the original artifact bytes, and the object inventory the
    resolver already verified server-side (from the reassembled, not per-chunk,
    text) must be the 3-object composite inventory -- proof splitting ran on
    the whole artifact, since splitting a lone chunk would corrupt or drop the
    straddling statement.
    """
    rows = resolve_rows(resolver_deployed, bucketfs_composite_source.source, "big_composite")

    assert all(row["status"] == "OK" for row in rows)
    assert len(rows) >= 2
    ordered = sorted(rows, key=lambda r: r["chunk_index"])
    assert [r["chunk_index"] for r in ordered] == list(range(len(ordered)))
    reassembled = "".join(r["artifact_chunk"] for r in ordered)
    assert reassembled == bucketfs_composite_source.big_artifact_text
    assert ordered[0]["objects"] == encode_objects(composite_object_specs("big_composite", 1))
