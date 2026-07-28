"""Tests for the module integration contract.

Covers ``module.toml`` parsing/validation and artifact structural/integrity
checks (``src/preproc/module/manifest.py``) as unit tests that run fully
offline — no database required, only fixture manifest dicts and fixture
artifact text.

The two ``@pytest.mark.integration`` tests deploy a minimal conformant module
and probe its Lua entry function through a thin harness script (see
tests/test_ergonomics_cast.py for the originating harness pattern); they skip
cleanly when no Exasol instance is configured (tests/conftest.py).
"""

import hashlib

import pytest
from _corpus import load_split_corpus, load_split_error_cases

from preproc.module.artifact import (
    ArtifactSyntaxError,
    derive_objects,
    normalize_object_name,
    split_statements,
)
from preproc.module.manifest import (
    ArtifactError,
    ManifestError,
    ObjectSpec,
    load_manifest,
    parse_manifest,
    verify_artifact_inventory,
    verify_artifact_sha256,
)

# PREPROC.MODULE_RESOLVER_V1 emits the artifact in VARCHAR(2000000) chunks
# (sql/udf/module_resolver_v1.sql CHUNK_CHARS); the corpus must straddle one.
_RESOLVER_CHUNK_CHARS = 2_000_000

_ARTIFACT_TEMPLATE = (
    "CREATE OR REPLACE LUA SCRIPT {script_name}() AS\n"
    "function {function}(sqltext)\n"
    "    return sqltext\n"
    "end\n"
)


def _artifact_text(
    script_name: str = "PREPROC_RT.CAST_SHORTHAND_V1",
    function: str = "cast_shorthand",
) -> str:
    return _ARTIFACT_TEMPLATE.format(script_name=script_name, function=function)


def _sha256_of(artifact_text: str) -> str:
    return hashlib.sha256(artifact_text.encode("utf-8")).hexdigest()


def _manifest_data(**overrides) -> dict:
    data = {
        "name": "cast_shorthand",
        "description": "Rewrites expr::type cast shorthand to CAST(expr AS type).",
        "phase": "TRANSLATE",
        "script_name": "PREPROC_RT.CAST_SHORTHAND_V1",
        "function": "cast_shorthand",
        "version": 1,
        "min_framework": "1.0.0",
        "suggested_scope": {"type": "ROLE", "value": "PREPROC_ADMIN"},
        "deploy_mode": "library-deployed",
        "sha256": hashlib.sha256(_artifact_text().encode("utf-8")).hexdigest(),
    }
    data.update(overrides)
    return data


# ---------------------------------------------------------------------------
# Manifest validation
# ---------------------------------------------------------------------------


def test_manifest_minimal_library_valid():
    """A minimal library-deployed manifest with a valid sha256 is accepted."""
    manifest = parse_manifest(_manifest_data())
    assert manifest.name == "cast_shorthand"
    assert manifest.phase == "TRANSLATE"
    assert manifest.deploy_mode == "library-deployed"
    assert manifest.version == 1
    assert manifest.suggested_scope.type == "ROLE"
    assert manifest.suggested_scope.value == "PREPROC_ADMIN"


def test_manifest_self_deployed_no_sha_valid():
    """A self-deployed manifest with source but no sha256 is accepted."""
    data = _manifest_data(
        deploy_mode="self-deployed",
        sha256=None,
        source={"repo": "exasol/semantic_views", "ref": "v3.0.0"},
    )
    manifest = parse_manifest(data)
    assert manifest.deploy_mode == "self-deployed"
    assert manifest.sha256 is None
    assert manifest.source.repo == "exasol/semantic_views"
    assert manifest.source.ref == "v3.0.0"


def test_manifest_library_missing_sha_rejected():
    """A library-deployed manifest without sha256 is rejected, naming the field."""
    data = _manifest_data(sha256=None)
    with pytest.raises(ManifestError) as exc_info:
        parse_manifest(data)
    assert any("sha256" in message for message in exc_info.value.errors)


def test_manifest_version_suffix_mismatch_rejected():
    """A manifest whose version disagrees with the script_name _V<N> suffix is rejected."""
    data = _manifest_data(version=2)
    with pytest.raises(ManifestError) as exc_info:
        parse_manifest(data)
    assert any("version" in message and "_V1" in message for message in exc_info.value.errors)


