"""Hermetic tests for the resolver UDF body (``sql/udf/module_resolver_v1.sql``).

The resolver's Python body is stdlib-only and reaches the runtime only through
the emit/iterate ``ctx`` protocol and the cluster-local BucketFS mount, so it
runs off-database against a ``ctx`` fake and a ``MOUNT_ROOT`` pointed at a
``tmp_path`` tree. These tests exercise every resolve path — the BucketFS
location grammar and its ``<service>/<bucket>/<path>`` derivation, tarball and
auto-extracted reads, https fetch, sha256 verification, self-deployed skip,
latest-version select, chunking, and the never-raise error signalling —
deterministically and with no cluster or network. The live counterparts live in
``tests/test_db_resolver.py``.
"""

from __future__ import annotations

import hashlib
import tarfile
import types
import urllib.error
from pathlib import Path

import pytest
from _dbfixtures import (
    EMITS_CHUNK_CHARS,
    RESOLVER_COLUMNS,
    RESOLVER_SQL_PATH,
    FakeEmitCtx,
    build_library_tarball,
    composite_library_module,
    composite_object_specs,
    default_artifact_text,
    fake_http_directory,
    library_module,
    resolver_python_body,
    run_resolver_body,
    self_deployed_module,
    standard_library_specs,
)

from preproc.module.manifest import ArtifactError, parse_manifest, verify_artifact_inventory
from preproc.module.registry import OBJECTS_MAX_CHARS, encode_objects

_ARTIFACT_PY_PATH = (
    Path(__file__).resolve().parent.parent / "src" / "preproc" / "module" / "artifact.py"
)
_SPLITTER_BEGIN = "# BEGIN ARTIFACT SPLITTER"
_SPLITTER_END = "# END ARTIFACT SPLITTER"

_TARBALL_NAME = "preproc-lib-0.1.0.tar.gz"
_EXTRACTED_DIR = "preproc-lib-0.1.0"

# The staged tree's service and bucket. Deliberately not interchangeable: they
# are different names, so a reader that transposed or dropped either segment
# derives a path that does not exist -- see
# test_service_and_bucket_are_not_interchangeable.
_SERVICE = "bfsdefault"
_BUCKET = "preproc-protected"


def _stage_local(tmp_path: Path, specs, *, mode="extracted", **kwargs):
    """Stage a release on a fake BucketFS mount; return (source, mount_root, built).

    The release is written at ``<mount_root>/<service>/<bucket>/`` and NOWHERE
    else -- exactly the path the resolver derives from the returned source. That
    is what keeps the seam honest: a reader that dropped a segment, transposed
    service and bucket, or took its path from anywhere but the source finds
    nothing and reports ``source_unreadable`` instead of quietly passing.

    ``mode="extracted"`` mirrors BucketFS's live behaviour — an uploaded archive
    is auto-extracted to the extension-less directory and the raw archive is not
    present on the mount. ``mode="tarball"`` leaves the raw ``.tar.gz`` file, for
    buckets that do not auto-extract.
    """
    mount_root = tmp_path / "buckets"
    bucket_root = mount_root / _SERVICE / _BUCKET
    bucket_root.mkdir(parents=True, exist_ok=True)
    tgz = bucket_root / _TARBALL_NAME
    built = build_library_tarball(tgz, specs, **kwargs)
    if mode == "extracted":
        with tarfile.open(tgz, "r:gz") as tar:
            tar.extractall(bucket_root / _EXTRACTED_DIR, filter="data")
        tgz.unlink()
    source = f"bucketfs:{_SERVICE}/{_BUCKET}/{_TARBALL_NAME}"
    return source, str(mount_root), built


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
    source, mount_root, _ = _stage_local(tmp_path, standard_library_specs())

    rows = _ok_rows(run_resolver_body(source, "cast_shorthand", mount_root=mount_root))

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
    source, mount_root, _ = _stage_local(tmp_path, standard_library_specs(), mode="tarball")

    rows = _ok_rows(run_resolver_body(source, "cast_shorthand", mount_root=mount_root))

    assert rows[0]["script_name"] == "PREPROC_RT.CAST_SHORTHAND_V1"
    assert rows[0]["artifact_chunk"] == default_artifact_text("cast_shorthand", 1, "cast_shorthand")


def test_sha256_mismatch_is_hard_failure(tmp_path):
    """A tampered artifact hashes to a different digest — no usable artifact is returned."""
    source, mount_root, _ = _stage_local(
        tmp_path, standard_library_specs(), tamper={"cast_shorthand": b"\n-- tampered\n"}
    )

    row = _error_row(run_resolver_body(source, "cast_shorthand", mount_root=mount_root))

    assert row["error_kind"] == "sha256_mismatch"
    assert row["artifact_chunk"] is None
    assert "cast_shorthand" in row["error_message"]


