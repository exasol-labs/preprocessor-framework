"""Tests for the ``preproc bundle`` CLI verb and the offline bundle generator.

These tests are offline: they do not require an Exasol instance. The bundle is
pure file assembly, so the generator is exercised directly against the
repository sources and against synthetic sources installed via monkeypatched
path constants. The subprocess test invokes ``uv run preproc bundle`` to
exercise the installed entry point.
"""

import hashlib
import subprocess
from pathlib import Path

import pytest

from preproc import bundle
from preproc.module.artifact import split_statements
from preproc.module.offline_bundle import build_offline_bundle, run_offline_bundle
from preproc.module.registry import RegistryEntry, decode_objects

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
    "_SET_SYS_GROUPING_LUA_PATH",
    "_CONTROL_SUGAR_LUA_PATH",
    # Module lifecycle (added with add-module-ecosystem plan)
    "_RULE_ENSURE_LUA_PATH",
    "_MODULE_REGISTER_LUA_PATH",
    "_MODULE_RETIRE_LUA_PATH",
    "_MODULE_RECLAIM_LUA_PATH",
    # In-database module installer (added with add-in-db-module-install plan)
    "_MODULE_INSTALL_LUA_PATH",
    "_MODULE_CATALOG_LUA_PATH",
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

    resolver_udf_path = tmp_path / "udf" / "module_resolver_v1.sql"
    resolver_udf_path.write_text(
        "CREATE OR REPLACE PYTHON3 SET SCRIPT PREPROC.MODULE_RESOLVER_V1"
        "(src VARCHAR(2000), module_name VARCHAR(256)) EMITS (status VARCHAR(16)) AS\n"
        "import hashlib;\ndef run(ctx):\n    ctx.emit('OK')\n/\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(bundle, "_MODULE_RESOLVER_UDF_PATH", resolver_udf_path)

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
        "PREPROC.RULE_ENSURE",
        "PREPROC.MODULE_REGISTER",
        "PREPROC.MODULE_RETIRE",
        "PREPROC.MODULE_RECLAIM",
        "PREPROC.SET_MODE",
        "PREPROC.SET_SELF_DISABLE",
        "PREPROC.SET_SYS_GROUPING",
        "PREPROC.MODULE_INSTALL",
        "PREPROC.MODULE_CATALOG",
    ):
        assert f"CREATE OR REPLACE LUA SCRIPT {script}" in text, f"{script} not in bundle"
    # Convenience module
    assert "CREATE OR REPLACE LUA SCRIPT PREPROC_RT.CONTROL_SUGAR_V1" in text
    # In-database module installer's Python3 resolver UDF (full bundle only path
    # shared with the demo UDF's wrapper convention).
    assert "CREATE OR REPLACE PYTHON3 SET SCRIPT PREPROC.MODULE_RESOLVER_V1" in text


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

    # 3 DDL statements (2 schemas + 1 grant) + 22 Lua wrappers (20 control-layer/
    # module-lifecycle + MODULE_INSTALL + MODULE_CATALOG) + 2 UDFs (the demo UDF
    # + the in-database module installer's resolver UDF) = 27.
    assert count == 27


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


_WIDGET_ARTIFACT = (
    "--/\n"
    "CREATE OR REPLACE LUA SCRIPT PREPROC_RT.WIDGET_V1() AS\n"
    "function widget(sqltext)\n"
    "    local x = 1; return sqltext\n"
    "end;\n"
    "/\n"
    "\n"
    "--/\n"
    "CREATE OR REPLACE PYTHON3 SCALAR SCRIPT PREPROC_RT.WIDGET_HELPER_V1"
    "(x VARCHAR(100)) RETURNS VARCHAR(100) AS\n"
    "import re;\n"
    "def run(ctx):\n"
    "    return ctx.x\n"
    "/\n"
    "\n"
    "CREATE TABLE PREPROC_RT.WIDGET_CACHE_V1 (ID INT);\n"
)
_WIDGET_OBJECTS = (
    {"type": "LUA SCRIPT", "name": "PREPROC_RT.WIDGET_V1"},
    {"type": "PYTHON3 SCALAR SCRIPT", "name": "PREPROC_RT.WIDGET_HELPER_V1"},
    {"type": "TABLE", "name": "PREPROC_RT.WIDGET_CACHE_V1"},
)


