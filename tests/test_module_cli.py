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
from preproc.module.artifact import derive_object
from preproc.module.commands import add_module_parser, run_module
from preproc.module.deploy import deploy_module, drop_sql
from preproc.module.manifest import ObjectSpec
from preproc.module.registry import (
    decode_objects,
    generate_index,
    read_registry,
    render_index,
)
from preproc.module.sync import DesiredModule, converge, update_module
from preproc.module.validate import ValidateError, validate_live

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


def _write_index(root, *, library_version=None):
    index = generate_index(
        root, repo="exasol-labs/preprocessor-library", library_version=library_version
    )
    registry_dir = root / "registry"
    registry_dir.mkdir(exist_ok=True)
    (registry_dir / "index.json").write_text(render_index(index), encoding="utf-8")
    return root


def _two_module_registry(root):
    _make_module(root, "cast_shorthand", description="cast shorthand transform")
    _make_module(root, "trailing_comma", description="trailing comma cleanup")
    return _write_index(root)


def _write_composite_module(
    root,
    name,
    *,
    version=1,
    phase="TRANSLATE",
    function=None,
    statements,
    objects,
    scope=("ROLE", "PREPROC_ADMIN"),
):
    """Write a composite modules/<name>/{module.toml, <name>_v<N>.sql} pair.

    ``statements`` are the bare SQL bodies (no terminator, no ``--/`` markers);
    each is wrapped in the ``--/`` ... ``/`` block form when its body contains
    a semicolon and plain ``;``-terminated otherwise, matching the same D4
    grammar the splitter parses. ``objects`` is the declared ``[[objects]]``
    inventory (a list of ``{"type", "name"}`` dicts); the first entry's
    ``name`` is used as the manifest's ``script_name``.
    """
    function = function or name.replace("-", "_")
    module_dir = root / "modules" / name
    module_dir.mkdir(parents=True, exist_ok=True)

    parts = [f"--/\n{stmt}\n/" if ";" in stmt else f"{stmt};" for stmt in statements]
    artifact = "\n\n".join(parts) + "\n"
    (module_dir / f"{name}_v{version}.sql").write_text(artifact, encoding="utf-8")

    lines = [
        f'name = "{name}"',
        f'description = "{name} composite module."',
        f'phase = "{phase}"',
        f'script_name = "{objects[0]["name"]}"',
        f'function = "{function}"',
        f"version = {version}",
        'min_framework = "1.0.0"',
        'deploy_mode = "library-deployed"',
        f'sha256 = "{hashlib.sha256(artifact.encode("utf-8")).hexdigest()}"',
        "",
        "[suggested_scope]",
        f'type = "{scope[0]}"',
        f'value = "{scope[1]}"',
    ]
    for obj in objects:
        lines += ["", "[[objects]]", f'type = "{obj["type"]}"', f'name = "{obj["name"]}"']
    (module_dir / "module.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return artifact


def _object_exists(conn, obj):
    """True if the inventory object exists, checked via the matching ``EXA_ALL_*`` view."""
    obj_type, name = obj["type"], obj["name"]
    if obj_type == "CONNECTION":
        return bool(
            conn.execute(
                "SELECT COUNT(*) FROM EXA_ALL_CONNECTIONS WHERE CONNECTION_NAME = {n}",
                {"n": name},
            ).fetchval()
        )
    view = {
        "TABLE": "EXA_ALL_TABLES",
        "VIEW": "EXA_ALL_VIEWS",
    }.get(obj_type)
    if view is None and obj_type.endswith("SCRIPT"):
        view = "EXA_ALL_SCRIPTS"
    if view is None:
        raise AssertionError(f"no inventory check known for object type {obj_type!r}")
    column_prefix = {"EXA_ALL_TABLES": "TABLE", "EXA_ALL_VIEWS": "VIEW"}.get(view, "SCRIPT")
    schema, _, ident = name.partition(".")
    return bool(
        conn.execute(
            f"SELECT COUNT(*) FROM {view} "
            f"WHERE {column_prefix}_SCHEMA = {{s}} AND {column_prefix}_NAME = {{n}}",
            {"s": schema, "n": ident},
        ).fetchval()
    )


def _drop_object_if_present(conn, obj):
    if _object_exists(conn, obj):
        conn.execute(drop_sql(ObjectSpec(type=obj["type"], name=obj["name"])))


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
    assert "cast_shorthand" in out
    assert "trailing_comma" in out
    assert "TRANSLATE" in out
    assert "library-deployed" in out
    assert "v1" in out


def test_cli_module_search(tmp_path, capsys, no_db):
    """``search cast`` prints cast_shorthand but not trailing_comma."""
    root = _two_module_registry(tmp_path)

    code = run_module(_parse(["module", "search", "cast", "--registry", str(root)]))

    assert code == 0
    out = capsys.readouterr().out
    assert "cast_shorthand" in out
    assert "trailing_comma" not in out


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
                "cast_shorthand",
                "trailing_comma",
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
    # Each provenance write ENDS with the module's encoded inventory — for a
    # single-statement artifact, the one entry object — and never SQL NULL, or a
    # later reclaim would have no undo list to work from.
    for script in ("PREPROC_RT.CAST_SHORTHAND_V1", "PREPROC_RT.TRAILING_COMMA_V1"):
        assert f"'LUA SCRIPT|{script}');" in text, (
            f"MODULE_REGISTER must record {script} as the module's inventory"
        )
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


def test_validate_rejects_script_name_and_inventory_mismatch(tmp_path, capsys, no_db):
    """`validate` rejects an artifact/script_name mismatch AND an [[objects]] mismatch.

    The manifest-level ``script_name`` check (pre-composite behaviour) and the
    composite ``[[objects]]``-vs-derived check are two different failure modes
    of the same ``plan_artifact`` pre-flight; both must be caught offline.
    """
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

    # [[objects]] deliberately omits the TABLE the artifact actually creates.
    _write_composite_module(
        tmp_path,
        "mismatch",
        function="mismatch",
        statements=[
            "CREATE OR REPLACE LUA SCRIPT PREPROC_RT.MISMATCH_V1() AS\n"
            "function mismatch(sqltext)\n    return sqltext\nend",
            "CREATE TABLE PREPROC_RT.MISMATCH_CACHE_V1 (K VARCHAR(64))",
        ],
        objects=[{"type": "LUA SCRIPT", "name": "PREPROC_RT.MISMATCH_V1"}],
    )
    root = _write_index(tmp_path)

    code = run_module(_parse(["module", "validate", "mismatch", "--registry", str(root)]))

    assert code != 0
    err = capsys.readouterr().err
    assert "MISMATCH_CACHE_V1" in err
    assert "does not declare it" in err


def test_validate_rejects_undeclared_python_udfs(tmp_path, capsys, no_db):
    """`validate` rejects a composite artifact whose Python UDFs are undeclared, naming both.

    The module.toml declares only the Lua entry script; the artifact also
    creates two Python UDF companions that never appear in ``[[objects]]``.
    """
    _write_composite_module(
        tmp_path,
        "undeclared",
        function="undeclared",
        statements=[
            "CREATE OR REPLACE LUA SCRIPT PREPROC_RT.UNDECLARED_V1() AS\n"
            "function undeclared(sqltext)\n    return sqltext\nend",
            "CREATE OR REPLACE PYTHON3 SCALAR SCRIPT PREPROC_RT.UNDECLARED_HELPER1_V1"
            "(x VARCHAR(2000)) RETURNS VARCHAR(2000) AS\ndef run(ctx):\n    return ctx.x",
            "CREATE OR REPLACE PYTHON3 SCALAR SCRIPT PREPROC_RT.UNDECLARED_HELPER2_V1"
            "(x VARCHAR(2000)) RETURNS VARCHAR(2000) AS\ndef run(ctx):\n    return ctx.x",
        ],
        objects=[{"type": "LUA SCRIPT", "name": "PREPROC_RT.UNDECLARED_V1"}],
    )
    root = _write_index(tmp_path)

    code = run_module(_parse(["module", "validate", "undeclared", "--registry", str(root)]))

    assert code != 0
    err = capsys.readouterr().err
    assert "UNDECLARED_HELPER1_V1" in err
    assert "UNDECLARED_HELPER2_V1" in err
    assert "does not declare it" in err


# ---------------------------------------------------------------------------
# validate — live path, offline against a recording fake connection
#
# The live path's whole burden is WHICH statements it sends: it must never name
# a real object of the module it is validating, never create anything outside
# its own disposable schema, and drop everything it created. Those are negative,
# statement-level properties, so a recording fake proves them where a live
# database could only sample them — the live integration tests below then
# confirm the same properties against real catalog state.
# ---------------------------------------------------------------------------


class _ValidateDbError(Exception):
    """Stands in for the driver error a statement the database rejects raises."""


class _ValidateResult:
    def __init__(self, rettype):
        self._rettype = rettype

    def fetchall(self):
        return [[self._rettype]]


class _ValidateConnection:
    """Records every statement and answers the probe with a Lua return type.

    Covers exactly the slice of ``pyexasol.ExaConnection`` the live validate
    path uses. ``fail_on`` holds substrings whose statement must raise, which is
    how a scratch statement the database rejects — and the teardown that must
    still run — are provoked with no database.
    """

    def __init__(self, *, rettype="nil", fail_on=()):
        self.executed: list[str] = []
        self._rettype = rettype
        self._fail_on = tuple(fail_on)

    def execute(self, query, query_params=None):
        self.executed.append(query)
        for needle in self._fail_on:
            if needle in query:
                raise _ValidateDbError(f"object not found: {needle}")
        return _ValidateResult(self._rettype)


def _live_entry(root, name):
    """The registry entry live validation is run against, read back from the checkout."""
    return next(e for e in read_registry(str(root)).entries if e.name == name)


def _created(conn):
    """Every object the recorded statements create, in execution order.

    Read with the canonical grammar itself — which sees through a leading
    comment and, by design, RAISES on a ``CREATE`` whose object name it cannot
    parse back. A statement in that state is one no database should have been
    asked to run, so the raise is the test failing.
    """
    created = []
    for sql in conn.executed:
        derived = derive_object(sql)
        if derived is not None:
            created.append(ObjectSpec(type=derived["type"], name=derived["name"]))
    return created


def _assert_creates_only_in_one_disposable_schema(conn):
    """Exactly one schema is created, and every other created object lives inside it."""
    created = _created(conn)
    schemas = [obj.name for obj in created if obj.type == "SCHEMA"]
    assert len(schemas) == 1, f"live validation must create exactly one schema, got {schemas}"
    scratch = schemas[0]
    assert scratch.startswith("PREPROC_VALIDATE_")
    for obj in created:
        assert obj.name == scratch or obj.name.startswith(f"{scratch}."), (
            f"{obj} was created outside the disposable schema {scratch}"
        )
    return scratch


def _assert_teardown_reverses_creation(conn):
    """Everything created is dropped, in exact reverse order, and nothing else is."""
    dropped = [sql for sql in conn.executed if sql.lstrip().upper().startswith("DROP")]
    assert dropped == [drop_sql(obj) for obj in reversed(_created(conn))]


_WIDGETV_OBJECTS = [
    {"type": "LUA SCRIPT", "name": "PREPROC_RT.WIDGETV_V1"},
    {"type": "TABLE", "name": "PREPROC_RT.WIDGETV_CACHE_V1"},
    {"type": "VIEW", "name": "PREPROC_RT.WIDGETV_VIEW_V1"},
]
_WIDGETV_STATEMENTS = [
    "CREATE OR REPLACE LUA SCRIPT PREPROC_RT.WIDGETV_V1() AS\n"
    "function widgetv(sqltext)\n    return sqltext\nend",
    "CREATE TABLE PREPROC_RT.WIDGETV_CACHE_V1 (K VARCHAR(64))",
    "CREATE VIEW PREPROC_RT.WIDGETV_VIEW_V1 AS SELECT K FROM PREPROC_RT.WIDGETV_CACHE_V1",
]


def test_validate_live_never_names_a_cross_referenced_companion(tmp_path):
    """A VIEW selecting from a companion TABLE must not drag the real table into validation.

    The composite shape the contract exists to enable: statement 3 references
    statement 2 BY NAME. Renaming only each statement's own creation site would
    leave that reference pointing at the real ``PREPROC_RT.WIDGETV_CACHE_V1`` —
    absent on a clean database, so a perfectly conformant module would fail
    validation, and present on an installed one, so validation would read live
    data. Neither statement is executed at all: the reference is never resolved
    and the real names never appear in anything sent to the database.
    """
    _write_composite_module(
        tmp_path,
        "widgetv",
        function="widgetv",
        statements=_WIDGETV_STATEMENTS,
        objects=_WIDGETV_OBJECTS,
    )
    root = _write_index(tmp_path)
    conn = _ValidateConnection()

    result = validate_live(conn, _live_entry(root, "widgetv"), str(root))

    _assert_creates_only_in_one_disposable_schema(conn)
    _assert_teardown_reverses_creation(conn)
    for real in ("WIDGETV_CACHE_V1", "WIDGETV_VIEW_V1", "WIDGETV_V1"):
        assert not any(real in sql for sql in conn.executed), (
            f"{real} is a real object of the module and must never be named"
        )
    assert "not deployed" in result


_WIDGETHOME_OBJECTS = [
    {"type": "LUA SCRIPT", "name": "PREPROC_RT.WIDGETHOME_V1"},
    {"type": "SCHEMA", "name": "WIDGETHOME_HOME_V1"},
    {"type": "TABLE", "name": "WIDGETHOME_HOME_V1.CACHE_V1"},
]
_WIDGETHOME_STATEMENTS = [
    "CREATE OR REPLACE LUA SCRIPT PREPROC_RT.WIDGETHOME_V1() AS\n"
    "function widgethome(sqltext)\n    return sqltext\nend",
    "CREATE SCHEMA WIDGETHOME_HOME_V1",
    "CREATE TABLE WIDGETHOME_HOME_V1.CACHE_V1 (K VARCHAR(64))",
]


def test_validate_live_never_writes_into_a_module_owned_schema(tmp_path):
    """A module that creates its own schema must not have objects put in the real one.

    Suffixing each creation site would create schema ``…_V1__VALIDATE`` and
    then a table in the UNSUFFIXED, real ``WIDGETHOME_HOME_V1`` — a spurious
    failure when that schema is absent and, far worse, a real object inside a
    live schema when it is present. A module-owned schema has no schema part to
    redirect, so its statements are not executed and its name is never sent.
    """
    _write_composite_module(
        tmp_path,
        "widgethome",
        function="widgethome",
        statements=_WIDGETHOME_STATEMENTS,
        objects=_WIDGETHOME_OBJECTS,
    )
    root = _write_index(tmp_path)
    conn = _ValidateConnection()

    validate_live(conn, _live_entry(root, "widgethome"), str(root))

    _assert_creates_only_in_one_disposable_schema(conn)
    _assert_teardown_reverses_creation(conn)
    assert not any("WIDGETHOME_HOME_V1" in sql for sql in conn.executed), (
        "the module's own schema must never be created, opened, or written into"
    )


# The declared name is written into a TOML basic string, so its quotes are
# backslash-escaped there; it reaches the manifest as PREPROC_RT."odd.name_V1"
# and canonicalises to PREPROC_RT.odd.name_V1 (a dot inside the OBJECT part is
# admitted by the contract — only one inside the SCHEMA part is not).
_WIDGETQ_OBJECTS = [
    {"type": "LUA SCRIPT", "name": "PREPROC_RT.WIDGETQ_V1"},
    {"type": "PYTHON3 SCALAR SCRIPT", "name": 'PREPROC_RT.\\"odd.name_V1\\"'},
]
_WIDGETQ_STATEMENTS = [
    "CREATE OR REPLACE LUA SCRIPT PREPROC_RT.WIDGETQ_V1() AS\n"
    "function widgetq(sqltext)\n    return sqltext\nend",
    'CREATE OR REPLACE PYTHON3 SCALAR SCRIPT PREPROC_RT."odd.name_V1"'
    "(x VARCHAR(2000)) RETURNS VARCHAR(2000) AS\ndef run(ctx):\n    return ctx.x",
]


def test_validate_live_redirects_a_quoted_object_name_intact(tmp_path):
    """A quoted, dot-carrying object name survives the redirect as valid SQL.

    Appending a suffix to such a name lands it PAST the closing quote
    (``PREPROC_RT."odd.name_V1"__VALIDATE``), which is not a name at all. The
    redirect replaces the whole name span with a freshly quoted disposable one,
    so the identifier the author chose is preserved verbatim inside its quotes.
    """
    _write_composite_module(
        tmp_path,
        "widgetq",
        function="widgetq",
        statements=_WIDGETQ_STATEMENTS,
        objects=_WIDGETQ_OBJECTS,
    )
    root = _write_index(tmp_path)
    conn = _ValidateConnection()

    validate_live(conn, _live_entry(root, "widgetq"), str(root))

    scratch = _assert_creates_only_in_one_disposable_schema(conn)
    _assert_teardown_reverses_creation(conn)
    assert ObjectSpec(type="PYTHON3 SCALAR SCRIPT", name=f"{scratch}.odd.name_V1") in _created(
        conn
    ), "the quoted companion must be compiled under its own identifier in the scratch schema"


_WIDGETC_OBJECTS = [{"type": "LUA SCRIPT", "name": "PREPROC_RT.WIDGETC_V1"}]
_WIDGETC_STATEMENTS = [
    "-- entry point: PREPROC_RT.WIDGETC_V1 rewrites the cast shorthand\n"
    "CREATE OR REPLACE LUA SCRIPT PREPROC_RT.WIDGETC_V1() AS\n"
    'function widgetc(sqltext)\n    local marker = "a;b"\n    return sqltext .. marker\nend'
]


def test_validate_live_ignores_a_name_span_outside_the_creation_site(tmp_path):
    """A leading comment naming the object does not derail the redirect.

    The first span matching the declared name is in the comment, and rewriting
    THERE would leave the real ``CREATE`` naming the real object — exactly what
    the re-derivation check rejects, so the search moves on to the creation site.
    The comment itself is left verbatim, which is right: a comment creates
    nothing and resolves to nothing. The statement is block-wrapped (its Lua body
    carries a semicolon), so this also covers the verbatim block form.
    """
    _write_composite_module(
        tmp_path,
        "widgetc",
        function="widgetc",
        statements=_WIDGETC_STATEMENTS,
        objects=_WIDGETC_OBJECTS,
    )
    root = _write_index(tmp_path)
    conn = _ValidateConnection()

    validate_live(conn, _live_entry(root, "widgetc"), str(root))

    scratch = _assert_creates_only_in_one_disposable_schema(conn)
    _assert_teardown_reverses_creation(conn)
    assert ObjectSpec(type="LUA SCRIPT", name=f"{scratch}.MODULE_ENTRY") in _created(conn)
    entry_sql = next(sql for sql in conn.executed if "MODULE_ENTRY" in sql and "DROP" not in sql)
    assert 'local marker = "a;b"' in entry_sql, "the verbatim block body must reach the database"
    assert entry_sql.startswith("-- entry point: PREPROC_RT.WIDGETC_V1"), (
        "only the creation target is rewritten; the author's text is otherwise verbatim"
    )


def test_validate_live_smoke_tests_a_single_statement_module(tmp_path):
    """A single-statement artifact is reproduced whole, so its invocation is conclusive.

    Nothing was left out of the scratch deploy, so the entry function's return
    type is the module's own answer and the phase contract can be judged on it.
    """
    _make_module(tmp_path, "cast_shorthand", function="cast_shorthand")
    root = _write_index(tmp_path)
    conn = _ValidateConnection(rettype="string")

    result = validate_live(conn, _live_entry(root, "cast_shorthand"), str(root))

    scratch = _assert_creates_only_in_one_disposable_schema(conn)
    _assert_teardown_reverses_creation(conn)
    assert not any("CAST_SHORTHAND_V1" in sql for sql in conn.executed)
    assert any(f'import("{scratch}.' in sql for sql in conn.executed), (
        "the probe must import the disposable copy of the entry script"
    )
    assert "returned string" in result


def test_validate_live_rejects_a_return_the_phase_contract_forbids(tmp_path):
    """A conclusive invocation returning neither nil nor a string fails validation."""
    _make_module(tmp_path, "cast_shorthand", function="cast_shorthand")
    root = _write_index(tmp_path)
    conn = _ValidateConnection(rettype="table")

    with pytest.raises(ValidateError, match="table"):
        validate_live(conn, _live_entry(root, "cast_shorthand"), str(root))

    _assert_teardown_reverses_creation(conn)


def test_validate_live_tears_down_when_the_probe_fails(tmp_path):
    """A failing invocation still drops the whole disposable namespace.

    For a single-statement artifact the failure is the module's own, so it is
    reported as one; the teardown in the ``finally`` runs either way.
    """
    _make_module(tmp_path, "cast_shorthand", function="cast_shorthand")
    root = _write_index(tmp_path)
    conn = _ValidateConnection(fail_on=("EXECUTE SCRIPT",))

    with pytest.raises(ValidateError):
        validate_live(conn, _live_entry(root, "cast_shorthand"), str(root))

    _assert_teardown_reverses_creation(conn)


def test_validate_live_reports_a_composite_invocation_as_inconclusive(tmp_path):
    """A composite whose entry needs its companions is inconclusive, never a failure.

    The companions exist only under their real names, which validation must not
    create, so an invocation error cannot be attributed to the module. Reporting
    it as a failure is exactly the wolf-crying this path must not do.
    """
    _write_composite_module(
        tmp_path,
        "widgetv",
        function="widgetv",
        statements=_WIDGETV_STATEMENTS,
        objects=_WIDGETV_OBJECTS,
    )
    root = _write_index(tmp_path)
    conn = _ValidateConnection(fail_on=("EXECUTE SCRIPT",))

    result = validate_live(conn, _live_entry(root, "widgetv"), str(root))

    assert "not conclusively" in result
    _assert_teardown_reverses_creation(conn)


# ---------------------------------------------------------------------------
# missing environment — offline
# ---------------------------------------------------------------------------


def test_cli_module_add_missing_env(tmp_path, capsys, monkeypatch):
    """A mutating verb exits with the missing-env code and names the variables."""
    for var in ("EXASOL_DSN", "EXASOL_USER", "EXASOL_PASSWORD"):
        monkeypatch.delenv(var, raising=False)
    root = _two_module_registry(tmp_path)

    code = run_module(_parse(["module", "add", "cast_shorthand", "--registry", str(root)]))

    assert code == 2
    combined = capsys.readouterr()
    text = combined.out + combined.err
    for var in ("EXASOL_DSN", "EXASOL_USER", "EXASOL_PASSWORD"):
        assert var in text


# ---------------------------------------------------------------------------
# sync / update provenance — offline, driven against a recording fake
# ---------------------------------------------------------------------------


class _SyncConnection:
    """The slice of ``pyexasol.ExaConnection`` the sync convergence path uses.

    ``deployed`` seeds what ``PREPROC_RT.INSTALLED_MODULES`` reports and
    ``rule_ids`` what the CONFIG lookup finds, so an add and an update are both
    drivable with no database. Every statement and its bind parameters are
    recorded, which is what lets a test assert the provenance a convergence
    actually writes rather than only what it reports.
    """

    def __init__(self, *, deployed=(), rule_ids=()):
        self.calls = []
        self._deployed = list(deployed)
        self._rule_ids = list(rule_ids)

    def execute(self, query, query_params=None):
        self.calls.append((query, dict(query_params or {})))
        return _SyncRows(self._rows_for(query))

    def _rows_for(self, query):
        if query.startswith("SELECT name, version, script_name"):
            return self._deployed
        if query.startswith("SELECT rule_id"):
            return self._rule_ids
        if query.startswith("EXECUTE SCRIPT PREPROC.RULE_ENSURE("):
            return [(1, True)]
        if query.startswith("EXECUTE SCRIPT PREPROC.MODULE_REGISTER("):
            return [("m", 1, "deployed", True)]
        return []

    def params_of(self, prefix):
        """The bind parameters of the one recorded statement starting with ``prefix``."""
        matches = [params for query, params in self.calls if query.startswith(prefix)]
        assert len(matches) == 1, f"expected one {prefix!r} call, recorded {len(matches)}"
        return matches[0]


class _SyncRows:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


_WIDGETSYNC_OBJECTS = [
    {"type": "LUA SCRIPT", "name": "PREPROC_RT.WIDGETSYNC_V1"},
    {"type": "PYTHON3 SCALAR SCRIPT", "name": "PREPROC_RT.WIDGETSYNC_HELPER_V1"},
    {"type": "TABLE", "name": "PREPROC_RT.WIDGETSYNC_CACHE_V1"},
]
_WIDGETSYNC_STATEMENTS = [
    "CREATE OR REPLACE LUA SCRIPT PREPROC_RT.WIDGETSYNC_V1() AS\n"
    "function widgetsync(sqltext)\n    return sqltext\nend",
    "CREATE OR REPLACE PYTHON3 SCALAR SCRIPT PREPROC_RT.WIDGETSYNC_HELPER_V1"
    "(x VARCHAR(2000)) RETURNS VARCHAR(2000) AS\ndef run(ctx):\n    return ctx.x",
    "CREATE TABLE PREPROC_RT.WIDGETSYNC_CACHE_V1 (K VARCHAR(64))",
]

_WIDGETUP_V2_OBJECTS = [
    {"type": "LUA SCRIPT", "name": "PREPROC_RT.WIDGETUP_V2"},
    {"type": "TABLE", "name": "PREPROC_RT.WIDGETUP_CACHE_V2"},
]
_WIDGETUP_V2_STATEMENTS = [
    "CREATE OR REPLACE LUA SCRIPT PREPROC_RT.WIDGETUP_V2() AS\n"
    "function widgetup(sqltext)\n    return sqltext\nend",
    "CREATE TABLE PREPROC_RT.WIDGETUP_CACHE_V2 (K VARCHAR(64))",
]

_REGISTER_CALL = "EXECUTE SCRIPT PREPROC.MODULE_REGISTER("


def test_sync_records_the_inventory_it_deployed(tmp_path):
    """``sync`` records the composite inventory it just deployed, never SQL NULL.

    The recorded inventory IS the module's ownership boundary and
    MODULE_RECLAIM's undo list: with a NULL column, reclaim takes its
    entry-script-only fallback, reports success, and deletes the provenance row —
    permanently orphaning every companion object. ``source_url`` and
    ``library_version`` are recorded on the same call for audit.
    """
    _write_composite_module(
        tmp_path,
        "widgetsync",
        function="widgetsync",
        statements=_WIDGETSYNC_STATEMENTS,
        objects=_WIDGETSYNC_OBJECTS,
    )
    root = _write_index(tmp_path, library_version="9.9.9")
    conn = _SyncConnection()

    result = converge(conn, [DesiredModule("widgetsync", 1, ("USER", "TESTER"))], str(root))

    assert result.added == 1
    params = conn.params_of(_REGISTER_CALL)
    assert params.get("ob") is not None, (
        "sync recorded a NULL inventory: MODULE_RECLAIM would then drop only the "
        "entry script and orphan every companion object"
    )
    assert decode_objects(params["ob"]) == _WIDGETSYNC_OBJECTS
    assert params.get("su") == str(root)
    assert params.get("lv") == "9.9.9"


def test_update_records_the_new_version_inventory(tmp_path):
    """``update`` records the inventory of the version it deployed, never SQL NULL.

    Same ownership-boundary requirement as ``sync``: the row written for the new
    version must carry that version's own objects, in file order.
    """
    _write_composite_module(
        tmp_path,
        "widgetup",
        version=2,
        function="widgetup",
        statements=_WIDGETUP_V2_STATEMENTS,
        objects=_WIDGETUP_V2_OBJECTS,
    )
    root = _write_index(tmp_path, library_version="9.9.9")
    conn = _SyncConnection(
        deployed=[("widgetup", 1, "PREPROC_RT.WIDGETUP_V1")],
        rule_ids=[(1,)],
    )

    result = update_module(conn, "widgetup", str(root), scope=("USER", "TESTER"))

    assert result.updated == 1
    params = conn.params_of(_REGISTER_CALL)
    assert params.get("ob") is not None, "update recorded a NULL inventory"
    assert decode_objects(params["ob"]) == _WIDGETUP_V2_OBJECTS
    assert params.get("su") == str(root)
    assert params.get("lv") == "9.9.9"


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
            for name in ("cast_shorthand", "trailing_comma"):
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


def _recorded_provenance(conn, name, version):
    """``(decoded objects, source_url, library_version)`` of one provenance row, or None."""
    rows = conn.execute(
        "SELECT objects, source_url, library_version FROM PREPROC_RT.INSTALLED_MODULES "
        "WHERE name = {n} AND version = {v}",
        {"n": name, "v": version},
    ).fetchall()
    if not rows:
        return None
    return decode_objects(rows[0][0]), rows[0][1], rows[0][2]


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
        "cast_shorthand",
        function="cast_shorthand",
        body=_TAG_BODY.format(function="cast_shorthand", seed=_SEED, tag="V1"),
    )
    root = _write_index(tmp_path)

    code = run_module(
        _parse(
            [
                "module",
                "add",
                "cast_shorthand",
                "--scope",
                f"USER:{user}",
                "--registry",
                str(root),
            ]
        )
    )

    assert code == 0
    assert _enabled_rules_for(conn, "PREPROC_RT.CAST_SHORTHAND_V1") == 1
    assert _provenance(conn, "cast_shorthand", 1) == "deployed"

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
    _make_module(tmp_path, "cast_shorthand", function="cast_shorthand")
    root = _write_index(tmp_path)
    # Tamper the artifact after the index (and its sha256) were written.
    artifact = root / "modules" / "cast_shorthand" / "cast_shorthand_v1.sql"
    artifact.write_text(artifact.read_text(encoding="utf-8") + "\n-- tampered\n", encoding="utf-8")

    code = run_module(
        _parse(
            [
                "module",
                "add",
                "cast_shorthand",
                "--scope",
                f"USER:{user}",
                "--registry",
                str(root),
            ]
        )
    )

    assert code != 0
    assert _enabled_rules_for(conn, "PREPROC_RT.CAST_SHORTHAND_V1") == 0
    assert _provenance(conn, "cast_shorthand", 1) is None
    exists = conn.execute(
        "SELECT COUNT(*) FROM EXA_ALL_SCRIPTS "
        "WHERE SCRIPT_SCHEMA = 'PREPROC_RT' AND SCRIPT_NAME = 'CAST_SHORTHAND_V1'"
    ).fetchval()
    assert exists == 0


