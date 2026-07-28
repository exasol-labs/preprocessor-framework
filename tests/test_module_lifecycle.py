"""Integration tests for the module DB lifecycle and provenance surface.

These exercise the DB-side module lifecycle as ``EXECUTE SCRIPT`` calls against a
live Exasol instance (the ``installed`` fixture): the idempotent provenance upsert
(``PREPROC.MODULE_REGISTER`` into ``PREPROC.MODULES``), the idempotent rule ensure
(``PREPROC.RULE_ENSURE``), and the never-mutate-a-live-module update flow with its
generational retire/reclaim (``PREPROC.MODULE_RETIRE`` / ``PREPROC.MODULE_RECLAIM``).

Deployment self-sufficiency
    Wiring these four scripts into ``run_install`` is Group D's task. To keep this
    suite runnable independently of that ordering, the session-scoped
    ``lifecycle_deployed`` fixture deploys them here with the canonical CREATE
    wrappers; the parameter order in each wrapper is the authoritative reference
    for the installer/bundle wiring. ``PREPROC.MODULES`` and
    ``PREPROC_RT.INSTALLED_MODULES`` come from ``run_install`` itself (the
    ``22_modules_table.sql`` DDL and the ``40_views.sql`` view). Exasol LUA SCRIPT
    parameters are bare names without SQL types (types apply to UDFs, not
    scripting), matching ``sql/lua/*`` and install.py.

The reclaim safety argument under test
    A retired module version's ``PREPROC_RT.<NAME>_V<N>`` script may be physically
    dropped only when it is provably unreachable, mirroring the profile
    never-same-refresh-drop invariant (ADR-022):

      * no ENABLED ``PREPROC.CONFIG`` rule names the script (MASTER dispatches only
        enabled rules, so none can import it for a live statement), AND
      * no NON-ORPHANED ``PREPROC.PROFILES`` manifest bakes the script (a live
        baked profile is the only kind that still statically imports its scripts;
        an orphaned profile carries the MASTER stub, which imports nothing).

    ``test_module_reclaim_blocked_by_profile`` isolates the profile guard: it
    presents a live PROFILES manifest baking V1 with NO enabled rule (so only the
    profile guard can block), proving an orphaned-but-still-present profile is the
    boundary the guard defends. ``test_module_reclaim_when_unreferenced`` proves
    the drop happens once both guards pass.

    Both guards are evaluated against the ENTRY script, because a rule names an
    entry script and a profile bakes an entry script — never a companion.

What reclaim drops, once both guards pass
    The RECORDED INVENTORY (``PREPROC.MODULES.objects``), in REVERSE file order,
    and nothing else. The inventory, not a schema name, is the ownership boundary
    (ADR-057): an object outside ``PREPROC_RT`` is reclaimed when the inventory
    records it, and an object the inventory does not record is never dropped
    however closely its name or schema resembles a recorded one. A NULL inventory
    is read as the single Lua script named by ``script_name``, which is what keeps
    a pre-composite provenance row reclaiming correctly.

    ``test_reclaim_spares_objects_absent_from_inventory`` is where that boundary is
    enforced rather than described: every object it asserts survives is one that a
    schema sweep, a CASCADE, or a name-prefix match would have taken.

Shared-state discipline (CONFIG/RESOLUTION/PROFILES/MODULES are global tables)
    ``lifecycle_env`` wipes all four tables before and after every test, restores
    convenience mode, deploys the two test module scripts (V1/V2), and on teardown
    deactivates the preprocessor, drops the test scripts, and refreshes to a clean
    (empty) RESOLUTION so nothing leaks into the rest of the shared session.
    ``composite_env`` layers the multi-type, multi-schema scaffolding and the
    decoys on top of it for the inventory-driven reclaim tests.

How a transform is observed
    MASTER is a SQL preprocessor: it rewrites statement text and returns nothing.
    Each test module function appends a per-version tag into a ``'SEED...'``
    sentinel literal (the technique from test_master_dispatch), so ``SELECT 'SEED'
    AS PROBE`` fetched back as ``SEED|V1`` / ``SEED|V2`` encodes which version
    dispatched. The behavioural probe runs on a FRESH connection so it always
    resolves the current committed pipeline (no stale profile pin or read
    snapshot), and MASTER dispatches the triggering statement before any
    convenience-mode swap, so the first statement is transformed.
"""

from pathlib import Path

import pytest

from preproc.connection import connect_from_env
from preproc.module.artifact import derive_objects, split_statements
from preproc.module.manifest import ENTRY_OBJECT_TYPE
from preproc.module.registry import decode_objects, encode_objects

_REPO_ROOT = Path(__file__).parent.parent
_LUA_DIR = _REPO_ROOT / "sql" / "lua"

# Canonical CREATE wrappers. Parameter order MUST match the documented Lua
# parameter order in each script body (and its read-globals luacheck directive);
# this is the authoritative reference for the installer/bundle wiring (Group D).
_MODULE_SCRIPTS = {
    "rule_ensure.lua": (
        "CREATE OR REPLACE LUA SCRIPT PREPROC.RULE_ENSURE("
        "scope_type, scope_value, phase, script_name, function_name, client_pattern, norefresh"
        ") RETURNS TABLE AS\n"
    ),
    "module_register.lua": (
        "CREATE OR REPLACE LUA SCRIPT PREPROC.MODULE_REGISTER("
        "name, version, source, ref, sha256, script_name, function_name, phase, deploy_mode, "
        "source_url, library_version, objects"
        ") RETURNS TABLE AS\n"
    ),
    "module_retire.lua": (
        "CREATE OR REPLACE LUA SCRIPT PREPROC.MODULE_RETIRE(name, version) RETURNS TABLE AS\n"
    ),
    "module_reclaim.lua": (
        "CREATE OR REPLACE LUA SCRIPT PREPROC.MODULE_RECLAIM(name, version) RETURNS TABLE AS\n"
    ),
}

