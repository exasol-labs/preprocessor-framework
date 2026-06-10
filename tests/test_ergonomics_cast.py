"""Tests for ERGONOMICS_CAST_V1: PostgreSQL-style expr::type cast shorthand.

Harness approach
----------------
The module under test is a pure-Lua importable script.  To capture its exact
string output we deploy a thin DB-side Lua harness that calls the module
function and returns the rewritten text as a single-row, single-column table.
This is the only way to assert byte-for-byte exact output — probing through
MASTER cannot distinguish the rewritten text from what the engine received.

Harness shape::

    CREATE OR REPLACE LUA SCRIPT PREPROC_RT.ERG_CAST_HARNESS(intext)
    RETURNS TABLE AS
    import('PREPROC_RT.ERGONOMICS_CAST_V1', 'm')
    exit({{m.cast_shorthand(intext)}}, "result_text VARCHAR(2000000)")
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
"""

import shutil
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_CAST_LUA = _REPO_ROOT / "sql" / "lua" / "ergonomics_cast_v1.lua"
_MODULE_SCRIPT = "PREPROC_RT.ERGONOMICS_CAST_V1"
_HARNESS_SCRIPT = "PREPROC_RT.ERG_CAST_HARNESS"


def _sql_escape(text: str) -> str:
    """Escape a Python string for safe embedding in a SQL single-quoted literal."""
    return text.replace("'", "''")


def _harness_call(conn, input_text: str) -> str:
    """Call the cast harness and return the single string result."""
    escaped = _sql_escape(input_text)
    rows = conn.execute(f"EXECUTE SCRIPT {_HARNESS_SCRIPT}('{escaped}')").fetchall()
    return rows[0][0]


