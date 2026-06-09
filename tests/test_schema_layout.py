"""Integration tests for the two-schema database layout.

All tests here require a live Exasol instance (``installed`` fixture) and are
marked ``integration``. They run idempotently — each assertion query is
read-only except for ``test_my_pipeline_filters_current_user`` and
``test_layout_idempotent``, which clean up after themselves in ``finally``.
"""

import pytest

from preproc.install import run_install


@pytest.mark.integration
def test_schemas_created(installed):
    """PREPROC and PREPROC_RT must both appear in EXA_ALL_SCHEMAS."""
    rows = installed.execute(
        "SELECT SCHEMA_NAME FROM EXA_ALL_SCHEMAS "
        "WHERE SCHEMA_NAME IN ('PREPROC', 'PREPROC_RT') "
        "ORDER BY SCHEMA_NAME"
    ).fetchall()
    schema_names = [r[0] for r in rows]
    assert "PREPROC" in schema_names
    assert "PREPROC_RT" in schema_names


@pytest.mark.integration
def test_admin_role_owns_schema(installed):
    """Schema PREPROC must be owned by the PREPROC_ADMIN role."""
    row = installed.execute(
        "SELECT SCHEMA_OWNER FROM EXA_ALL_SCHEMAS WHERE SCHEMA_NAME = 'PREPROC'"
    ).fetchone()
    assert row is not None, "PREPROC schema not found in EXA_ALL_SCHEMAS"
    assert row[0] == "PREPROC_ADMIN"


@pytest.mark.integration
def test_runtime_schema_owned_by_admin(installed):
    """PREPROC_RT must be owned by PREPROC_ADMIN.

    REFRESH_CORE generates the PREPROC_RT.P_<hash> profile scripts under the
    invoker's rights, so a PREPROC_ADMIN holder triggering a refresh must own the
    runtime schema to create/replace/drop scripts there.
    """
    row = installed.execute(
        "SELECT SCHEMA_OWNER FROM EXA_ALL_SCHEMAS WHERE SCHEMA_NAME = 'PREPROC_RT'"
    ).fetchone()
    assert row is not None, "PREPROC_RT schema not found in EXA_ALL_SCHEMAS"
    assert row[0] == "PREPROC_ADMIN"


@pytest.mark.integration
def test_admin_tables_created(installed):
    """CONFIG, RESOLUTION, SETTINGS, and PROFILES must exist in the PREPROC schema.

    Also asserts TABLE_OWNER = 'PREPROC_ADMIN' for all four tables. Exasol
    assigns contained objects to the schema owner, so this catches regressions
    where schema ownership drifts.
    """
    rows = installed.execute(
        "SELECT TABLE_NAME, TABLE_OWNER FROM EXA_ALL_TABLES "
        "WHERE TABLE_SCHEMA = 'PREPROC' "
        "AND TABLE_NAME IN ('CONFIG', 'RESOLUTION', 'SETTINGS', 'PROFILES') "
        "ORDER BY TABLE_NAME"
    ).fetchall()
    found = {r[0] for r in rows}
    assert found == {"CONFIG", "RESOLUTION", "SETTINGS", "PROFILES"}
    wrong_owner = [(name, owner) for name, owner in rows if owner != "PREPROC_ADMIN"]
    assert wrong_owner == [], (
        f"Tables owned by unexpected owner (expected PREPROC_ADMIN): {wrong_owner}"
    )


@pytest.mark.integration
def test_settings_seeded_single_row(installed):
    """PREPROC.SETTINGS must contain exactly one row with the expected defaults."""
    rows = installed.execute("SELECT mode, self_disable_allowed FROM PREPROC.SETTINGS").fetchall()
    assert len(rows) == 1, f"Expected exactly 1 row in PREPROC.SETTINGS, got {len(rows)}"
    mode, self_disable_allowed = rows[0]
    assert mode == "convenience"
    assert self_disable_allowed is False


@pytest.mark.integration
def test_my_pipeline_view_created(installed):
    """PREPROC_RT.MY_PIPELINE must be present in EXA_ALL_VIEWS."""
    rows = installed.execute(
        "SELECT VIEW_NAME FROM EXA_ALL_VIEWS "
        "WHERE VIEW_SCHEMA = 'PREPROC_RT' AND VIEW_NAME = 'MY_PIPELINE'"
    ).fetchall()
    assert len(rows) == 1, "MY_PIPELINE view not found in PREPROC_RT"


@pytest.mark.integration
def test_public_schema_grants(installed):
    """EXECUTE and SELECT on schema PREPROC_RT must be granted to PUBLIC."""
    # Schema-level grants list the schema itself as the object:
    # OBJECT_SCHEMA is NULL, OBJECT_NAME is the schema, OBJECT_TYPE = 'SCHEMA'.
    rows = installed.execute(
        "SELECT PRIVILEGE FROM EXA_DBA_OBJ_PRIVS "
        "WHERE OBJECT_NAME = 'PREPROC_RT' "
        "AND OBJECT_TYPE = 'SCHEMA' "
        "AND GRANTEE = 'PUBLIC' "
        "AND PRIVILEGE IN ('EXECUTE', 'SELECT') "
        "ORDER BY PRIVILEGE"
    ).fetchall()
    granted = {r[0] for r in rows}
    assert "EXECUTE" in granted, "EXECUTE on PREPROC_RT not granted to PUBLIC"
    assert "SELECT" in granted, "SELECT on PREPROC_RT not granted to PUBLIC"


