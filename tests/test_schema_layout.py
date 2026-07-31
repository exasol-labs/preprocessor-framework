"""Tests for the two-schema database layout.

Every test here except the two static-source guards
(``test_install_ddl_sources_contain_no_create_connection`` and
``test_no_sql_source_names_a_connection_in_a_bucketfs_path``) requires a live
Exasol instance (``installed`` fixture) and is marked ``integration``. Those two
read only the files under ``sql/`` and are deliberately left DB-free and
unmarked, so they still run in a cluster-less loop.

The integration tests run idempotently — each assertion query is
read-only except for ``test_my_pipeline_filters_current_user``,
``test_layout_idempotent``, ``test_group_sys_with_dba_upgrade_idempotent``,
``test_modules_objects_column_widening_is_idempotent``, and
``test_installed_modules_view_exposes_objects``, which clean up after themselves
in ``finally``. The widening test also narrows a column and restores it there, so
a failure mid-test cannot leave the schema behind for the tests that follow.
"""

import re
from pathlib import Path

import pytest

from preproc.install import run_install
from preproc.module.registry import OBJECTS_MAX_CHARS, decode_objects, encode_objects

_REPO_ROOT = Path(__file__).parent.parent
_SQL_INSTALL_DIR = _REPO_ROOT / "sql" / "install"
_SQL_DIR = _REPO_ROOT / "sql"

# A CONNECTION name is conventionally UPPER_SNAKE and may carry digits, so this
# matches the retired two-segment 'bucketfs:<CONNECTION>/<path>' form without
# matching a genuine lowercase '<service>/<bucket>' location, an angle-bracketed
# placeholder, or the bare 'bucketfs:' scheme token -- the same pattern the plan's
# final doc/code sweep runs across the whole repo.
_BUCKETFS_CONNECTION_SOURCE = re.compile(r"bucketfs:[A-Z_][A-Z0-9_]*/")

# The width PREPROC.MODULES.objects shipped with before it was widened. Hard-coded
# on purpose: this is the historical state the widening upgrade has to convert
# FROM, so it is a fixed fact rather than a mirror of the current width.
_ORIGINAL_OBJECTS_WIDTH = 4000


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
    rows = installed.execute(
        "SELECT mode, self_disable_allowed, group_sys_with_dba FROM PREPROC.SETTINGS"
    ).fetchall()
    assert len(rows) == 1, f"Expected exactly 1 row in PREPROC.SETTINGS, got {len(rows)}"
    mode, self_disable_allowed, group_sys_with_dba = rows[0]
    assert mode == "convenience"
    assert self_disable_allowed is False
    assert group_sys_with_dba is True


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


@pytest.mark.integration
def test_settings_has_group_sys_with_dba_column(installed):
    """PREPROC.SETTINGS must have a nullable GROUP_SYS_WITH_DBA column.

    Nullable (not NOT NULL) is a hard requirement: a NOT NULL add would fail
    on the existing single SETTINGS row during an upgrade.
    """
    rows = installed.execute(
        "SELECT COLUMN_IS_NULLABLE FROM EXA_ALL_COLUMNS "
        "WHERE COLUMN_SCHEMA = 'PREPROC' AND COLUMN_TABLE = 'SETTINGS' "
        "AND COLUMN_NAME = 'GROUP_SYS_WITH_DBA'"
    ).fetchall()
    assert len(rows) == 1, "GROUP_SYS_WITH_DBA column not found in PREPROC.SETTINGS"
    assert rows[0][0] is True, "GROUP_SYS_WITH_DBA must be nullable"