def test_script_name_mismatch_is_hard_failure(tmp_path):
    """An artifact whose CREATE targets a name other than the declared script_name fails.

    Structural parity with the CLI's ``manifest.verify_artifact_inventory``: the
    sha256 still matches (the index describes exactly these bytes), but the
    artifact's CREATE targets ``PREPROC_RT.IMPOSTOR_V1`` rather than the declared
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
    source, mount_root, _ = _stage_local(tmp_path, specs)

    row = _error_row(run_resolver_body(source, "cast_shorthand", mount_root=mount_root))

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
    source, mount_root, _ = _stage_local(tmp_path, specs)

    row = _error_row(run_resolver_body(source, "cast_shorthand", mount_root=mount_root))

    assert row["error_kind"] == "artifact_invalid"
    assert row["artifact_chunk"] is None
    assert "carries no statements" in row["error_message"]


def test_self_deployed_returns_metadata_only(tmp_path):
    """A self-deployed entry carries no artifact: metadata only, no fetch/verify."""
    specs = [
        self_deployed_module(
            "semantic_views", version=3, phase="EXPAND", source=("exasol/semantic_views", "v3.0.0")
        )
    ]
    source, mount_root, _ = _stage_local(tmp_path, specs)

    rows = _ok_rows(run_resolver_body(source, "semantic_views", mount_root=mount_root))

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
    source, mount_root, _ = _stage_local(
        tmp_path, standard_library_specs(), library_version="0.2.0"
    )

    rows = _ok_rows(run_resolver_body(source, "cast_shorthand", mount_root=mount_root))

    assert rows
    assert all(row["library_version"] == "0.2.0" for row in rows)


def test_library_version_absent_is_null(tmp_path):
    """An index with no library_version surfaces NULL — older indexes still resolve."""
    source, mount_root, _ = _stage_local(tmp_path, standard_library_specs())

    rows = _ok_rows(run_resolver_body(source, "cast_shorthand", mount_root=mount_root))

    assert rows
    assert all(row["library_version"] is None for row in rows)


def test_module_not_found_is_signalled(tmp_path):
    """A module absent from the index yields a not-found signal, not an artifact."""
    source, mount_root, _ = _stage_local(tmp_path, standard_library_specs())

    row = _error_row(run_resolver_body(source, "no-such-module", mount_root=mount_root))

    assert row["error_kind"] == "not_found"
    assert "no-such-module" in row["error_message"]
    assert row["artifact_chunk"] is None


def test_module_name_matched_case_insensitively(tmp_path):
    """A module name resolves regardless of case — the sugar folds an unquoted name
    to UPPER while registry keys are lower_snake_case, so matching ignores case."""
    source, mount_root, _ = _stage_local(tmp_path, standard_library_specs())

    for queried in ("cast_shorthand", "CAST_SHORTHAND", "Cast_Shorthand"):
        rows = _ok_rows(run_resolver_body(source, queried, mount_root=mount_root))
        assert rows[0]["name"] == "cast_shorthand"
        assert rows[0]["script_name"] == "PREPROC_RT.CAST_SHORTHAND_V1"


def test_latest_version_is_selected(tmp_path):
    """With versions 1 and 2 listed, the highest version wins and carries the _V2 suffix."""
    specs = [
        library_module("cast_shorthand", version=1, function="cast_shorthand"),
        library_module("cast_shorthand", version=2, function="cast_shorthand"),
    ]
    source, mount_root, _ = _stage_local(tmp_path, specs)

    rows = _ok_rows(run_resolver_body(source, "cast_shorthand", mount_root=mount_root))

    assert rows[0]["version"] == 2
    assert rows[0]["script_name"] == "PREPROC_RT.CAST_SHORTHAND_V2"


def test_large_artifact_is_chunked_in_order(tmp_path):
    """An artifact exceeding one EMITS value is emitted as ordered, reassemblable chunks."""
    big_text = default_artifact_text("big_module", 1, "big_module") + (
        "-- " + "x" * (EMITS_CHUNK_CHARS + 500)
    )
    specs = [library_module("big_module", version=1, function="big_module", artifact_text=big_text)]
    source, mount_root, _ = _stage_local(tmp_path, specs)

    rows = _ok_rows(run_resolver_body(source, "big_module", mount_root=mount_root))

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


# ---------------------------------------------------------------------------
# The BucketFS location grammar: bucketfs:<service>/<bucket>/<path>.
#
# A source names a LOCATION and nothing else -- no CONNECTION is read, and the
# mount path is derived from the source's own segments. These tests own the two
# claims that are negatives (no CONNECTION lookup, no filesystem touch before the
# parse) and therefore need instruments rather than plain assertions.
# ---------------------------------------------------------------------------


def _resolver_namespace(mount_root: str | None = None) -> dict:
    """Exec the resolver body and return its module namespace.

    For the tests whose claim is about that namespace itself -- what is bound in
    it, and what the body does with a name rebound in it. Every other test goes
    through ``run_resolver_body``, which is this plus the ``urlopen`` swap.
    """
    namespace: dict = {}
    exec(compile(resolver_python_body(), str(RESOLVER_SQL_PATH), "exec"), namespace)  # noqa: S102
    if mount_root is not None:
        namespace["MOUNT_ROOT"] = mount_root
    return namespace


def _emit_rows(namespace: dict, source: str, module_name: str) -> list[dict]:
    ctx = FakeEmitCtx(source, module_name)
    namespace["run"](ctx)
    return [dict(zip(RESOLVER_COLUMNS, row)) for row in ctx.rows]


def _reachable_names(code: types.CodeType) -> set[str]:
    """Every name the compiled body can look up, nested functions included."""
    names = set(code.co_names)
    for const in code.co_consts:
        if isinstance(const, types.CodeType):
            names |= _reachable_names(const)
    return names


class _FilesystemTouched(Exception):
    """Raised by the tripwire the instant the resolver reaches for ``os``."""


class _FilesystemTripwire:
    """An ``os`` stand-in that records every attribute touch and then refuses it.

    Stubbing ``os`` is exactly what the mount-root seam exists to avoid, with one
    exception: asserting the ABSENCE of filesystem calls cannot be done with the
    real ``os``, and absence is the whole content of "a malformed source is
    refused before any probe". So this fakes no results -- it makes any touch
    observable twice over, as a non-empty ``touches`` list and as an ``error`` row
    where a ``bad_source`` row was expected.

    ``test_filesystem_tripwire_fires_on_a_well_formed_source`` is the positive
    control: without it an empty ``touches`` list would prove nothing.
    """

    def __init__(self) -> None:
        self.touches: list[str] = []

    def __getattr__(self, name: str):
        self.touches.append(name)
        raise _FilesystemTouched(f"the resolver touched os.{name}")


def _run_with_filesystem_tripwire(
    source: str, module_name: str, *, mount_root: str
) -> tuple[list[dict], list[str]]:
    """Run the resolver body with ``os`` replaced by the tripwire; return (rows, touches)."""
    namespace = _resolver_namespace(mount_root)
    tripwire = _FilesystemTripwire()
    namespace["os"] = tripwire
    return _emit_rows(namespace, source, module_name), tripwire.touches


def _mount_root_without_bucket(tmp_path: Path) -> str:
    """A mount root where the SERVICE is present but the named bucket is not.

    The live shape of BOTH an ungranted read-protected bucket and a typo'd bucket
    name (verified on the instance): ``<root>/<service>`` is there, the bucket
    under it is not. The filesystem cannot tell those two causes apart, which is
    why a single ``source_unreadable`` covers them.
    """
    (tmp_path / "buckets" / _SERVICE).mkdir(parents=True)
    return str(tmp_path / "buckets")


def test_bucketfs_path_makes_no_connection_lookup(tmp_path):
    """A BucketFS release resolves in a namespace with no ``exa`` bound at all.

    The claim is a negative, so it is made twice from different directions.
    Statically: the compiled body cannot look up the name ``exa`` anywhere, so no
    code path -- taken or not, BucketFS or https -- can reach a CONNECTION.
    Dynamically: the resolve that succeeds below runs in a namespace where ``exa``
    is provably unbound, and a surviving lookup would surface as an ``error`` row
    through the never-raise catch-all rather than as a crash. Asserting on the
    same namespace the resolve uses is what keeps this from being a happy path
    that a re-bound ``exa`` could quietly satisfy.
    """
    assert "exa" not in _reachable_names(compile(resolver_python_body(), "<resolver>", "exec"))

    source, mount_root, _ = _stage_local(tmp_path, standard_library_specs())
    namespace = _resolver_namespace(mount_root)
    assert "exa" not in namespace

    rows = _ok_rows(_emit_rows(namespace, source, "cast_shorthand"))

    assert rows[0]["script_name"] == "PREPROC_RT.CAST_SHORTHAND_V1"


def test_service_and_bucket_are_not_interchangeable(tmp_path):
    """The first segment is the service and the second the bucket, in that order.

    The staged release exists at ``<root>/bfsdefault/preproc-protected`` only, so
    naming those same two segments the other way round must NOT find it. What this
    pins that the resolving tests cannot: that the derived path follows the
    source's segment order, so a reader that searched, globbed, or tried the other
    order as a fallback is caught -- and the message proves the path it probed was
    built from the source's segments as given.
    """
    source, mount_root, _ = _stage_local(tmp_path, standard_library_specs())
    _ok_rows(run_resolver_body(source, "cast_shorthand", mount_root=mount_root))

    transposed = f"bucketfs:{_BUCKET}/{_SERVICE}/{_TARBALL_NAME}"
    row = _error_row(run_resolver_body(transposed, "cast_shorthand", mount_root=mount_root))

    assert row["error_kind"] == "source_unreadable"
    assert f"{mount_root}/{_BUCKET}/{_SERVICE}/{_TARBALL_NAME}" in row["error_message"]


def test_filesystem_tripwire_fires_on_a_well_formed_source(tmp_path):
    """The positive control for the tripwire: a well-formed source DOES touch ``os``.

    The two ``bad_source`` tests below assert an empty touch list. That is only
    evidence if the instrument fires when a probe does happen -- otherwise a
    tripwire wired to nothing would pass them both.
    """
    _, touches = _run_with_filesystem_tripwire(
        f"bucketfs:{_SERVICE}/{_BUCKET}/{_TARBALL_NAME}",
        "cast_shorthand",
        mount_root=str(tmp_path),
    )

    assert touches, "the tripwire observed no os access on a well-formed source"


_TOO_FEW_SEGMENT_SOURCES = {
    # An all-caps first segment because that is what the superseded form actually
    # carried -- an Exasol CONNECTION name. This literal is a DELIBERATE remnant of
    # the old grammar, kept because the test asserts its rejection: a sweep for
    # stale `bucketfs:<CONNECTION>/<path>` literals should leave it alone.
    "legacy_connection_form": "bucketfs:PREPROC_BFS/preproc-lib-0.1.0.tar.gz",
    "one_segment": "bucketfs:preproc-lib-0.1.0.tar.gz",
    "scheme_only": "bucketfs:",
}


@pytest.mark.parametrize(
    "source", list(_TOO_FEW_SEGMENT_SOURCES.values()), ids=list(_TOO_FEW_SEGMENT_SOURCES)
)
def test_two_segment_source_is_bad_source(tmp_path, source):
    """A source carrying fewer than three segments is malformed, and probes nothing.

    The headline case is the superseded ``bucketfs:<CONNECTION>/<path>`` form: the
    two forms are grammatically identical, so there is no fallback to fall back to
    -- probing one and then the other would make a source's meaning depend on
    database state. The message therefore names the shape that would have worked
    and quotes the source back.

    Rejection must also precede any filesystem call, which is what the empty touch
    list asserts (see ``test_filesystem_tripwire_fires_on_a_well_formed_source``
    for why an empty list is evidence). A parse that ran interleaved with the
    probes could still emit ``bad_source`` while having already stat'd a path
    derived from an unvalidated segment.
    """
    rows, touches = _run_with_filesystem_tripwire(
        source, "cast_shorthand", mount_root=str(tmp_path)
    )

    row = _error_row(rows)
    assert row["error_kind"] == "bad_source"
    assert "bucketfs:<service>/<bucket>/<path>" in row["error_message"]
    assert source in row["error_message"]
    assert touches == []


_BAD_SEGMENT_SOURCES = {
    "empty_bucket": (f"bucketfs:{_SERVICE}//{_TARBALL_NAME}", ""),
    "empty_release_segment": (f"bucketfs:{_SERVICE}/{_BUCKET}//{_TARBALL_NAME}", ""),
    "trailing_slash": (f"bucketfs:{_SERVICE}/{_BUCKET}/{_TARBALL_NAME}/", ""),
    "leading_slash": (f"bucketfs:/buckets/{_SERVICE}/{_BUCKET}/{_TARBALL_NAME}", ""),
    "dot_as_bucket": (f"bucketfs:{_SERVICE}/./{_TARBALL_NAME}", "."),
    "dot_mid_path": (f"bucketfs:{_SERVICE}/{_BUCKET}/./{_TARBALL_NAME}", "."),
    "dotdot_escapes_bucket": (f"bucketfs:{_SERVICE}/{_BUCKET}/../other/{_TARBALL_NAME}", ".."),
    "dotdot_escapes_service": (
        f"bucketfs:{_SERVICE}/{_BUCKET}/../../other-service/b/{_TARBALL_NAME}",
        "..",
    ),
    "dotdot_as_service": (f"bucketfs:../{_SERVICE}/{_BUCKET}/{_TARBALL_NAME}", ".."),
}


@pytest.mark.parametrize(
    ("source", "offending"), list(_BAD_SEGMENT_SOURCES.values()), ids=list(_BAD_SEGMENT_SOURCES)
)
def test_traversal_segment_is_bad_source(tmp_path, source, offending):
    """An empty, ``.`` or ``..`` segment is malformed wherever it appears.

    The mount base is now built from operator-supplied text, so this is what keeps
    a source unable to address anything outside its own bucket -- and the empty
    touch list is the stronger half of that claim: nothing outside the bucket is
    merely *not opened*, it is never even reached. ``leading_slash`` is the shape
    that matters most for the regression: the deleted ``_mount_base`` accepted an
    absolute ``/buckets/…`` path verbatim, and that form must now be refused like
    any other empty first segment.
    """
    rows, touches = _run_with_filesystem_tripwire(
        source, "cast_shorthand", mount_root=str(tmp_path)
    )

    row = _error_row(rows)
    assert row["error_kind"] == "bad_source"
    assert f"segment {offending!r}" in row["error_message"]
    assert "empty or a path traversal" in row["error_message"]
    assert source in row["error_message"]
    assert touches == []


def test_unreadable_location_message_names_paths_and_grant(tmp_path):
    """A failed probe names both probed paths AND the credential prerequisite.

    One error kind carries all three elements because the filesystem cannot say
    which cause applies, which makes the message the operator's whole diagnostic
    -- so each element is asserted separately. The paths are matched WITH their
    labels on purpose: the auto-extracted directory is a prefix of the tarball
    path, so a bare substring check for it would pass on a message that named only
    the tarball.
    """
    mount_root = _mount_root_without_bucket(tmp_path)
    source = f"bucketfs:{_SERVICE}/{_BUCKET}/{_TARBALL_NAME}"

    row = _error_row(run_resolver_body(source, "cast_shorthand", mount_root=mount_root))

    assert row["error_kind"] == "source_unreadable"
    mount_base = f"{mount_root}/{_SERVICE}/{_BUCKET}"
    assert f"{mount_base}/{_TARBALL_NAME} (tarball)" in row["error_message"]
    assert f"{mount_base}/{_EXTRACTED_DIR} (auto-extracted)" in row["error_message"]
    assert (
        f"CONNECTION TO 'bucketfs:{_SERVICE}/{_BUCKET}' IDENTIFIED BY '<read-password>'"
        in row["error_message"]
    )


_UNMOUNTED_SOURCE = f"bucketfs:{_SERVICE}/nosuchbucket/lib.tar.gz"


def test_unmounted_location_is_signalled(tmp_path):
    """A well-formed location whose bucket is not mounted is signalled, not raised.

    Retargeted from the deleted ``exa.get_connection`` failure: a BucketFS source
    no longer names a CONNECTION, so the only remaining "cannot get at it" case is
    an absent mount, and the never-raise contract must still turn it into one
    structured ERROR row rather than an exception escaping the UDF.
    """
    row = _error_row(
        run_resolver_body(
            _UNMOUNTED_SOURCE, "cast_shorthand", mount_root=_mount_root_without_bucket(tmp_path)
        )
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
    source, mount_root, _ = _stage_local(tmp_path, standard_library_specs())

    rows = _ok_rows(run_resolver_body(source, "*", mount_root=mount_root))

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
    row = _error_row(
        run_resolver_body(_UNMOUNTED_SOURCE, "*", mount_root=_mount_root_without_bucket(tmp_path))
    )
    assert row["error_kind"] == "source_unreadable"
    assert row["artifact_chunk"] is None


# ---------------------------------------------------------------------------
# Embedded splitter: byte-identity + block-structure (ADR: canonical Python
# splitter embedded verbatim into the resolver UDF, CI byte-identical).
# ---------------------------------------------------------------------------


def _resolver_sql_text() -> str:
    return RESOLVER_SQL_PATH.read_text(encoding="utf-8")


def _embedded_splitter_source() -> str:
    """Extract the splitter delimited by the sentinel comments, VERBATIM."""
    text = _resolver_sql_text()
    assert _SPLITTER_BEGIN in text and _SPLITTER_END in text, (
        f"{RESOLVER_SQL_PATH} carries no splitter delimited by "
        f"{_SPLITTER_BEGIN!r} ... {_SPLITTER_END!r}"
    )
    return text.split(_SPLITTER_BEGIN + "\n", 1)[1].split(_SPLITTER_END, 1)[0]


def test_embedded_splitter_is_byte_identical_to_artifact_py():
    """The resolver's embedded splitter must never drift from artifact.py.

    There is exactly ONE canonical Python splitter implementation
    (``src/preproc/module/artifact.py``); the resolver UDF carries a verbatim
    copy between sentinel comments, and this assertion is what makes
    Python-vs-Python divergence structurally impossible rather than merely
    tested (ADR: canonical Python splitter embedded verbatim into the UDF).
    This test is the actual CI enforcement: it runs hermetically, with no
    database, on every test run.
    """
    embedded = _embedded_splitter_source()
    canonical = _ARTIFACT_PY_PATH.read_text(encoding="utf-8")

    assert embedded == canonical


def test_embedded_splitter_carries_no_lone_terminator_lines():
    """Neither the embedded splitter nor artifact.py itself may carry a lone '/' or '--/' line.

    ``install.py`` strips every line whose ``strip() == '/'`` from the resolver
    ``.sql`` before sending it to pyexasol (the EXAplus interactive terminator
    convention). If ``artifact.py`` ever gained such a line, the DEPLOYED
    resolver would be silently corrupted at deploy time -- strictly AFTER the
    byte-identity check above passes -- so byte-identity alone cannot catch
    this failure mode; it needs this separate structural assertion. A lone
    '--/' line is checked too, for the same reason on the block-OPEN side
    (though a bare '--/' is invalid Python and would already fail to compile).
    """
    embedded = _embedded_splitter_source()
    canonical = _ARTIFACT_PY_PATH.read_text(encoding="utf-8")

    for label, text in (("embedded copy", embedded), ("src/preproc/module/artifact.py", canonical)):
        offending = [
            (index, line)
            for index, line in enumerate(text.split("\n"), start=1)
            if line.strip() in ("/", "--/")
        ]
        assert not offending, f"{label} carries a lone '/' or '--/' line: {offending}"


def test_resolver_body_has_no_premature_block_terminator():
    """The resolver's whole Python body ends its EXAplus block with exactly one lone '/'.

    ``bundle.py``'s ``_strip_terminator``/``_emit_script`` (and ``install.py``'s
    stripping) only special-case the FINAL lone-'/' line of the file -- an
    earlier stray lone '/' anywhere in the body would prematurely close the
    ``--/`` ... ``/`` block release.sql wraps this UDF in, silently truncating
    the deployed script. This asserts the embed did not introduce one: across
    the ENTIRE Python body (not just the embedded region), only the file's own
    last line may be a lone '/', and no line at all may be a lone '--/'.
    """
    text = _resolver_sql_text()
    marker = ") AS\n"
    idx = text.find(marker)
    assert idx != -1, "resolver CREATE statement has no ') AS' body marker"
    body = text[idx + len(marker) :]

    lines = body.split("\n")
    # A trailing split("\n") artifact is an empty final element when the file
    # ends in "\n"; drop it so "the last line" means the last line with content.
    if lines and lines[-1] == "":
        lines = lines[:-1]

    assert lines[-1].strip() == "/", "the resolver body must end with a lone '/' terminator"
    for index, line in enumerate(lines[:-1], start=1):
        assert line.strip() != "/", f"line {index} is a premature lone '/' terminator: {line!r}"
        assert line.strip() != "--/", f"line {index} is a stray lone '--/' block-open: {line!r}"


# ---------------------------------------------------------------------------
# Composite-artifact object inventory: derive + verify + emit.
# ---------------------------------------------------------------------------


def test_composite_module_inventory_is_verified_and_emitted(tmp_path):
    """A composite module whose declared objects agree with the artifact resolves OK.

    The artifact creates one Lua entry script + two Python UDFs; the manifest
    declares exactly that inventory. The resolver splits the artifact, derives
    the same three objects, confirms they agree with the entry's declared
    ``objects``, and emits the derived inventory (file order) encoded in
    ``registry.encode_objects``'s wire format.
    """
    specs = [composite_library_module("subsystem_mod", version=1)]
    source, mount_root, _ = _stage_local(tmp_path, specs)

    rows = _ok_rows(run_resolver_body(source, "subsystem_mod", mount_root=mount_root))

    assert len(rows) == 1
    expected = composite_object_specs("subsystem_mod", 1)
    assert rows[0]["objects"] == encode_objects(expected)


def test_composite_inventory_undeclared_object_is_hard_failure(tmp_path):
    """An object the artifact creates but the entry does not declare is a hard failure."""
    specs = [composite_library_module("subsystem_mod", version=1)]
    declared = composite_object_specs("subsystem_mod", 1)[:-1]  # drop HELPER_B
    source, mount_root, _ = _stage_local(
        tmp_path, specs, objects_override={"subsystem_mod": declared}
    )

    row = _error_row(run_resolver_body(source, "subsystem_mod", mount_root=mount_root))

    assert row["error_kind"] == "artifact_invalid"
    assert row["artifact_chunk"] is None
    assert row["objects"] is None
    assert "PREPROC_RT.SUBSYSTEM_MOD_HELPER_B_V1" in row["error_message"]
    assert "does not declare it" in row["error_message"]


def test_composite_inventory_missing_declared_object_is_hard_failure(tmp_path):
    """An object the entry declares but the artifact does not create is a hard failure."""
    specs = [composite_library_module("subsystem_mod", version=1)]
    declared = [
        *composite_object_specs("subsystem_mod", 1),
        {"type": "TABLE", "name": "PREPROC_RT.SUBSYSTEM_MOD_PHANTOM_V1"},
    ]
    source, mount_root, _ = _stage_local(
        tmp_path, specs, objects_override={"subsystem_mod": declared}
    )

    row = _error_row(run_resolver_body(source, "subsystem_mod", mount_root=mount_root))

    assert row["error_kind"] == "artifact_invalid"
    assert row["artifact_chunk"] is None
    assert "PREPROC_RT.SUBSYSTEM_MOD_PHANTOM_V1" in row["error_message"]
    assert "does not create it" in row["error_message"]


def test_schema_version_1_entry_derives_single_object_inventory(tmp_path):
    """A schema_version-1 index (no `objects` key at all) still resolves unchanged.

    Its inventory is derived as the single ENTRY_OBJECT_TYPE object named by
    the entry's script_name -- exactly the pre-composite check -- and that
    single-object inventory is what gets emitted.
    """
    source, mount_root, _ = _stage_local(tmp_path, standard_library_specs(), schema_version=1)

    rows = _ok_rows(run_resolver_body(source, "cast_shorthand", mount_root=mount_root))

    assert len(rows) == 1
    assert rows[0]["script_name"] == "PREPROC_RT.CAST_SHORTHAND_V1"
    assert rows[0]["objects"] == encode_objects(
        [{"type": "LUA SCRIPT", "name": "PREPROC_RT.CAST_SHORTHAND_V1"}]
    )


def test_self_deployed_objects_column_is_null(tmp_path):
    """A self-deployed entry's objects column stays NULL: no artifact travels through here."""
    specs = [
        self_deployed_module(
            "semantic_views", version=3, phase="EXPAND", source=("exasol/semantic_views", "v3.0.0")
        )
    ]
    source, mount_root, _ = _stage_local(tmp_path, specs)

    rows = _ok_rows(run_resolver_body(source, "semantic_views", mount_root=mount_root))

    assert rows[0]["objects"] is None