# REFRESH_CORE (imported by RULE_ENSURE on its insert path) is already deployed by
# run_install, so only the four new scripts are deployed here.
_DEPLOY_ORDER = [
    "rule_ensure.lua",
    "module_register.lua",
    "module_retire.lua",
    "module_reclaim.lua",
]

_ACTIVATE = "ALTER SESSION SET sql_preprocessor_script = PREPROC_RT.MASTER"
_DEACTIVATE = "ALTER SESSION SET sql_preprocessor_script = ''"

_SEED = "SEED"
_PROBE = f"SELECT '{_SEED}' AS PROBE"

_MODULE_NAME = "cast_shorthand"
_FUNCTION = "cast_shorthand"
_PHASE = "TRANSLATE"
_SCRIPT_V1 = "PREPROC_RT.CAST_SHORTHAND_V1"
_SCRIPT_V2 = "PREPROC_RT.CAST_SHORTHAND_V2"
_DEPLOY_MODE = "library-deployed"

# A test module version: its cast_shorthand() appends the version tag inside the
# sentinel literal so the fetched scalar records which version dispatched.
_MODULE_BODY = (
    "CREATE OR REPLACE LUA SCRIPT {name}() AS\n"
    "    function cast_shorthand(sqltext)\n"
    '        local out = string.gsub(sqltext, "({seed}[^\']*)\'", "%1|{tag}\'", 1)\n'
    "        return out\n"
    "    end\n"
)

# --- composite scaffolding --------------------------------------------------
#
# Reclaim is now inventory-driven, so the reclaim tests need real objects of more
# than one type living in more than one schema, plus DECOYS that no inventory
# records. ``composite_env`` creates and tears down everything below; each test
# then states which of them its own provenance row records, so "in the recorded
# inventory" and "in the database" stay independently visible.
#
# Two of the schemas matter structurally:
#   * ``_OUT_SCHEMA`` is a schema the module only writes INTO. It is never in an
#     inventory, so it must survive every reclaim (ADR-057 widens the boundary to
#     the inventory, it does not widen it to a schema).
#   * ``_OWNED_SCHEMA_V1`` is a schema a module version creates and therefore
#     DOES record, together with a table inside it — the dependency that makes
#     reverse-order teardown observable.
_OUT_SCHEMA = "ZZ_PREPROC_LC_OUT"
_OWNED_SCHEMA_V1 = "ZZ_PREPROC_LC_OWNED_V1"

_STATE_V1 = f"{_OUT_SCHEMA}.MOD_STATE_V1"
_STATE_V2 = f"{_OUT_SCHEMA}.MOD_STATE_V2"
_OWNED_CHILD_V1 = f"{_OWNED_SCHEMA_V1}.CHILD_V1"
_HELPER_V1 = "PREPROC_RT.CAST_SHORTHAND_HELPER_V1"
_HELPER_V2 = "PREPROC_RT.CAST_SHORTHAND_HELPER_V2"
_UDF_V1 = "PREPROC_RT.CAST_SHORTHAND_UDF_V1"
_FMT_UDF_V1 = "PREPROC_RT.CAST_SHORTHAND_FMT_V1"

# Decoys, recorded by no inventory. _DECOY_SCRIPT's name has the entry script's
# fully-qualified name as a STRICT PREFIX and shares its schema; _DECOY_TABLE
# sits in the very schema a recorded object lives in. Between them they fail any
# implementation that resolved its drop set from a schema or a name pattern
# instead of from the recorded inventory.
_DECOY_SCRIPT = f"{_SCRIPT_V1}_KEEP"
_DECOY_TABLE = f"{_OUT_SCHEMA}.KEEP_TABLE_V1"

# manifest.ENTRY_OBJECT_TYPE is the Lua-script object type; a companion Lua
# script carries the same type, so the one constant serves both roles.
_LUA_SCRIPT = ENTRY_OBJECT_TYPE
_PY_UDF = "PYTHON3 SCALAR SCRIPT"
_TABLE = "TABLE"
_SCHEMA = "SCHEMA"

# A companion whose body is BYTE-IDENTICAL between generations: only the object
# name carries the _V<N> suffix (ADR-056), so nothing but the recorded name can
# tell a v1 companion from its v2 counterpart.
_HELPER_BODY = (
    "CREATE OR REPLACE LUA SCRIPT {name}() AS\n    function helper()\n        return 1\n    end\n"
)

_UDF_BODY = (
    "CREATE OR REPLACE PYTHON3 SCALAR SCRIPT {name}(x DECIMAL(9,0)) RETURNS DECIMAL(9,0) AS\n"
    "def run(ctx):\n"
    "    return ctx.x\n"
)

# A composite artifact in the canonical grammar: three block-wrapped statements
# and one ';'-terminated one, creating a Lua entry script, two Python UDFs, and a
# table. test_provenance_records_full_inventory splits it with the canonical
# splitter so the declared inventory is pinned to what the artifact really
# creates rather than to a hand-written literal.
_COMPOSITE_ARTIFACT = f"""--/
CREATE OR REPLACE LUA SCRIPT {_SCRIPT_V1}() AS
    function cast_shorthand(sqltext)
        return sqltext
    end
/
--/
CREATE OR REPLACE PYTHON3 SCALAR SCRIPT {_UDF_V1}(x DECIMAL(9,0)) RETURNS DECIMAL(9,0) AS
def run(ctx):
    return ctx.x
/
--/
CREATE OR REPLACE PYTHON3 SCALAR SCRIPT {_FMT_UDF_V1}(x VARCHAR(100)) RETURNS VARCHAR(100) AS
def run(ctx):
    return ctx.x
/
CREATE TABLE IF NOT EXISTS {_STATE_V1} (payload DECIMAL(9,0));
"""