def test_manifest_unknown_phase_rejected():
    """A manifest with an unrecognised phase is rejected, naming the allowed set."""
    data = _manifest_data(phase="OPTIMIZE")
    with pytest.raises(ManifestError) as exc_info:
        parse_manifest(data)
    assert any(
        "TRANSLATE" in message and "EXPAND" in message and "REWRITE" in message
        for message in exc_info.value.errors
    )


def test_manifest_hyphenated_name_rejected():
    """A module name with a hyphen is rejected: names must be unquoted-identifier safe.

    An unquoted Exasol identifier cannot contain a hyphen, so a hyphenated name
    could not be used in the `PREPROC INSTALL MODULE <name>` sugar; the contract
    requires underscores instead (e.g. cast_shorthand, not cast-shorthand).
    """
    data = _manifest_data(name="cast-shorthand")
    with pytest.raises(ManifestError) as exc_info:
        parse_manifest(data)
    assert any("hyphen" in message or "identifier" in message for message in exc_info.value.errors)


def test_manifest_load_from_file(tmp_path):
    """load_manifest reads and validates a module.toml file from disk."""
    sha256 = _manifest_data()["sha256"]
    toml_text = (
        'name = "cast_shorthand"\n'
        'description = "desc"\n'
        'phase = "TRANSLATE"\n'
        'script_name = "PREPROC_RT.CAST_SHORTHAND_V1"\n'
        'function = "cast_shorthand"\n'
        "version = 1\n"
        'min_framework = "1.0.0"\n'
        'deploy_mode = "library-deployed"\n'
        f'sha256 = "{sha256}"\n'
        "\n"
        "[suggested_scope]\n"
        'type = "ROLE"\n'
        'value = "PREPROC_ADMIN"\n'
    )
    manifest_path = tmp_path / "module.toml"
    manifest_path.write_text(toml_text, encoding="utf-8")
    manifest = load_manifest(manifest_path)
    assert manifest.name == "cast_shorthand"
    assert manifest.sha256 == sha256


# ---------------------------------------------------------------------------
# Artifact inventory verification: declared vs parsed
# ---------------------------------------------------------------------------

_LUA_ENTRY_SQL = (
    "--/\n"
    "CREATE OR REPLACE LUA SCRIPT PREPROC_RT.CONTROL_DATABASE_V1 AS\n"
    "function control_database(sqltext)\n"
    "    return sqltext;\n"
    "end\n"
    "/\n"
)
_PYTHON_UDF_SQL = (
    "--/\n"
    "CREATE OR REPLACE PYTHON3 SCALAR SCRIPT PREPROC_RT.CONFD_JOB_V1(p VARCHAR(100))\n"
    "RETURNS VARCHAR(100) AS\n"
    "def run(ctx):\n"
    "    return ctx.p;\n"
    "/\n"
)
_SECOND_PYTHON_UDF_SQL = (
    "--/\n"
    "CREATE OR REPLACE PYTHON3 SET SCRIPT PREPROC_RT.CONFD_REDUCE_V1(p VARCHAR(100))\n"
    "EMITS (q VARCHAR(100)) AS\n"
    "def run(ctx):\n"
    "    ctx.emit(ctx.p);\n"
    "/\n"
)
_COMPOSITE_ARTIFACT = _LUA_ENTRY_SQL + "\n" + _PYTHON_UDF_SQL

_ENTRY_OBJECT = {"type": "LUA SCRIPT", "name": "PREPROC_RT.CONTROL_DATABASE_V1"}
_UDF_OBJECT = {"type": "PYTHON3 SCALAR SCRIPT", "name": "PREPROC_RT.CONFD_JOB_V1"}


def _composite_manifest_data(artifact_text: str = _COMPOSITE_ARTIFACT, **overrides) -> dict:
    data = _manifest_data(
        name="control_database",
        description="Front door to the confd subsystem.",
        phase="EXPAND",
        script_name="PREPROC_RT.CONTROL_DATABASE_V1",
        function="control_database",
        sha256=hashlib.sha256(artifact_text.encode("utf-8")).hexdigest(),
        objects=[dict(_ENTRY_OBJECT), dict(_UDF_OBJECT)],
    )
    data.update(overrides)
    return data


def test_artifact_accepts_one_or_more_statements():
    """One statement and many statements are both accepted; the inventory follows file order."""
    single = parse_manifest(_manifest_data())
    assert verify_artifact_inventory(single, _artifact_text()) == [
        ObjectSpec(type="LUA SCRIPT", name="PREPROC_RT.CAST_SHORTHAND_V1")
    ]
    composite = parse_manifest(_composite_manifest_data())
    assert len(verify_artifact_inventory(composite, _COMPOSITE_ARTIFACT)) == 2