def test_list_all_reports_declared_objects_including_v1_fallback(tmp_path):
    """List-all mode reports each entry's DECLARED inventory, v1 fallback included.

    No artifact is read in list-all mode, so a composite entry's objects come
    straight from the index declaration, and a schema_version-1 entry (no
    `objects` key) falls back to the single script_name object -- matching
    module/db-catalog's pre-install review contract.
    """
    specs = [*standard_library_specs(), composite_library_module("subsystem_mod", version=1)]
    source, mount_root, _ = _stage_local(tmp_path, specs, schema_version=1)

    rows = _ok_rows(run_resolver_body(source, "*", mount_root=mount_root))

    by_name = {row["name"]: row for row in rows}
    assert by_name["cast_shorthand"]["objects"] == encode_objects(
        [{"type": "LUA SCRIPT", "name": "PREPROC_RT.CAST_SHORTHAND_V1"}]
    )
    # schema_version=1 strips every declared 'objects' key, even the composite
    # entry's -- the fallback derives ONLY the single script_name object, which
    # is the honest reading of a genuine version-1 index (it predates 'objects'
    # entirely, so a v1 index could never have declared the two companions).
    assert by_name["subsystem_mod"]["objects"] == encode_objects(
        [{"type": "LUA SCRIPT", "name": "PREPROC_RT.SUBSYSTEM_MOD_V1"}]
    )


