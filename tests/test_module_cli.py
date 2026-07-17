"""Tests for the ``preproc module`` CLI command group.

The read-only verbs (``list``, ``search``, ``bundle``, static ``validate``) and
the missing-environment guard run fully offline here — they build a throwaway
registry checkout under ``tmp_path`` and never touch a database or the network.
The mutating verbs (``add``, ``update``, ``remove``, ``sync``, live ``validate``)
are marked ``integration`` and skip without an env-configured Exasol instance.

Every command is exercised through the real argparse tree (``add_module_parser``)
and dispatch (``run_module``), so the wiring is covered, not just the helpers.
"""

import argparse
import hashlib

import pytest

from preproc.module import commands
from preproc.module.commands import add_module_parser, run_module
from preproc.module.registry import generate_index, render_index

# ---------------------------------------------------------------------------
# registry-checkout builder (offline)
# ---------------------------------------------------------------------------

_DEFAULT_BODY = "function {function}(sqltext)\n    return sqltext\nend\n"


def _make_module(
    root,
    name,
    *,
    version=1,
    phase="TRANSLATE",
    function=None,
    deploy_mode="library-deployed",
    description=None,
    body=None,
    artifact_script_name=None,
    manifest_script_name=None,
    scope=("ROLE", "PREPROC_ADMIN"),
):
    """Write a modules/<name>/{module.toml, <name>_v<N>.sql} pair.

    ``artifact_script_name`` and ``manifest_script_name`` default to the derived
    ``PREPROC_RT.<NAME>_V<N>``; passing them apart from each other builds a
    deliberately nonconformant module for the static-reject scenario.
    """
    function = function or name.replace("-", "_")
    upper = name.upper().replace("-", "_")
    derived = f"PREPROC_RT.{upper}_V{version}"
    artifact_script_name = artifact_script_name or derived
    manifest_script_name = manifest_script_name or derived

    module_dir = root / "modules" / name
    module_dir.mkdir(parents=True, exist_ok=True)

    body = body or _DEFAULT_BODY.format(function=function)
    artifact = f"CREATE OR REPLACE LUA SCRIPT {artifact_script_name}() AS\n{body}"
    (module_dir / f"{name}_v{version}.sql").write_text(artifact, encoding="utf-8")

    lines = [
        f'name = "{name}"',
        f'description = "{description or f"{name} module."}"',
        f'phase = "{phase}"',
        f'script_name = "{manifest_script_name}"',
        f'function = "{function}"',
        f"version = {version}",
        'min_framework = "1.0.0"',
        f'deploy_mode = "{deploy_mode}"',
    ]
    if deploy_mode == "library-deployed":
        digest = hashlib.sha256(artifact.encode("utf-8")).hexdigest()
        lines.append(f'sha256 = "{digest}"')
    if deploy_mode == "self-deployed":
        lines += ["", "[source]", 'repo = "exasol/other"', 'ref = "v1"']
    lines += ["", "[suggested_scope]", f'type = "{scope[0]}"', f'value = "{scope[1]}"']
    (module_dir / "module.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_index(root):
    index = generate_index(root, repo="exasol-labs/preprocessor-library")
    registry_dir = root / "registry"
    registry_dir.mkdir(exist_ok=True)
    (registry_dir / "index.json").write_text(render_index(index), encoding="utf-8")
    return root


def _two_module_registry(root):
    _make_module(root, "cast-shorthand", description="cast shorthand transform")
    _make_module(root, "trailing-comma", description="trailing comma cleanup")
    return _write_index(root)


def _parse(argv):
    parser = argparse.ArgumentParser(prog="preproc")
    subparsers = parser.add_subparsers(dest="command")
    add_module_parser(subparsers)
    return parser.parse_args(argv)


@pytest.fixture
def no_db(monkeypatch):
    """Scrub the connection env and make any connection attempt fail loudly."""
    for var in ("EXASOL_DSN", "EXASOL_USER", "EXASOL_PASSWORD"):
        monkeypatch.delenv(var, raising=False)

    def _forbidden(*_args, **_kwargs):
        raise AssertionError("a read-only verb must not open a database connection")

    monkeypatch.setattr(commands, "connect_from_env", _forbidden)
    return monkeypatch


# ---------------------------------------------------------------------------
# list / search — offline, no database
# ---------------------------------------------------------------------------


def test_cli_module_list(tmp_path, capsys, no_db):
    """``list`` prints both modules' name, version, phase, and deploy mode; exit 0."""
    root = _two_module_registry(tmp_path)

    code = run_module(_parse(["module", "list", "--registry", str(root)]))

    assert code == 0
    out = capsys.readouterr().out
    assert "cast-shorthand" in out
    assert "trailing-comma" in out
    assert "TRANSLATE" in out
    assert "library-deployed" in out
    assert "v1" in out


def test_cli_module_search(tmp_path, capsys, no_db):
    """``search cast`` prints cast-shorthand but not trailing-comma."""
    root = _two_module_registry(tmp_path)

    code = run_module(_parse(["module", "search", "cast", "--registry", str(root)]))

    assert code == 0
    out = capsys.readouterr().out
    assert "cast-shorthand" in out
    assert "trailing-comma" not in out


# ---------------------------------------------------------------------------
# bundle — offline, no database
# ---------------------------------------------------------------------------


def test_cli_module_bundle_offline(tmp_path, capsys, no_db):
    """``bundle`` writes one self-contained .sql with both artifacts + registrations."""
    root = _two_module_registry(tmp_path)
    out_file = tmp_path / "offline_modules.sql"

    code = run_module(
        _parse(
            [
                "module",
                "bundle",
                "cast-shorthand",
                "trailing-comma",
                "--output",
                str(out_file),
                "--registry",
                str(root),
            ]
        )
    )

    assert code == 0
    assert out_file.exists()
    text = out_file.read_text(encoding="utf-8")
    # Both artifacts, wrapped in the --/ ... / script-block delimiters.
    assert "CREATE OR REPLACE LUA SCRIPT PREPROC_RT.CAST_SHORTHAND_V1" in text
    assert "CREATE OR REPLACE LUA SCRIPT PREPROC_RT.TRAILING_COMMA_V1" in text
    assert "--/" in text
    assert any(line == "/" for line in text.splitlines())
    # Rule registrations and provenance writes for both modules.
    assert text.count("EXECUTE SCRIPT PREPROC.RULE_ENSURE(") == 2
    assert text.count("EXECUTE SCRIPT PREPROC.MODULE_REGISTER(") == 2
    # A single closing refresh for the whole file.
    assert text.rstrip().endswith("EXECUTE SCRIPT PREPROC.REFRESH;")


# ---------------------------------------------------------------------------
# validate — static path, offline
# ---------------------------------------------------------------------------


def test_cli_module_validate_static_reject(tmp_path, capsys, no_db):
    """A module whose artifact does not define the declared script_name is rejected."""
    _make_module(
        tmp_path,
        "broken",
        manifest_script_name="PREPROC_RT.DECLARED_V1",
        artifact_script_name="PREPROC_RT.ACTUAL_V1",
    )
    root = _write_index(tmp_path)

    code = run_module(_parse(["module", "validate", "broken", "--registry", str(root)]))

    assert code != 0
    err = capsys.readouterr().err.lower()
    assert "script_name" in err or "script name" in err


# ---------------------------------------------------------------------------
# missing environment — offline
# ---------------------------------------------------------------------------


def test_cli_module_add_missing_env(tmp_path, capsys, monkeypatch):
    """A mutating verb exits with the missing-env code and names the variables."""
    for var in ("EXASOL_DSN", "EXASOL_USER", "EXASOL_PASSWORD"):
        monkeypatch.delenv(var, raising=False)
    root = _two_module_registry(tmp_path)

    code = run_module(_parse(["module", "add", "cast-shorthand", "--registry", str(root)]))

    assert code == 2
    combined = capsys.readouterr()
    text = combined.out + combined.err
    for var in ("EXASOL_DSN", "EXASOL_USER", "EXASOL_PASSWORD"):
        assert var in text


# ===========================================================================
# Integration — require a live Exasol instance (skip without one)
# ===========================================================================

_SEED = "SEED"
_ACTIVATE = "ALTER SESSION SET sql_preprocessor_script = PREPROC_RT.MASTER"
_DEACTIVATE = "ALTER SESSION SET sql_preprocessor_script = ''"

# A conformant TRANSLATE module that tags a SEED sentinel so a transform is
# observable, mirroring test_master_dispatch / test_module_lifecycle.
_TAG_BODY = (
    "function {function}(sqltext)\n"
    '    local out = string.gsub(sqltext, "({seed}[^\']*)\'", "%1|{tag}\'", 1)\n'
    "    return out\n"
    "end\n"
)


def _wipe(conn):
    for table in ("RESOLUTION", "CONFIG", "PROFILES", "MODULES"):
        conn.execute(f"DELETE FROM PREPROC.{table}")
    conn.execute("EXECUTE SCRIPT PREPROC.SET_MODE('convenience', TRUE)")
    conn.commit()


def _drop_script_if_present(conn, qualified):
    schema, _, name = qualified.partition(".")
    exists = conn.execute(
        "SELECT COUNT(*) FROM EXA_ALL_SCRIPTS WHERE SCRIPT_SCHEMA = {s} AND SCRIPT_NAME = {n}",
        {"s": schema, "n": name},
    ).fetchval()
    if exists:
        conn.execute(f"DROP SCRIPT {qualified}")


@pytest.fixture
def module_env(installed):
    """Clean global tables around each integration test and drop any test scripts."""
    conn = installed
    _wipe(conn)
    try:
        yield conn
    finally:
        try:
            conn.execute(_DEACTIVATE)
        finally:
            _wipe(conn)
            for name in ("cast-shorthand", "trailing-comma"):
                upper = name.upper().replace("-", "_")
                for version in (1, 2):
                    _drop_script_if_present(conn, f"PREPROC_RT.{upper}_V{version}")
            conn.execute("EXECUTE SCRIPT PREPROC.REFRESH").fetchall()


def _current_user(conn):
    return conn.execute("SELECT CURRENT_USER").fetchval()


def _provenance(conn, name, version):
    rows = conn.execute(
        "SELECT status FROM PREPROC_RT.INSTALLED_MODULES WHERE name = {n} AND version = {v}",
        {"n": name, "v": version},
    ).fetchall()
    return rows[0][0] if rows else None


def _enabled_rules_for(conn, script_name):
    return conn.execute(
        "SELECT COUNT(*) FROM PREPROC.CONFIG WHERE enabled = TRUE AND script_name = {sn}",
        {"sn": script_name},
    ).fetchval()


@pytest.mark.integration
def test_cli_module_add_library(tmp_path, module_env):
    """``add`` deploys the artifact, registers the rule, writes provenance, transforms."""
    conn = module_env
    user = _current_user(conn)
    _make_module(
        tmp_path,
        "cast-shorthand",
        function="cast_shorthand",
        body=_TAG_BODY.format(function="cast_shorthand", seed=_SEED, tag="V1"),
    )
    root = _write_index(tmp_path)

    code = run_module(
        _parse(
            [
                "module",
                "add",
                "cast-shorthand",
                "--scope",
                f"USER:{user}",
                "--registry",
                str(root),
            ]
        )
    )

    assert code == 0
    assert _enabled_rules_for(conn, "PREPROC_RT.CAST_SHORTHAND_V1") == 1
    assert _provenance(conn, "cast-shorthand", 1) == "deployed"

    from preproc.connection import connect_from_env

    probe = connect_from_env()
    try:
        probe.execute(_ACTIVATE)
        assert probe.execute(f"SELECT '{_SEED}' AS PROBE").fetchval() == f"{_SEED}|V1"
    finally:
        try:
            probe.execute(_DEACTIVATE)
        finally:
            probe.close()


@pytest.mark.integration
def test_cli_module_add_self_deployed(tmp_path, module_env):
    """``add`` for a self-deployed module registers the rule + provenance, no CREATE."""
    conn = module_env
    user = _current_user(conn)
    # The backing script must already exist (its own installer deployed it).
    conn.execute("OPEN SCHEMA PREPROC_RT")
    conn.execute(
        "CREATE OR REPLACE LUA SCRIPT PREPROC_RT.SELFDEP_V1() AS\n"
        "function selfdep(sqltext)\n    return nil\nend\n"
    )
    _make_module(
        tmp_path,
        "selfdep",
        phase="EXPAND",
        function="selfdep",
        deploy_mode="self-deployed",
    )
    root = _write_index(tmp_path)

    code = run_module(
        _parse(["module", "add", "selfdep", "--scope", f"USER:{user}", "--registry", str(root)])
    )

    assert code == 0
    assert _enabled_rules_for(conn, "PREPROC_RT.SELFDEP_V1") == 1
    assert _provenance(conn, "selfdep", 1) == "deployed"
    _drop_script_if_present(conn, "PREPROC_RT.SELFDEP_V1")


@pytest.mark.integration
def test_cli_module_add_sha_mismatch_aborts(tmp_path, module_env):
    """A tampered artifact aborts add with no script, rule, or provenance written."""
    conn = module_env
    user = _current_user(conn)
    _make_module(tmp_path, "cast-shorthand", function="cast_shorthand")
    root = _write_index(tmp_path)
    # Tamper the artifact after the index (and its sha256) were written.
    artifact = root / "modules" / "cast-shorthand" / "cast-shorthand_v1.sql"
    artifact.write_text(artifact.read_text(encoding="utf-8") + "\n-- tampered\n", encoding="utf-8")

    code = run_module(
        _parse(
            [
                "module",
                "add",
                "cast-shorthand",
                "--scope",
                f"USER:{user}",
                "--registry",
                str(root),
            ]
        )
    )

    assert code != 0
    assert _enabled_rules_for(conn, "PREPROC_RT.CAST_SHORTHAND_V1") == 0
    assert _provenance(conn, "cast-shorthand", 1) is None
    exists = conn.execute(
        "SELECT COUNT(*) FROM EXA_ALL_SCRIPTS "
        "WHERE SCRIPT_SCHEMA = 'PREPROC_RT' AND SCRIPT_NAME = 'CAST_SHORTHAND_V1'"
    ).fetchval()
    assert exists == 0


@pytest.mark.integration
def test_cli_module_validate_live_smoke(tmp_path, module_env):
    """``validate`` with a DB scratch-deploys, smoke-tests, and tears down."""
    _make_module(tmp_path, "cast-shorthand", function="cast_shorthand")
    root = _write_index(tmp_path)

    code = run_module(_parse(["module", "validate", "cast-shorthand", "--registry", str(root)]))

    assert code == 0
    # The scratch objects must be gone afterward.
    for suffix in ("__VALIDATE", "__VALIDATE_PROBE"):
        exists = module_env.execute(
            "SELECT COUNT(*) FROM EXA_ALL_SCRIPTS WHERE SCRIPT_SCHEMA = 'PREPROC_RT' "
            "AND SCRIPT_NAME = {n}",
            {"n": f"CAST_SHORTHAND_V1{suffix}"},
        ).fetchval()
        assert exists == 0


@pytest.mark.integration
def test_cli_module_sync_idempotent(tmp_path, module_env):
    """Re-running sync with an unchanged desired file makes no changes."""
    conn = module_env
    user = _current_user(conn)
    _make_module(tmp_path, "cast-shorthand", function="cast_shorthand")
    root = _write_index(tmp_path)

    desired = tmp_path / "desired.toml"
    desired.write_text(
        f'[[modules]]\nname = "cast-shorthand"\nversion = 1\nscope = "USER:{user}"\n',
        encoding="utf-8",
    )

    first = run_module(_parse(["module", "sync", str(desired), "--registry", str(root)]))
    assert first == 0
    assert _enabled_rules_for(conn, "PREPROC_RT.CAST_SHORTHAND_V1") == 1

    rules_before = conn.execute(
        "SELECT rule_id, script_name FROM PREPROC.CONFIG ORDER BY rule_id"
    ).fetchall()

    second = run_module(_parse(["module", "sync", str(desired), "--registry", str(root)]))
    assert second == 0

    rules_after = conn.execute(
        "SELECT rule_id, script_name FROM PREPROC.CONFIG ORDER BY rule_id"
    ).fetchall()
    assert rules_after == rules_before, "an unchanged sync must not alter CONFIG"
    assert _enabled_rules_for(conn, "PREPROC_RT.CAST_SHORTHAND_V1") == 1


@pytest.mark.integration
def test_cli_module_sync_retire(tmp_path, module_env):
    """A module dropped from the desired file has its rule removed and row retired."""
    conn = module_env
    user = _current_user(conn)
    _make_module(tmp_path, "cast-shorthand", function="cast_shorthand")
    root = _write_index(tmp_path)

    synced = tmp_path / "desired.toml"
    synced.write_text(
        f'[[modules]]\nname = "cast-shorthand"\nversion = 1\nscope = "USER:{user}"\n',
        encoding="utf-8",
    )
    assert run_module(_parse(["module", "sync", str(synced), "--registry", str(root)])) == 0
    assert _enabled_rules_for(conn, "PREPROC_RT.CAST_SHORTHAND_V1") == 1

    empty = tmp_path / "empty.toml"
    empty.write_text("modules = []\n", encoding="utf-8")
    assert run_module(_parse(["module", "sync", str(empty), "--registry", str(root)])) == 0

    assert _enabled_rules_for(conn, "PREPROC_RT.CAST_SHORTHAND_V1") == 0
    assert _provenance(conn, "cast-shorthand", 1) == "retired"
