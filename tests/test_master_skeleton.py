"""Behaviour integration tests for the MASTER preprocessor skeleton.

These tests verify the safety contract of ``PREPROC_RT.MASTER`` against a live
Exasol instance: installation, byte-for-byte pass-through, the ALTER guard, and
fail-open on a broken pipeline lookup. They activate the preprocessor with
SESSION scope only — never ALTER SYSTEM — and unconditionally restore session
and schema state in ``finally`` so a failure cannot poison the session-scoped
connection shared with the rest of the suite.

The luacheck test needs no database and runs wherever the ``luacheck`` binary is
available; it skips cleanly otherwise.
"""

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]

_REFRESH_CORE = _REPO_ROOT / "sql" / "lua" / "refresh_core.lua"

_ACTIVATE = "ALTER SESSION SET sql_preprocessor_script = PREPROC_RT.MASTER"
_DEACTIVATE = "ALTER SESSION SET sql_preprocessor_script = ''"


def _activate(connection):
    """Activate MASTER as the session preprocessor."""
    connection.execute(_ACTIVATE)


def _deactivate(connection):
    """Clear the session preprocessor; safe to call unconditionally."""
    connection.execute(_DEACTIVATE)


@pytest.mark.integration
def test_master_script_installed(installed):
    """After install, PREPROC_RT.MASTER is present in the script catalogue."""
    rows = installed.execute(
        "SELECT SCRIPT_NAME FROM EXA_ALL_SCRIPTS "
        "WHERE SCRIPT_SCHEMA = 'PREPROC_RT' AND SCRIPT_NAME = 'MASTER'"
    ).fetchall()
    assert rows == [("MASTER",)]


@pytest.mark.integration
def test_normal_statement_passthrough(installed):
    """An ordinary SELECT executes unchanged while MASTER is active.

    This test also covers the no-rules row shape: MY_PIPELINE returns an
    all-NULL phase/script/function row (the LEFT JOIN sentinel) when the
    current user has no entries in PREPROC.RESOLUTION. MASTER must treat that
    row as the no-rules case and pass the statement through unchanged.

    Precondition: the current user has zero rules in PREPROC.RESOLUTION. If
    this assertion fails, a leftover test rule is polluting the table and the
    no-rules coverage claim is false.
    """
    # Establish the no-rules precondition. Install now seeds a DBA-scoped
    # control-sugar bootstrap rule and this connection (SYS) holds DBA, so it
    # resolves that rule; clear CURRENT_USER's resolution so the no-rules
    # sentinel path is genuinely exercised.
    installed.execute("DELETE FROM PREPROC.RESOLUTION WHERE user_name = CURRENT_USER")
    rule_count = installed.execute(
        "SELECT COUNT(*) FROM PREPROC.RESOLUTION WHERE user_name = CURRENT_USER"
    ).fetchval()
    assert rule_count == 0, (
        f"Expected 0 rules for CURRENT_USER in PREPROC.RESOLUTION, "
        f"got {rule_count} — leftover rule invalidates the no-rules coverage claim"
    )

    _activate(installed)
    try:
        value = installed.execute("SELECT 987654321 AS PROBE").fetchval()
        assert value == 987654321
    finally:
        _deactivate(installed)


@pytest.mark.integration
def test_alter_statements_guarded(installed):
    """ALTER SESSION is guarded (returns early) and the session stays usable.

    The guard must return before any pipeline lookup, so the ALTER itself
    succeeds and a following ordinary statement still works. If the guard were
    absent the ALTER could provoke lookup/recursion noise; asserting both the
    ALTER and a subsequent SELECT succeed pins the guarded behaviour.
    """
    _activate(installed)
    try:
        installed.execute("ALTER SESSION SET QUERY_TIMEOUT = 600")
        value = installed.execute("SELECT 424242 AS PROBE").fetchval()
        assert value == 424242
    finally:
        try:
            installed.execute("ALTER SESSION SET QUERY_TIMEOUT = 0")
        finally:
            _deactivate(installed)


@pytest.mark.integration
def test_lookup_failure_fails_open(installed):
    """A broken pipeline lookup fails open: statements still execute.

    The pipeline view is renamed out from under MASTER so its sole data access
    (``SELECT ... FROM PREPROC_RT.MY_PIPELINE``) raises. MASTER wraps that read
    in pcall and returns the statement unchanged, so ``SELECT 1`` must still
    return 1. The view is renamed back unconditionally; the preprocessor is
    deactivated unconditionally even if the restore raises.
    """
    installed.execute("RENAME VIEW PREPROC_RT.MY_PIPELINE TO MY_PIPELINE_BROKEN_TEST")
    try:
        _activate(installed)
        try:
            value = installed.execute("SELECT 1").fetchval()
            assert value == 1
        finally:
            _deactivate(installed)
    finally:
        installed.execute("RENAME VIEW PREPROC_RT.MY_PIPELINE_BROKEN_TEST TO MY_PIPELINE")


def test_master_is_inline_dispatcher():
    """master.lua is the single canonical INLINE dispatcher, not an import wrapper.

    The earlier import("PREPROC_RT.MASTER_CORE") indirection was abandoned: in the
    Exasol preprocessor context the per-rule dynamic import() works only at
    top-level script scope, never from inside a function of an imported module, so
    a thin import-wrapper silently fail-opened every dispatch. The dispatch +
    swap logic therefore lives INLINE in master.lua, which is now the one source
    of truth for the dispatch contract (the retirement stub is sourced from this
    body verbatim). This pins that: master.lua MUST carry the inline dispatch (the
    MY_PIPELINE read, the dispatch loop, and the swap ALTER SESSION) and MUST NOT
    import a MASTER_CORE module. The header comment legitimately documents WHY the
    MASTER_CORE indirection was abandoned, so the no-import check strips ``--``
    comment lines before asserting; if the import reappears in real code, the
    inline-dispatcher invariant has been broken.
    """
    master_src = (_REPO_ROOT / "sql" / "lua" / "master.lua").read_text(encoding="utf-8")

    assert "MY_PIPELINE" in master_src
    assert "dispatch_rule" in master_src
    assert "sql_preprocessor_script" in master_src

    code_lines = [line for line in master_src.splitlines() if not line.lstrip().startswith("--")]
    code = "\n".join(code_lines)
    assert 'import("PREPROC_RT.MASTER_CORE"' not in code