# ---------------------------------------------------------------------------
# Generational discipline and inventory width: rules manifest.py enforces for
# the CLI, which the DB path MUST enforce identically or MODULE_INSTALL accepts
# what `preproc module add` rejects on the identical bytes.
# ---------------------------------------------------------------------------

# The width of PREPROC.MODULES.objects (sql/install/22_modules_table.sql), of
# MODULE_REGISTER's CAST, and of the resolver's own `objects` EMITS column.
#
# Imported, never restated: the resolver body under test carries its own PINNED
# copy of this number (it cannot import from Python), so asserting the CANONICAL
# value appears in the resolver's own error message makes these tests double as a
# drift check on that mirror — a resolver left at the old width would fail here
# rather than silently becoming the effective limit.
_OBJECTS_MAX_CHARS = OBJECTS_MAX_CHARS


def _terminated(statement: str) -> str:
    return statement.rstrip("\n") + "\n;\n"


def _composite(*statements: str) -> str:
    """Join statements into one ``;``-separated composite artifact."""
    return "".join(_terminated(statement) for statement in statements)


def _table(name: str) -> str:
    return f"CREATE OR REPLACE TABLE {name} (k VARCHAR(64))"


def _manifest_for(name: str, version: int, objects, artifact_text: str):
    """A contract-valid manifest declaring ``objects`` for ``artifact_text``.

    Lets a resolver test assert the CLI's verdict on the very bytes it stages,
    so a parity claim is proven rather than assumed.
    """
    return parse_manifest(
        {
            "name": name,
            "description": f"{name} module.",
            "phase": "TRANSLATE",
            "script_name": f"PREPROC_RT.{name.upper()}_V{version}",
            "function": name,
            "version": version,
            "min_framework": "1.0.0",
            "deploy_mode": "library-deployed",
            "sha256": hashlib.sha256(artifact_text.encode("utf-8")).hexdigest(),
            "suggested_scope": {"type": "ROLE", "value": "PREPROC_ADMIN"},
            "objects": list(objects),
        }
    )