@pytest.fixture(scope="session")
def lifecycle_deployed(installed):
    """Deploy the four module lifecycle scripts, then return the connection.

    Mirrors what install.py's _deploy_admin_scripts will do for these scripts once
    Group D wires them in. REFRESH_CORE (imported by RULE_ENSURE) is already
    deployed by run_install.
    """
    installed.execute("OPEN SCHEMA PREPROC")
    for name in _DEPLOY_ORDER:
        body = (_LUA_DIR / name).read_text(encoding="utf-8")
        installed.execute(_MODULE_SCRIPTS[name] + body)
    return installed


@pytest.fixture
def lifecycle_env(lifecycle_deployed):
    """Clean CONFIG/RESOLUTION/PROFILES/MODULES, convenience mode, test scripts.

    Wipes all four tables before and after, restores convenience mode, deploys the
    V1/V2 test module scripts, and on teardown deactivates the preprocessor, drops
    the test scripts, and refreshes to a clean state.
    """
    conn = lifecycle_deployed
    _reset(conn)
    _deploy_test_modules(conn)
    try:
        yield conn
    finally:
        try:
            conn.execute(_DEACTIVATE)
        finally:
            _reset(conn)
            _drop_test_modules(conn)
            conn.execute("EXECUTE SCRIPT PREPROC.REFRESH").fetchall()


@pytest.fixture
def composite_env(lifecycle_env):
    """``lifecycle_env`` plus the composite scaffolding objects and decoys.

    Layered on ``lifecycle_env`` rather than folded into it so the tests that
    need only the V1/V2 entry scripts stay cheap; the inner fixture also finalises
    first, so the scaffolding is gone before the shared tables are wiped.
    """
    conn = lifecycle_env
    _create_composite_objects(conn)
    try:
        yield conn
    finally:
        _drop_composite_objects(conn)


def _create_composite_objects(conn):
    """Create the composite scaffolding: two schemas, four tables, four scripts."""
    for schema in (_OUT_SCHEMA, _OWNED_SCHEMA_V1):
        conn.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
    # CREATE SCHEMA also OPENs it. Every statement here is fully qualified, but
    # PREPROC is reopened so the session is left exactly as the fixture found it.
    conn.execute("OPEN SCHEMA PREPROC")
    for table in (_STATE_V1, _STATE_V2, _OWNED_CHILD_V1, _DECOY_TABLE):
        conn.execute(f"CREATE TABLE IF NOT EXISTS {table} (payload DECIMAL(9,0))")
    for script in (_HELPER_V1, _HELPER_V2):
        conn.execute(_HELPER_BODY.format(name=script))
    conn.execute(_MODULE_BODY.format(name=_DECOY_SCRIPT, seed=_SEED, tag="KEEP"))
    conn.execute(_UDF_BODY.format(name=_UDF_V1))
    conn.commit()


def _drop_composite_objects(conn):
    """Remove the scaffolding. CASCADE is safe here — these are test-owned schemas."""
    for script in (_UDF_V1, _DECOY_SCRIPT, _HELPER_V2, _HELPER_V1):
        conn.execute(f"DROP SCRIPT IF EXISTS {script}")
    for schema in (_OWNED_SCHEMA_V1, _OUT_SCHEMA):
        conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
    conn.execute("OPEN SCHEMA PREPROC")
    conn.commit()


def _reset(conn):
    """Wipe the four global tables and restore convenience mode (committed)."""
    for table in ("RESOLUTION", "CONFIG", "PROFILES", "MODULES"):
        conn.execute(f"DELETE FROM PREPROC.{table}")
    # norefresh=TRUE: COMMIT the wipe with the mode change; no rebuild needed.
    conn.execute("EXECUTE SCRIPT PREPROC.SET_MODE('convenience', TRUE)")
    conn.commit()


def _deploy_test_modules(conn):
    """Deploy PREPROC_RT.CAST_SHORTHAND_V1 and _V2 (distinct version tags)."""
    conn.execute(_MODULE_BODY.format(name=_SCRIPT_V1, seed=_SEED, tag="V1"))
    conn.execute(_MODULE_BODY.format(name=_SCRIPT_V2, seed=_SEED, tag="V2"))


def _drop_test_modules(conn):
    for script in (_SCRIPT_V1, _SCRIPT_V2):
        _drop_script_if_present(conn, script)


def _drop_script_if_present(conn, qualified):
    schema, _, name = qualified.partition(".")
    exists = conn.execute(
        "SELECT COUNT(*) FROM EXA_ALL_SCRIPTS WHERE SCRIPT_SCHEMA = {s} AND SCRIPT_NAME = {n}",
        {"s": schema, "n": name},
    ).fetchval()
    if exists:
        conn.execute(f"DROP SCRIPT {qualified}")


def _current_user(conn):
    return conn.execute("SELECT CURRENT_USER").fetchval()


def _register(
    conn,
    *,
    name=_MODULE_NAME,
    version,
    source="library",
    ref="v1",
    sha256=None,
    script_name,
    function_name=_FUNCTION,
    phase=_PHASE,
    deploy_mode=_DEPLOY_MODE,
    source_url=None,
    library_version=None,
    objects=None,
):
    """Run MODULE_REGISTER and return its result rows [(name, version, status, created)]."""
    su_sql = "{su}" if source_url is not None else "NULL"
    lv_sql = "{lv}" if library_version is not None else "NULL"
    ob_sql = "{ob}" if objects is not None else "NULL"
    params = {
        "n": name,
        "v": version,
        "src": source,
        "rf": ref,
        "sh": sha256 if sha256 is not None else "a" * 64,
        "sn": script_name,
        "fn": function_name,
        "ph": phase,
        "dm": deploy_mode,
    }
    if source_url is not None:
        params["su"] = source_url
    if library_version is not None:
        params["lv"] = library_version
    if objects is not None:
        params["ob"] = objects
    return conn.execute(
        "EXECUTE SCRIPT PREPROC.MODULE_REGISTER("
        "{n}, {v}, {src}, {rf}, {sh}, {sn}, {fn}, {ph}, {dm}, "
        + su_sql
        + ", "
        + lv_sql
        + ", "
        + ob_sql
        + ")",
        params,
    ).fetchall()