@pytest.mark.integration
def test_group_sys_with_dba_upgrade_idempotent(installed):
    """Re-applying the column upgrade must be a no-op and NULL must mean grouped.

    Running install again re-applies ``25_column_upgrades.sql`` through its
    ``--@tolerate-exists`` guard; GROUP_SYS_WITH_DBA must survive re-application
    unchanged. Setting it to NULL directly (as an upgraded, not-yet-touched row
    would have it) must not violate any constraint, and the COALESCE-to-TRUE
    read pattern used by resolution must treat that NULL as grouped.
    """
    run_install(installed)
    rows = installed.execute(
        "SELECT COLUMN_NAME FROM EXA_ALL_COLUMNS "
        "WHERE COLUMN_SCHEMA = 'PREPROC' AND COLUMN_TABLE = 'SETTINGS' "
        "AND COLUMN_NAME = 'GROUP_SYS_WITH_DBA'"
    ).fetchall()
    assert len(rows) == 1, "GROUP_SYS_WITH_DBA column missing after re-install"

    try:
        installed.execute("UPDATE PREPROC.SETTINGS SET group_sys_with_dba = NULL")
        grouped = installed.execute(
            "SELECT COALESCE(group_sys_with_dba, TRUE) FROM PREPROC.SETTINGS"
        ).fetchval()
        assert grouped is True, "NULL group_sys_with_dba must be treated as grouped (TRUE)"
    finally:
        installed.execute("UPDATE PREPROC.SETTINGS SET group_sys_with_dba = TRUE")


@pytest.mark.integration
def test_modules_objects_column_idempotent_upgrade(installed):
    """PREPROC.MODULES gains a nullable objects column, surviving a second install.

    Covers the ``install/schema-layout`` scenario "PREPROC.MODULES gains a
    nullable objects column via an idempotent upgrade": the column must exist
    and be nullable (a NOT NULL add would fail on any pre-existing MODULES
    row, mirroring GROUP_SYS_WITH_DBA), and running install again — which
    re-applies ``25_column_upgrades.sql`` through its ``--@tolerate-exists``
    guard, or a no-op ``CREATE TABLE IF NOT EXISTS`` for a fresh install that
    already got the column from ``22_modules_table.sql`` — must not fail and
    must not duplicate or drop it.

    This is the ADDITIVE guarantee only. The column's WIDTH, and the separate
    guarantee that widening it over an existing install preserves rows and
    converges on re-application, are pinned by
    ``test_modules_objects_column_widening_is_idempotent``.
    """
    rows = installed.execute(
        "SELECT COLUMN_IS_NULLABLE FROM EXA_ALL_COLUMNS "
        "WHERE COLUMN_SCHEMA = 'PREPROC' AND COLUMN_TABLE = 'MODULES' "
        "AND COLUMN_NAME = 'OBJECTS'"
    ).fetchall()
    assert len(rows) == 1, "OBJECTS column not found in PREPROC.MODULES"
    assert rows[0][0] is True, "OBJECTS must be nullable"

    run_install(installed)

    rows_again = installed.execute(
        "SELECT COLUMN_NAME FROM EXA_ALL_COLUMNS "
        "WHERE COLUMN_SCHEMA = 'PREPROC' AND COLUMN_TABLE = 'MODULES' "
        "AND COLUMN_NAME = 'OBJECTS'"
    ).fetchall()
    assert len(rows_again) == 1, "OBJECTS column missing after re-install"


def _objects_column_width(connection):
    """The declared character width of PREPROC.MODULES.objects."""
    return int(
        connection.execute(
            "SELECT COLUMN_MAXSIZE FROM EXA_ALL_COLUMNS "
            "WHERE COLUMN_SCHEMA = 'PREPROC' AND COLUMN_TABLE = 'MODULES' "
            "AND COLUMN_NAME = 'OBJECTS'"
        ).fetchval()
    )


def _objects_column_is_nullable(connection):
    """Whether PREPROC.MODULES.objects still admits NULL."""
    return connection.execute(
        "SELECT COLUMN_IS_NULLABLE FROM EXA_ALL_COLUMNS "
        "WHERE COLUMN_SCHEMA = 'PREPROC' AND COLUMN_TABLE = 'MODULES' "
        "AND COLUMN_NAME = 'OBJECTS'"
    ).fetchval()