@pytest.mark.integration
def test_my_pipeline_filters_current_user(installed):
    """MY_PIPELINE must not surface rows belonging to a different user.

    A RESOLUTION row for a synthetic user name is inserted, the view is queried
    via the test connection (CURRENT_USER != synthetic name), and the row must
    not appear.  The row is deleted in the finally block.
    """
    sentinel_user = "NO_SUCH_USER_XYZ"
    try:
        # pyexasol uses named {placeholders} with a dict, not ? positional params.
        installed.execute(
            "INSERT INTO PREPROC.RESOLUTION "
            "(user_name, client_pattern, phase, script_name, function_name) "
            "VALUES ({u}, NULL, 'PRE', 'DUMMY_SCRIPT', 'dummy_fn')",
            {"u": sentinel_user},
        )
        rows = installed.execute(
            "SELECT script_name FROM PREPROC_RT.MY_PIPELINE WHERE script_name = 'DUMMY_SCRIPT'"
        ).fetchall()
        assert rows == [], f"MY_PIPELINE exposed rows for '{sentinel_user}' to the current user"
    finally:
        installed.execute(
            "DELETE FROM PREPROC.RESOLUTION WHERE user_name = {u}",
            {"u": sentinel_user},
        )


@pytest.mark.integration
def test_layout_idempotent(installed):
    """Running install a second time must not raise and SETTINGS must still have 1 row."""
    run_install(installed)
    rows = installed.execute("SELECT COUNT(*) FROM PREPROC.SETTINGS").fetchval()
    assert rows == 1, f"Expected 1 row in PREPROC.SETTINGS after re-install, got {rows}"


@pytest.mark.integration
def test_admin_tables_have_ordinal_and_fingerprint(installed):
    """PREPROC.RESOLUTION must have ORDINAL; PREPROC.SETTINGS must have STATE_FINGERPRINT.

    Checks EXA_ALL_COLUMNS which stores names in UPPERCASE.
    """
    rows = installed.execute(
        "SELECT COLUMN_NAME FROM EXA_ALL_COLUMNS "
        "WHERE COLUMN_SCHEMA = 'PREPROC' AND COLUMN_TABLE = 'RESOLUTION' "
        "AND COLUMN_NAME = 'ORDINAL'"
    ).fetchall()
    assert len(rows) == 1, "ordinal column not found in PREPROC.RESOLUTION"

    rows = installed.execute(
        "SELECT COLUMN_NAME FROM EXA_ALL_COLUMNS "
        "WHERE COLUMN_SCHEMA = 'PREPROC' AND COLUMN_TABLE = 'SETTINGS' "
        "AND COLUMN_NAME = 'STATE_FINGERPRINT'"
    ).fetchall()
    assert len(rows) == 1, "state_fingerprint column not found in PREPROC.SETTINGS"


@pytest.mark.integration
def test_my_pipeline_orders_phase_ordinal(installed):
    """MY_PIPELINE must expose CLIENT_PATTERN and ORDINAL columns.

    Checks EXA_ALL_COLUMNS for the PREPROC_RT.MY_PIPELINE view. Column names
    are stored in UPPERCASE by Exasol.
    """
    rows = installed.execute(
        "SELECT COLUMN_NAME FROM EXA_ALL_COLUMNS "
        "WHERE COLUMN_SCHEMA = 'PREPROC_RT' AND COLUMN_TABLE = 'MY_PIPELINE' "
        "ORDER BY COLUMN_ORDINAL_POSITION"
    ).fetchall()
    col_names = [r[0] for r in rows]
    assert "CLIENT_PATTERN" in col_names, (
        f"CLIENT_PATTERN not found in MY_PIPELINE columns: {col_names}"
    )
    assert "ORDINAL" in col_names, f"ORDINAL not found in MY_PIPELINE columns: {col_names}"


@pytest.mark.integration
def test_refresh_status_scripts_created_not_public(installed):
    """PREPROC.REFRESH and PREPROC.STATUS must exist and must not be PUBLIC-executable.

    Verifies the scripts appear in EXA_ALL_SCRIPTS, then checks EXA_DBA_OBJ_PRIVS
    to confirm no EXECUTE grant to PUBLIC exists on either script.
    """
    rows = installed.execute(
        "SELECT SCRIPT_NAME FROM EXA_ALL_SCRIPTS "
        "WHERE SCRIPT_SCHEMA = 'PREPROC' "
        "AND SCRIPT_NAME IN ('REFRESH', 'STATUS') "
        "ORDER BY SCRIPT_NAME"
    ).fetchall()
    found = {r[0] for r in rows}
    assert "REFRESH" in found, "PREPROC.REFRESH script not found in EXA_ALL_SCRIPTS"
    assert "STATUS" in found, "PREPROC.STATUS script not found in EXA_ALL_SCRIPTS"

    rows = installed.execute(
        "SELECT OBJECT_NAME FROM EXA_DBA_OBJ_PRIVS "
        "WHERE OBJECT_SCHEMA = 'PREPROC' "
        "AND OBJECT_NAME IN ('REFRESH', 'STATUS') "
        "AND GRANTEE = 'PUBLIC' "
        "AND PRIVILEGE = 'EXECUTE'"
    ).fetchall()
    assert rows == [], f"REFRESH/STATUS are PUBLIC-executable: {rows}"


