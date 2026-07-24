"""Hermetic tests for the resolver UDF body (``sql/udf/module_resolver_v1.sql``).

The resolver's Python body is stdlib-only and reaches the runtime only through
``exa.get_connection`` and the emit/iterate ``ctx`` protocol, so it runs
off-database against fakes. These tests exercise every resolve path — BucketFS
tarball read, https fetch, sha256 verification, self-deployed skip, latest-
version select, chunking, and the never-raise error signalling — deterministically
and with no cluster or network. The live counterparts live in
``tests/test_db_resolver.py``.
"""

from __future__ import annotations

import tarfile
import urllib.error
from pathlib import Path

from _dbfixtures import (
    EMITS_CHUNK_CHARS,
    build_library_tarball,
    default_artifact_text,
    fake_http_directory,
    library_module,
    make_fake_exa,
    run_resolver_body,
    self_deployed_module,
    standard_library_specs,
)

_TARBALL_NAME = "preproc-lib-0.1.0.tar.gz"
_EXTRACTED_DIR = "preproc-lib-0.1.0"


def _stage_local(tmp_path: Path, specs, *, mode="extracted", **kwargs):
    """Stage a release under a fake bucket root; return (source, exa, built).

    ``mode="extracted"`` mirrors BucketFS's live behaviour — an uploaded archive
    is auto-extracted to the extension-less directory and the raw archive is not
    present on the mount. ``mode="tarball"`` leaves the raw ``.tar.gz`` file, for
    buckets that do not auto-extract.
    """
    bucket_root = tmp_path / "buckets"
    bucket_root.mkdir(parents=True, exist_ok=True)
    tgz = bucket_root / _TARBALL_NAME
    built = build_library_tarball(tgz, specs, **kwargs)
    if mode == "extracted":
        with tarfile.open(tgz, "r:gz") as tar:
            tar.extractall(bucket_root / _EXTRACTED_DIR, filter="data")
        tgz.unlink()
    source = f"bucketfs:LIB/{_TARBALL_NAME}"
    exa = make_fake_exa({"LIB": str(bucket_root)})
    return source, exa, built


def _ok_rows(rows):
    assert rows, "resolver emitted no rows"
    for row in rows:
        assert row["status"] == "OK", f"expected OK, got {row['status']}: {row['error_message']}"
    return rows


def _error_row(rows):
    assert len(rows) == 1, f"an error must be a single row; got {len(rows)}"
    assert rows[0]["status"] == "ERROR"
    return rows[0]


def test_bucketfs_library_deployed_resolves(tmp_path):
    """A library-deployed module reads its artifact + metadata from the auto-extracted release."""
    source, exa, _ = _stage_local(tmp_path, standard_library_specs())

    rows = _ok_rows(run_resolver_body(source, "cast_shorthand", exa=exa))

    assert len(rows) == 1
    row = rows[0]
    assert row["name"] == "cast_shorthand"
    assert row["version"] == 1
    assert row["phase"] == "TRANSLATE"
    assert row["script_name"] == "PREPROC_RT.CAST_SHORTHAND_V1"
    assert row["function_name"] == "cast_shorthand"
    assert row["deploy_mode"] == "library-deployed"
    assert row["suggested_scope_type"] == "ROLE"
    assert row["suggested_scope_value"] == "PREPROC_ADMIN"
    assert row["chunk_count"] == 1
    assert row["chunk_index"] == 0
    assert row["artifact_chunk"] == default_artifact_text("cast_shorthand", 1, "cast_shorthand")


def test_bucketfs_raw_tarball_file_resolves(tmp_path):
    """A bucket that does NOT auto-extract keeps the raw .tar.gz; tarfile reads it."""
    source, exa, _ = _stage_local(tmp_path, standard_library_specs(), mode="tarball")

    rows = _ok_rows(run_resolver_body(source, "cast_shorthand", exa=exa))

    assert rows[0]["script_name"] == "PREPROC_RT.CAST_SHORTHAND_V1"
    assert rows[0]["artifact_chunk"] == default_artifact_text("cast_shorthand", 1, "cast_shorthand")


def test_sha256_mismatch_is_hard_failure(tmp_path):
    """A tampered artifact hashes to a different digest — no usable artifact is returned."""
    source, exa, _ = _stage_local(
        tmp_path, standard_library_specs(), tamper={"cast_shorthand": b"\n-- tampered\n"}
    )

    row = _error_row(run_resolver_body(source, "cast_shorthand", exa=exa))

    assert row["error_kind"] == "sha256_mismatch"
    assert row["artifact_chunk"] is None
    assert "cast_shorthand" in row["error_message"]