@pytest.mark.integration
def test_modules_objects_column_widening_is_idempotent(installed):
    """PREPROC.MODULES.objects is widened in place, preserving rows, and re-widens cleanly.

    Covers the ``install/schema-layout`` scenario "PREPROC.MODULES.objects is
    widened in place on an existing install". The column shipped at 4,000
    characters — about 39 objects at realistic ~100-character qualified names,
    too tight for a subsystem module that owns its own schema (ADR-057) — so
    ``25_column_upgrades.sql`` carries an ``ALTER TABLE ... MODIFY`` after the
    additive ``ADD COLUMN``. A widening is a different operation from an
    addition: MODIFY states the target type ABSOLUTELY, which is what makes
    re-application converge instead of erroring (and why it carries no
    ``--@tolerate-exists`` marker — a second run raises no "already exists" error
    to tolerate), and Exasol converts the stored values in place rather than
    dropping and recreating the column, which is what makes it safe on an install
    that already holds provenance rows.

    The column is deliberately narrowed back to its original width first, and a
    row with a non-NULL ``objects`` value is seeded through the narrow column:
    that reconstructs the pre-widening state of a real upgrade, which is the only
    way to prove the ALTER genuinely widens. Asserting on the installed state
    alone would pass on a fresh install where ``22_modules_table.sql`` already
    declared the final width and the ALTER did nothing.
    """
    name, version = "schema_layout_widening_test", 1
    assert _objects_column_width(installed) == OBJECTS_MAX_CHARS, (
        "PREPROC.MODULES.objects is not at the canonical width before the test starts"
    )

    longest = installed.execute(
        "SELECT COALESCE(MAX(LENGTH(objects)), 0) FROM PREPROC.MODULES"
    ).fetchval()
    assert int(longest) <= _ORIGINAL_OBJECTS_WIDTH, (
        "a recorded inventory is already wider than the original column width, so the "
        "pre-widening state cannot be reconstructed without destroying it"
    )

    # Sized to the narrow column so it can be written BEFORE the widening, which
    # is what makes "existing rows survive" an assertion rather than an aspiration.
    recorded = encode_objects(
        [{"type": "LUA SCRIPT", "name": "PREPROC_RT.SCHEMA_LAYOUT_WIDENING_TEST_V1"}]
    )
    try:
        installed.execute(
            f"ALTER TABLE PREPROC.MODULES MODIFY (objects VARCHAR({_ORIGINAL_OBJECTS_WIDTH}))"
        )
        assert _objects_column_width(installed) == _ORIGINAL_OBJECTS_WIDTH

        installed.execute(
            "INSERT INTO PREPROC.MODULES "
            "(name, version, script_name, function_name, phase, deploy_mode, status, "
            "installed_at, objects) VALUES "
            "({n}, {v}, {sn}, {fn}, 'TRANSLATE', 'library-deployed', 'deployed', "
            "CURRENT_TIMESTAMP, {ob})",
            {
                "n": name,
                "v": version,
                "sn": "PREPROC_RT.SCHEMA_LAYOUT_WIDENING_TEST_V1",
                "fn": "schema_layout_widening_test",
                "ob": recorded,
            },
        )

        run_install(installed)

        assert _objects_column_width(installed) == OBJECTS_MAX_CHARS, (
            "25_column_upgrades.sql did not widen PREPROC.MODULES.objects"
        )
        assert _objects_column_is_nullable(installed) is True, "OBJECTS must stay nullable"
        survived = installed.execute(
            "SELECT objects FROM PREPROC.MODULES WHERE name = {n} AND version = {v}",
            {"n": name, "v": version},
        ).fetchall()
        assert [row[0] for row in survived] == [recorded], (
            "the widening did not preserve the existing provenance row's inventory"
        )

        # Second application: the widening runs again against an ALREADY-wide
        # column. Convergent, not tolerated — no error, no narrowing, no data loss.
        run_install(installed)

        assert _objects_column_width(installed) == OBJECTS_MAX_CHARS
        assert _objects_column_is_nullable(installed) is True
        survived_again = installed.execute(
            "SELECT objects FROM PREPROC.MODULES WHERE name = {n} AND version = {v}",
            {"n": name, "v": version},
        ).fetchall()
        assert [row[0] for row in survived_again] == [recorded]
    finally:
        installed.execute(
            "DELETE FROM PREPROC.MODULES WHERE name = {n} AND version = {v}",
            {"n": name, "v": version},
        )
        # Never leave the column narrowed for the tests that follow, even if an
        # assertion above failed between the narrowing and the re-install.
        installed.execute(
            f"ALTER TABLE PREPROC.MODULES MODIFY (objects VARCHAR({OBJECTS_MAX_CHARS}))"
        )