_WIDGETADD_OBJECTS = [
    {"type": "LUA SCRIPT", "name": "PREPROC_RT.WIDGETADD_V1"},
    {"type": "PYTHON3 SCALAR SCRIPT", "name": "PREPROC_RT.WIDGETADD_HELPER1_V1"},
    {"type": "PYTHON3 SCALAR SCRIPT", "name": "PREPROC_RT.WIDGETADD_HELPER2_V1"},
    {"type": "TABLE", "name": "PREPROC_RT.WIDGETADD_CACHE_V1"},
]
_WIDGETADD_STATEMENTS = [
    "CREATE OR REPLACE LUA SCRIPT PREPROC_RT.WIDGETADD_V1() AS\n"
    "function widgetadd(sqltext)\n    return sqltext\nend",
    "CREATE OR REPLACE PYTHON3 SCALAR SCRIPT PREPROC_RT.WIDGETADD_HELPER1_V1"
    "(x VARCHAR(2000)) RETURNS VARCHAR(2000) AS\ndef run(ctx):\n    return ctx.x",
    "CREATE OR REPLACE PYTHON3 SCALAR SCRIPT PREPROC_RT.WIDGETADD_HELPER2_V1"
    "(x VARCHAR(2000)) RETURNS VARCHAR(2000) AS\ndef run(ctx):\n    return ctx.x",
    "CREATE TABLE PREPROC_RT.WIDGETADD_CACHE_V1 (K VARCHAR(64))",
]