def _inventory(*objects):
    """Encode ``(type, name)`` pairs with the canonical registry encoder.

    Using ``registry.encode_objects`` rather than a hand-written string is what
    makes these tests an agreement check between the Python encoder and the Lua
    parser in ``module_reclaim.lua``, not merely a check of the Lua against
    itself.
    """
    return encode_objects([{"type": obj_type, "name": name} for obj_type, name in objects])


def _retire(conn, name, version):
    return conn.execute(
        "EXECUTE SCRIPT PREPROC.MODULE_RETIRE({n}, {v})", {"n": name, "v": version}
    ).fetchall()


def _reclaim(conn, name, version):
    """Run MODULE_RECLAIM; result row is (name, version, dropped, reclaimed, reason)."""
    return conn.execute(
        "EXECUTE SCRIPT PREPROC.MODULE_RECLAIM({n}, {v})", {"n": name, "v": version}
    ).fetchall()


def _ensure_rule(
    conn,
    *,
    scope_type,
    scope_value,
    phase=_PHASE,
    script_name,
    function_name=_FUNCTION,
    norefresh=False,
):
    """Run RULE_ENSURE with a NULL client_pattern; result row is (rule_id, created)."""
    flag = "TRUE" if norefresh else "FALSE"
    return conn.execute(
        "EXECUTE SCRIPT PREPROC.RULE_ENSURE({st}, {sv}, {ph}, {sn}, {fn}, NULL, " + flag + ")",
        {"st": scope_type, "sv": scope_value, "ph": phase, "sn": script_name, "fn": function_name},
    ).fetchall()


def _module_row(conn, name, version):
    """The full PREPROC.MODULES row for (name, version), or None.

    Column order: name(0) version(1) source_repo(2) source_ref(3) sha256(4) script_name(5)
    function_name(6) phase(7) deploy_mode(8) status(9) installed_at(10) retired_at(11)
    source_url(12) library_version(13) objects(14). The audit columns are appended
    last so the original positional indices stay valid.
    """
    rows = conn.execute(
        "SELECT name, version, source_repo, source_ref, sha256, script_name, function_name, "
        "phase, deploy_mode, status, installed_at, retired_at, source_url, library_version, "
        "objects FROM PREPROC.MODULES WHERE name = {n} AND version = {v}",
        {"n": name, "v": version},
    ).fetchall()
    return rows[0] if rows else None


def _module_count(conn, name, version):
    return conn.execute(
        "SELECT COUNT(*) FROM PREPROC.MODULES WHERE name = {n} AND version = {v}",
        {"n": name, "v": version},
    ).fetchval()


def _script_exists(conn, qualified):
    schema, _, name = qualified.partition(".")
    return (
        conn.execute(
            "SELECT COUNT(*) FROM EXA_ALL_SCRIPTS WHERE SCRIPT_SCHEMA = {s} AND SCRIPT_NAME = {n}",
            {"s": schema, "n": name},
        ).fetchval()
        > 0
    )


def _table_exists(conn, qualified):
    schema, _, name = qualified.partition(".")
    return (
        conn.execute(
            "SELECT COUNT(*) FROM EXA_ALL_TABLES WHERE TABLE_SCHEMA = {s} AND TABLE_NAME = {n}",
            {"s": schema, "n": name},
        ).fetchval()
        > 0
    )


def _schema_exists(conn, name):
    return (
        conn.execute(
            "SELECT COUNT(*) FROM EXA_ALL_SCHEMAS WHERE SCHEMA_NAME = {n}", {"n": name}
        ).fetchval()
        > 0
    )


def _enabled_rule_ids(conn, *, scope_type, scope_value, phase, script_name, function_name):
    """rule_ids of ENABLED, NULL-client_pattern rules matching the natural key."""
    return [
        r[0]
        for r in conn.execute(
            "SELECT rule_id FROM PREPROC.CONFIG WHERE enabled = TRUE "
            "AND scope_type = {st} AND scope_value = {sv} AND phase = {ph} "
            "AND script_name = {sn} AND function_name = {fn} AND client_pattern IS NULL "
            "ORDER BY rule_id",
            {
                "st": scope_type,
                "sv": scope_value,
                "ph": phase,
                "sn": script_name,
                "fn": function_name,
            },
        ).fetchall()
    ]


def _probe_transformed():
    """Activate MASTER on a FRESH connection and return the fetched sentinel scalar.

    A fresh session resolves the current committed pipeline (no stale pin) and
    MASTER transforms the triggering statement before any convenience-mode swap.
    """
    probe = connect_from_env()
    try:
        probe.execute(_ACTIVATE)
        return probe.execute(_PROBE).fetchval()
    finally:
        try:
            probe.execute(_DEACTIVATE)
        finally:
            probe.close()


# --- provenance: MODULE_REGISTER -------------------------------------------