def test_script_name_mismatch_is_hard_failure(tmp_path):
    """An artifact whose CREATE targets a name other than the declared script_name fails.

    Structural parity with the CLI's ``verify_artifact_script_name``: the sha256
    still matches (the index describes exactly these bytes), but the artifact's
    CREATE targets ``PREPROC_RT.IMPOSTOR_V1`` rather than the declared
    ``PREPROC_RT.CAST_SHORTHAND_V1`` -- the resolver returns no usable artifact.
    """
    impostor = (
        "CREATE OR REPLACE LUA SCRIPT PREPROC_RT.IMPOSTOR_V1() AS\n"
        "function cast_shorthand(sqltext)\n"
        "    return sqltext\n"
        "end\n"
    )
    specs = [
        library_module(
            "cast_shorthand", version=1, function="cast_shorthand", artifact_text=impostor
        )
    ]
    source, exa, _ = _stage_local(tmp_path, specs)

    row = _error_row(run_resolver_body(source, "cast_shorthand", exa=exa))

    assert row["error_kind"] == "artifact_invalid"
    assert row["artifact_chunk"] is None
    assert "PREPROC_RT.IMPOSTOR_V1" in row["error_message"]
    assert "PREPROC_RT.CAST_SHORTHAND_V1" in row["error_message"]


def test_artifact_without_create_is_hard_failure(tmp_path):
    """An artifact carrying zero CREATE OR REPLACE LUA SCRIPT statements fails structurally."""
    specs = [
        library_module(
            "cast_shorthand",
            version=1,
            function="cast_shorthand",
            artifact_text="-- not a script at all\n",
        )
    ]
    source, exa, _ = _stage_local(tmp_path, specs)

    row = _error_row(run_resolver_body(source, "cast_shorthand", exa=exa))

    assert row["error_kind"] == "artifact_invalid"
    assert row["artifact_chunk"] is None
    assert "found 0" in row["error_message"]


def test_self_deployed_returns_metadata_only(tmp_path):
    """A self-deployed entry carries no artifact: metadata only, no fetch/verify."""
    specs = [
        self_deployed_module(
            "semantic_views", version=3, phase="EXPAND", source=("exasol/semantic_views", "v3.0.0")
        )
    ]
    source, exa, _ = _stage_local(tmp_path, specs)

    rows = _ok_rows(run_resolver_body(source, "semantic_views", exa=exa))

    assert len(rows) == 1
    row = rows[0]
    assert row["deploy_mode"] == "self-deployed"
    assert row["sha256"] is None
    assert row["artifact_chunk"] is None
    assert row["chunk_count"] == 0
    # A self-deployed module hosted in the library dir is stamped with the
    # library's repo/ref by generate_index (module.toml [source] is used only
    # for registry/external entries), so the entry names the library source.
    assert row["source_repo"] == "exasol-labs/preprocessor-library"
    assert row["source_ref"] == "v0.1.0"


def test_library_version_is_surfaced_on_every_row(tmp_path):
    """The index's top-level library_version is echoed in each emitted row.

    This is what lets an install resolved through a mutable `latest` source
    record the concrete release it actually got.
    """
    source, exa, _ = _stage_local(
        tmp_path, standard_library_specs(), library_version="0.2.0"
    )

    rows = _ok_rows(run_resolver_body(source, "cast_shorthand", exa=exa))

    assert rows
    assert all(row["library_version"] == "0.2.0" for row in rows)


def test_library_version_absent_is_null(tmp_path):
    """An index with no library_version surfaces NULL — older indexes still resolve."""
    source, exa, _ = _stage_local(tmp_path, standard_library_specs())

    rows = _ok_rows(run_resolver_body(source, "cast_shorthand", exa=exa))

    assert rows
    assert all(row["library_version"] is None for row in rows)


def test_module_not_found_is_signalled(tmp_path):
    """A module absent from the index yields a not-found signal, not an artifact."""
    source, exa, _ = _stage_local(tmp_path, standard_library_specs())

    row = _error_row(run_resolver_body(source, "no-such-module", exa=exa))

    assert row["error_kind"] == "not_found"
    assert "no-such-module" in row["error_message"]
    assert row["artifact_chunk"] is None


def test_module_name_matched_case_insensitively(tmp_path):
    """A module name resolves regardless of case — the sugar folds an unquoted name
    to UPPER while registry keys are lower_snake_case, so matching ignores case."""
    source, exa, _ = _stage_local(tmp_path, standard_library_specs())

    for queried in ("cast_shorthand", "CAST_SHORTHAND", "Cast_Shorthand"):
        rows = _ok_rows(run_resolver_body(source, queried, exa=exa))
        assert rows[0]["name"] == "cast_shorthand"
        assert rows[0]["script_name"] == "PREPROC_RT.CAST_SHORTHAND_V1"


def test_latest_version_is_selected(tmp_path):
    """With versions 1 and 2 listed, the highest version wins and carries the _V2 suffix."""
    specs = [
        library_module("cast_shorthand", version=1, function="cast_shorthand"),
        library_module("cast_shorthand", version=2, function="cast_shorthand"),
    ]
    source, exa, _ = _stage_local(tmp_path, specs)

    rows = _ok_rows(run_resolver_body(source, "cast_shorthand", exa=exa))

    assert rows[0]["version"] == 2
    assert rows[0]["script_name"] == "PREPROC_RT.CAST_SHORTHAND_V2"