@pytest.fixture(scope="module")
def cast_harness(installed):
    """Deploy ERGONOMICS_CAST_V1 and the thin harness; tear down on exit.

    Uses ``installed`` (session-scoped) to ensure the framework is present.
    Module scope keeps the two CREATE statements to one per test run.
    Teardown drops both scripts unconditionally so they cannot pollute
    the static 'not in core install' assertion.
    """
    conn = installed
    module_body = _CAST_LUA.read_text(encoding="utf-8")
    conn.execute(f"CREATE OR REPLACE LUA SCRIPT {_MODULE_SCRIPT}() AS\n{module_body}")
    harness_body = (
        "import('PREPROC_RT.ERGONOMICS_CAST_V1', 'm')\n"
        'exit({{m.cast_shorthand(intext)}}, "result_text VARCHAR(2000000)")\n'
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
# Rewriting scenarios
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_cast_simple_identifier(cast_harness):
    """A simple identifier cast is rewritten to CAST(expr AS type)."""
    result = _harness_call(cast_harness, "SELECT col::integer FROM t")
    assert result == "SELECT CAST(col AS integer) FROM t"


@pytest.mark.integration
def test_cast_schema_qualified_identifier(cast_harness):
    """A schema-qualified identifier cast is rewritten correctly."""
    result = _harness_call(cast_harness, "SELECT s.t.col::varchar(20) FROM s.t")
    assert result == "SELECT CAST(s.t.col AS varchar(20)) FROM s.t"


@pytest.mark.integration
def test_cast_parenthesised_expression(cast_harness):
    """A parenthesised expression cast is rewritten correctly."""
    result = _harness_call(cast_harness, "SELECT (a + b)::decimal(10,2) FROM t")
    assert result == "SELECT CAST((a + b) AS decimal(10,2)) FROM t"


@pytest.mark.integration
def test_cast_function_call_operand(cast_harness):
    """A function-call operand is rewritten correctly."""
    result = _harness_call(cast_harness, "SELECT trim(col)::varchar(50) FROM t")
    assert result == "SELECT CAST(trim(col) AS varchar(50)) FROM t"


@pytest.mark.integration
def test_cast_schema_qualified_function_call_operand(cast_harness):
    """A schema-qualified function-call operand keeps its qualifier in the cast.

    Regression: the backwards scan for the identifier before '(' must consume
    the dotted qualifier (schema.func) as well, otherwise 's.func(x)::int'
    leaves 's.' stranded and emits 's.CAST(func(x) AS int)'.
    """
    result = _harness_call(cast_harness, "SELECT s.fn(col)::int FROM t")
    assert result == "SELECT CAST(s.fn(col) AS int) FROM t"


@pytest.mark.integration
def test_cast_chained(cast_harness):
    """A chained cast is rewritten inside-out in a single left-to-right pass."""
    result = _harness_call(cast_harness, "SELECT a::int::text FROM t")
    assert result == "SELECT CAST(CAST(a AS int) AS text) FROM t"


@pytest.mark.integration
def test_cast_parameterised_type(cast_harness):
    """A parameterised type is captured including its argument list."""
    result = _harness_call(cast_harness, "SELECT x::decimal(10,2) FROM t")
    assert result == "SELECT CAST(x AS decimal(10,2)) FROM t"


# ---------------------------------------------------------------------------
# Passthrough (byte-for-byte unchanged) scenarios
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_cast_passthrough_no_colons(cast_harness):
    """A statement with no '::' tokens is returned byte-for-byte unchanged."""
    input_text = "SELECT col FROM t"
    result = _harness_call(cast_harness, input_text)
    assert result == input_text


@pytest.mark.integration
def test_cast_inside_single_quoted_string(cast_harness):
    """A '::' inside a single-quoted string literal is NOT rewritten."""
    input_text = "SELECT '::not_a_cast' AS x FROM dual"
    result = _harness_call(cast_harness, input_text)
    assert result == input_text


@pytest.mark.integration
def test_cast_inside_double_quoted_identifier(cast_harness):
    """A '::' inside a double-quoted identifier is NOT rewritten."""
    input_text = 'SELECT "col::name" FROM t'
    result = _harness_call(cast_harness, input_text)
    assert result == input_text


@pytest.mark.integration
def test_cast_inside_line_comment(cast_harness):
    """A '::' inside a line comment is NOT rewritten."""
    input_text = "SELECT col FROM t -- col::integer is just a comment"
    result = _harness_call(cast_harness, input_text)
    assert result == input_text


@pytest.mark.integration
def test_cast_inside_block_comment(cast_harness):
    """A '::' inside a block comment is NOT rewritten."""
    input_text = "SELECT /* a::int placeholder */ col FROM t"
    result = _harness_call(cast_harness, input_text)
    assert result == input_text


@pytest.mark.integration
def test_cast_escaped_string_mixed(cast_harness):
    """A string with '' escape sequences is scanned correctly; only the real cast is rewritten."""
    input_text = "SELECT 'it''s::fine' AS x, col::integer FROM t"
    result = _harness_call(cast_harness, input_text)
    assert result == "SELECT 'it''s::fine' AS x, CAST(col AS integer) FROM t"


# ---------------------------------------------------------------------------
# Fail-closed scenario
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_cast_unrecognised_operand_fail_closed(cast_harness):
    """An unrecognised left operand causes fail-closed: original text returned unchanged.

    'SELECT arr[1]::int FROM t' has ']' as the last character before '::'.
    The backwards scan hits ']' which is not in the supported operand character
    classes, so scan_backward_operand returns nil and the module returns the
    original text unchanged.
    """
    input_text = "SELECT arr[1]::int FROM t"
    result = _harness_call(cast_harness, input_text)
    assert result == input_text


# ---------------------------------------------------------------------------
# Static / deployment assertions (no DB fixture needed)
# ---------------------------------------------------------------------------


def test_cast_not_in_core_install():
    """ERGONOMICS_CAST_V1 must not appear in any core install source.

    This is a static assertion: we scan install.py and sql/install/ for the
    module name.  The harness fixture deploys the script during integration
    tests, which would corrupt a runtime EXA_ALL_SCRIPTS check.  A source-
    level scan is pollution-free and definitively answers whether the module
    is part of the core install bundle.
    """
    install_py = (_REPO_ROOT / "src" / "preproc" / "install.py").read_text(encoding="utf-8")
    assert "ERGONOMICS_CAST_V1" not in install_py, (
        "ERGONOMICS_CAST_V1 found in src/preproc/install.py — "
        "it must only be deployed via examples/ergonomics_cast.sql"
    )

    install_dir = _REPO_ROOT / "sql" / "install"
    for sql_file in install_dir.glob("*.sql"):
        content = sql_file.read_text(encoding="utf-8")
        assert "ERGONOMICS_CAST_V1" not in content, (
            f"ERGONOMICS_CAST_V1 found in core install file {sql_file.name} — "
            "it must only be deployed via examples/ergonomics_cast.sql"
        )


def test_cast_module_passes_luacheck():
    """The ERGONOMICS_CAST_V1 Lua source passes luacheck with zero warnings/errors."""
    if shutil.which("luacheck") is None:
        pytest.skip("luacheck binary is not installed")
    result = subprocess.run(
        ["luacheck", "sql/lua/ergonomics_cast_v1.lua"],
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, (
        f"luacheck failed for ergonomics_cast_v1.lua:\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