@pytest.mark.integration
def test_installed_modules_view_exposes_objects(installed):
    """PREPROC_RT.INSTALLED_MODULES exposes a composite module's recorded inventory.

    Covers the ``install/schema-layout`` scenario "The installed-modules view
    exposes the object inventory to a non-admin user": registers a composite
    module's provenance row (four objects, ``preproc.module.registry
    .encode_objects`` — see its docstring for the exact wire format) directly
    through ``PREPROC.MODULE_REGISTER`` (already deployed by ``run_install``,
    see ``install.py._deploy_admin_scripts``) and reads it back through the
    definer-rights view. The view is exercised over the same connection used
    to install (mirroring ``tests/test_module_lifecycle.py``'s rationale for
    not provisioning a throwaway non-admin user): ``INSTALLED_MODULES`` is
    covered by the schema-level PUBLIC SELECT grant on PREPROC_RT (ADR-002,
    asserted independently elsewhere), so this only needs to prove the column
    is exposed and round-trips, not who may read it.
    """
    name, version = "schema_layout_objects_test", 1
    declared = [
        {"type": "LUA SCRIPT", "name": "PREPROC_RT.SCHEMA_LAYOUT_OBJECTS_TEST_V1"},
        {
            "type": "PYTHON3 SCALAR SCRIPT",
            "name": "PREPROC_RT.SCHEMA_LAYOUT_OBJECTS_TEST_HELPER_V1",
        },
        {"type": "TABLE", "name": "PREPROC_RT.SCHEMA_LAYOUT_OBJECTS_TEST_STATE_V1"},
        {"type": "VIEW", "name": "PREPROC_RT.SCHEMA_LAYOUT_OBJECTS_TEST_VIEW_V1"},
    ]
    try:
        installed.execute(
            "EXECUTE SCRIPT PREPROC.MODULE_REGISTER("
            "{n}, {v}, {src}, {rf}, {sh}, {sn}, {fn}, {ph}, {dm}, {su}, {lv}, {ob})",
            {
                "n": name,
                "v": version,
                "src": "library",
                "rf": "v1",
                "sh": "b" * 64,
                "sn": declared[0]["name"],
                "fn": "schema_layout_objects_test",
                "ph": "TRANSLATE",
                "dm": "library-deployed",
                "su": "https://example.invalid/registry/index.json",
                "lv": "0.9.0",
                "ob": encode_objects(declared),
            },
        ).fetchall()

        row = installed.execute(
            "SELECT objects FROM PREPROC_RT.INSTALLED_MODULES WHERE name = {n} AND version = {v}",
            {"n": name, "v": version},
        ).fetchone()
        assert row is not None, "registered row not visible through INSTALLED_MODULES"
        assert decode_objects(row[0]) == declared
    finally:
        installed.execute(
            "DELETE FROM PREPROC.MODULES WHERE name = {n} AND version = {v}",
            {"n": name, "v": version},
        )


@pytest.mark.integration
def test_indb_objects_admin_only(installed):
    """The resolver UDF, MODULE_INSTALL, and MODULE_CATALOG are PREPROC-admin-only.

    Covers the ``install/schema-layout`` DELTA:NEW scenario "The resolver UDF and
    install/catalog scripts are created admin-only in PREPROC": after
    ``run_install``, all three exist in ``EXA_ALL_SCRIPTS`` under schema PREPROC,
    are owned by PREPROC_ADMIN, and carry no PUBLIC EXECUTE grant. Re-deploying
    (a second ``run_install``) must succeed without error, proving the
    ``CREATE OR REPLACE`` shape is truly idempotent.
    """
    names = ("MODULE_RESOLVER_V1", "MODULE_INSTALL", "MODULE_CATALOG")

    rows = installed.execute(
        "SELECT SCRIPT_NAME, SCRIPT_OWNER FROM EXA_ALL_SCRIPTS "
        "WHERE SCRIPT_SCHEMA = 'PREPROC' "
        f"AND SCRIPT_NAME IN {names} "
        "ORDER BY SCRIPT_NAME"
    ).fetchall()
    found = {name: owner for name, owner in rows}
    assert set(found) == set(names), f"expected {set(names)}, found {set(found)}"
    wrong_owner = {name: owner for name, owner in found.items() if owner != "PREPROC_ADMIN"}
    assert wrong_owner == {}, f"expected PREPROC_ADMIN ownership, got: {wrong_owner}"

    public_rows = installed.execute(
        "SELECT OBJECT_NAME FROM EXA_DBA_OBJ_PRIVS "
        "WHERE OBJECT_SCHEMA = 'PREPROC' "
        f"AND OBJECT_NAME IN {names} "
        "AND GRANTEE = 'PUBLIC' "
        "AND PRIVILEGE = 'EXECUTE'"
    ).fetchall()
    assert public_rows == [], f"admin-only objects are PUBLIC-executable: {public_rows}"

    # Re-deploying (CREATE OR REPLACE) must succeed without error.
    run_install(installed)
    rows_again = installed.execute(
        "SELECT SCRIPT_NAME FROM EXA_ALL_SCRIPTS "
        "WHERE SCRIPT_SCHEMA = 'PREPROC' "
        f"AND SCRIPT_NAME IN {names}"
    ).fetchall()
    assert {r[0] for r in rows_again} == set(names), "objects missing after re-deploy"