def test_composite_artifact_with_companions_is_valid():
    """A library-deployed composite with a Lua entry script and a Python UDF companion is valid."""
    manifest = parse_manifest(_composite_manifest_data())
    assert manifest.deploy_mode == "library-deployed"
    assert verify_artifact_inventory(manifest, _COMPOSITE_ARTIFACT) == [
        ObjectSpec(type="LUA SCRIPT", name="PREPROC_RT.CONTROL_DATABASE_V1"),
        ObjectSpec(type="PYTHON3 SCALAR SCRIPT", name="PREPROC_RT.CONFD_JOB_V1"),
    ]


def test_inventory_disagreement_rejected_both_directions():
    """An object in the artifact but not the manifest, and the reverse, are both rejected."""
    extra = _COMPOSITE_ARTIFACT + "\nCREATE TABLE PREPROC_RT.CONFD_LOG_V1 (c INT);\n"
    undeclared = parse_manifest(_composite_manifest_data(artifact_text=extra))
    with pytest.raises(ArtifactError) as artifact_side:
        verify_artifact_inventory(undeclared, extra)
    assert "PREPROC_RT.CONFD_LOG_V1" in str(artifact_side.value)

    overdeclared = parse_manifest(
        _composite_manifest_data(
            objects=[
                dict(_ENTRY_OBJECT),
                dict(_UDF_OBJECT),
                {"type": "TABLE", "name": "PREPROC_RT.ABSENT_V1"},
            ]
        )
    )
    with pytest.raises(ArtifactError) as manifest_side:
        verify_artifact_inventory(overdeclared, _COMPOSITE_ARTIFACT)
    assert "PREPROC_RT.ABSENT_V1" in str(manifest_side.value)


def test_undeclared_python_udfs_rejected_statically():
    """1 Lua + 2 undeclared Python UDFs fails on the CLI, naming both, with no DB connection.

    This is the latent bug the composite contract closes: the pre-delta
    Lua-only regex accepted this artifact and left it to fail at DB install.
    """
    text = _LUA_ENTRY_SQL + "\n" + _PYTHON_UDF_SQL + "\n" + _SECOND_PYTHON_UDF_SQL
    manifest = parse_manifest(
        _composite_manifest_data(artifact_text=text, objects=[dict(_ENTRY_OBJECT)])
    )
    with pytest.raises(ArtifactError) as exc_info:
        verify_artifact_inventory(manifest, text)
    message = str(exc_info.value)
    assert "PREPROC_RT.CONFD_JOB_V1" in message
    assert "PREPROC_RT.CONFD_REDUCE_V1" in message


def test_object_without_version_suffix_rejected():
    """A declared object missing the module's _V<N> suffix is rejected, naming the rule."""
    data = _composite_manifest_data(
        objects=[
            dict(_ENTRY_OBJECT),
            {"type": "PYTHON3 SCALAR SCRIPT", "name": "PREPROC_RT.CONFD_JOB"},
        ]
    )
    with pytest.raises(ManifestError) as exc_info:
        parse_manifest(data)
    assert any(
        "PREPROC_RT.CONFD_JOB" in message and "_V1" in message for message in exc_info.value.errors
    )


@pytest.mark.parametrize(
    ("bad_type", "bad_name"),
    [
        ("PYTHON3\nSCALAR SCRIPT", "PREPROC_RT.CONFD_JOB_V1"),
        ("PYTHON3 SCALAR SCRIPT", "PREPROC_RT.CONFD\rJOB_V1"),
        ("PYTHON3 SCALAR SCRIPT", 'PREPROC_RT."CONFD|JOB_V1"'),
    ],
)
def test_object_with_delimiter_character_rejected(bad_type, bad_name):
    """A declared object type/name containing '\\n', '\\r', or '|' is rejected at parse time.

    These are exactly the characters the PREPROC.MODULES.objects column's
    'TYPE|NAME' line format uses as delimiters; validation (not escaping) is
    what keeps them from ever reaching the encoded column.
    """
    data = _composite_manifest_data(
        objects=[dict(_ENTRY_OBJECT), {"type": bad_type, "name": bad_name}]
    )
    with pytest.raises(ManifestError) as exc_info:
        parse_manifest(data)
    message = "; ".join(exc_info.value.errors)
    assert repr(bad_name) in message
    assert any(word in message for word in ("newline", "carriage return", "pipe"))