@pytest.mark.integration
def test_add_deploys_all_composite_objects(tmp_path, module_env):
    """`add` on a composite module deploys every declared object, not just the entry script.

    ``DeployResult.objects`` is file-ordered, so the exact objects to look for
    are already known without re-querying the registry.
    """
    conn = module_env
    user = _current_user(conn)
    _write_composite_module(
        tmp_path,
        "widgetadd",
        function="widgetadd",
        statements=_WIDGETADD_STATEMENTS,
        objects=_WIDGETADD_OBJECTS,
    )
    root = _write_index(tmp_path)

    try:
        index = read_registry(str(root))
        entry = next(e for e in index.entries if e.name == "widgetadd")
        result = deploy_module(conn, entry, str(root), scope=("USER", user))

        assert [obj.name for obj in result.objects] == [obj["name"] for obj in _WIDGETADD_OBJECTS]
        for obj in _WIDGETADD_OBJECTS:
            assert _object_exists(conn, obj), f"{obj} must exist after add"
    finally:
        conn.execute("DELETE FROM PREPROC.CONFIG WHERE script_name = 'PREPROC_RT.WIDGETADD_V1'")
        conn.execute("DELETE FROM PREPROC.MODULES WHERE name = 'widgetadd'")
        conn.commit()
        for obj in reversed(_WIDGETADD_OBJECTS):
            _drop_object_if_present(conn, obj)


