"""Tests for top_objects: the SHOW TOP [n] OBJECTS command.

Harness approach
----------------
Mirrors modules/cast_shorthand/tests/. The module under test is deployed from its
canonical artifact, ``top_objects_v1.sql``, and a thin DB-side Lua harness calls
the entry function and returns its output as a single-row table - the only way to
assert the exact rewritten text, since probing through MASTER cannot distinguish
the rewrite from what the engine received.

Because this is an EXPAND module the entry function returns ``nil`` for anything
it declines, so the harness stringifies the result: a declined statement comes
back as the literal ``"nil"``.

The harness and the module it imports are deployed and torn down inside a
module-scoped fixture so they cannot pollute any other test in the session.

The static gates (manifest, sha256, inventory) need only the framework package.
The live tests use the ``installed`` fixture from the repo root conftest.py and
skip cleanly when no database is configured.
"""

from pathlib import Path

import pytest

from preproc.module.manifest import (
    load_manifest,
    verify_artifact_inventory,
    verify_artifact_sha256,
)

_MODULE_DIR = Path(__file__).resolve().parents[1]
_ARTIFACT = _MODULE_DIR / "top_objects_v1.sql"
_MANIFEST_PATH = _MODULE_DIR / "module.toml"
_MODULE_SCRIPT = "PREPROC_RT.TOP_OBJECTS_V1"
_HARNESS_SCRIPT = "PREPROC_RT.TOP_OBJECTS_TEST_HARNESS"

_EXPECTED_PREFIX = "SELECT ROOT_NAME AS SCHEMA_NAME"


def _create_statement() -> str:
    """Return the artifact's CREATE OR REPLACE statement without the EXAplus delimiters."""
    text = _ARTIFACT.read_text(encoding="utf-8")
    return text.removeprefix("--/\n").rstrip().removesuffix("/").rstrip()


def _sql_escape(text: str) -> str:
    """Escape a Python string for safe embedding in a SQL single-quoted literal."""
    return text.replace("'", "''")


def _harness_call(conn, input_text: str) -> str:
    """Call the harness and return the entry function's output, stringified."""
    rows = conn.execute(f"EXECUTE SCRIPT {_HARNESS_SCRIPT}('{_sql_escape(input_text)}')").fetchall()
    return rows[0][0]


# --- static gates ------------------------------------------------------------


def test_manifest_conforms():
    manifest = load_manifest(_MANIFEST_PATH)
    assert manifest.name == "top_objects"
    assert manifest.phase == "EXPAND"
    assert manifest.version == 1
    assert manifest.script_name == _MODULE_SCRIPT
    assert manifest.function == "top_objects"


def test_sha256_matches_artifact():
    verify_artifact_sha256(load_manifest(_MANIFEST_PATH), _ARTIFACT.read_bytes())


def test_declared_inventory_matches_artifact():
    manifest = load_manifest(_MANIFEST_PATH)
    derived = verify_artifact_inventory(manifest, _ARTIFACT.read_text(encoding="utf-8"))
    assert [obj.name for obj in derived] == [_MODULE_SCRIPT]


# --- live behaviour ----------------------------------------------------------


@pytest.fixture(scope="module")
def top_objects_harness(installed):
    """Deploy the artifact and a thin harness; tear both down on exit."""
    conn = installed
    conn.execute(_create_statement())
    harness_body = (
        f"import('{_MODULE_SCRIPT}', 'm')\n"
        'exit({{tostring(m.top_objects(intext))}}, "result_text VARCHAR(2000000)")\n'
    )
    conn.execute(
        f"CREATE OR REPLACE LUA SCRIPT {_HARNESS_SCRIPT}(intext) RETURNS TABLE AS\n{harness_body}"
    )
    try:
        yield conn
    finally:
        conn.execute(f"DROP SCRIPT IF EXISTS {_HARNESS_SCRIPT}")
        conn.execute(f"DROP SCRIPT IF EXISTS {_MODULE_SCRIPT}")


@pytest.mark.parametrize(
    "statement",
    [
        "SHOW TOP OBJECTS",
        "show top objects",
        "SHOW TOP 3 OBJECTS",
        "  SHOW TOP 3 OBJECTS  ",
        "SHOW TOP 10 OBJECTS;",
        "-- a leading comment\nSHOW TOP OBJECTS",
    ],
)
def test_claims_its_own_command(top_objects_harness, statement):
    assert _harness_call(top_objects_harness, statement).startswith(_EXPECTED_PREFIX)


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT 1",
        "SHOW TOP OBJECTSX",
        "SHOW TOP abc OBJECTS",
        "SHOW TOP OBJECTS 3",
        "SHOW TOP",
        "DROP SCHEMA X",
    ],
)
def test_declines_everything_else(top_objects_harness, statement):
    """An EXPAND module returns nil for anything it does not recognise."""
    assert _harness_call(top_objects_harness, statement) == "nil"


def test_default_row_limit_is_five(top_objects_harness):
    assert _harness_call(top_objects_harness, "SHOW TOP OBJECTS").endswith("LIMIT 5")


def test_explicit_row_limit_is_honoured(top_objects_harness):
    assert _harness_call(top_objects_harness, "SHOW TOP 17 OBJECTS").endswith("LIMIT 17")


def test_expansion_is_a_single_statement(top_objects_harness):
    """The framework does not split on semicolons; a rewrite must be one statement."""
    expansion = _harness_call(top_objects_harness, "SHOW TOP 3 OBJECTS")
    assert ";" not in expansion