def test_install_ddl_sources_contain_no_create_connection():
    """No install DDL source under ``sql/install/`` contains a CREATE CONNECTION.

    Reads files only, so it carries no ``integration`` mark and no fixture: the
    operator alone creates the BucketFS CONNECTION, and a ``CREATE CONNECTION``
    appearing in the framework's own DDL is a defect visible in the source tree
    long before any cluster is involved. Its live counterpart — that running the
    installer adds no CONNECTION to the database — is
    ``test_install_ddl_creates_no_connection``.
    """
    offenders = [
        sql_file.name
        for sql_file in sorted(_SQL_INSTALL_DIR.glob("*.sql"))
        if "CREATE CONNECTION" in sql_file.read_text(encoding="utf-8").upper()
    ]
    assert offenders == [], f"install DDL sources creating a CONNECTION object: {offenders}"


def test_no_sql_source_names_a_connection_in_a_bucketfs_path():
    """No framework artefact under ``sql/`` names a CONNECTION on a bucketfs source path.

    The anti-regression guard for change-bucketfs-source-to-location: a
    ``bucketfs:`` source is the location ``bucketfs:<service>/<bucket>/<path>``,
    never the retired ``bucketfs:<CONNECTION>/<path>``, and the two forms are
    grammatically identical, so a stale literal cannot be caught by the resolver —
    only by reading the text. DB-free and unmarked on purpose: it is pure file
    text, and a cluster-less developer loop is exactly the environment in which a
    stale literal gets reintroduced — gating it behind ``installed``, where it
    first shipped, meant it never ran there at all.

    Covers the static half of the ``install/schema-layout`` DELTA:NEW scenario "The
    BucketFS CONNECTION is a credential grant the framework never creates and never
    names" — specifically that the CONNECTION's name appears in no framework
    surface, the DDL, the Lua scripts, and the resolver UDF included.
    """
    offenders = []
    for path in sorted((*_SQL_DIR.rglob("*.sql"), *_SQL_DIR.rglob("*.lua"))):
        match = _BUCKETFS_CONNECTION_SOURCE.search(path.read_text(encoding="utf-8"))
        if match is not None:
            offenders.append(f"{path.relative_to(_REPO_ROOT)}: {match.group(0)}")
    assert offenders == [], f"CONNECTION names on bucketfs source paths: {offenders}"


@pytest.mark.integration
def test_install_ddl_creates_no_connection(installed):
    """Running the idempotent installer adds no CONNECTION object to the database.

    The live half of the ``install/schema-layout`` DELTA:NEW scenario "The BucketFS
    CONNECTION is a credential grant the framework never creates and never names":
    the operator alone creates the BucketFS CONNECTION, which the resolver reads as
    a plain location and never by name (change-bucketfs-source-to-location). The two
    static source-tree halves are the DB-free tests above, and stay above so a
    cluster-less run still executes them.
    """
    before = {
        r[0]
        for r in installed.execute("SELECT CONNECTION_NAME FROM EXA_DBA_CONNECTIONS").fetchall()
    }
    run_install(installed)
    after = {
        r[0]
        for r in installed.execute("SELECT CONNECTION_NAME FROM EXA_DBA_CONNECTIONS").fetchall()
    }
    assert after == before, f"run_install created/removed a CONNECTION: {after - before}"
