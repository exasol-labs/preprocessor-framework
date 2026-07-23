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

Shared-state discipline (CONFIG/RESOLUTION/PROFILES/MODULES are global tables)
    ``lifecycle_env`` wipes all four tables before and after every test, restores
    convenience mode, deploys the two test module scripts (V1/V2), and on teardown
    deactivates the preprocessor, drops the test scripts, and refreshes to a clean
    (empty) RESOLUTION so nothing leaks into the rest of the shared session.

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
        "name, version, source, ref, sha256, script_name, function_name, phase, deploy_mode"
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
):
    """Run MODULE_REGISTER and return its result rows [(name, version, status, created)]."""
    return conn.execute(
        "EXECUTE SCRIPT PREPROC.MODULE_REGISTER("
        "{n}, {v}, {src}, {rf}, {sh}, {sn}, {fn}, {ph}, {dm})",
        {
            "n": name,
            "v": version,
            "src": source,
            "rf": ref,
            "sh": sha256 if sha256 is not None else "a" * 64,
            "sn": script_name,
            "fn": function_name,
            "ph": phase,
            "dm": deploy_mode,
        },
    ).fetchall()


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
    function_name(6) phase(7) deploy_mode(8) status(9) installed_at(10) retired_at(11).
    """
    rows = conn.execute(
        "SELECT name, version, source_repo, source_ref, sha256, script_name, function_name, "
        "phase, deploy_mode, status, installed_at, retired_at "
        "FROM PREPROC.MODULES WHERE name = {n} AND version = {v}",
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
def test_module_register_writes_row(lifecycle_env):
    """MODULE_REGISTER writes one deployed row and exposes it via the RT view."""
    conn = lifecycle_env

    result = _register(conn, version=1, ref="v1", script_name=_SCRIPT_V1)
    assert result[0][3] is True, "first register must report created=TRUE"

    row = _module_row(conn, _MODULE_NAME, 1)
    assert row is not None, "no provenance row was written"
    assert row[9] == "deployed"
    assert row[10] is not None, "installed_at must be stamped"
    assert row[11] is None, "retired_at must be NULL for a deployed version"

    # PREPROC_RT.INSTALLED_MODULES exposes the row. It is PUBLIC-readable via the
    # schema-level grant on PREPROC_RT (ADR-002); the read is verified here from
    # the admin connection because provisioning a throwaway non-admin user is not
    # reliable in the integration env (see test_control_admin_scripts).
    view = conn.execute(
        "SELECT name, version, status FROM PREPROC_RT.INSTALLED_MODULES "
        "WHERE name = {n} AND version = 1",
        {"n": _MODULE_NAME},
    ).fetchall()
    assert len(view) == 1
    assert view[0][0] == _MODULE_NAME
    assert int(view[0][1]) == 1
    assert view[0][2] == "deployed"


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