def test_object_without_version_suffix_is_hard_failure(tmp_path):
    """An index-declared object with no ``_V<N>`` suffix is refused, naming the rule.

    This is the failure the widened reclaim ownership boundary depends on being
    impossible. An unsuffixed name is shared across generations, so an index
    declaring ``PREPROC_RT.SHARED_STATE`` for both v1 and v2 lets a v1 reclaim
    drop an object live v2 depends on. ``manifest.py`` rejects the declaration
    at parse time, so only a hand-edited ``index.json``, a fork, or a publisher
    not using ``generate_index`` can produce one -- and it reaches the database
    through this resolver, never through the CLI.

    The declared and derived inventories AGREE here, so the both-directions
    comparison cannot catch it: the suffix rule is the only gate.
    """
    shared = "PREPROC_RT.SHARED_STATE"
    artifact = _composite(default_artifact_text("shared_mod", 1, "shared_mod"), _table(shared))
    hand_edited = [
        {"type": "LUA SCRIPT", "name": "PREPROC_RT.SHARED_MOD_V1"},
        {"type": "TABLE", "name": shared},
    ]
    # module.toml keeps a contract-valid declaration -- manifest.py would reject
    # the hand-edited one -- so only the SERIALIZED index carries the unsuffixed
    # object, exactly as `tamper` corrupts only the tarball copy of the bytes.
    specs = [
        library_module(
            "shared_mod",
            version=1,
            function="shared_mod",
            artifact_text=artifact,
            objects=(
                {"type": "LUA SCRIPT", "name": "PREPROC_RT.SHARED_MOD_V1"},
                {"type": "TABLE", "name": f"{shared}_V1"},
            ),
        )
    ]
    source, mount_root, _ = _stage_local(
        tmp_path, specs, objects_override={"shared_mod": hand_edited}
    )

    row = _error_row(run_resolver_body(source, "shared_mod", mount_root=mount_root))

    assert row["error_kind"] == "artifact_invalid"
    assert row["artifact_chunk"] is None
    assert row["objects"] is None
    assert shared in row["error_message"]
    assert "_V1" in row["error_message"]