@pytest.mark.integration
def test_module_register_writes_provenance_with_objects(lifecycle_env):
    """MODULE_REGISTER writes one deployed row, inventory included, RT-visible.

    The nullable audit trio is source_url, library_version, and objects: each is
    recorded when supplied and NULL when it is not, so a pre-composite caller and
    a composite one both round-trip through the same MERGE.
    """
    conn = lifecycle_env
    url = (
        "https://raw.githubusercontent.com/exasol-labs/preprocessor-library/"
        "v0.2.0/registry/index.json"
    )
    declared = [
        {"type": ENTRY_OBJECT_TYPE, "name": _SCRIPT_V1},
        {"type": _TABLE, "name": _STATE_V1},
    ]

    result = _register(
        conn,
        version=1,
        ref="v1",
        script_name=_SCRIPT_V1,
        source_url=url,
        library_version="0.2.0",
        objects=encode_objects(declared),
    )
    assert result[0][3] is True, "first register must report created=TRUE"

    row = _module_row(conn, _MODULE_NAME, 1)
    assert row is not None, "no provenance row was written"
    assert row[9] == "deployed"
    assert row[10] is not None, "installed_at must be stamped"
    assert row[11] is None, "retired_at must be NULL for a deployed version"
    assert row[12] == url
    assert row[13] == "0.2.0"
    assert decode_objects(row[14]) == declared, "the object inventory was not recorded verbatim"

    # A caller supplying none of the three records NULL for each.
    _register(conn, version=2, ref="v2", script_name=_SCRIPT_V2)
    bare = _module_row(conn, _MODULE_NAME, 2)
    assert (bare[12], bare[13], bare[14]) == (None, None, None)

    # PREPROC_RT.INSTALLED_MODULES exposes the row and its inventory. It is
    # PUBLIC-readable via the schema-level grant on PREPROC_RT (ADR-002); the read
    # is verified here from the admin connection because provisioning a throwaway
    # non-admin user is not reliable in the integration env (see
    # test_control_admin_scripts).
    view = conn.execute(
        "SELECT name, version, status, objects FROM PREPROC_RT.INSTALLED_MODULES "
        "WHERE name = {n} AND version = 1",
        {"n": _MODULE_NAME},
    ).fetchall()
    assert len(view) == 1
    assert view[0][0] == _MODULE_NAME
    assert int(view[0][1]) == 1
    assert view[0][2] == "deployed"
    assert decode_objects(view[0][3]) == declared


@pytest.mark.integration
def test_provenance_records_full_inventory(lifecycle_env):
    """A composite module's row records every object its artifact creates, in file order.

    The recorded inventory is pinned to what the artifact REALLY creates, not to a
    hand-written literal: the declared list is first checked against the inventory
    the canonical splitter derives from ``_COMPOSITE_ARTIFACT``, and only then
    registered and read back. Declared, derived, and recorded therefore all have
    to agree for this to pass.
    """
    conn = lifecycle_env
    declared = [
        {"type": ENTRY_OBJECT_TYPE, "name": _SCRIPT_V1},
        {"type": _PY_UDF, "name": _UDF_V1},
        {"type": _PY_UDF, "name": _FMT_UDF_V1},
        {"type": _TABLE, "name": _STATE_V1},
    ]
    assert derive_objects(split_statements(_COMPOSITE_ARTIFACT)) == declared, (
        "the fixture artifact no longer creates exactly the four declared objects"
    )

    _register(conn, version=1, ref="v1", script_name=_SCRIPT_V1, objects=encode_objects(declared))

    row = _module_row(conn, _MODULE_NAME, 1)
    assert row is not None
    assert decode_objects(row[14]) == declared, "the full four-object inventory was not recorded"


@pytest.mark.integration
def test_module_register_records_source_url_and_library_version(lifecycle_env):
    """MODULE_REGISTER persists the effective source URL and resolved library release.

    These are the audit anchor for an install resolved through a mutable `latest`
    source: even though the URL's tag keeps moving, the recorded library_version
    ties the install to a concrete release, and source_url shows what was fetched.
    """
    conn = lifecycle_env
    url = "https://raw.githubusercontent.com/exasol-labs/preprocessor-library/latest/registry/index.json"

    _register(
        conn,
        version=1,
        ref="v1",
        script_name=_SCRIPT_V1,
        source_url=url,
        library_version="0.2.0",
    )

    row = _module_row(conn, _MODULE_NAME, 1)
    assert row is not None
    assert row[12] == url, "source_url must record the effective fetched source"
    assert row[13] == "0.2.0", "library_version must record the resolved release"

    # A NULL library_version (older index / manual registration) is accepted.
    _register(conn, version=2, ref="v2", script_name=_SCRIPT_V1)
    row2 = _module_row(conn, _MODULE_NAME, 2)
    assert row2 is not None
    assert row2[13] is None


@pytest.mark.integration
def test_module_register_idempotent(lifecycle_env):
    """Re-registering the identical (name, version) leaves exactly one row."""
    conn = lifecycle_env

    first = _register(conn, version=1, ref="v1", script_name=_SCRIPT_V1)
    assert first[0][3] is True

    second = _register(conn, version=1, ref="v1", script_name=_SCRIPT_V1)
    assert second[0][3] is False, "re-register must report created=FALSE (matched)"

    assert _module_count(conn, _MODULE_NAME, 1) == 1


# --- idempotent rule ensure: RULE_ENSURE -----------------------------------


@pytest.mark.integration
def test_rule_ensure_no_duplicate(lifecycle_env):
    """RULE_ENSURE on an identical enabled rule returns the existing id, no dup."""
    conn = lifecycle_env
    scope_value = "TEST_ANALYSTS"

    first = _ensure_rule(conn, scope_type="ROLE", scope_value=scope_value, script_name=_SCRIPT_V1)
    assert first[0][1] is True, "first ensure must create the rule"
    first_id = first[0][0]

    second = _ensure_rule(conn, scope_type="ROLE", scope_value=scope_value, script_name=_SCRIPT_V1)
    assert second[0][1] is False, "second ensure must not create a duplicate"
    assert second[0][0] == first_id, "ensure must return the existing rule_id"

    matching = _enabled_rule_ids(
        conn,
        scope_type="ROLE",
        scope_value=scope_value,
        phase=_PHASE,
        script_name=_SCRIPT_V1,
        function_name=_FUNCTION,
    )
    assert matching == [first_id], "exactly one enabled rule must match the tuple"