_WIDGETFAIL_OBJECTS = [
    {"type": "LUA SCRIPT", "name": "PREPROC_RT.WIDGETFAIL_V1"},
    {"type": "PYTHON3 SCALAR SCRIPT", "name": "PREPROC_RT.WIDGETFAIL_HELPER_V1"},
    {"type": "TABLE", "name": "PREPROC_RT.WIDGETFAIL_CACHE_V1"},
    {"type": "VIEW", "name": "PREPROC_RT.WIDGETFAIL_VIEW_V1"},
]
_WIDGETFAIL_STATEMENTS = [
    "CREATE OR REPLACE LUA SCRIPT PREPROC_RT.WIDGETFAIL_V1() AS\n"
    "function widgetfail(sqltext)\n    return sqltext\nend",
    "CREATE OR REPLACE PYTHON3 SCALAR SCRIPT PREPROC_RT.WIDGETFAIL_HELPER_V1"
    "(x VARCHAR(2000)) RETURNS VARCHAR(2000) AS\ndef run(ctx):\n    return ctx.x",
    "CREATE TABLE PREPROC_RT.WIDGETFAIL_CACHE_V1 (K VARCHAR(64))",
    # Splits cleanly (valid CREATE VIEW DDL) but fails at execution: the
    # source table does not exist.
    "CREATE VIEW PREPROC_RT.WIDGETFAIL_VIEW_V1 AS "
    "SELECT * FROM PREPROC_RT.WIDGETFAIL_NO_SUCH_TABLE_V1",
]