def test_large_artifact_is_chunked_in_order(tmp_path):
    """An artifact exceeding one EMITS value is emitted as ordered, reassemblable chunks."""
    big_text = default_artifact_text("big_module", 1, "big_module") + (
        "-- " + "x" * (EMITS_CHUNK_CHARS + 500)
    )
    specs = [library_module("big_module", version=1, function="big_module", artifact_text=big_text)]
    source, exa, _ = _stage_local(tmp_path, specs)

    rows = _ok_rows(run_resolver_body(source, "big_module", exa=exa))

    assert len(rows) >= 2
    assert {row["chunk_count"] for row in rows} == {len(rows)}
    ordered = sorted(rows, key=lambda r: r["chunk_index"])
    assert [r["chunk_index"] for r in ordered] == list(range(len(rows)))
    assert all(len(r["artifact_chunk"]) <= EMITS_CHUNK_CHARS for r in ordered)
    assert "".join(r["artifact_chunk"] for r in ordered) == big_text


def test_https_fetches_and_verifies(tmp_path):
    """An https source GETs the index + artifact and returns them on a sha match."""
    built = build_library_tarball(tmp_path / _TARBALL_NAME, standard_library_specs())
    index_url = "https://example.test/registry/index.json"
    artifact_url = "https://example.test/modules/cast_shorthand/cast_shorthand_v1.sql"
    artifact_bytes = default_artifact_text("cast_shorthand", 1, "cast_shorthand").encode("utf-8")
    urlopen = fake_http_directory({index_url: built.index_bytes, artifact_url: artifact_bytes})

    rows = _ok_rows(run_resolver_body(index_url, "cast_shorthand", urlopen=urlopen))

    assert rows[0]["script_name"] == "PREPROC_RT.CAST_SHORTHAND_V1"
    assert rows[0]["artifact_chunk"] == artifact_bytes.decode("utf-8")


def test_https_network_error_is_signalled(tmp_path):
    """A network failure on the https path is signalled, never raised."""

    def _boom(*_args, **_kwargs):
        raise urllib.error.URLError("Name or service not known")

    row = _error_row(
        run_resolver_body(
            "https://unreachable.invalid/registry/index.json", "cast_shorthand", urlopen=_boom
        )
    )

    assert row["error_kind"] == "network_error"
    assert "unreachable.invalid" in row["error_message"]
    assert row["artifact_chunk"] is None


def test_unsupported_scheme_is_rejected(tmp_path):
    """A source with no recognised scheme is rejected without raising."""
    row = _error_row(run_resolver_body("ftp://host/lib.tar.gz", "cast_shorthand"))
    assert row["error_kind"] == "bad_source"


def test_missing_connection_is_signalled(tmp_path):
    """A bucketfs source naming an inaccessible connection is signalled, not raised."""
    row = _error_row(
        run_resolver_body("bucketfs:NOPE/lib.tar.gz", "cast_shorthand", exa=make_fake_exa({}))
    )
    assert row["error_kind"] == "source_unreadable"
    assert row["artifact_chunk"] is None


def test_list_all_sentinel_emits_one_metadata_row_per_module(tmp_path):
    """module_name='*' lists every advertised module, metadata-only, no artifact fetch.

    PREPROC.MODULE_CATALOG drives this mode to build the DB-side registry
    listing: the resolver's single-module contract stays intact (a bare module
    name still resolves exactly as before), but '*' is an additive sentinel that
    emits one OK row per index entry with a null artifact body -- no artifact
    member is read and no sha256 is verified, even for a library-deployed entry
    that has one.
    """
    source, exa, _ = _stage_local(tmp_path, standard_library_specs())

    rows = _ok_rows(run_resolver_body(source, "*", exa=exa))

    assert len(rows) == 2
    by_name = {row["name"]: row for row in rows}
    assert set(by_name) == {"cast_shorthand", "trailing_comma"}
    for row in rows:
        assert row["artifact_chunk"] is None
        assert row["chunk_count"] == 0
        assert row["chunk_index"] == 0
    assert by_name["cast_shorthand"]["phase"] == "TRANSLATE"
    assert by_name["cast_shorthand"]["deploy_mode"] == "library-deployed"
    assert by_name["cast_shorthand"]["script_name"] == "PREPROC_RT.CAST_SHORTHAND_V1"


def test_list_all_sentinel_propagates_index_read_errors(tmp_path):
    """A source whose index cannot be read still signals a single ERROR row in list mode."""
    row = _error_row(run_resolver_body("bucketfs:NOPE/lib.tar.gz", "*", exa=make_fake_exa({})))
    assert row["error_kind"] == "source_unreadable"
    assert row["artifact_chunk"] is None