def test_object_with_another_generations_suffix_is_hard_failure(tmp_path):
    """A declared object suffixed ``_V2`` while the module is v1 is refused.

    The other half of ``manifest.py``'s rule: carrying *a* suffix is not
    enough, it must be the module's own. A v1 entry declaring a ``_V2``
    companion would make v1's reclaim drop the v2 generation's object.
    """
    crossgen = "PREPROC_RT.CROSSGEN_MOD_STATE_V2"
    artifact = _composite(
        default_artifact_text("crossgen_mod", 1, "crossgen_mod"), _table(crossgen)
    )
    hand_edited = [
        {"type": "LUA SCRIPT", "name": "PREPROC_RT.CROSSGEN_MOD_V1"},
        {"type": "TABLE", "name": crossgen},
    ]
    specs = [
        library_module(
            "crossgen_mod",
            version=1,
            function="crossgen_mod",
            artifact_text=artifact,
            objects=(
                {"type": "LUA SCRIPT", "name": "PREPROC_RT.CROSSGEN_MOD_V1"},
                {"type": "TABLE", "name": "PREPROC_RT.CROSSGEN_MOD_STATE_V1"},
            ),
        )
    ]
    source, mount_root, _ = _stage_local(
        tmp_path, specs, objects_override={"crossgen_mod": hand_edited}
    )

    row = _error_row(run_resolver_body(source, "crossgen_mod", mount_root=mount_root))

    assert row["error_kind"] == "artifact_invalid"
    assert row["artifact_chunk"] is None
    assert crossgen in row["error_message"]
    assert "_V1" in row["error_message"]