@pytest.mark.integration
def test_failed_add_cleans_up_and_registers_no_rule(tmp_path, capsys, module_env):
    """A composite module whose 4th statement fails leaves no partial state and no rule.

    ``commands.py`` catches the ``ArtifactDeployError`` and prints
    ``Error: {exc}``, so the "statement 4 of 4" message reaches stderr
    verbatim; compensating cleanup already dropped statements 1..3's objects
    before that error propagated.
    """
    conn = module_env
    user = _current_user(conn)
    _write_composite_module(
        tmp_path,
        "widgetfail",
        function="widgetfail",
        statements=_WIDGETFAIL_STATEMENTS,
        objects=_WIDGETFAIL_OBJECTS,
    )
    root = _write_index(tmp_path)

    try:
        code = run_module(
            _parse(
                [
                    "module",
                    "add",
                    "widgetfail",
                    "--scope",
                    f"USER:{user}",
                    "--registry",
                    str(root),
                ]
            )
        )

        assert code != 0
        err = capsys.readouterr().err
        assert "statement 4 of 4" in err

        assert _enabled_rules_for(conn, "PREPROC_RT.WIDGETFAIL_V1") == 0
        assert _provenance(conn, "widgetfail", 1) is None
        for obj in _WIDGETFAIL_OBJECTS:
            assert not _object_exists(conn, obj), f"{obj} must not survive a failed add"
    finally:
        conn.execute("DELETE FROM PREPROC.CONFIG WHERE script_name = 'PREPROC_RT.WIDGETFAIL_V1'")
        conn.execute("DELETE FROM PREPROC.MODULES WHERE name = 'widgetfail'")
        conn.commit()
        for obj in reversed(_WIDGETFAIL_OBJECTS):
            _drop_object_if_present(conn, obj)


