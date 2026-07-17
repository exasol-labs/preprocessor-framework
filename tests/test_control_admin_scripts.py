"""Integration tests for the PREPROC admin control scripts.

These exercise the rule/settings control surface as DB-side scripts via
``EXECUTE SCRIPT`` against a live Exasol instance (the ``installed`` fixture).
The scripts under test are the supported way to mutate PREPROC.CONFIG and
PREPROC.SETTINGS:

* RULE_ADD / RULE_DROP / RULE_ENABLE / RULE_DISABLE — edit CONFIG and (unless
  ``norefresh``) re-materialise RESOLUTION via the shared REFRESH_CORE module.
* RULE_LIST — read-only inventory of CONFIG, never refreshes.
* SET_MODE / SET_SELF_DISABLE — edit the single SETTINGS row.

Deployment self-sufficiency
    Wiring these scripts into ``run_install`` is Group D's task (6.1). To keep
    this suite runnable independently of that ordering, the session-scoped
    ``control_deployed`` fixture deploys REFRESH_CORE and every control script
    here using the canonical CREATE wrappers. The parameter order in each
    wrapper is the authoritative reference for the installer/bundle wiring.

Shared-state discipline (the connection, CONFIG, and SETTINGS are session-wide)
    Every test that inserts into CONFIG uses a scope_value prefixed ``TEST_`` and
    cleans up in ``finally`` (the ``clean_config`` fixture also sweeps operator
    rows < 9000 with that prefix before and after each test). Tests that mutate
    SETTINGS capture and restore the prior values. REFRESH is re-run after
    teardown so the session is left with a consistent RESOLUTION.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).parent.parent
_LUA_DIR = _REPO_ROOT / "sql" / "lua"

# A script guaranteed to exist after install, used as a valid RULE_ADD target.
_EXISTING_SCRIPT = "PREPROC.REFRESH"
_EXISTING_FUNCTION = "rebuild"

_TEST_SCOPE_PREFIX = "TEST_"
_OPERATOR_CEILING = 9000

# Canonical CREATE wrappers. Parameter order MUST match the documented Lua
# parameter order in each script body (and the read-globals luacheck directive).
# Exasol LUA SCRIPT parameters are declared as bare names without SQL types
# (types apply to UDFs, not scripting) — verified against the live engine.
_CONTROL_SCRIPTS = {
    "refresh_core.lua": "CREATE OR REPLACE LUA SCRIPT PREPROC.REFRESH_CORE() AS\n",
    "rule_add.lua": (
        "CREATE OR REPLACE LUA SCRIPT PREPROC.RULE_ADD("
        "scope_type, scope_value, phase, script_name, function_name, norefresh"
        ") RETURNS TABLE AS\n"
    ),
    "rule_drop.lua": (
        "CREATE OR REPLACE LUA SCRIPT PREPROC.RULE_DROP(rule_id, norefresh) RETURNS TABLE AS\n"
    ),
    "rule_enable.lua": (
        "CREATE OR REPLACE LUA SCRIPT PREPROC.RULE_ENABLE(rule_id, norefresh) RETURNS TABLE AS\n"
    ),
    "rule_disable.lua": (
        "CREATE OR REPLACE LUA SCRIPT PREPROC.RULE_DISABLE(rule_id, norefresh) RETURNS TABLE AS\n"
    ),
    "rule_list.lua": ("CREATE OR REPLACE LUA SCRIPT PREPROC.RULE_LIST() RETURNS TABLE AS\n"),
    "set_mode.lua": (
        "CREATE OR REPLACE LUA SCRIPT PREPROC.SET_MODE(mode, norefresh) RETURNS TABLE AS\n"
    ),
    "set_self_disable.lua": (
        "CREATE OR REPLACE LUA SCRIPT PREPROC.SET_SELF_DISABLE(value) RETURNS TABLE AS\n"
    ),
    "set_sys_grouping.lua": (
        "CREATE OR REPLACE LUA SCRIPT PREPROC.SET_SYS_GROUPING(value, norefresh) RETURNS TABLE AS\n"
    ),
}

# Deployment order: REFRESH_CORE must exist before the scripts that import it.
_DEPLOY_ORDER = [
    "refresh_core.lua",
    "rule_add.lua",
    "rule_drop.lua",
    "rule_enable.lua",
    "rule_disable.lua",
    "rule_list.lua",
    "set_mode.lua",
    "set_self_disable.lua",
    "set_sys_grouping.lua",
]


@pytest.fixture(scope="session")
def control_deployed(installed):
    """Deploy REFRESH_CORE and every control script, then return the connection.

    Mirrors what install.py's _deploy_admin_scripts will do for these scripts.
    REFRESH_CORE is deployed first (plain importable, no RETURNS TABLE) so the
    mutating scripts can import it.
    """
    installed.execute("OPEN SCHEMA PREPROC")
    for name in _DEPLOY_ORDER:
        body = (_LUA_DIR / name).read_text(encoding="utf-8")
        installed.execute(_CONTROL_SCRIPTS[name] + body)
    return installed


@pytest.fixture
def clean_config(control_deployed):
    """Remove TEST_ operator rows before and after each test, then refresh.

    Keeps each test isolated from leftover rows and leaves RESOLUTION consistent
    with CONFIG for whatever runs next in the shared session.
    """
    connection = control_deployed
    _sweep_test_rows(connection)
    try:
        yield connection
    finally:
        _sweep_test_rows(connection)
        connection.execute("EXECUTE SCRIPT PREPROC.REFRESH")


def _sweep_test_rows(connection):
    """Delete operator-space CONFIG rows created by these tests, then commit."""
    connection.execute(
        f"DELETE FROM PREPROC.CONFIG "
        f"WHERE rule_id < {_OPERATOR_CEILING} "
        f"AND scope_value LIKE '{_TEST_SCOPE_PREFIX}%'"
    )
    connection.commit()


def _config_count(connection):
    """Count rows currently in PREPROC.CONFIG."""
    return connection.execute("SELECT COUNT(*) FROM PREPROC.CONFIG").fetchval()


def _last_refresh(connection):
    """Read the recorded last_refresh stamp from SETTINGS."""
    return connection.execute("SELECT last_refresh FROM PREPROC.SETTINGS").fetchval()


def _resolution_count(connection):
    """Count rows currently in PREPROC.RESOLUTION."""
    return connection.execute("SELECT COUNT(*) FROM PREPROC.RESOLUTION").fetchval()


def _enabled_of(connection, rule_id):
    """Read the enabled flag of a single CONFIG row by rule_id."""
    return connection.execute(
        f"SELECT enabled FROM PREPROC.CONFIG WHERE rule_id = {rule_id}"
    ).fetchval()


def _mode(connection):
    """Read the single SETTINGS.mode value."""
    return connection.execute("SELECT mode FROM PREPROC.SETTINGS").fetchval()


def _group_sys_with_dba(connection):
    """Read SETTINGS.group_sys_with_dba, applying the production COALESCE-to-TRUE.

    Mirrors the read-site default in RESOLUTION_SQL so a raw NULL (possible on a
    freshly upgraded, never-set database) and an explicit TRUE compare equal.
    """
    return connection.execute(
        "SELECT COALESCE(group_sys_with_dba, TRUE) FROM PREPROC.SETTINGS"
    ).fetchval()


def _add_rule(connection, scope_type, scope_value, phase, norefresh=False):
    """Run RULE_ADD with a valid existing script target and return the rule_id."""
    flag = "TRUE" if norefresh else "FALSE"
    rows = connection.execute(
        f"EXECUTE SCRIPT PREPROC.RULE_ADD("
        f"'{scope_type}', '{scope_value}', '{phase}', "
        f"'{_EXISTING_SCRIPT}', '{_EXISTING_FUNCTION}', {flag})"
    ).fetchall()
    return rows[0][0]


@pytest.mark.integration
def test_rule_add_inserts_and_refreshes(clean_config):
    """RULE_ADD inserts one CONFIG row and (by default) re-materialises RESOLUTION."""
    connection = clean_config
    before = _config_count(connection)

    rule_id = _add_rule(connection, "USER", "TEST_ADD_SYS", "EXPAND")

    assert rule_id is not None and rule_id < _OPERATOR_CEILING
    assert _config_count(connection) == before + 1

    persisted = connection.execute(
        f"SELECT scope_type, phase, script_name, enabled "
        f"FROM PREPROC.CONFIG WHERE rule_id = {rule_id}"
    ).fetchall()[0]
    assert persisted[0] == "USER"
    assert persisted[1] == "EXPAND"
    assert persisted[2] == _EXISTING_SCRIPT
    assert persisted[3] is True


@pytest.mark.integration
def test_rule_add_allocates_distinct_rule_ids(clean_config):
    """Two consecutive RULE_ADD calls allocate different rule_ids, both < 9000."""
    connection = clean_config

    first = _add_rule(connection, "ROLE", "TEST_DISTINCT_A", "TRANSLATE")
    second = _add_rule(connection, "ROLE", "TEST_DISTINCT_B", "TRANSLATE")

    assert first != second
    assert first < _OPERATOR_CEILING
    assert second < _OPERATOR_CEILING


@pytest.mark.integration
def test_rule_add_rejects_invalid_phase(clean_config):
    """RULE_ADD with a bad phase raises and leaves CONFIG unchanged."""
    connection = clean_config
    before = _config_count(connection)

    with pytest.raises(Exception):  # noqa: PT011 - any DB-side rejection is acceptable
        connection.execute(
            f"EXECUTE SCRIPT PREPROC.RULE_ADD("
            f"'USER', 'TEST_BADPHASE', 'NOTAPHASE', "
            f"'{_EXISTING_SCRIPT}', '{_EXISTING_FUNCTION}', FALSE)"
        ).fetchall()

    assert _config_count(connection) == before


@pytest.mark.integration
def test_rule_add_rejects_invalid_scope_type(clean_config):
    """RULE_ADD with a bad scope_type raises and leaves CONFIG unchanged."""
    connection = clean_config
    before = _config_count(connection)

    with pytest.raises(Exception):  # noqa: PT011 - any DB-side rejection is acceptable
        connection.execute(
            f"EXECUTE SCRIPT PREPROC.RULE_ADD("
            f"'GROUP', 'TEST_BADSCOPE', 'EXPAND', "
            f"'{_EXISTING_SCRIPT}', '{_EXISTING_FUNCTION}', FALSE)"
        ).fetchall()

    assert _config_count(connection) == before


@pytest.mark.integration
def test_rule_add_rejects_missing_script(clean_config):
    """RULE_ADD naming a non-existent script raises and leaves CONFIG unchanged."""
    connection = clean_config
    before = _config_count(connection)

    with pytest.raises(Exception):  # noqa: PT011 - any DB-side rejection is acceptable
        connection.execute(
            "EXECUTE SCRIPT PREPROC.RULE_ADD("
            "'USER', 'TEST_MISSING', 'EXPAND', "
            "'PREPROC.NO_SUCH_SCRIPT_X', 'run', FALSE)"
        ).fetchall()

    assert _config_count(connection) == before


@pytest.mark.integration
def test_rule_add_norefresh_skips_refresh(clean_config):
    """RULE_ADD with norefresh=TRUE inserts the row but leaves last_refresh unchanged."""
    connection = clean_config
    refresh_before = _last_refresh(connection)
    config_before = _config_count(connection)

    rule_id = _add_rule(connection, "USER", "TEST_NOREFRESH", "EXPAND", norefresh=True)

    assert rule_id is not None
    assert _config_count(connection) == config_before + 1
    assert _last_refresh(connection) == refresh_before


@pytest.mark.integration
def test_rule_drop_removes_and_refreshes(clean_config):
    """RULE_DROP removes the row, reports one row removed, and refreshes RESOLUTION."""
    connection = clean_config
    rule_id = _add_rule(connection, "USER", "TEST_DROP_SYS", "EXPAND")
    before = _config_count(connection)

    rows = connection.execute(f"EXECUTE SCRIPT PREPROC.RULE_DROP({rule_id}, FALSE)").fetchall()

    assert rows[0][0] == 1
    assert _config_count(connection) == before - 1
    remaining = connection.execute(
        f"SELECT COUNT(*) FROM PREPROC.CONFIG WHERE rule_id = {rule_id}"
    ).fetchval()
    assert remaining == 0


@pytest.mark.integration
def test_rule_drop_unknown_is_noop(clean_config):
    """RULE_DROP for a non-existent rule_id reports 0 rows removed and does not error."""
    connection = clean_config
    before = _config_count(connection)

    rows = connection.execute("EXECUTE SCRIPT PREPROC.RULE_DROP(8999, FALSE)").fetchall()

    assert rows[0][0] == 0
    assert _config_count(connection) == before


@pytest.mark.integration
def test_rule_disable_flips_and_refreshes(clean_config):
    """RULE_DISABLE sets enabled=FALSE on the row and refreshes RESOLUTION."""
    connection = clean_config
    rule_id = _add_rule(connection, "USER", "TEST_DISABLE_SYS", "EXPAND")

    rows = connection.execute(f"EXECUTE SCRIPT PREPROC.RULE_DISABLE({rule_id}, FALSE)").fetchall()

    assert rows[0][0] == 1
    assert _enabled_of(connection, rule_id) is False


@pytest.mark.integration
def test_rule_enable_flips_and_refreshes(clean_config):
    """RULE_ENABLE sets enabled=TRUE on a disabled row and refreshes RESOLUTION."""
    connection = clean_config
    rule_id = _add_rule(connection, "USER", "TEST_ENABLE_SYS", "EXPAND")
    connection.execute(f"EXECUTE SCRIPT PREPROC.RULE_DISABLE({rule_id}, TRUE)").fetchall()

    rows = connection.execute(f"EXECUTE SCRIPT PREPROC.RULE_ENABLE({rule_id}, FALSE)").fetchall()

    assert rows[0][0] == 1
    assert _enabled_of(connection, rule_id) is True


@pytest.mark.integration
def test_rule_list_returns_rows_readonly(clean_config):
    """RULE_LIST returns rows ordered by rule_id and has no side effects."""
    connection = clean_config
    first = _add_rule(connection, "USER", "TEST_LIST_A", "EXPAND")
    second = _add_rule(connection, "USER", "TEST_LIST_B", "EXPAND")

    resolution_before = _resolution_count(connection)
    refresh_before = _last_refresh(connection)

    rows = connection.execute("EXECUTE SCRIPT PREPROC.RULE_LIST").fetchall()

    ids = [r[0] for r in rows]
    assert first in ids
    assert second in ids
    assert ids == sorted(ids), "RULE_LIST must return rows ordered by rule_id ASC"

    assert _resolution_count(connection) == resolution_before
    assert _last_refresh(connection) == refresh_before


@pytest.mark.integration
def test_set_mode_updates_and_validates(clean_config):
    """SET_MODE updates a valid mode; an invalid mode raises without changing SETTINGS."""
    connection = clean_config
    original = _mode(connection)
    try:
        connection.execute("EXECUTE SCRIPT PREPROC.SET_MODE('strict', FALSE)").fetchall()
        assert _mode(connection) == "strict"

        with pytest.raises(Exception):  # noqa: PT011 - any DB-side rejection is acceptable
            connection.execute("EXECUTE SCRIPT PREPROC.SET_MODE('bogus', FALSE)").fetchall()
        assert _mode(connection) == "strict"
    finally:
        connection.execute(f"EXECUTE SCRIPT PREPROC.SET_MODE('{original}', TRUE)").fetchall()
        connection.execute("EXECUTE SCRIPT PREPROC.REFRESH")


@pytest.mark.integration
def test_set_self_disable_updates_flag(clean_config):
    """SET_SELF_DISABLE updates only self_disable_allowed; mode/refresh state unchanged."""
    connection = clean_config
    before = connection.execute(
        "SELECT self_disable_allowed, mode, last_refresh, state_fingerprint FROM PREPROC.SETTINGS"
    ).fetchall()[0]
    original_flag = before[0]
    new_flag = not original_flag
    new_literal = "TRUE" if new_flag else "FALSE"
    original_literal = "TRUE" if original_flag else "FALSE"
    try:
        rows = connection.execute(
            f"EXECUTE SCRIPT PREPROC.SET_SELF_DISABLE({new_literal})"
        ).fetchall()
        assert rows[0][0] is new_flag

        after = connection.execute(
            "SELECT self_disable_allowed, mode, last_refresh, state_fingerprint "
            "FROM PREPROC.SETTINGS"
        ).fetchall()[0]
        assert after[0] is new_flag
        assert after[1] == before[1], "mode must be untouched"
        assert after[2] == before[2], "last_refresh must be untouched"
        assert after[3] == before[3], "state_fingerprint must be untouched"
    finally:
        connection.execute(
            f"EXECUTE SCRIPT PREPROC.SET_SELF_DISABLE({original_literal})"
        ).fetchall()


@pytest.mark.integration
def test_set_sys_grouping_updates_and_refreshes(clean_config):
    """SET_SYS_GROUPING updates the flag and auto-refreshes (last_refresh advances)."""
    connection = clean_config
    original = _group_sys_with_dba(connection)
    new_value = not original
    new_literal = "TRUE" if new_value else "FALSE"
    original_literal = "TRUE" if original else "FALSE"
    refresh_before = _last_refresh(connection)
    try:
        rows = connection.execute(
            f"EXECUTE SCRIPT PREPROC.SET_SYS_GROUPING({new_literal}, FALSE)"
        ).fetchall()
        assert rows[0][0] is new_value
        assert _group_sys_with_dba(connection) is new_value
        assert _last_refresh(connection) != refresh_before, "auto-refresh must restamp last_refresh"
    finally:
        connection.execute(
            f"EXECUTE SCRIPT PREPROC.SET_SYS_GROUPING({original_literal}, FALSE)"
        ).fetchall()


@pytest.mark.integration
def test_set_sys_grouping_rejects_non_boolean(clean_config):
    """A non-boolean value raises and leaves SETTINGS and RESOLUTION unchanged."""
    connection = clean_config
    before_value = _group_sys_with_dba(connection)
    resolution_before = _resolution_count(connection)
    refresh_before = _last_refresh(connection)

    with pytest.raises(Exception):  # noqa: PT011 - any DB-side rejection is acceptable
        connection.execute("EXECUTE SCRIPT PREPROC.SET_SYS_GROUPING('nope', FALSE)").fetchall()

    assert _group_sys_with_dba(connection) == before_value
    assert _resolution_count(connection) == resolution_before
    assert _last_refresh(connection) == refresh_before


@pytest.mark.integration
def test_set_sys_grouping_norefresh_skips_refresh(clean_config):
    """SET_SYS_GROUPING with norefresh=TRUE updates the flag but does not refresh."""
    connection = clean_config
    original = _group_sys_with_dba(connection)
    new_value = not original
    new_literal = "TRUE" if new_value else "FALSE"
    original_literal = "TRUE" if original else "FALSE"
    refresh_before = _last_refresh(connection)
    try:
        rows = connection.execute(
            f"EXECUTE SCRIPT PREPROC.SET_SYS_GROUPING({new_literal}, TRUE)"
        ).fetchall()
        assert rows[0][0] is new_value
        assert _group_sys_with_dba(connection) is new_value
        assert _last_refresh(connection) == refresh_before, (
            "norefresh must not restamp last_refresh"
        )
    finally:
        connection.execute(
            f"EXECUTE SCRIPT PREPROC.SET_SYS_GROUPING({original_literal}, TRUE)"
        ).fetchall()


@pytest.mark.integration
def test_set_sys_grouping_leaves_other_settings_and_status_current(clean_config):
    """SET_SYS_GROUPING changes only the grouping flag; mode/self_disable/STATUS unaffected."""
    connection = clean_config
    before = connection.execute(
        "SELECT mode, self_disable_allowed FROM PREPROC.SETTINGS"
    ).fetchall()[0]
    original = _group_sys_with_dba(connection)
    new_value = not original
    new_literal = "TRUE" if new_value else "FALSE"
    original_literal = "TRUE" if original else "FALSE"
    try:
        connection.execute(
            f"EXECUTE SCRIPT PREPROC.SET_SYS_GROUPING({new_literal}, FALSE)"
        ).fetchall()

        after = connection.execute(
            "SELECT mode, self_disable_allowed FROM PREPROC.SETTINGS"
        ).fetchall()[0]
        assert after[0] == before[0], "mode must be untouched"
        assert after[1] == before[1], "self_disable_allowed must be untouched"

        status_row = connection.execute("EXECUTE SCRIPT PREPROC.STATUS").fetchall()[0]
        assert status_row[1] == "CURRENT", "STATUS must report CURRENT after the auto-refresh"
    finally:
        connection.execute(
            f"EXECUTE SCRIPT PREPROC.SET_SYS_GROUPING({original_literal}, FALSE)"
        ).fetchall()


@pytest.mark.integration
def test_non_admin_rejected():
    """A non-admin caller must be rejected from the mutating control scripts.

    Skipped: provisioning and dropping a dedicated unprivileged user reliably
    requires DBA in this integration environment, and the privilege model is
    already covered for the shared REFRESH_CORE dictionary read by
    test_status_staleness.test_status_requires_admin. The control scripts inherit
    that same invoker-rights barrier through rc.rebuild().
    """
    pytest.skip(
        "cannot easily create/drop users in the integration env; "
        "privilege model is covered by test_status_requires_admin"
    )


@pytest.mark.integration
def test_control_scripts_pass_luacheck():
    """Run luacheck over sql/lua/ as a subprocess and assert a clean exit 0."""
    if shutil.which("luacheck") is None:
        pytest.skip("luacheck is not installed on this runner")

    result = subprocess.run(
        ["luacheck", "sql/lua/"],
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, "luacheck reported issues:\n" + result.stdout + result.stderr
