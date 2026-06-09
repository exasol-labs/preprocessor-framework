"""Tests for the ``preproc bundle`` CLI verb and the offline bundle generator.

These tests are offline: they do not require an Exasol instance. The bundle is
pure file assembly, so the generator is exercised directly against the
repository sources and against synthetic sources installed via monkeypatched
path constants. The subprocess test invokes ``uv run preproc bundle`` to
exercise the installed entry point.
"""

import subprocess
from pathlib import Path

import pytest

from preproc import bundle

_REPO_ROOT = Path(__file__).resolve().parents[1]

_LUA_PATH_ATTRS = (
    "_MASTER_LUA_PATH",
    "_DEMO_PING_LUA_PATH",
    "_DEMO_TRANSLATE_LUA_PATH",
    "_DEMO_AUDIT_LUA_PATH",
    "_REFRESH_LUA_PATH",
    "_STATUS_LUA_PATH",
    # Control layer (added with add-control-layer plan)
    "_REFRESH_CORE_LUA_PATH",
    "_RULE_ADD_LUA_PATH",
    "_RULE_DROP_LUA_PATH",
    "_RULE_ENABLE_LUA_PATH",
    "_RULE_DISABLE_LUA_PATH",
    "_RULE_LIST_LUA_PATH",
    "_SET_MODE_LUA_PATH",
    "_SET_SELF_DISABLE_LUA_PATH",
    "_CONTROL_SUGAR_LUA_PATH",
)