def test_single_statement_without_objects_array_valid():
    """cast_shorthand's manifest, with no [[objects]], stays valid and needs no edit."""
    manifest = parse_manifest(_manifest_data())
    assert manifest.objects is None
    assert verify_artifact_inventory(manifest, _artifact_text()) == [
        ObjectSpec(type="LUA SCRIPT", name="PREPROC_RT.CAST_SHORTHAND_V1")
    ]


def test_multi_statement_without_objects_array_rejected():
    """Omitting [[objects]] is only allowed at one statement; two statements require it."""
    manifest = parse_manifest(
        _composite_manifest_data(objects=None, sha256=_sha256_of(_COMPOSITE_ARTIFACT))
    )
    with pytest.raises(ArtifactError) as exc_info:
        verify_artifact_inventory(manifest, _COMPOSITE_ARTIFACT)
    message = str(exc_info.value)
    assert "objects" in message
    assert "REQUIRED" in message


def test_objects_outside_preproc_rt_accepted():
    """A declared object in a schema other than PREPROC_RT or PREPROC is accepted."""
    text = _LUA_ENTRY_SQL + "\nCREATE TABLE MY_SUBSYSTEM.JOB_LOG_V1 (c INT);\n"
    manifest = parse_manifest(
        _composite_manifest_data(
            artifact_text=text,
            objects=[dict(_ENTRY_OBJECT), {"type": "TABLE", "name": "MY_SUBSYSTEM.JOB_LOG_V1"}],
        )
    )
    assert verify_artifact_inventory(manifest, text) == [
        ObjectSpec(type="LUA SCRIPT", name="PREPROC_RT.CONTROL_DATABASE_V1"),
        ObjectSpec(type="TABLE", name="MY_SUBSYSTEM.JOB_LOG_V1"),
    ]


def test_artifact_without_the_entry_script_rejected():
    """An artifact that creates no LUA SCRIPT for the manifest script_name is rejected."""
    manifest = parse_manifest(_manifest_data())
    text = _artifact_text(script_name="PREPROC_RT.WRONG_NAME_V1")
    with pytest.raises(ArtifactError) as exc_info:
        verify_artifact_inventory(manifest, text)
    assert "PREPROC_RT.CAST_SHORTHAND_V1" in str(exc_info.value)


def test_artifact_creating_the_same_object_twice_rejected():
    """A duplicate object in the artifact is rejected: an inventory must be an exact undo list."""
    duplicate = _LUA_ENTRY_SQL + "\n" + _LUA_ENTRY_SQL
    manifest = parse_manifest(
        _composite_manifest_data(artifact_text=duplicate, objects=[dict(_ENTRY_OBJECT)])
    )
    with pytest.raises(ArtifactError) as exc_info:
        verify_artifact_inventory(manifest, duplicate)
    assert "PREPROC_RT.CONTROL_DATABASE_V1" in str(exc_info.value)


def test_duplicate_declared_object_rejected():
    """The same object declared twice in [[objects]] is rejected by manifest parsing."""
    data = _composite_manifest_data(objects=[dict(_ENTRY_OBJECT), dict(_ENTRY_OBJECT)])
    with pytest.raises(ManifestError) as exc_info:
        parse_manifest(data)
    assert any("PREPROC_RT.CONTROL_DATABASE_V1" in message for message in exc_info.value.errors)


def test_quoted_schema_containing_a_dot_rejected():
    """A dot inside a quoted SCHEMA part is rejected at parse time, not just inside the object part.

    ``qualified_parts`` joins parts with '.' after stripping quotes, so
    ``"MY.SCHEMA".FOO_V1`` canonicalises to ``MY.SCHEMA.FOO_V1`` — two parts, the
    object part carrying the version suffix, which the rest of ``_validate_objects``
    would otherwise accept. But ``deploy.py`` and ``module_reclaim.lua`` both
    re-split a canonical name on its FIRST dot only, which would then misparse this
    as schema ``MY`` / object ``SCHEMA.FOO_V1`` — silently probing and dropping the
    wrong object. The dot is safe in the object part (the tail absorbs it); it is
    not safe in the schema part, so only the schema part is rejected.
    """
    data = _composite_manifest_data(
        objects=[dict(_ENTRY_OBJECT), {"type": "TABLE", "name": '"MY.SCHEMA".FOO_V1'}]
    )
    with pytest.raises(ManifestError) as exc_info:
        parse_manifest(data)
    message = "; ".join(exc_info.value.errors)
    assert "MY.SCHEMA" in message


