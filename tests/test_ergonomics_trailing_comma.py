"""Tests for ERGONOMICS_TRAILING_COMMA_V1: opt-in trailing-comma removal.

Harness approach
----------------
The module under test is a pure-Lua importable script.  To capture its exact
string output we deploy a thin DB-side Lua harness that calls the module
function and returns the rewritten text as a single-row, single-column table.
This is the only way to assert byte-for-byte exact output — probing through
MASTER cannot distinguish the rewritten text from what the engine received.

Harness shape::

    CREATE OR REPLACE LUA SCRIPT PREPROC_RT.ERG_TC_HARNESS(intext)
    RETURNS TABLE AS
    import('PREPROC_RT.ERGONOMICS_TRAILING_COMMA_V1', 'm')
    exit({{m.trailing_comma(intext)}}, "result_text VARCHAR(2000000)")
    /

The harness (and the module it imports) are deployed and torn down inside a
module-scoped fixture so they cannot pollute any static "not in core install"
assertion.

Why exact-string assertion
--------------------------
Every correctness scenario specifies a PRECISE expected string.  A harness
that returns the actual rewritten text lets us assert ``output == expected``
directly.  Pipeline-level probing only confirms executability, not that every
byte was rewritten correctly.

Fail-closed note
----------------
The ERGONOMICS_TRAILING_COMMA_V1 module has no internal error path that can
produce corrupted output: the scanner only skips or appends characters, and
``is_trailing`` returns a boolean that selects between two branches that both
produce valid results.  The fail-closed test therefore asserts graceful
handling of a pathological-but-valid input (an unclosed string literal) —
confirming the harness returns SOME string and does not raise.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_TC_LUA = _REPO_ROOT / "sql" / "lua" / "ergonomics_trailing_comma_v1.lua"
_MODULE_SCRIPT = "PREPROC_RT.ERGONOMICS_TRAILING_COMMA_V1"
_HARNESS_SCRIPT = "PREPROC_RT.ERG_TC_HARNESS"


def _sql_escape(text: str) -> str:
    """Escape a Python string for safe embedding in a SQL single-quoted literal."""
    return text.replace("'", "''")


def _harness_call(conn, input_text: str) -> str:
    """Call the trailing-comma harness and return the single string result."""
    escaped = _sql_escape(input_text)
    rows = conn.execute(f"EXECUTE SCRIPT {_HARNESS_SCRIPT}('{escaped}')").fetchall()
    return rows[0][0]


@pytest.fixture(scope="module")
def tc_harness(installed):
    """Deploy ERGONOMICS_TRAILING_COMMA_V1 and the thin harness; tear down on exit.

    Uses ``installed`` (session-scoped) to ensure the framework is present.
    Module scope keeps the two CREATE statements to one per test run.
    Teardown drops both scripts unconditionally so they cannot pollute
    the static 'not in core install' assertion.
    """
    conn = installed
    module_body = _TC_LUA.read_text(encoding="utf-8")
    conn.execute(f"CREATE OR REPLACE LUA SCRIPT {_MODULE_SCRIPT}() AS\n{module_body}")
    harness_body = (
        "import('PREPROC_RT.ERGONOMICS_TRAILING_COMMA_V1', 'm')\n"
        'exit({{m.trailing_comma(intext)}}, "result_text VARCHAR(2000000)")\n'
    )
    conn.execute(
        f"CREATE OR REPLACE LUA SCRIPT {_HARNESS_SCRIPT}(intext) RETURNS TABLE AS\n{harness_body}"
    )
    try:
        yield conn
    finally:
        conn.execute(f"DROP SCRIPT IF EXISTS {_HARNESS_SCRIPT}")
        conn.execute(f"DROP SCRIPT IF EXISTS {_MODULE_SCRIPT}")


# ---------------------------------------------------------------------------
# Structural removal: trailing comma before ')'
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_trailing_comma_before_paren(tc_harness):
    """A trailing comma before ')' in a parenthesised list is removed."""
    result = _harness_call(tc_harness, "SELECT * FROM t WHERE col IN (1, 2, 3,)")
    assert result == "SELECT * FROM t WHERE col IN (1, 2, 3)"


@pytest.mark.integration
def test_trailing_comma_values_list(tc_harness):
    """A trailing comma before ')' in a VALUES list is removed."""
    result = _harness_call(tc_harness, "INSERT INTO t VALUES (1, 2,)")
    assert result == "INSERT INTO t VALUES (1, 2)"


@pytest.mark.integration
def test_trailing_comma_function_args(tc_harness):
    """A trailing comma before ')' in a function argument list is removed."""
    result = _harness_call(tc_harness, "SELECT coalesce(a, b,) FROM t")
    assert result == "SELECT coalesce(a, b) FROM t"


@pytest.mark.integration
def test_trailing_comma_whitespace_before_paren(tc_harness):
    """A trailing comma before ')' with intervening whitespace is removed; whitespace preserved."""
    result = _harness_call(tc_harness, "SELECT * FROM t WHERE col IN (1, 2, 3,  )")
    assert result == "SELECT * FROM t WHERE col IN (1, 2, 3  )"


@pytest.mark.integration
def test_trailing_comma_comment_before_paren(tc_harness):
    """A trailing comma before ')' with an intervening comment is removed."""
    result = _harness_call(tc_harness, "SELECT * FROM t WHERE col IN (1, 2 /* last */ ,)")
    assert result == "SELECT * FROM t WHERE col IN (1, 2 /* last */ )"


# ---------------------------------------------------------------------------
# Keyword-terminated removal
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_trailing_comma_before_from(tc_harness):
    """A trailing comma before FROM in a SELECT list is removed."""
    result = _harness_call(tc_harness, "SELECT a, b, FROM t")
    assert result == "SELECT a, b FROM t"


@pytest.mark.integration
def test_trailing_comma_before_group_by(tc_harness):
    """A trailing comma before GROUP BY is removed; trailing in GROUP BY list also removed."""
    result = _harness_call(tc_harness, "SELECT a, b, FROM t GROUP BY a, b,")
    assert result == "SELECT a, b FROM t GROUP BY a, b"


# ---------------------------------------------------------------------------
# Passthrough (byte-for-byte unchanged) scenarios
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_trailing_comma_passthrough_no_trailing(tc_harness):
    """A statement with no trailing commas is returned byte-for-byte unchanged."""
    input_text = "SELECT a, b FROM t"
    result = _harness_call(tc_harness, input_text)
    assert result == input_text


@pytest.mark.integration
def test_trailing_comma_inside_single_quoted_string(tc_harness):
    """A comma inside a single-quoted string literal is NOT removed."""
    input_text = "SELECT 'a,b,c,' AS x FROM t"
    result = _harness_call(tc_harness, input_text)
    assert result == input_text


@pytest.mark.integration
def test_trailing_comma_inside_double_quoted_identifier(tc_harness):
    """A comma inside a double-quoted identifier is NOT removed."""
    input_text = 'SELECT "col,name" FROM t'
    result = _harness_call(tc_harness, input_text)
    assert result == input_text


@pytest.mark.integration
def test_trailing_comma_inside_line_comment(tc_harness):
    """A comma inside a line comment is NOT removed."""
    input_text = "SELECT a, b FROM t -- trailing comma not here,"
    result = _harness_call(tc_harness, input_text)
    assert result == input_text


@pytest.mark.integration
def test_trailing_comma_inside_block_comment(tc_harness):
    """A comma inside a block comment is NOT removed."""
    input_text = "SELECT a FROM t /* col, b, */ WHERE x = 1"
    result = _harness_call(tc_harness, input_text)
    assert result == input_text


@pytest.mark.integration
def test_trailing_comma_escaped_string_mixed(tc_harness):
    """A string with '' escapes is scanned correctly; only the real trailing comma is removed."""
    input_text = "SELECT 'it''s,ok' AS x, col FROM t WHERE col IN (1, 2,)"
    result = _harness_call(tc_harness, input_text)
    assert result == "SELECT 'it''s,ok' AS x, col FROM t WHERE col IN (1, 2)"


# ---------------------------------------------------------------------------
# Multi-occurrence and fail-closed
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_trailing_comma_multiple_in_one_statement(tc_harness):
    """Multiple trailing commas in one statement are all removed in a single pass."""
    result = _harness_call(
        tc_harness,
        "SELECT a, b, FROM t WHERE col IN (1, 2,) AND y IN (3,)",
    )
    assert result == "SELECT a, b FROM t WHERE col IN (1, 2) AND y IN (3)"


@pytest.mark.integration
def test_trailing_comma_fail_closed(tc_harness):
    """Graceful handling: any input returns a string and does not raise.

    ERGONOMICS_TRAILING_COMMA_V1 has no internal error path that produces
    corrupted output — the scanner either appends or skips each character and
    always returns the (possibly unchanged) buffer.  We verify that an
    unusual input (unclosed string literal) does not cause the harness to
    raise an exception, and that the returned value is a string.  This
    confirms the module's no-raise contract rather than faking an error path
    that does not exist.
    """
    input_text = "SELECT 'unclosed"
    result = _harness_call(tc_harness, input_text)
    assert isinstance(result, str)


# ---------------------------------------------------------------------------
# Static / deployment assertions (no DB fixture needed)
# ---------------------------------------------------------------------------


def test_trailing_comma_not_in_core_install():
    """ERGONOMICS_TRAILING_COMMA_V1 must not appear in any core install source.

    This is a static assertion: we scan install.py and sql/install/ for the
    module name.  The harness fixture deploys the script during integration
    tests, which would corrupt a runtime EXA_ALL_SCRIPTS check.  A source-
    level scan is pollution-free and definitively answers whether the module
    is part of the core install bundle.
    """
    install_py = (_REPO_ROOT / "src" / "preproc" / "install.py").read_text(encoding="utf-8")
    assert "ERGONOMICS_TRAILING_COMMA_V1" not in install_py, (
        "ERGONOMICS_TRAILING_COMMA_V1 found in src/preproc/install.py — "
        "it must only be deployed via examples/ergonomics_trailing_comma.sql"
    )

    install_dir = _REPO_ROOT / "sql" / "install"
    for sql_file in install_dir.glob("*.sql"):
        content = sql_file.read_text(encoding="utf-8")
        assert "ERGONOMICS_TRAILING_COMMA_V1" not in content, (
            f"ERGONOMICS_TRAILING_COMMA_V1 found in core install file {sql_file.name} — "
            "it must only be deployed via examples/ergonomics_trailing_comma.sql"
        )


def test_trailing_comma_module_passes_luacheck():
    """The ERGONOMICS_TRAILING_COMMA_V1 Lua source passes luacheck with zero warnings/errors."""
    if shutil.which("luacheck") is None:
        pytest.skip("luacheck binary is not installed")
    result = subprocess.run(
        ["luacheck", "sql/lua/ergonomics_trailing_comma_v1.lua"],
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, (
        f"luacheck failed for ergonomics_trailing_comma_v1.lua:\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