def _write_composite_widget(tmp_path):
    """Write the synthetic 3-object composite module under ``tmp_path`` and index it.

    Returns the ``RegistryEntry`` a registry index would carry for it: a Lua
    entry script, a companion Python UDF, and a plain table (ADR-054/D3).
    """
    module_dir = tmp_path / "modules" / "widget"
    module_dir.mkdir(parents=True)
    (module_dir / "widget_v1.sql").write_text(_WIDGET_ARTIFACT, encoding="utf-8")
    return RegistryEntry(
        name="widget",
        version=1,
        phase="TRANSLATE",
        script_name="PREPROC_RT.WIDGET_V1",
        function="widget",
        description="widget module.",
        min_framework="1.0.0",
        suggested_scope={"type": "ROLE", "value": "PREPROC_ADMIN"},
        deploy_mode="library-deployed",
        source={"repo": "exasol-labs/preprocessor-library", "ref": "main"},
        sha256=hashlib.sha256(_WIDGET_ARTIFACT.encode("utf-8")).hexdigest(),
        artifact_path="modules/widget/widget_v1.sql",
        objects=_WIDGET_OBJECTS,
    )


def _declared_parameters(create_header):
    """The bare parameter names a generated ``CREATE ... SCRIPT`` header declares."""
    return [name.strip() for name in create_header.split("(", 1)[1].split(")", 1)[0].split(",")]


def _call_arguments(statement):
    """The top-level arguments of an ``EXECUTE SCRIPT x(...)`` call, verbatim.

    Commas inside a single-quoted literal (``''`` escapes a quote) do not
    separate arguments, so a literal carrying a comma or a newline stays one
    argument — which is how the emitted call is read by the database too.
    """
    inner = statement[statement.index("(") + 1 : statement.rindex(")")]
    args = [""]
    quoted = False
    for char in inner:
        if char == "'":
            quoted = not quoted
        if char == "," and not quoted:
            args.append("")
        else:
            args[-1] += char
    return [arg.strip() for arg in args]


def _literal_value(argument):
    """The Python string a rendered SQL literal argument carries."""
    assert argument.startswith("'") and argument.endswith("'"), argument
    return argument[1:-1].replace("''", "'")


def test_bundle_emits_composite_statements_with_delimiters(tmp_path):
    """A composite module's statements are each delimited so the bundle stays hand-runnable.

    Exercises ``preproc.module.offline_bundle.build_offline_bundle`` directly
    against a synthetic composite artifact (a Lua entry script, a companion
    Python UDF, and a plain table — ADR-054/D3). Verifies ADR-058 (D4): a
    statement whose body contains a semicolon is preserved VERBATIM inside a
    ``--/`` ... ``/`` block — never routed through the terminator stripper that
    would corrupt a trailing ``;`` the body's own syntax needs — while a plain
    object statement with no internal semicolon is ``;``-terminated instead.
    """
    entry = _write_composite_widget(tmp_path)

    body, count = build_offline_bundle([entry], str(tmp_path))

    # Every statement's body survives verbatim, including its own internal ';'.
    assert "local x = 1; return sqltext\nend;" in body, "block body must keep its own ';'"
    assert "import re;\ndef run(ctx):" in body, "UDF body's ';' preserved verbatim"
    assert "CREATE TABLE PREPROC_RT.WIDGET_CACHE_V1 (ID INT);" in body, (
        "a statement with no internal ';' is plain ';'-terminated"
    )
    # The two script statements are block-delimited; the table statement is not.
    assert body.count("--/\n") == 2
    slash_lines = [ln for ln in body.splitlines() if ln == "/"]
    assert len(slash_lines) == 2
    # 3 artifact statements + RULE_ENSURE + MODULE_REGISTER + closing REFRESH.
    assert count == 6