@pytest.mark.integration
def test_rule_ensure_creates(lifecycle_env):
    """RULE_ENSURE creates the rule when none matches and resolves it on refresh."""
    conn = lifecycle_env
    user = _current_user(conn)  # USER scope so the rule resolves for this user

    result = _ensure_rule(conn, scope_type="USER", scope_value=user, script_name=_SCRIPT_V1)
    assert result[0][1] is True, "ensure must create when no rule matches"

    matching = _enabled_rule_ids(
        conn,
        scope_type="USER",
        scope_value=user,
        phase=_PHASE,
        script_name=_SCRIPT_V1,
        function_name=_FUNCTION,
    )
    assert len(matching) == 1, "exactly one new enabled rule must exist"

    # norefresh defaulted to FALSE, so the auto-refresh materialised the rule.
    resolved = conn.execute(
        "SELECT COUNT(*) FROM PREPROC.RESOLUTION "
        "WHERE user_name = {u} AND phase = {ph} AND script_name = {sn} AND function_name = {fn}",
        {"u": user, "ph": _PHASE, "sn": _SCRIPT_V1, "fn": _FUNCTION},
    ).fetchval()
    assert resolved >= 1, "the new rule must be resolved into RESOLUTION after the refresh"


# --- update flow: deploy V+1 -> repoint -> retire V ------------------------


@pytest.mark.integration
def test_module_update_flow(lifecycle_env):
    """Update deploys V2, repoints the rule, retires V1; V2 transforms in-scope."""
    conn = lifecycle_env
    user = _current_user(conn)

    # V1 deployed and dispatching at USER scope.
    _register(conn, version=1, ref="v1", script_name=_SCRIPT_V1)
    created = _ensure_rule(conn, scope_type="USER", scope_value=user, script_name=_SCRIPT_V1)
    rule_id = created[0][0]
    assert _probe_transformed() == f"{_SEED}|V1", "V1 must transform before the update"

    # Deploy V2 (script created by the fixture), register V2, repoint the SAME rule
    # to V2 and refresh (the atomic cutover), then retire V1.
    _register(conn, version=2, ref="v2", script_name=_SCRIPT_V2)
    conn.execute(
        "UPDATE PREPROC.CONFIG SET script_name = {sn} WHERE rule_id = {rid}",
        {"sn": _SCRIPT_V2, "rid": rule_id},
    )
    conn.execute("EXECUTE SCRIPT PREPROC.REFRESH").fetchall()
    _retire(conn, _MODULE_NAME, 1)

    # CONFIG now names V2 for that rule.
    names = conn.execute(
        "SELECT script_name FROM PREPROC.CONFIG WHERE rule_id = {rid}", {"rid": rule_id}
    ).fetchval()
    assert names == _SCRIPT_V2

    # Provenance: V2 deployed, V1 retired with a retired_at stamp.
    v1 = _module_row(conn, _MODULE_NAME, 1)
    v2 = _module_row(conn, _MODULE_NAME, 2)
    assert v1[9] == "retired" and v1[11] is not None
    assert v2[9] == "deployed" and v2[11] is None

    # An in-scope statement is now transformed by V2.
    assert _probe_transformed() == f"{_SEED}|V2", "V2 must transform after the repoint"


# --- generational reclaim: retire, then guarded drop ------------------------


@pytest.mark.integration
def test_module_reclaim_blocked_by_profile(lifecycle_env):
    """A retired version is not dropped while a live profile bakes its script.

    The profile guard is isolated: a live (non-orphaned) PROFILES manifest bakes
    CAST_SHORTHAND_V1 while NO enabled CONFIG rule names it, so only the profile
    guard can block. The manifest mirrors REFRESH_CORE's pipe-delimited format.
    """
    conn = lifecycle_env

    _register(conn, version=1, ref="v1", script_name=_SCRIPT_V1)
    _retire(conn, _MODULE_NAME, 1)

    manifest = f"{_PHASE}|1|{_SCRIPT_V1}|{_FUNCTION}|"
    conn.execute(
        "INSERT INTO PREPROC.PROFILES (profile_hash, manifest, created_at, orphaned_at) "
        "VALUES ({h}, {m}, CURRENT_TIMESTAMP, NULL)",
        {"h": "0123456789abcdef0123456789abcdef", "m": manifest},
    )
    conn.commit()

    result = _reclaim(conn, _MODULE_NAME, 1)
    assert result[0][2] is False, "the script must NOT be dropped"
    assert result[0][3] is False, "the provenance row must NOT be removed"
    assert result[0][4] == "blocked_by_profile"

    assert _script_exists(conn, _SCRIPT_V1), "a referenced retired script was dropped"
    v1 = _module_row(conn, _MODULE_NAME, 1)
    assert v1 is not None and v1[9] == "retired", "the V1 row must remain retired"


@pytest.mark.integration
def test_module_reclaim_when_unreferenced(lifecycle_env):
    """A retired version is dropped once no rule and no live profile reference it."""
    conn = lifecycle_env

    _register(conn, version=1, ref="v1", script_name=_SCRIPT_V1)
    _retire(conn, _MODULE_NAME, 1)

    # CONFIG and PROFILES are empty (guards pass), so reclaim drops and removes.
    assert _script_exists(conn, _SCRIPT_V1)
    result = _reclaim(conn, _MODULE_NAME, 1)
    assert result[0][2] is True, "the artifact must be dropped"
    assert result[0][3] is True, "the provenance row must be removed"
    assert result[0][4] == "reclaimed"

    assert not _script_exists(conn, _SCRIPT_V1), "the unreferenced retired script was not dropped"
    assert _module_row(conn, _MODULE_NAME, 1) is None, "the reclaimed row was not removed"