def test_master_passes_luacheck():
    """The Lua sources pass luacheck with the project configuration."""
    if shutil.which("luacheck") is None:
        pytest.skip("luacheck binary is not installed")

    try:
        result = subprocess.run(
            ["luacheck", "sql/lua/", "--config", ".luacheckrc"],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        pytest.skip(f"luacheck is present on PATH but not executable: {exc}")

    assert result.returncode == 0, result.stdout + result.stderr


def _extract_long_string(source, name):
    """Return the body of a ``local <name> = [=[ ... ]=]`` level-1 long string."""
    match = re.search(
        r"local " + re.escape(name) + r" = \[=\[(.*?)\]=\]",
        source,
        re.DOTALL,
    )
    assert match is not None, f"could not extract {name} from refresh_core.lua"
    return match.group(1)


def _generate_sample_profile_body(client_pattern=None):
    """Build a P_<hash> body from the REAL codegen template with one baked rule.

    Mirrors ``build_profile_body`` in refresh_core.lua: the template sections
    (``PROFILE_BODY_PREFIX``, the chosen CLIENT section, ``PROFILE_BODY_DISPATCH``)
    are extracted verbatim from the source so this validates the SAME template the
    live codegen emits, not a hand-copied approximation. A single REWRITE rule is
    spliced in; when ``client_pattern`` is None the rule carries a NULL pattern and
    the no-lookup client section is used (matching codegen's per-chain choice).
    """
    source = _REFRESH_CORE.read_text()
    prefix = _extract_long_string(source, "PROFILE_BODY_PREFIX")
    dispatch = _extract_long_string(source, "PROFILE_BODY_DISPATCH")
    if client_pattern is None:
        client = _extract_long_string(source, "PROFILE_BODY_NOCLIENT")
        cp_literal = "nil"
    else:
        client = _extract_long_string(source, "PROFILE_BODY_CLIENT")
        cp_literal = f'"{client_pattern}"'
    rule = (
        '{phase="REWRITE",ordinal=1,'
        'script_name="PREPROC_RT.DEMO_AUDIT_V1",'
        f'function_name="tag",client_pattern={cp_literal}}}'
    )
    return prefix + rule + "}\n" + client + dispatch


def test_retirement_stub_is_master_body():
    """The retirement stub is MASTER's own body, so stub == MASTER by construction.

    The earlier baked-in stub constant (``RETIREMENT_STUB_BODY``) was abandoned.
    refresh_core.lua now sources the stub by reading ``PREPROC_RT.MASTER``'s own
    SCRIPT_TEXT from the catalog and stripping the leading ``CREATE ... AS`` line
    (``master_stub_body()``), so the deployed stub is byte-for-byte MASTER's body
    — one source of truth, and a dispatch-contract change in master.lua
    propagates to every stub created or refreshed afterward.

    Because the stub IS the master.lua body, its lint coverage is exactly
    ``test_master_passes_luacheck`` (which lints all of ``sql/lua/``); there is no
    separate stub source to validate. This pins the contract: refresh_core.lua
    sources the stub from MASTER's catalog text and bakes NO independent stub
    body (no ``RETIREMENT_STUB_BODY`` constant, no MASTER_CORE import wrapper).
    """
    refresh_src = _REFRESH_CORE.read_text(encoding="utf-8")

    assert "EXA_ALL_SCRIPTS" in refresh_src
    assert "'PREPROC_RT'" in refresh_src and "'MASTER'" in refresh_src
    assert "master_stub_body" in refresh_src

    assert "RETIREMENT_STUB_BODY" not in refresh_src
    assert 'import("PREPROC_RT.MASTER_CORE"' not in refresh_src


def test_generated_profile_passes_luacheck():
    """A generated profile script body must pass luacheck.

    Profile bodies are codegen output, not source files under ``sql/lua/``, so
    the suite's directory luacheck never sees them. This generates a body from
    the real template halves and validates it standalone with the same Exasol
    globals the body declares, guarding the codegen template against syntax or
    lint regressions independently of a live database.
    """
    if shutil.which("luacheck") is None:
        pytest.skip("luacheck binary is not installed")

    # Validate BOTH client variants: the no-lookup body (no client_pattern rules)
    # and the with-lookup body (a client_pattern present).
    for client_pattern in (None, "EXAplus"):
        body = _generate_sample_profile_body(client_pattern=client_pattern)
        with tempfile.NamedTemporaryFile(
            "w", suffix=".lua", delete=False, encoding="utf-8"
        ) as handle:
            handle.write(body)
            path = handle.name
        try:
            result = subprocess.run(
                [
                    "luacheck",
                    path,
                    "--std",
                    "lua51",
                    "--globals",
                    "sqlparsing",
                    "pquery",
                    "null",
                    "import",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError as exc:
            pytest.skip(f"luacheck is present on PATH but not executable: {exc}")
        finally:
            Path(path).unlink()

        assert result.returncode == 0, (
            f"client_pattern={client_pattern!r}:\n{result.stdout}{result.stderr}"
        )