def test_artifact_creating_an_object_twice_is_hard_failure(tmp_path):
    """An artifact creating the same object twice is refused, as the CLI refuses it.

    A single declaration plus two creating statements makes the declared and
    derived *sets* equal, so the both-directions comparison is blind to it --
    set equality has no multiplicity. ``verify_artifact_inventory`` catches it
    with an explicit duplicate check, and this asserts both verdicts on the one
    artifact so the parity requirement is proven, not assumed.
    """
    state = "PREPROC_RT.DUP_MOD_STATE_V1"
    artifact = _composite(
        default_artifact_text("dup_mod", 1, "dup_mod"), _table(state), _table(state)
    )
    declared = [
        {"type": "LUA SCRIPT", "name": "PREPROC_RT.DUP_MOD_V1"},
        {"type": "TABLE", "name": state},
    ]

    with pytest.raises(ArtifactError, match="more than once"):
        verify_artifact_inventory(_manifest_for("dup_mod", 1, declared, artifact), artifact)

    specs = [
        library_module(
            "dup_mod",
            version=1,
            function="dup_mod",
            artifact_text=artifact,
            objects=tuple(declared),
        )
    ]
    source, mount_root, _ = _stage_local(tmp_path, specs)

    row = _error_row(run_resolver_body(source, "dup_mod", mount_root=mount_root))

    assert row["error_kind"] == "artifact_invalid"
    assert row["artifact_chunk"] is None
    assert row["objects"] is None
    assert "more than once" in row["error_message"]
    assert state in row["error_message"]


def test_declaration_missing_its_name_stays_a_structured_failure(tmp_path):
    """A declaration with no ``name`` key is refused as ``artifact_invalid``, not ``error``.

    The suffix rule has to read each declared name, and a hand-edited entry may
    simply omit it. The resolver's contract is that every failure is a
    *structured* row, so this must stay in the ``artifact_invalid`` vocabulary
    rather than surfacing a Python ``AttributeError`` as the operator's
    diagnostic under the catch-all ``error`` kind.
    """
    artifact = _composite(
        default_artifact_text("noname_mod", 1, "noname_mod"),
        _table("PREPROC_RT.NONAME_MOD_STATE_V1"),
    )
    legal = (
        {"type": "LUA SCRIPT", "name": "PREPROC_RT.NONAME_MOD_V1"},
        {"type": "TABLE", "name": "PREPROC_RT.NONAME_MOD_STATE_V1"},
    )
    specs = [
        library_module(
            "noname_mod",
            version=1,
            function="noname_mod",
            artifact_text=artifact,
            objects=legal,
        )
    ]
    source, mount_root, _ = _stage_local(
        tmp_path, specs, objects_override={"noname_mod": [dict(legal[0]), {"type": "TABLE"}]}
    )

    row = _error_row(run_resolver_body(source, "noname_mod", mount_root=mount_root))

    assert row["error_kind"] == "artifact_invalid"
    assert "must declare both 'type' and 'name'" in row["error_message"]
    assert "AttributeError" not in row["error_message"]