# --- inventory-driven reclaim (ADR-055, ADR-056, ADR-057) -------------------


@pytest.mark.integration
def test_reclaim_drops_all_inventory_objects_reverse_order(composite_env):
    """Reclaim drops every recorded object, and only REVERSE file order can succeed.

    The inventory is ordered so that forward teardown is impossible: the module's
    own schema comes FIRST and a table inside it comes second, and a SCHEMA is
    dropped RESTRICT, never CASCADE (reclaim must not destroy data it does not
    own). Dropping the schema before its table therefore fails and leaves the
    schema behind, so reaching ``reclaimed`` with all four objects gone is
    achievable only by walking the inventory backwards.
    """
    conn = composite_env
    objects = (
        (_SCHEMA, _OWNED_SCHEMA_V1),
        (_TABLE, _OWNED_CHILD_V1),
        (_PY_UDF, _UDF_V1),
        (ENTRY_OBJECT_TYPE, _SCRIPT_V1),
    )
    _register(conn, version=1, ref="v1", script_name=_SCRIPT_V1, objects=_inventory(*objects))
    _retire(conn, _MODULE_NAME, 1)

    assert _schema_exists(conn, _OWNED_SCHEMA_V1)
    assert _table_exists(conn, _OWNED_CHILD_V1)
    assert _script_exists(conn, _UDF_V1)
    assert _script_exists(conn, _SCRIPT_V1)

    result = _reclaim(conn, _MODULE_NAME, 1)
    assert result[0][4] == "reclaimed", (
        "forward-order teardown fails here on the still-populated owned schema"
    )
    assert result[0][2] is True, "the inventory must be reported dropped"
    assert result[0][3] is True, "the provenance row must be removed"

    assert not _script_exists(conn, _SCRIPT_V1)
    assert not _script_exists(conn, _UDF_V1), "a Python UDF companion was not dropped"
    assert not _table_exists(conn, _OWNED_CHILD_V1)
    assert not _schema_exists(conn, _OWNED_SCHEMA_V1), "the module-owned schema was not dropped"
    assert _module_row(conn, _MODULE_NAME, 1) is None, "the reclaimed row was not removed"


@pytest.mark.integration
def test_reclaim_drops_out_of_schema_objects(composite_env):
    """A recorded object outside PREPROC_RT is dropped; the old schema guard is gone.

    ``_OUT_SCHEMA`` itself is NOT in the inventory — the module only put a table
    inside it — so the schema must survive. The inventory is the boundary in both
    directions: it admits the out-of-schema table AND it protects the schema.
    """
    conn = composite_env
    objects = ((ENTRY_OBJECT_TYPE, _SCRIPT_V1), (_TABLE, _STATE_V1))
    _register(conn, version=1, ref="v1", script_name=_SCRIPT_V1, objects=_inventory(*objects))
    _retire(conn, _MODULE_NAME, 1)
    assert _table_exists(conn, _STATE_V1)

    result = _reclaim(conn, _MODULE_NAME, 1)
    assert result[0][4] == "reclaimed", "the former PREPROC_RT-only guard still blocks"
    assert result[0][2] is True

    assert not _table_exists(conn, _STATE_V1), "the non-PREPROC_RT object was not dropped"
    assert not _script_exists(conn, _SCRIPT_V1)
    assert _schema_exists(conn, _OUT_SCHEMA), "reclaim dropped a schema no inventory records"
    assert _module_row(conn, _MODULE_NAME, 1) is None


@pytest.mark.integration
def test_reclaim_spares_objects_absent_from_inventory(composite_env):
    """Reclaim drops the recorded inventory and NOTHING else, in any schema.

    This is the test that makes the widened ownership boundary real, so every
    survivor is a decoy a WIDER boundary would have taken:

      * ``_DECOY_SCRIPT`` has the entry script's fully-qualified name as a strict
        prefix and shares its schema, so a name-prefix or LIKE match, or a sweep
        of the entry script's schema, drops it.
      * ``_DECOY_TABLE`` sits in the very schema a recorded object lives in, so a
        sweep of a touched schema — or CASCADE-ing it — drops it.
      * ``_HELPER_V1`` and ``_UDF_V1`` share the module's name prefix AND its _V1
        generation suffix but are absent from THIS version's inventory, so only
        reading the inventory spares them.
      * ``_OUT_SCHEMA`` and ``_OWNED_SCHEMA_V1`` are schemas the inventory does
        not name, so neither may be dropped even though the reclaim empties part
        of one.

    The reclaim must also SUCCEED. Sparing every decoy by refusing to do anything
    would satisfy the survival assertions on their own, so ``reclaimed`` and the
    recorded objects' absence are asserted alongside them.
    """
    conn = composite_env
    objects = ((ENTRY_OBJECT_TYPE, _SCRIPT_V1), (_TABLE, _STATE_V1))
    _register(conn, version=1, ref="v1", script_name=_SCRIPT_V1, objects=_inventory(*objects))
    _retire(conn, _MODULE_NAME, 1)

    assert _script_exists(conn, _DECOY_SCRIPT), "the decoy script fixture did not deploy"
    assert _table_exists(conn, _DECOY_TABLE), "the decoy table fixture did not deploy"

    result = _reclaim(conn, _MODULE_NAME, 1)
    assert result[0][4] == "reclaimed", "the reclaim itself must have succeeded"
    assert not _script_exists(conn, _SCRIPT_V1), "the recorded entry script was not dropped"
    assert not _table_exists(conn, _STATE_V1), "the recorded table was not dropped"

    assert _script_exists(conn, _DECOY_SCRIPT), (
        "reclaim dropped a PREPROC_RT script whose name merely extends the entry script's"
    )
    assert _table_exists(conn, _DECOY_TABLE), (
        "reclaim dropped a sibling table in a schema it only had one recorded object in"
    )
    for spared in (_HELPER_V1, _UDF_V1):
        assert _script_exists(conn, spared), (
            f"reclaim dropped {spared}, which shares the module's name and generation "
            "but is absent from this version's recorded inventory"
        )
    for schema in (_OUT_SCHEMA, _OWNED_SCHEMA_V1):
        assert _schema_exists(conn, schema), f"reclaim dropped schema {schema}, in no inventory"