# ---------------------------------------------------------------------------
# Statement splitting: the canonical artifact grammar
# ---------------------------------------------------------------------------

_BLOCK_LUA = (
    "--/\n"
    "CREATE OR REPLACE LUA SCRIPT PREPROC_RT.CAST_SHORTHAND_V1 AS\n"
    "function cast_shorthand(sqltext)\n"
    "    local parts = {};\n"
    "    return sqltext;\n"
    "end\n"
    "/\n"
)
_BLOCK_LUA_STATEMENT = (
    "CREATE OR REPLACE LUA SCRIPT PREPROC_RT.CAST_SHORTHAND_V1 AS\n"
    "function cast_shorthand(sqltext)\n"
    "    local parts = {};\n"
    "    return sqltext;\n"
    "end"
)
_TABLE_SQL = "CREATE TABLE PREPROC_RT.CAST_CACHE_V1 (\n    cache_key VARCHAR(64)\n);\n"
_TABLE_STATEMENT = "CREATE TABLE PREPROC_RT.CAST_CACHE_V1 (\n    cache_key VARCHAR(64)\n)"


def test_splitter_separates_block_and_semicolon_statements():
    """A block-wrapped statement and a `;`-terminated one yield exactly two statements.

    The block-wrapped body keeps its semicolons unsplit and neither marker line
    survives into the yielded text (spec: module/contract, block-marker grammar).
    """
    statements = split_statements(_BLOCK_LUA + "\n" + _TABLE_SQL)
    assert statements == [_BLOCK_LUA_STATEMENT, _TABLE_STATEMENT]


def test_splitter_keeps_semicolons_inside_strings_and_comments():
    """A `;` inside a string literal, a line comment, or a block comment does not split."""
    text = (
        "-- a comment; with a semicolon\n"
        "/* and a block\n"
        "   comment; too */\n"
        "INSERT INTO PREPROC_RT.T_V1 (c) VALUES ('a;b', 'it''s; fine');\n"
        'CREATE TABLE PREPROC_RT."odd;name_V1" (c INT);\n'
    )
    statements = split_statements(text)
    assert len(statements) == 2
    assert statements[0].endswith("VALUES ('a;b', 'it''s; fine')")
    assert statements[1] == 'CREATE TABLE PREPROC_RT."odd;name_V1" (c INT)'


def test_splitter_strips_lone_slash_and_terminates_unwrapped_statement():
    """A lone `/` terminates a pending statement and is never part of the text."""
    text = "CREATE OR REPLACE LUA SCRIPT PREPROC_RT.M_V1 AS\nfunction m(t) return t end\n/\n\n/\n"
    statements = split_statements(text)
    assert statements == [
        "CREATE OR REPLACE LUA SCRIPT PREPROC_RT.M_V1 AS\nfunction m(t) return t end"
    ]


def test_splitter_normalizes_crlf_and_marker_trailing_whitespace():
    """CRLF line endings and trailing whitespace on marker lines are both tolerated."""
    text = (
        "--/  \r\n"
        "CREATE OR REPLACE LUA SCRIPT PREPROC_RT.M_V1 AS\r\n"
        "function m(t) return t; end\r\n"
        "  /  \r\n"
    )
    assert split_statements(text) == [
        "CREATE OR REPLACE LUA SCRIPT PREPROC_RT.M_V1 AS\nfunction m(t) return t; end"
    ]


def test_splitter_preserves_a_block_body_verbatim():
    """Only the marker lines are removed; a block body's own trailing `;` is kept.

    A block body is never scanned for terminators, so a statement whose syntax
    requires a trailing semicolon (``CREATE FUNCTION … END;``) survives intact.
    """
    text = "--/\nCREATE TABLE PREPROC_RT.T_V1 (c INT);\n/\n"
    assert split_statements(text) == ["CREATE TABLE PREPROC_RT.T_V1 (c INT);"]


def test_splitter_yields_nothing_for_a_blank_artifact():
    """Whitespace and comments alone carry no statement."""
    assert split_statements("\n\n   \n-- nothing here\n") == []


def test_splitter_rejects_an_unterminated_block():
    """A `--/` block that is never closed by a lone `/` is a syntax error."""
    with pytest.raises(ArtifactSyntaxError) as exc_info:
        split_statements("--/\nCREATE TABLE PREPROC_RT.T_V1 (c INT);\n")
    assert "--/" in str(exc_info.value)


