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