def _schema_exists(conn, name):
    return bool(
        conn.execute(
            "SELECT COUNT(*) FROM EXA_ALL_SCHEMAS WHERE SCHEMA_NAME = {n}", {"n": name}
        ).fetchval()
    )


def _leftover_validation_schemas(conn):
    """Every disposable validation schema still present — must always be empty."""
    return [
        row[0]
        for row in conn.execute(
            "SELECT SCHEMA_NAME FROM EXA_ALL_SCHEMAS WHERE SCHEMA_NAME LIKE 'PREPROC_VALIDATE!_%' "
            "ESCAPE '!' ORDER BY SCHEMA_NAME"
        ).fetchall()
    ]


def _declared_exists(conn, obj):
    """True if a declared inventory object exists, covering the SCHEMA case too."""
    if obj["type"] == "SCHEMA":
        return _schema_exists(conn, obj["name"])
    return _object_exists(conn, obj)


@pytest.mark.integration
def test_cli_module_validate_live_smoke(tmp_path, module_env):
    """``validate`` with a DB compiles into a disposable schema, smoke-tests, and tears down."""
    conn = module_env
    _make_module(tmp_path, "cast_shorthand", function="cast_shorthand")
    root = _write_index(tmp_path)

    code = run_module(_parse(["module", "validate", "cast_shorthand", "--registry", str(root)]))

    assert code == 0
    assert _leftover_validation_schemas(conn) == []
    assert not _object_exists(
        conn, {"type": "LUA SCRIPT", "name": "PREPROC_RT.CAST_SHORTHAND_V1"}
    ), "validate must not leave the module's real entry script behind"