def test_splitter_rejects_a_block_marker_inside_an_unterminated_statement():
    """A `--/` opener while a `;`-mode statement is still pending is a syntax error."""
    text = "CREATE TABLE PREPROC_RT.T_V1 (c INT)\n--/\nCREATE TABLE PREPROC_RT.B_V1 (c INT)\n/\n"
    with pytest.raises(ArtifactSyntaxError):
        split_statements(text)


# ---------------------------------------------------------------------------
# The shared conformance corpus, driven through the canonical Python splitter
# ---------------------------------------------------------------------------
#
# The same corpus drives the in-database Lua splitter (see
# tests/test_db_module_install.py), which is what pins the two implementations
# to one grammar.


@pytest.mark.parametrize("case", load_split_corpus(), ids=lambda case: case.name)
def test_python_splitter_matches_conformance_corpus(case):
    """The canonical Python splitter yields the corpus's expected split for every case."""
    assert split_statements(case.text) == case.statements


@pytest.mark.parametrize("case", load_split_error_cases(), ids=lambda case: case.name)
def test_python_splitter_rejects_conformance_error_cases(case):
    """The canonical Python splitter rejects every artifact text the corpus marks invalid."""
    with pytest.raises(ArtifactSyntaxError):
        split_statements(case.text)


def test_conformance_corpus_covers_a_chunk_boundary_statement():
    """One corpus case must exceed the resolver's 2,000,000-character chunk size.

    The in-database path reassembles chunked artifact text before splitting; a
    corpus case longer than one chunk is what proves the split happens after
    reassembly rather than per chunk.
    """
    longest = max(len(case.text) for case in load_split_corpus())
    assert longest > _RESOLVER_CHUNK_CHARS


# ---------------------------------------------------------------------------
# Object derivation: every DDL form the contract admits
# ---------------------------------------------------------------------------


def test_derive_objects_recognises_every_admitted_ddl_form():
    """Scripts of any language and mode, tables, views, schemas, and connections."""
    statements = [
        "CREATE OR REPLACE LUA SCRIPT PREPROC_RT.ENTRY_V1 AS\nfunction f() end",
        "CREATE OR REPLACE PYTHON3 SCALAR SCRIPT PREPROC_RT.SCALAR_V1(a INT) RETURNS INT AS\npass",
        "CREATE OR REPLACE PYTHON3 SET SCRIPT PREPROC_RT.SETTER_V1(a INT) EMITS (b INT) AS\npass",
        "CREATE OR REPLACE JAVA SCALAR SCRIPT PREPROC_RT.JAVA_V1(a INT) RETURNS INT AS\n{}",
        "CREATE OR REPLACE R SET SCRIPT PREPROC_RT.RSET_V1(a INT) EMITS (b INT) AS\nNULL",
        "CREATE OR REPLACE SCRIPT PREPROC_RT.DEFAULTLANG_V1 AS\npass",
        "CREATE TABLE IF NOT EXISTS PREPROC_RT.T_V1 (c INT)",
        "CREATE OR REPLACE VIEW PREPROC_RT.V_V1 AS SELECT 1",
        "CREATE OR REPLACE FORCE VIEW PREPROC_RT.FV_V1 AS SELECT 1",
        "CREATE SCHEMA IF NOT EXISTS MYMOD_V1",
        "CREATE OR REPLACE CONNECTION MYMOD_CONN_V1 TO 'x'",
        "CREATE OR REPLACE FUNCTION PREPROC_RT.FN_V1() RETURN INT IS BEGIN RETURN 1; END",
    ]
    assert derive_objects(statements) == [
        {"type": "LUA SCRIPT", "name": "PREPROC_RT.ENTRY_V1"},
        {"type": "PYTHON3 SCALAR SCRIPT", "name": "PREPROC_RT.SCALAR_V1"},
        {"type": "PYTHON3 SET SCRIPT", "name": "PREPROC_RT.SETTER_V1"},
        {"type": "JAVA SCALAR SCRIPT", "name": "PREPROC_RT.JAVA_V1"},
        {"type": "R SET SCRIPT", "name": "PREPROC_RT.RSET_V1"},
        {"type": "SCRIPT", "name": "PREPROC_RT.DEFAULTLANG_V1"},
        {"type": "TABLE", "name": "PREPROC_RT.T_V1"},
        {"type": "VIEW", "name": "PREPROC_RT.V_V1"},
        {"type": "VIEW", "name": "PREPROC_RT.FV_V1"},
        {"type": "SCHEMA", "name": "MYMOD_V1"},
        {"type": "CONNECTION", "name": "MYMOD_CONN_V1"},
        {"type": "FUNCTION", "name": "PREPROC_RT.FN_V1"},
    ]