def test_bundle_register_call_matches_the_installed_script_signature(tmp_path):
    """The generated MODULE_REGISTER call supplies exactly the parameters the script declares.

    The air-gap path aborts AFTER every artifact statement has committed if the
    generated call is short an argument, leaving the module installed but
    unregistered — so the argument count is compared against the parameter list
    the release bundle's own ``CREATE ... PREPROC.MODULE_REGISTER`` header
    declares, not against a hard-coded number that could drift with it.
    """
    entry = _write_composite_widget(tmp_path)

    body, _count = build_offline_bundle([entry], str(tmp_path))

    calls = [
        statement
        for statement in split_statements(body)
        if statement.startswith("EXECUTE SCRIPT PREPROC.MODULE_REGISTER(")
    ]
    assert len(calls) == 1
    parameters = _declared_parameters(bundle._script_statements()["MODULE_REGISTER"])
    arguments = _call_arguments(calls[0])
    assert len(arguments) == len(parameters), (
        f"PREPROC.MODULE_REGISTER declares {len(parameters)} parameters {parameters} "
        f"but the bundle supplies {len(arguments)}: {arguments}"
    )


def test_bundle_register_call_carries_the_encoded_inventory(tmp_path):
    """A composite module's recorded inventory is emitted, not left as SQL NULL.

    The inventory is the ownership boundary and MODULE_RECLAIM's undo list, so a
    bundle that registers NULL would orphan every companion object of a module
    installed from it. The encoded value must round-trip through
    ``decode_objects`` back to the declared inventory, in file order.
    """
    entry = _write_composite_widget(tmp_path)

    body, _count = build_offline_bundle([entry], str(tmp_path))

    call = next(
        statement
        for statement in split_statements(body)
        if statement.startswith("EXECUTE SCRIPT PREPROC.MODULE_REGISTER(")
    )
    objects_argument = _call_arguments(call)[-1]
    assert objects_argument != "NULL", "a composite module's inventory must not be recorded as NULL"
    assert decode_objects(_literal_value(objects_argument)) == [dict(o) for o in _WIDGET_OBJECTS]


def test_bundle_multiline_inventory_literal_stays_one_statement(tmp_path):
    """The multi-line inventory literal keeps the file hand-runnable in EXAplus.

    The encoded inventory is newline-separated, so it renders as a SQL literal
    spanning lines. Re-parsing the generated file with the framework's own
    model of the EXAplus grammar (``artifact.split_statements`` — the same code
    embedded in the in-database resolver) must yield exactly the statements the
    generator counted, with the register call whole. Every continuation line of
    the literal is additionally inert to a naive ``;``-scanning client: it
    carries no terminator, no comment marker, and is neither block marker.
    """
    entry = _write_composite_widget(tmp_path)

    body, count = build_offline_bundle([entry], str(tmp_path))

    statements = split_statements(body)
    assert len(statements) == count, "the file must re-parse into exactly `count` statements"

    call = next(s for s in statements if s.startswith("EXECUTE SCRIPT PREPROC.MODULE_REGISTER("))
    inventory = _literal_value(_call_arguments(call)[-1])
    lines = inventory.split("\n")
    assert len(lines) == len(_WIDGET_OBJECTS), "one inventory line per object"
    for line in lines:
        assert ";" not in line, f"an inventory line must not carry a terminator: {line!r}"
        assert "--" not in line, f"an inventory line must not open a comment: {line!r}"
        assert line.strip() not in ("/", "--/"), f"an inventory line must not be a marker: {line!r}"