# Case A — the composite shape the contract exists to enable: statement 3
# references statement 2 BY NAME. Live validation must pass this on a database
# where the module is NOT installed, which is precisely what a per-statement
# rename could not do: the view's FROM would have named the real, absent table.
_WIDGETXREF_OBJECTS = [
    {"type": "LUA SCRIPT", "name": "PREPROC_RT.WIDGETXREF_V1"},
    {"type": "TABLE", "name": "PREPROC_RT.WIDGETXREF_CACHE_V1"},
    {"type": "VIEW", "name": "PREPROC_RT.WIDGETXREF_VIEW_V1"},
]
_WIDGETXREF_STATEMENTS = [
    "CREATE OR REPLACE LUA SCRIPT PREPROC_RT.WIDGETXREF_V1() AS\n"
    "function widgetxref(sqltext)\n    return sqltext\nend",
    "CREATE TABLE PREPROC_RT.WIDGETXREF_CACHE_V1 (K VARCHAR(64))",
    "CREATE VIEW PREPROC_RT.WIDGETXREF_VIEW_V1 AS SELECT K FROM PREPROC_RT.WIDGETXREF_CACHE_V1",
]

# Case B — a module that owns its own schema (the headline ADR-057 shape). A
# per-statement rename created schema ``…__VALIDATE`` but put the table in the
# REAL schema: absent, a spurious failure; present, a real object inside a live
# schema.
_WIDGETOWN_OBJECTS = [
    {"type": "LUA SCRIPT", "name": "PREPROC_RT.WIDGETOWN_V1"},
    {"type": "SCHEMA", "name": "WIDGETOWN_HOME_V1"},
    {"type": "TABLE", "name": "WIDGETOWN_HOME_V1.CACHE_V1"},
]
_WIDGETOWN_STATEMENTS = [
    "CREATE OR REPLACE LUA SCRIPT PREPROC_RT.WIDGETOWN_V1() AS\n"
    "function widgetown(sqltext)\n    return sqltext\nend",
    "CREATE SCHEMA WIDGETOWN_HOME_V1",
    "CREATE TABLE WIDGETOWN_HOME_V1.CACHE_V1 (K VARCHAR(64))",
]


@pytest.mark.integration
@pytest.mark.parametrize(
    ("name", "statements", "objects"),
    [
        ("widgetxref", _WIDGETXREF_STATEMENTS, _WIDGETXREF_OBJECTS),
        ("widgetown", _WIDGETOWN_STATEMENTS, _WIDGETOWN_OBJECTS),
    ],
)
def test_validate_live_passes_a_composite_without_creating_its_objects(
    tmp_path, module_env, name, statements, objects
):
    """A conformant composite validates on a clean database and creates none of its objects.

    Two shapes, one invariant: ``validate`` MUST exit 0 (no wolf-crying for a
    conformant module), MUST NOT create any object the module declares — not the
    cross-referenced table, not the module's own schema — and MUST leave no
    disposable schema behind.
    """
    conn = module_env
    _write_composite_module(tmp_path, name, function=name, statements=statements, objects=objects)
    root = _write_index(tmp_path)
    for obj in objects:
        assert not _declared_exists(conn, obj), f"{obj} must be absent before validate runs"

    code = run_module(_parse(["module", "validate", name, "--registry", str(root)]))

    assert code == 0
    for obj in objects:
        assert not _declared_exists(conn, obj), f"validate must not create {obj}"
    assert _leftover_validation_schemas(conn) == []


class _ProbeWatcher:
    """Wraps a live connection to snapshot the database at the moment the probe fires.

    The disposable objects only exist WHILE validation runs, so the one point
    that proves they were really created is the probe's ``EXECUTE SCRIPT`` — by
    then the whole disposable deploy is in place. The snapshot is recorded, never
    asserted here: an assertion raised inside ``execute`` would be caught by the
    composite path's inconclusive-invocation handler and silently vanish.
    """

    def __init__(self, connection, declared):
        self._connection = connection
        self._declared = declared
        self.executed: list[str] = []
        self.snapshot: dict[str, object] = {}

    @property
    def scratch_schema(self):
        for sql in self.executed:
            derived = derive_object(sql)
            if derived is not None and derived["type"] == "SCHEMA":
                return derived["name"]
        return None

    def execute(self, query, query_params=None):
        self.executed.append(query)
        if query.startswith("EXECUTE SCRIPT"):
            self._snapshot()
        if query_params is None:
            return self._connection.execute(query)
        return self._connection.execute(query, query_params)

    def _snapshot(self):
        schema = self.scratch_schema
        self.snapshot["schema"] = schema
        self.snapshot["schema_exists"] = _schema_exists(self._connection, schema)
        self.snapshot["entry_exists"] = _object_exists(
            self._connection, {"type": "LUA SCRIPT", "name": f"{schema}.MODULE_ENTRY"}
        )
        self.snapshot["declared_present"] = [
            obj for obj in self._declared if _declared_exists(self._connection, obj)
        ]


@pytest.mark.integration
def test_validate_live_disposable_objects_exist_during_the_deploy(tmp_path, module_env):
    """The disposable objects really are created — and the real ones really are not.

    Asserting only on the end state cannot tell a working scratch deploy from
    one that never deployed anything, nor from one that clobbered the real
    objects and tidied up after itself. This pins all three: during the run the
    disposable schema and entry copy exist and no declared object does; after it,
    the disposable schema is gone.
    """
    conn = module_env
    _write_composite_module(
        tmp_path,
        "widgetxref",
        function="widgetxref",
        statements=_WIDGETXREF_STATEMENTS,
        objects=_WIDGETXREF_OBJECTS,
    )
    root = _write_index(tmp_path)
    watcher = _ProbeWatcher(conn, _WIDGETXREF_OBJECTS)

    result = validate_live(watcher, _live_entry(root, "widgetxref"), str(root))

    assert "live smoke" in result
    assert watcher.snapshot["schema_exists"], "the disposable schema must exist during the deploy"
    assert watcher.snapshot["entry_exists"], (
        "the disposable entry copy must exist during the deploy"
    )
    assert watcher.snapshot["declared_present"] == [], (
        "no object the module declares may exist at any point during validation"
    )
    assert not _schema_exists(conn, watcher.snapshot["schema"])
    assert _leftover_validation_schemas(conn) == []