@pytest.mark.integration
def test_null_inventory_row_reclaims_entry_script(composite_env):
    """A row with NULL objects reclaims exactly the one object script_name names.

    This is the pre-composite provenance row, and equally the single-statement
    module: the NULL is read as the single LUA SCRIPT named by ``script_name`` —
    the same one-object inventory ``preproc.module.registry`` derives for a
    schema_version 1 index entry — so the drop set is one object, never a sweep,
    and the absent inventory is not an error.
    """
    conn = composite_env
    _register(conn, version=1, ref="v1", script_name=_SCRIPT_V1)
    row = _module_row(conn, _MODULE_NAME, 1)
    assert row[14] is None, "this test requires a NULL recorded inventory"
    _retire(conn, _MODULE_NAME, 1)

    result = _reclaim(conn, _MODULE_NAME, 1)
    assert result[0][4] == "reclaimed", "an absent inventory must not fail the reclaim"
    assert result[0][2] is True, "the entry script must be reported dropped"
    assert result[0][3] is True
    assert not _script_exists(conn, _SCRIPT_V1), "the entry script named by script_name survived"
    assert _module_row(conn, _MODULE_NAME, 1) is None

    # The fallback names ONE object: same-generation companions are not swept up.
    assert _script_exists(conn, _HELPER_V1)
    assert _script_exists(conn, _UDF_V1)
    assert _table_exists(conn, _STATE_V1)


@pytest.mark.integration
def test_reclaim_v1_leaves_v2_companions(composite_env):
    """Reclaiming v1 drops every v1 object and no v2 object; v2 still transforms.

    ``_HELPER_V1`` and ``_HELPER_V2`` have BYTE-IDENTICAL bodies and differ only in
    their _V<N>-suffixed names (ADR-056), so nothing but the recorded name can
    separate the two generations — which is the discipline under test. v2 is the
    live generation, so both liveness guards pass for v1: the rule names V2, and
    the profile the probe bakes contains V2, whose manifest entry cannot
    prefix-match the V1 needle because the needle is flanked by '|'.
    """
    conn = composite_env
    user = _current_user(conn)
    v1_objects = (
        (ENTRY_OBJECT_TYPE, _SCRIPT_V1),
        (_LUA_SCRIPT, _HELPER_V1),
        (_TABLE, _STATE_V1),
    )
    v2_objects = (
        (ENTRY_OBJECT_TYPE, _SCRIPT_V2),
        (_LUA_SCRIPT, _HELPER_V2),
        (_TABLE, _STATE_V2),
    )
    _register(conn, version=1, ref="v1", script_name=_SCRIPT_V1, objects=_inventory(*v1_objects))
    _register(conn, version=2, ref="v2", script_name=_SCRIPT_V2, objects=_inventory(*v2_objects))

    _ensure_rule(conn, scope_type="USER", scope_value=user, script_name=_SCRIPT_V2)
    assert _probe_transformed() == f"{_SEED}|V2", "V2 must transform before the v1 reclaim"

    _retire(conn, _MODULE_NAME, 1)
    result = _reclaim(conn, _MODULE_NAME, 1)
    assert result[0][4] == "reclaimed"
    assert result[0][2] is True

    for gone in (_SCRIPT_V1, _HELPER_V1):
        assert not _script_exists(conn, gone), f"{gone} survived the v1 reclaim"
    assert not _table_exists(conn, _STATE_V1)

    for live in (_SCRIPT_V2, _HELPER_V2):
        assert _script_exists(conn, live), f"reclaiming v1 dropped the live {live}"
    assert _table_exists(conn, _STATE_V2), "reclaiming v1 dropped the live v2 table"
    assert _module_row(conn, _MODULE_NAME, 2) is not None, "the v2 provenance row was removed"

    assert _probe_transformed() == f"{_SEED}|V2", "v2 must still transform after the v1 reclaim"


@pytest.mark.integration
def test_module_update_missing_provenance_rejected(lifecycle_env):
    """Retiring V1 with no provenance row fails before any write; state unchanged.

    This is the enforcement point behind "an update naming a version with no
    provenance MUST fail before mutating anything": the update flow's retire step
    refuses to proceed, leaving CONFIG and MODULES untouched.
    """
    conn = lifecycle_env
    user = _current_user(conn)

    _ensure_rule(conn, scope_type="USER", scope_value=user, script_name=_SCRIPT_V1)
    config_before = conn.execute(
        "SELECT rule_id, script_name FROM PREPROC.CONFIG ORDER BY rule_id"
    ).fetchall()
    assert conn.execute("SELECT COUNT(*) FROM PREPROC.MODULES").fetchval() == 0

    with pytest.raises(Exception):  # noqa: PT011 - any DB-side rejection is acceptable
        _retire(conn, _MODULE_NAME, 1)

    config_after = conn.execute(
        "SELECT rule_id, script_name FROM PREPROC.CONFIG ORDER BY rule_id"
    ).fetchall()
    assert config_after == config_before, "CONFIG must be unchanged on rejection"
    assert conn.execute("SELECT COUNT(*) FROM PREPROC.MODULES").fetchval() == 0, (
        "MODULES must be unchanged on rejection"
    )