def test_bundle_file_separates_inventory_lines_with_lf(tmp_path):
    """The written file uses LF, so the inventory literal never carries a CR.

    ``module_reclaim.lua`` splits the recorded inventory on ``[^\\n]+``. A CRLF
    file would leave a trailing ``\\r`` on every object name but the last, and
    reclaim's DROP would name an object that does not exist — so the generator
    must not inherit the platform line separator. Guards the Windows path from
    any platform.
    """
    entry = _write_composite_widget(tmp_path)
    out = tmp_path / "off.sql"

    run_offline_bundle([entry], str(tmp_path), out)

    raw = out.read_bytes()
    assert b"\r" not in raw, "the bundle must be written with LF endings on every platform"
    assert b"'LUA SCRIPT|PREPROC_RT.WIDGET_V1\nPYTHON3 SCALAR SCRIPT|" in raw


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
        "CREATE OR REPLACE LUA SCRIPT PREPROC.RULE_ENSURE(",
        "CREATE OR REPLACE LUA SCRIPT PREPROC.MODULE_REGISTER(",
        "CREATE OR REPLACE LUA SCRIPT PREPROC.MODULE_RETIRE(",
        "CREATE OR REPLACE LUA SCRIPT PREPROC.MODULE_RECLAIM(",
        "CREATE OR REPLACE LUA SCRIPT PREPROC.SET_MODE(",
        "CREATE OR REPLACE LUA SCRIPT PREPROC.SET_SYS_GROUPING(",
        # In-database module installer (resolver UDF + orchestrator + catalog):
        # a product feature, not a demo, so it ships in the release bundle too
        # (unlike DEMO_SQLGLOT_TRANSPILE_V1, which is full-bundle only).
        "CREATE OR REPLACE PYTHON3 SET SCRIPT PREPROC.MODULE_RESOLVER_V1(",
        "CREATE OR REPLACE LUA SCRIPT PREPROC.MODULE_INSTALL(",
        "CREATE OR REPLACE LUA SCRIPT PREPROC.MODULE_CATALOG(",
    ):
        assert present in text, f"release bundle missing {present!r}"

    # No demo module is *deployed* (framework comments may mention demo names —
    # e.g. master.lua's safety contract — so match on the CREATE wrapper, not the
    # bare substring).
    assert "CREATE OR REPLACE LUA SCRIPT PREPROC_RT.DEMO_" not in text, (
        "release bundle must not deploy any demo module"
    )
    assert "CREATE OR REPLACE PYTHON3 SCALAR SCRIPT" not in text, (
        "release bundle must not include the demo UDF"
    )
    assert "ADD COLUMN" not in text, "release bundle must omit upgrade-only column ALTERs"
    # The upgrade file also carries a WIDENING (ALTER TABLE ... MODIFY) for a
    # column an existing install already has; the bundle is clean-install only, so
    # 22_modules_table.sql declares the final width and the MODIFY has no place
    # here either.
    assert "MODIFY (" not in text, "release bundle must omit upgrade-only column widenings"

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


def test_release_bundle_seeds_sugar_for_preproc_admin_and_dba(tmp_path):
    """The control-sugar bootstrap is seeded for BOTH PREPROC_ADMIN and DBA.

    SYS (and other superusers) cannot be granted PREPROC_ADMIN, but they hold DBA
    and resolution walks the role-grant graph — so scoping the sugar to DBA as
    well lets SYS/DBA holders use the `PREPROC ...` commands out of the box. This
    guards that both ROLE-scoped seed rows ship in the release artifact.
    """
    out = tmp_path / "release.sql"
    bundle.run_release_bundle(out)
    text = out.read_text()

    # Two CONTROL_SUGAR_V1 EXPAND seed INSERTs, one per role.
    assert text.count("'PREPROC_RT.CONTROL_SUGAR_V1',") >= 2, (
        "expected a control-sugar seed row for each of PREPROC_ADMIN and DBA"
    )
    assert "'PREPROC_ADMIN'," in text, "missing PREPROC_ADMIN-scoped control-sugar seed"
    assert "'DBA'," in text, "missing DBA-scoped control-sugar seed"


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