def test_derive_objects_skips_statements_that_create_nothing():
    """A GRANT or an INSERT contributes no inventory entry."""
    statements = [
        "CREATE TABLE PREPROC_RT.T_V1 (c INT)",
        "INSERT INTO PREPROC_RT.T_V1 (c) VALUES (1)",
        "GRANT SELECT ON PREPROC_RT.T_V1 TO PUBLIC",
    ]
    assert derive_objects(statements) == [{"type": "TABLE", "name": "PREPROC_RT.T_V1"}]


def test_derive_objects_ignores_leading_comments():
    """Comments preceding the CREATE inside a statement do not hide the object."""
    statement = (
        "-- the entry script\n"
        "/* still a comment */\n"
        "CREATE OR REPLACE LUA SCRIPT PREPROC_RT.E_V1 AS\n"
        "function e(t) return t end"
    )
    assert derive_objects([statement]) == [{"type": "LUA SCRIPT", "name": "PREPROC_RT.E_V1"}]


def test_derive_objects_rejects_an_unrecognised_create():
    """A CREATE the contract does not admit fails loudly rather than deriving nothing."""
    with pytest.raises(ArtifactSyntaxError) as exc_info:
        derive_objects(["CREATE ROLE SOME_ROLE_V1"])
    assert "CREATE ROLE SOME_ROLE_V1" in str(exc_info.value)


def test_derive_objects_requires_a_schema_qualified_name():
    """An unqualified object name is rejected: the inventory must not depend on OPEN SCHEMA."""
    with pytest.raises(ArtifactSyntaxError) as exc_info:
        derive_objects(["CREATE TABLE T_V1 (c INT)"])
    assert "qualified" in str(exc_info.value)


def test_normalize_object_name_folds_case_and_honours_quoting():
    """Unquoted identifier parts fold to upper case; quoted parts keep their case."""
    assert normalize_object_name("preproc_rt.cast_shorthand_v1") == "PREPROC_RT.CAST_SHORTHAND_V1"
    assert normalize_object_name('"MixedCase".t_v1') == "MixedCase.T_V1"


# ---------------------------------------------------------------------------
# Artifact integrity (sha256)
# ---------------------------------------------------------------------------


def test_artifact_sha_mismatch_rejected():
    """A library-deployed artifact whose bytes do not hash to the manifest sha256 is rejected."""
    manifest = parse_manifest(_manifest_data())
    tampered = (_artifact_text() + "\n-- tampered\n").encode("utf-8")
    with pytest.raises(ArtifactError):
        verify_artifact_sha256(manifest, tampered)


def test_sha256_mismatch_rejected_multi_statement():
    """The digest covers the whole file, so a tampered composite artifact is rejected too.

    The declared sha256 stays a digest over the whole file regardless of how
    many statements it carries, and a mismatch deploys no statement of it.
    """
    manifest = parse_manifest(_composite_manifest_data())
    assert manifest.sha256 == _sha256_of(_COMPOSITE_ARTIFACT)
    tampered = (_COMPOSITE_ARTIFACT + "\nCREATE TABLE PREPROC_RT.SNEAKY_V1 (c INT);\n").encode(
        "utf-8"
    )
    with pytest.raises(ArtifactError):
        verify_artifact_sha256(manifest, tampered)


def test_artifact_sha_match_accepted():
    """A library-deployed artifact whose bytes match the manifest sha256 verifies cleanly."""
    manifest = parse_manifest(_manifest_data())
    verify_artifact_sha256(manifest, _artifact_text().encode("utf-8"))  # does not raise


def test_artifact_sha_verification_noop_for_self_deployed():
    """sha256 verification is a no-op when the manifest carries no sha256 (self-deployed)."""
    data = _manifest_data(
        deploy_mode="self-deployed",
        sha256=None,
        source={"repo": "exasol/semantic_views", "ref": "v3.0.0"},
    )
    manifest = parse_manifest(data)
    verify_artifact_sha256(manifest, b"anything at all")  # does not raise


# ---------------------------------------------------------------------------
# Integration: conformant module behaviour through a live database
# ---------------------------------------------------------------------------
#
# The exit() table-constructor cannot carry a bare Lua `nil` as a value in a
# fixed-arity row and be relied upon to serialise as SQL NULL across engine
# versions, so the harnesses below report "did the module decline?" via an
# explicit boolean column rather than passing a raw nil through exit().