@pytest.mark.integration
def test_cli_module_sync_idempotent(tmp_path, module_env):
    """Re-running sync with an unchanged desired file makes no changes."""
    conn = module_env
    user = _current_user(conn)
    _make_module(tmp_path, "cast_shorthand", function="cast_shorthand")
    root = _write_index(tmp_path)

    desired = tmp_path / "desired.toml"
    desired.write_text(
        f'[[modules]]\nname = "cast_shorthand"\nversion = 1\nscope = "USER:{user}"\n',
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


_WIDGETUP_V1_OBJECTS = [
    {"type": "LUA SCRIPT", "name": "PREPROC_RT.WIDGETUP_V1"},
    {"type": "TABLE", "name": "PREPROC_RT.WIDGETUP_CACHE_V1"},
]
_WIDGETUP_V1_STATEMENTS = [
    "CREATE OR REPLACE LUA SCRIPT PREPROC_RT.WIDGETUP_V1() AS\n"
    "function widgetup(sqltext)\n    return sqltext\nend",
    "CREATE TABLE PREPROC_RT.WIDGETUP_CACHE_V1 (K VARCHAR(64))",
]


def _clean_up_module(conn, name, objects):
    """Drop every rule, provenance row, and object of a test module, in reverse order."""
    for obj in objects:
        if obj["type"].endswith("SCRIPT"):
            conn.execute("DELETE FROM PREPROC.CONFIG WHERE script_name = {sn}", {"sn": obj["name"]})
    conn.execute("DELETE FROM PREPROC.MODULES WHERE name = {n}", {"n": name})
    conn.commit()
    for obj in reversed(objects):
        _drop_object_if_present(conn, obj)


@pytest.mark.integration
def test_cli_module_sync_records_composite_inventory(tmp_path, module_env):
    """``sync`` leaves the composite module's FULL inventory in its provenance row.

    The live end state is what ``MODULE_RECLAIM`` later reads as its undo list, so
    this asserts the recorded column itself — not merely that the convergence
    reported an add — along with the audit fields written on the same call.
    """
    conn = module_env
    user = _current_user(conn)
    _write_composite_module(
        tmp_path,
        "widgetsync",
        function="widgetsync",
        statements=_WIDGETSYNC_STATEMENTS,
        objects=_WIDGETSYNC_OBJECTS,
    )
    root = _write_index(tmp_path, library_version="9.9.9")
    desired = tmp_path / "desired.toml"
    desired.write_text(
        f'[[modules]]\nname = "widgetsync"\nversion = 1\nscope = "USER:{user}"\n',
        encoding="utf-8",
    )

    try:
        assert run_module(_parse(["module", "sync", str(desired), "--registry", str(root)])) == 0

        for obj in _WIDGETSYNC_OBJECTS:
            assert _object_exists(conn, obj), f"{obj} must exist after sync"
        objects, source_url, library_version = _recorded_provenance(conn, "widgetsync", 1)
        assert objects == _WIDGETSYNC_OBJECTS, (
            "sync must record every deployed object; a NULL or short inventory orphans "
            "the companions when the version is later reclaimed"
        )
        assert source_url == str(root)
        assert library_version == "9.9.9"
    finally:
        _clean_up_module(conn, "widgetsync", _WIDGETSYNC_OBJECTS)


@pytest.mark.integration
def test_cli_module_update_records_new_version_inventory(tmp_path, module_env):
    """``update`` records the new version's own inventory and leaves the old row's intact.

    Reclaiming either version must drop exactly what that version created, so
    both rows are asserted: V2 carries V2's objects and V1 keeps V1's while being
    retired.
    """
    conn = module_env
    user = _current_user(conn)
    _write_composite_module(
        tmp_path,
        "widgetup",
        version=1,
        function="widgetup",
        statements=_WIDGETUP_V1_STATEMENTS,
        objects=_WIDGETUP_V1_OBJECTS,
    )
    root = _write_index(tmp_path, library_version="9.9.9")

    add_argv = ["module", "add", "widgetup", "--scope", f"USER:{user}", "--registry", str(root)]
    try:
        assert run_module(_parse(add_argv)) == 0
        # Re-index the SAME checkout at version 2: the index then names only V2,
        # which is what `update` resolves as the module's latest version.
        _write_composite_module(
            tmp_path,
            "widgetup",
            version=2,
            function="widgetup",
            statements=_WIDGETUP_V2_STATEMENTS,
            objects=_WIDGETUP_V2_OBJECTS,
        )
        _write_index(tmp_path, library_version="9.9.9")

        assert run_module(_parse(["module", "update", "widgetup", "--registry", str(root)])) == 0

        new_objects, source_url, library_version = _recorded_provenance(conn, "widgetup", 2)
        assert new_objects == _WIDGETUP_V2_OBJECTS
        assert source_url == str(root)
        assert library_version == "9.9.9"
        assert _provenance(conn, "widgetup", 2) == "deployed"
        # The superseded row keeps its OWN inventory so reclaiming V1 drops V1.
        assert _recorded_provenance(conn, "widgetup", 1)[0] == _WIDGETUP_V1_OBJECTS
        assert _provenance(conn, "widgetup", 1) == "retired"
    finally:
        _clean_up_module(conn, "widgetup", _WIDGETUP_V1_OBJECTS + _WIDGETUP_V2_OBJECTS)


@pytest.mark.integration
def test_cli_module_sync_retire(tmp_path, module_env):
    """A module dropped from the desired file has its rule removed and row retired."""
    conn = module_env
    user = _current_user(conn)
    _make_module(tmp_path, "cast_shorthand", function="cast_shorthand")
    root = _write_index(tmp_path)

    synced = tmp_path / "desired.toml"
    synced.write_text(
        f'[[modules]]\nname = "cast_shorthand"\nversion = 1\nscope = "USER:{user}"\n',
        encoding="utf-8",
    )
    assert run_module(_parse(["module", "sync", str(synced), "--registry", str(root)])) == 0
    assert _enabled_rules_for(conn, "PREPROC_RT.CAST_SHORTHAND_V1") == 1

    empty = tmp_path / "empty.toml"
    empty.write_text("modules = []\n", encoding="utf-8")
    assert run_module(_parse(["module", "sync", str(empty), "--registry", str(root)])) == 0

    assert _enabled_rules_for(conn, "PREPROC_RT.CAST_SHORTHAND_V1") == 0
    assert _provenance(conn, "cast_shorthand", 1) == "retired"