def _inventory_over_limit(name: str, version: int) -> list[dict[str, str]]:
    """A declared inventory whose encoded wire form just exceeds the objects column.

    Grown one TABLE at a time until it does, so the fixture tracks the limit
    instead of hard-coding an object count that a column widening would rot.

    The running length is accumulated as objects are appended rather than
    re-encoding the whole list per candidate: at the current width the inventory
    runs to a few thousand objects, where re-encoding is quadratic in that count
    and would dominate this file's runtime. The single ``encode_objects`` call at
    the end keeps that arithmetic honest against the real wire format.
    """
    upper = name.upper()
    objects = [{"type": "LUA SCRIPT", "name": f"PREPROC_RT.{upper}_V{version}"}]
    encoded_length = len(encode_objects(objects))
    while encoded_length <= _OBJECTS_MAX_CHARS:
        table = {
            "type": "TABLE",
            "name": f"PREPROC_RT.{upper}_STATE_{len(objects):04d}_V{version}",
        }
        objects.append(table)
        encoded_length += len("\n") + len(table["type"]) + len("|") + len(table["name"])
    assert len(encode_objects(objects)) == encoded_length
    return objects


def _inventory_at_limit(name: str, version: int) -> list[dict[str, str]]:
    """A declared inventory whose encoded wire form is EXACTLY the column width."""
    objects = _inventory_over_limit(name, version)[:-1]
    suffix = f"_V{version}"
    padding = "X" * (_OBJECTS_MAX_CHARS - len(encode_objects(objects)))
    tail = objects[-1]
    objects[-1] = {"type": tail["type"], "name": tail["name"][: -len(suffix)] + padding + suffix}
    assert len(encode_objects(objects)) == _OBJECTS_MAX_CHARS
    return objects


def _artifact_for_inventory(name: str, version: int, objects) -> str:
    """A composite artifact creating exactly ``objects``, in declaration order."""
    statements = [default_artifact_text(name, version, name)]
    statements += [_table(obj["name"]) for obj in objects[1:]]
    return _composite(*statements)


def _inventory_spec(name: str, objects) -> list:
    return [
        library_module(
            name,
            version=1,
            function=name,
            artifact_text=_artifact_for_inventory(name, 1, objects),
            objects=tuple(objects),
        )
    ]


def test_inventory_wider_than_the_objects_column_is_hard_failure(tmp_path):
    """An inventory too wide for PREPROC.MODULES.objects is refused before any write.

    Nothing in the contract caps the object count, so a large subsystem module
    reaches an encoded inventory the provenance column cannot hold -- and this
    manifest is entirely contract-valid, so the CLI accepts it too. The
    provenance row is written LAST, after every CREATE and after RULE_ENSURE, so
    discovering the width there would leave the module live and dispatching with
    no provenance row: un-retirable and un-reclaimable, the exact inverse of "a
    partial install is inert".

    The over-width value is also invisible to this harness's own emit (a Python
    list has no column width) and would fail opaquely inside the UDF on a real
    cluster, which is why the resolver needs its own length check rather than
    Exasol's.
    """
    objects = _inventory_over_limit("wide_inventory_mod", 1)
    source, mount_root, _ = _stage_local(tmp_path, _inventory_spec("wide_inventory_mod", objects))

    row = _error_row(run_resolver_body(source, "wide_inventory_mod", mount_root=mount_root))

    assert row["error_kind"] == "artifact_invalid"
    assert row["artifact_chunk"] is None
    assert row["objects"] is None
    assert str(_OBJECTS_MAX_CHARS) in row["error_message"]
    assert str(len(encode_objects(objects))) in row["error_message"]


def test_inventory_exactly_at_the_column_width_resolves(tmp_path):
    """An inventory encoding to exactly the column width is legal and resolves.

    Pins the comparison as "over the limit", never "at the limit": an
    off-by-one would refuse a maximal but perfectly recordable inventory.
    """
    objects = _inventory_at_limit("edge_inventory_mod", 1)
    source, mount_root, _ = _stage_local(tmp_path, _inventory_spec("edge_inventory_mod", objects))

    rows = _ok_rows(run_resolver_body(source, "edge_inventory_mod", mount_root=mount_root))

    assert rows[0]["objects"] == encode_objects(objects)
    assert len(rows[0]["objects"]) == _OBJECTS_MAX_CHARS


def test_list_all_over_wide_inventory_emits_only_the_error_row(tmp_path):
    """One over-wide entry fails list-all as a single ERROR row, leaking no OK rows.

    The catalog's ``objects`` column has the same width, so an over-wide entry
    cannot be listed at all. Every entry is therefore encoded BEFORE the first
    row is emitted, which keeps the documented "a failure is a single ERROR row"
    shape rather than emitting OK rows for whichever entries sorted ahead of the
    offender (``wide_inventory_mod`` sorts LAST of the three staged here, so a
    per-entry check would emit two OK rows first).
    """
    objects = _inventory_over_limit("wide_inventory_mod", 1)
    specs = [*standard_library_specs(), *_inventory_spec("wide_inventory_mod", objects)]
    source, mount_root, _ = _stage_local(tmp_path, specs)

    row = _error_row(run_resolver_body(source, "*", mount_root=mount_root))

    assert row["error_kind"] == "artifact_invalid"
    assert str(_OBJECTS_MAX_CHARS) in row["error_message"]
