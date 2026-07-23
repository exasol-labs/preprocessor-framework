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

from preproc.module.manifest import (
    ArtifactError,
    ManifestError,
    load_manifest,
    parse_manifest,
    verify_artifact_script_name,
    verify_artifact_sha256,
)

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
# Artifact structural checks
# ---------------------------------------------------------------------------


def test_artifact_single_create_statement():
    """A conformant artifact is exactly one CREATE OR REPLACE naming the manifest's script."""
    manifest = parse_manifest(_manifest_data())
    verify_artifact_script_name(manifest, _artifact_text())  # does not raise


def test_artifact_multiple_create_statements_rejected():
    """An artifact with more than one CREATE OR REPLACE LUA SCRIPT statement is rejected."""
    manifest = parse_manifest(_manifest_data())
    text = (
        _artifact_text()
        + "\n"
        + _artifact_text(script_name="PREPROC_RT.OTHER_V1", function="other")
    )
    with pytest.raises(ArtifactError):
        verify_artifact_script_name(manifest, text)


def test_artifact_script_name_mismatch_rejected():
    """An artifact whose script name disagrees with the manifest is rejected."""
    manifest = parse_manifest(_manifest_data())
    text = _artifact_text(script_name="PREPROC_RT.WRONG_NAME_V1")
    with pytest.raises(ArtifactError):
        verify_artifact_script_name(manifest, text)


# ---------------------------------------------------------------------------
# Artifact integrity (sha256)
# ---------------------------------------------------------------------------


def test_artifact_sha_mismatch_rejected():
    """A library-deployed artifact whose bytes do not hash to the manifest sha256 is rejected."""
    manifest = parse_manifest(_manifest_data())
    tampered = (_artifact_text() + "\n-- tampered\n").encode("utf-8")
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