_EXPAND_MODULE_SCRIPT = "PREPROC_RT.TEST_CONTRACT_EXPAND_V1"
_EXPAND_HARNESS_SCRIPT = "PREPROC_RT.TEST_CONTRACT_EXPAND_HARNESS"
_TRANSLATE_MODULE_SCRIPT = "PREPROC_RT.TEST_CONTRACT_TRANSLATE_V1"
_TRANSLATE_HARNESS_SCRIPT = "PREPROC_RT.TEST_CONTRACT_TRANSLATE_HARNESS"


def _sql_escape(text: str) -> str:
    return text.replace("'", "''")


@pytest.fixture
def expand_contract_harness(installed):
    """Deploy a minimal conformant EXPAND module and a thin probe harness."""
    conn = installed
    module_body = (
        "function expand(sqltext)\n"
        "    if sqltext == 'CONTRACT_PING' then\n"
        "        return \"SELECT 'PONG'\"\n"
        "    end\n"
        "    return nil\n"
        "end\n"
    )
    conn.execute(f"CREATE OR REPLACE LUA SCRIPT {_EXPAND_MODULE_SCRIPT}() AS\n{module_body}")
    harness_body = (
        f"import('{_EXPAND_MODULE_SCRIPT}', 'm')\n"
        "local result = m.expand(intext)\n"
        "local matched = false\n"
        "local result_text = ''\n"
        "if result ~= nil then\n"
        "    matched = true\n"
        "    result_text = result\n"
        "end\n"
        'exit({{matched, result_text}}, "did_match BOOLEAN, result_text VARCHAR(2000000)")\n'
    )
    conn.execute(
        f"CREATE OR REPLACE LUA SCRIPT {_EXPAND_HARNESS_SCRIPT}(intext) "
        f"RETURNS TABLE AS\n{harness_body}"
    )
    try:
        yield conn
    finally:
        conn.execute(f"DROP SCRIPT IF EXISTS {_EXPAND_HARNESS_SCRIPT}")
        conn.execute(f"DROP SCRIPT IF EXISTS {_EXPAND_MODULE_SCRIPT}")


@pytest.mark.integration
def test_contract_expand_returns_nil(expand_contract_harness):
    """A conformant EXPAND module declines (returns nil) for an unrecognised statement."""
    rows = expand_contract_harness.execute(
        f"EXECUTE SCRIPT {_EXPAND_HARNESS_SCRIPT}('SELECT 1')"
    ).fetchall()
    assert rows[0][0] is False


@pytest.mark.integration
def test_contract_expand_matches_recognised_statement(expand_contract_harness):
    """The same conformant EXPAND module accepts (returns non-nil) the statement it recognises."""
    rows = expand_contract_harness.execute(
        f"EXECUTE SCRIPT {_EXPAND_HARNESS_SCRIPT}('CONTRACT_PING')"
    ).fetchall()
    assert rows[0][0] is True
    assert rows[0][1] == "SELECT 'PONG'"


@pytest.fixture
def translate_contract_harness(installed):
    """Deploy a minimal conformant TRANSLATE module and a thin probe harness."""
    conn = installed
    module_body = "function translate(sqltext)\n    return sqltext\nend\n"
    conn.execute(f"CREATE OR REPLACE LUA SCRIPT {_TRANSLATE_MODULE_SCRIPT}() AS\n{module_body}")
    harness_body = (
        f"import('{_TRANSLATE_MODULE_SCRIPT}', 'm')\n"
        "local result = m.translate(intext)\n"
        'exit({{result}}, "result_text VARCHAR(2000000)")\n'
    )
    conn.execute(
        f"CREATE OR REPLACE LUA SCRIPT {_TRANSLATE_HARNESS_SCRIPT}(intext) "
        f"RETURNS TABLE AS\n{harness_body}"
    )
    try:
        yield conn
    finally:
        conn.execute(f"DROP SCRIPT IF EXISTS {_TRANSLATE_HARNESS_SCRIPT}")
        conn.execute(f"DROP SCRIPT IF EXISTS {_TRANSLATE_MODULE_SCRIPT}")


@pytest.mark.integration
def test_contract_translate_returns_input(translate_contract_harness):
    """A conformant TRANSLATE module returns the input unchanged when it does not act."""
    input_text = "SELECT col FROM t"
    rows = translate_contract_harness.execute(
        f"EXECUTE SCRIPT {_TRANSLATE_HARNESS_SCRIPT}('{_sql_escape(input_text)}')"
    ).fetchall()
    assert rows[0][0] == input_text