@pytest.mark.integration
def test_preproc_admin_dictionary_grant(installed):
    """SELECT ANY DICTIONARY must be granted to PREPROC_ADMIN.

    This system privilege allows the invoker-rights REFRESH and STATUS scripts
    to read EXA_DBA_ROLE_PRIVS and EXA_DBA_USERS when called by a PREPROC_ADMIN holder.
    """
    rows = installed.execute(
        "SELECT PRIVILEGE FROM EXA_DBA_SYS_PRIVS "
        "WHERE GRANTEE = 'PREPROC_ADMIN' "
        "AND PRIVILEGE = 'SELECT ANY DICTIONARY'"
    ).fetchall()
    assert len(rows) == 1, "SELECT ANY DICTIONARY not granted to PREPROC_ADMIN"


@pytest.mark.integration
def test_preproc_admin_create_script_grant(installed):
    """CREATE SCRIPT must be granted to PREPROC_ADMIN.

    REFRESH_CORE generates PREPROC_RT.P_<hash> profile scripts under the invoker's
    rights; with PREPROC_RT owned by PREPROC_ADMIN, the CREATE SCRIPT system
    privilege lets a PREPROC_ADMIN holder's refresh create/replace/drop them.
    Without it the refresh fails with "insufficient privileges for creating a script".
    """
    rows = installed.execute(
        "SELECT PRIVILEGE FROM EXA_DBA_SYS_PRIVS "
        "WHERE GRANTEE = 'PREPROC_ADMIN' "
        "AND PRIVILEGE = 'CREATE SCRIPT'"
    ).fetchall()
    assert len(rows) == 1, "CREATE SCRIPT not granted to PREPROC_ADMIN"


@pytest.mark.integration
def test_resolution_has_profile_hash_column(installed):
    """PREPROC.RESOLUTION must have a PROFILE_HASH column."""
    rows = installed.execute(
        "SELECT COLUMN_NAME FROM EXA_ALL_COLUMNS "
        "WHERE COLUMN_SCHEMA = 'PREPROC' AND COLUMN_TABLE = 'RESOLUTION' "
        "AND COLUMN_NAME = 'PROFILE_HASH'"
    ).fetchall()
    assert len(rows) == 1, "PROFILE_HASH column not found in PREPROC.RESOLUTION"


@pytest.mark.integration
def test_pipeline_view_exposes_mode_and_hash(installed):
    """PREPROC_RT.MY_PIPELINE must expose MODE and PROFILE_HASH columns."""
    rows = installed.execute(
        "SELECT COLUMN_NAME FROM EXA_ALL_COLUMNS "
        "WHERE COLUMN_SCHEMA = 'PREPROC_RT' AND COLUMN_TABLE = 'MY_PIPELINE' "
        "AND COLUMN_NAME IN ('MODE', 'PROFILE_HASH') "
        "ORDER BY COLUMN_NAME"
    ).fetchall()
    col_names = [r[0] for r in rows]
    assert "MODE" in col_names, f"MODE not found in MY_PIPELINE columns: {col_names}"
    assert "PROFILE_HASH" in col_names, (
        f"PROFILE_HASH not found in MY_PIPELINE columns: {col_names}"
    )


@pytest.mark.integration
def test_profiles_has_orphaned_at_column(installed):
    """PREPROC.PROFILES must have an ORPHANED_AT column."""
    rows = installed.execute(
        "SELECT COLUMN_NAME FROM EXA_ALL_COLUMNS "
        "WHERE COLUMN_SCHEMA = 'PREPROC' AND COLUMN_TABLE = 'PROFILES' "
        "AND COLUMN_NAME = 'ORPHANED_AT'"
    ).fetchall()
    assert len(rows) == 1, "ORPHANED_AT column not found in PREPROC.PROFILES"


@pytest.mark.integration
def test_orphaned_at_upgrade_idempotent(installed):
    """Running install a second time must not drop ORPHANED_AT from PREPROC.PROFILES."""
    run_install(installed)
    rows = installed.execute(
        "SELECT COLUMN_NAME FROM EXA_ALL_COLUMNS "
        "WHERE COLUMN_SCHEMA = 'PREPROC' AND COLUMN_TABLE = 'PROFILES' "
        "AND COLUMN_NAME = 'ORPHANED_AT'"
    ).fetchall()
    assert len(rows) == 1, "ORPHANED_AT column missing after re-install"