def _install_synthetic_sources(monkeypatch, tmp_path):
    """Point bundle's path constants at a self-contained synthetic source tree.

    Returns the temp directory so callers can add or remove files to drive
    missing-source and content assertions. Lua bodies and the UDF body each
    contain a semicolon so the slash-delimiter requirement is meaningful.
    """
    install_dir = tmp_path / "install"
    install_dir.mkdir()
    (install_dir / "10_schemas.sql").write_text(
        "-- schemas\n"
        "CREATE SCHEMA IF NOT EXISTS PREPROC;\n\n"
        "CREATE SCHEMA IF NOT EXISTS PREPROC_RT;\n",
        encoding="utf-8",
    )
    (install_dir / "50_grants.sql").write_text(
        "GRANT SELECT ON SCHEMA PREPROC_RT TO PUBLIC;\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(bundle, "_SQL_INSTALL_DIR", install_dir)

    lua_dir = tmp_path / "lua"
    lua_dir.mkdir()
    for attr in _LUA_PATH_ATTRS:
        name = attr.replace("_LUA_PATH", "").strip("_").lower()
        path = lua_dir / f"{name}.lua"
        path.write_text(f"-- {name}\nlocal x = 1; return x\n", encoding="utf-8")
        monkeypatch.setattr(bundle, attr, path)

    udf_path = tmp_path / "udf" / "demo_sqlglot_transpile_v1.sql"
    udf_path.parent.mkdir()
    # The real UDF source (Group A) ships its own trailing ``/`` terminator
    # line for direct EXAplus use; the synthetic source mirrors that so the
    # generator's single-terminator guarantee is exercised.
    udf_path.write_text(
        "CREATE OR REPLACE PYTHON3 SCALAR SCRIPT PREPROC_RT.DEMO_SQLGLOT_TRANSPILE_V1"
        "(dialect VARCHAR(64), txt VARCHAR(2000)) RETURNS VARCHAR(2000) AS\n"
        "import sqlglot;\ndef run(ctx):\n    return ctx.txt\n/\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(bundle, "_DEMO_UDF_PATH", udf_path)

    return tmp_path


def test_bundle_emits_single_file(monkeypatch, tmp_path):
    _install_synthetic_sources(monkeypatch, tmp_path)
    out = tmp_path / "deploy_all.sql"

    count = bundle.run_bundle(out)

    assert out.exists()
    assert count > 0


def test_bundle_contains_install_ddl_in_order(monkeypatch, tmp_path):
    _install_synthetic_sources(monkeypatch, tmp_path)
    out = tmp_path / "deploy_all.sql"

    bundle.run_bundle(out)
    text = out.read_text(encoding="utf-8")

    schema_pos = text.index("CREATE SCHEMA IF NOT EXISTS PREPROC")
    grant_pos = text.index("GRANT SELECT ON SCHEMA PREPROC_RT TO PUBLIC")
    assert schema_pos < grant_pos, "10_ DDL must precede 50_ DDL"


def test_bundle_wraps_scripts_and_udf(monkeypatch, tmp_path):
    _install_synthetic_sources(monkeypatch, tmp_path)
    out = tmp_path / "deploy_all.sql"

    bundle.run_bundle(out)
    text = out.read_text(encoding="utf-8")

    assert "CREATE OR REPLACE LUA SCRIPT PREPROC_RT.MASTER() AS" in text
    assert "CREATE OR REPLACE LUA SCRIPT PREPROC_RT.DEMO_PING_V1() AS" in text
    assert "CREATE OR REPLACE LUA SCRIPT PREPROC_RT.DEMO_TRANSLATE_V1() AS" in text
    assert "CREATE OR REPLACE LUA SCRIPT PREPROC_RT.DEMO_AUDIT_V1() AS" in text
    assert "CREATE OR REPLACE LUA SCRIPT PREPROC.REFRESH() RETURNS TABLE AS" in text
    assert "CREATE OR REPLACE LUA SCRIPT PREPROC.STATUS() RETURNS TABLE AS" in text
    assert "CREATE OR REPLACE PYTHON3 SCALAR SCRIPT PREPROC_RT.DEMO_SQLGLOT_TRANSPILE_V1" in text


def test_bundle_wraps_control_layer(monkeypatch, tmp_path):
    _install_synthetic_sources(monkeypatch, tmp_path)
    out = tmp_path / "deploy_all.sql"
    bundle.run_bundle(out)
    text = out.read_text(encoding="utf-8")
    # Admin scripts
    for script in (
        "PREPROC.REFRESH_CORE",
        "PREPROC.RULE_ADD",
        "PREPROC.RULE_DROP",
        "PREPROC.RULE_ENABLE",
        "PREPROC.RULE_DISABLE",
        "PREPROC.RULE_LIST",
        "PREPROC.SET_MODE",
        "PREPROC.SET_SELF_DISABLE",
    ):
        assert f"CREATE OR REPLACE LUA SCRIPT {script}" in text, f"{script} not in bundle"
    # Convenience module
    assert "CREATE OR REPLACE LUA SCRIPT PREPROC_RT.CONTROL_SUGAR_V1" in text


def test_bundle_uses_slash_delimiters(monkeypatch, tmp_path):
    _install_synthetic_sources(monkeypatch, tmp_path)
    out = tmp_path / "deploy_all.sql"

    count = bundle.run_bundle(out)
    text = out.read_text(encoding="utf-8")

    slash_lines = [ln for ln in text.splitlines() if ln == "/"]
    assert len(slash_lines) == count, "one bare-/ terminator line per statement"
    # No statement is terminated with a trailing semicolon immediately before /.
    assert ";\n/" not in text


def test_bundle_strips_trailing_semicolon_from_ddl(monkeypatch, tmp_path):
    _install_synthetic_sources(monkeypatch, tmp_path)
    out = tmp_path / "deploy_all.sql"

    bundle.run_bundle(out)
    text = out.read_text(encoding="utf-8")

    assert "CREATE SCHEMA IF NOT EXISTS PREPROC_RT;" not in text
    assert "CREATE SCHEMA IF NOT EXISTS PREPROC_RT\n/" in text


def test_bundle_strips_source_owned_slash_terminator(monkeypatch, tmp_path):
    """A source that already ends with a ``/`` line yields exactly one terminator.

    The synthetic UDF ships its own trailing ``/``; the generator must own the
    single terminator and never emit two consecutive ``/`` lines.
    """
    _install_synthetic_sources(monkeypatch, tmp_path)
    out = tmp_path / "deploy_all.sql"

    bundle.run_bundle(out)
    text = out.read_text(encoding="utf-8")

    assert "/\n/\n" not in text, "no doubled terminator lines"
    assert "return ctx.txt\n/\n\n" in text


def test_bundle_reproducible_no_activation_no_seed(monkeypatch, tmp_path):
    _install_synthetic_sources(monkeypatch, tmp_path)
    out1 = tmp_path / "a.sql"
    out2 = tmp_path / "b.sql"

    bundle.run_bundle(out1)
    bundle.run_bundle(out2)

    b1 = out1.read_bytes()
    b2 = out2.read_bytes()
    assert b1 == b2, "bundle must be byte-for-byte reproducible"
    assert b"ALTER SYSTEM SET sql_preprocessor_script" not in b1
    assert b"ALTER SYSTEM" not in b1
    assert b"INSERT INTO PREPROC.CONFIG" not in b1


def test_bundle_count_matches_statements(monkeypatch, tmp_path):
    _install_synthetic_sources(monkeypatch, tmp_path)
    out = tmp_path / "deploy_all.sql"

    count = bundle.run_bundle(out)

    # 3 DDL statements (2 schemas + 1 grant) + 15 Lua wrappers + 1 UDF = 19.
    assert count == 19


def test_bundle_missing_lua_source_raises(monkeypatch, tmp_path):
    _install_synthetic_sources(monkeypatch, tmp_path)
    bundle._DEMO_TRANSLATE_LUA_PATH.unlink()
    out = tmp_path / "deploy_all.sql"

    with pytest.raises(FileNotFoundError) as exc:
        bundle.run_bundle(out)

    assert "demo_translate_v1.lua" in str(exc.value)


def test_bundle_missing_udf_source_raises(monkeypatch, tmp_path):
    _install_synthetic_sources(monkeypatch, tmp_path)
    bundle._DEMO_UDF_PATH.unlink()
    out = tmp_path / "deploy_all.sql"

    with pytest.raises(FileNotFoundError) as exc:
        bundle.run_bundle(out)

    assert "demo_sqlglot_transpile_v1.sql" in str(exc.value)


def test_bundle_no_install_files_raises(monkeypatch, tmp_path):
    _install_synthetic_sources(monkeypatch, tmp_path)
    for f in bundle._SQL_INSTALL_DIR.glob("*.sql"):
        f.unlink()
    out = tmp_path / "deploy_all.sql"

    with pytest.raises(FileNotFoundError):
        bundle.run_bundle(out)


def test_cli_bundle_help():
    """``preproc bundle --help`` exits 0 and documents --output."""
    result = subprocess.run(
        ["uv", "run", "preproc", "bundle", "--help"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "--output" in result.stdout


def test_release_bundle_is_framework_only(tmp_path):
    """run_release_bundle emits the framework + control surface, no demos/UDF.

    Exercised against the real repository sources (the release artifact CI ships):
    it must contain the dispatcher, the command sugar, and the admin control
    surface; must NOT contain any demo module or the demo UDF; must omit the
    upgrade-only column ALTERs; and must end with a REFRESH so the seeded
    control-sugar rule resolves on a clean install.
    """
    out = tmp_path / "release.sql"
    count = bundle.run_release_bundle(out)
    text = out.read_text()

    assert count > 0
    for present in (
        "CREATE OR REPLACE LUA SCRIPT PREPROC_RT.MASTER()",
        "CREATE OR REPLACE LUA SCRIPT PREPROC_RT.CONTROL_SUGAR_V1()",
        "CREATE OR REPLACE LUA SCRIPT PREPROC.REFRESH_CORE()",
        "CREATE OR REPLACE LUA SCRIPT PREPROC.RULE_ADD(",
        "CREATE OR REPLACE LUA SCRIPT PREPROC.SET_MODE(",
    ):
        assert present in text, f"release bundle missing {present!r}"

    # No demo module is *deployed* (framework comments may mention demo names —
    # e.g. master.lua's safety contract — so match on the CREATE wrapper, not the
    # bare substring).
    assert "CREATE OR REPLACE LUA SCRIPT PREPROC_RT.DEMO_" not in text, (
        "release bundle must not deploy any demo module"
    )
    assert "PYTHON3 SCALAR SCRIPT" not in text, "release bundle must not include the demo UDF"
    assert "ADD COLUMN" not in text, "release bundle must omit upgrade-only column ALTERs"

    # Delimiter contract (EXAplus style): plain SQL is ';'-terminated; each Lua
    # script CREATE is wrapped between a '--/' line and a lone '/' line so the
    # semicolons inside the body are not treated as terminators.
    assert "CREATE ROLE PREPROC_ADMIN;" in text, "install DDL must be ';'-terminated"
    assert "--/\nCREATE OR REPLACE LUA SCRIPT PREPROC_RT.MASTER() AS" in text, (
        "script bodies must be wrapped in a --/ ... / block"
    )
    # The file ends with the REFRESH (plain SQL, ';'-terminated) that resolves the
    # seeded control-sugar rule.
    assert text.rstrip().endswith("EXECUTE SCRIPT PREPROC.REFRESH;"), (
        "release bundle must end with a ';'-terminated REFRESH"
    )


def test_cli_bundle_release_flag(tmp_path):
    """``preproc bundle --release`` writes a framework-only file via the CLI."""
    out = tmp_path / "release.sql"
    result = subprocess.run(
        ["uv", "run", "preproc", "bundle", "--release", "--output", str(out)],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    text = out.read_text()
    assert "PREPROC_RT.CONTROL_SUGAR_V1()" in text
    assert "CREATE OR REPLACE LUA SCRIPT PREPROC_RT.DEMO_" not in text
