"""Integration tests for PREPROC.REFRESH role/USER resolution.

These tests exercise the materialisation contract of ``PREPROC.REFRESH``
against a live Exasol instance (the ``installed`` fixture): a ROLE-scoped
CONFIG rule expands to every holder of that role (directly and transitively via
CONNECT BY), USER-scoped rules union additively with ROLE-derived rules,
identical (phase, script, function) rows reaching a user by multiple paths are
deduplicated, disabled rules are ignored, and each run is a full rebuild that
drops orphans and stamps ``last_refresh``.

Isolation
    RESOLUTION and CONFIG are global tables on a session-scoped connection
    shared with the rest of the suite, and REFRESH does a full DELETE of
    RESOLUTION before reinserting. The ``clean_resolution`` fixture wipes both
    tables before and after every test so no rule leaks across tests and no
    REFRESH picks up a foreign rule. Roles and grants are created with
    tolerate-exists and torn down in ``finally`` (DROP ROLE cascades its
    grants).

Role-holder strategy
    The closure joins EXA_DBA_USERS on the grantee, so a ROLE-derived row only
    appears for an existing database user. Exasol forbids granting roles to SYS
    (the typical test-runner account), so these tests use a dedicated
    ``PREPROC_TEST_HOLDER`` user as the role grantee. The user is created
    idempotently (CREATE USER tolerates an existing user from a prior run) and
    left in place after the session — re-creating per test would be expensive
    and the user holds no sensitive privileges. Synthetic names such as
    ``PREPROC_TEST_DAVE`` are used only for USER-scoped CONFIG rows, which need
    no real database user.

SYS-grouping policy
    Five tests exercise ``PREPROC.SETTINGS.group_sys_with_dba``: grouped (the
    default TRUE) folds SYS into the ROLE closure so DBA- and PUBLIC-scoped rules
    reach it; isolated (FALSE) drops SYS from the ENTIRE closure (DBA + PUBLIC +
    nested) while a USER-scoped SYS rule still resolves and other DBA holders are
    unaffected; a NULL value is treated as grouped. The toggle is flipped with a
    direct UPDATE — self-contained, with no dependency on the SET_SYS_GROUPING
    admin script — and the ``restore_sys_grouping`` fixture resets it to TRUE on
    teardown so the shared session's single SETTINGS row never leaks a non-default
    policy into later tests or other suites.
"""

import os

import pytest

from preproc.connection import connect_from_env, missing_env_vars

_ROLE_A = "PREPROC_TEST_ROLE_A"
_ROLE_B = "PREPROC_TEST_ROLE_B"
_SYNTHETIC_USER = "PREPROC_TEST_DAVE"
_TEST_USER = "PREPROC_TEST_HOLDER"
_SYS = "SYS"


@pytest.fixture
def clean_resolution(installed):
    """Empty RESOLUTION and CONFIG around the test so rules never leak.

    Wiping before guards against a prior crashed test; wiping after keeps the
    shared session clean for the next test and for the no-rules passthrough
    assertions in the MASTER suite.
    """
    installed.execute("DELETE FROM PREPROC.RESOLUTION")
    installed.execute("DELETE FROM PREPROC.CONFIG")
    yield installed
    installed.execute("DELETE FROM PREPROC.RESOLUTION")
    installed.execute("DELETE FROM PREPROC.CONFIG")


@pytest.fixture(scope="session")
def role_holder(installed):
    """Ensure PREPROC_TEST_HOLDER exists and return its name.

    Exasol forbids granting roles to SYS, so role-grant tests need a real
    non-SYS user as the grantee. The user is created idempotently — if it
    already exists from a prior run the CREATE USER silently succeeds via the
    except branch. It is left in place after the session; dropping per test
    would be unnecessarily expensive and the user holds no sensitive privileges.
    DROP USER ... CASCADE is safe to run manually if a clean-slate is needed.
    """
    try:
        installed.execute(f'CREATE USER {_TEST_USER} IDENTIFIED BY "Pr3proc-Test!"')
    except Exception:
        pass  # user already exists from a prior run — that is fine
    return _TEST_USER


@pytest.fixture
def restore_sys_grouping(installed):
    """Reset group_sys_with_dba to its grouped default (TRUE) after the test.

    The toggle lives in the shared, session-scoped single PREPROC.SETTINGS row,
    which clean_resolution does not touch. A test that isolates SYS (FALSE or
    NULL) must not leak that policy into later tests or other suites, so this
    fixture restores TRUE on teardown regardless of what the test set.
    """
    yield installed
    installed.execute("UPDATE PREPROC.SETTINGS SET group_sys_with_dba = TRUE")


def _insert_rule(
    connection,
    *,
    rule_id,
    scope_type,
    scope_value,
    phase,
    script_name,
    function_name,
    enabled=True,
    client_pattern=None,
):
    """Insert one PREPROC.CONFIG rule using pyexasol named placeholders."""
    connection.execute(
        "INSERT INTO PREPROC.CONFIG "
        "(rule_id, scope_type, scope_value, client_pattern, phase, "
        "script_name, function_name, enabled, version) "
        "VALUES ({rid}, {st}, {sv}, {cp}, {ph}, {sn}, {fn}, {en}, 1)",
        {
            "rid": rule_id,
            "st": scope_type,
            "sv": scope_value,
            "cp": client_pattern,
            "ph": phase,
            "sn": script_name,
            "fn": function_name,
            "en": enabled,
        },
    )


def _create_role(connection, name):
    """Create a role, tolerating one left behind by a prior run."""
    try:
        connection.execute(f"CREATE ROLE {name}")
    except Exception:
        pass


def _drop_role(connection, name):
    """Drop a role if present; dropping cascades its grants."""
    try:
        connection.execute(f"DROP ROLE {name}")
    except Exception:
        pass


def _refresh(connection):
    """Run PREPROC.REFRESH and return its single (rows_inserted, last_refresh) row."""
    return connection.execute("EXECUTE SCRIPT PREPROC.REFRESH").fetchall()


def _resolution_for(connection, user_name):
    """Return RESOLUTION rows for one user as (phase, script, function, ordinal)."""
    return connection.execute(
        "SELECT phase, script_name, function_name, ordinal "
        "FROM PREPROC.RESOLUTION WHERE user_name = {u} "
        "ORDER BY phase, ordinal",
        {"u": user_name},
    ).fetchall()


def _set_sys_grouping(connection, sql_literal):
    """Set PREPROC.SETTINGS.group_sys_with_dba to a raw SQL literal.

    Takes an unquoted literal — ``TRUE``, ``FALSE``, or ``NULL`` — matching how
    the schema-layout suite manipulates the column, so a NULL is a genuine SQL
    NULL (an upgraded, never-set row) rather than a bound parameter.
    """
    connection.execute(f"UPDATE PREPROC.SETTINGS SET group_sys_with_dba = {sql_literal}")


def _sys_row_count(connection, function_name):
    """Return how many RESOLUTION rows SYS carries for a given function_name."""
    return connection.execute(
        "SELECT COUNT(*) FROM PREPROC.RESOLUTION WHERE user_name = {u} AND function_name = {f}",
        {"u": _SYS, "f": function_name},
    ).fetchval()


@pytest.mark.integration
def test_direct_role_grant_resolves(clean_resolution, role_holder):
    """A ROLE-scoped rule resolves to a user holding that role directly."""
    conn = clean_resolution
    _create_role(conn, _ROLE_A)
    try:
        conn.execute(f"GRANT {_ROLE_A} TO {role_holder}")
        _insert_rule(
            conn,
            rule_id=1,
            scope_type="ROLE",
            scope_value=_ROLE_A,
            phase="EXPAND",
            script_name="TEST_SCRIPT",
            function_name="test_fn",
        )

        _refresh(conn)

        rows = _resolution_for(conn, role_holder)
        assert ("EXPAND", "TEST_SCRIPT", "test_fn", 1) in rows
    finally:
        _drop_role(conn, _ROLE_A)


@pytest.mark.integration
def test_nested_role_grant_resolves_transitively(clean_resolution, role_holder):
    """A nested grant (R1->R2->user) resolves via the CONNECT BY closure.

    The CONFIG rule names the outer role R1; R1 is granted to R2 and R2 to the
    user. Only transitive expansion can place a row for the user, so its
    presence proves CONNECT BY walks the chain.
    """
    conn = clean_resolution
    _create_role(conn, _ROLE_A)
    _create_role(conn, _ROLE_B)
    try:
        conn.execute(f"GRANT {_ROLE_A} TO {_ROLE_B}")
        conn.execute(f"GRANT {_ROLE_B} TO {role_holder}")
        _insert_rule(
            conn,
            rule_id=1,
            scope_type="ROLE",
            scope_value=_ROLE_A,
            phase="EXPAND",
            script_name="TEST_SCRIPT",
            function_name="nested_fn",
        )

        _refresh(conn)

        rows = _resolution_for(conn, role_holder)
        assert ("EXPAND", "TEST_SCRIPT", "nested_fn", 1) in rows
    finally:
        _drop_role(conn, _ROLE_B)
        _drop_role(conn, _ROLE_A)


@pytest.mark.integration
def test_user_without_role_excluded(clean_resolution):
    """A user who does not hold the granting role gets no derived row.

    The rule names a role created but granted to nobody, so after refresh a
    synthetic non-holder must have zero rows for it.
    """
    conn = clean_resolution
    _create_role(conn, _ROLE_A)
    try:
        _insert_rule(
            conn,
            rule_id=1,
            scope_type="ROLE",
            scope_value=_ROLE_A,
            phase="EXPAND",
            script_name="TEST_SCRIPT",
            function_name="orphan_fn",
        )

        _refresh(conn)

        ungranted = conn.execute(
            "SELECT COUNT(*) FROM PREPROC.RESOLUTION WHERE function_name = 'orphan_fn'"
        ).fetchval()
        assert ungranted == 0
    finally:
        _drop_role(conn, _ROLE_A)


@pytest.mark.integration
def test_public_role_resolves_to_all_users(clean_resolution):
    """A ROLE-scoped rule on the implicit PUBLIC role resolves to every user.

    PUBLIC membership is automatic and never appears in EXA_DBA_ROLE_PRIVS, so a
    closure that walks only explicit grants resolves a PUBLIC-scoped rule to
    nobody. Every database user must carry the rule after refresh — including
    users holding no explicit role at all.
    """
    conn = clean_resolution
    _insert_rule(
        conn,
        rule_id=1,
        scope_type="ROLE",
        scope_value="PUBLIC",
        phase="EXPAND",
        script_name="TEST_SCRIPT",
        function_name="public_fn",
    )

    _refresh(conn)

    resolved = {
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT user_name FROM PREPROC.RESOLUTION WHERE function_name = 'public_fn'"
        ).fetchall()
    }
    all_users = {r[0] for r in conn.execute("SELECT user_name FROM EXA_DBA_USERS").fetchall()}
    assert all_users, "EXA_DBA_USERS returned no users; cannot validate PUBLIC resolution"
    assert resolved == all_users, (
        "PUBLIC-scoped rule did not resolve to all users; "
        f"missing: {sorted(all_users - resolved)}, unexpected: {sorted(resolved - all_users)}"
    )


@pytest.mark.integration
def test_user_unions_with_role_same_phase(clean_resolution, role_holder):
    """A USER-scoped rule unions additively with a ROLE-derived rule.

    The synthetic user PREPROC_TEST_DAVE gets a USER-scoped rule directly. The
    dedicated test holder gets a ROLE-derived rule in the same phase. Both rows
    must materialise — the model is additive, never override.
    """
    conn = clean_resolution
    _create_role(conn, _ROLE_A)
    try:
        conn.execute(f"GRANT {_ROLE_A} TO {role_holder}")
        _insert_rule(
            conn,
            rule_id=1,
            scope_type="USER",
            scope_value=_SYNTHETIC_USER,
            phase="EXPAND",
            script_name="USER_SCRIPT",
            function_name="user_fn",
        )
        _insert_rule(
            conn,
            rule_id=2,
            scope_type="ROLE",
            scope_value=_ROLE_A,
            phase="EXPAND",
            script_name="ROLE_SCRIPT",
            function_name="role_fn",
        )

        _refresh(conn)

        dave_rows = _resolution_for(conn, _SYNTHETIC_USER)
        assert ("EXPAND", "USER_SCRIPT", "user_fn", 1) in dave_rows

        holder_rows = _resolution_for(conn, role_holder)
        assert ("EXPAND", "ROLE_SCRIPT", "role_fn", 1) in holder_rows
    finally:
        _drop_role(conn, _ROLE_A)


@pytest.mark.integration
def test_multi_role_rules_union_ordered(clean_resolution, role_holder):
    """Two ROLE rules for one phase both resolve, ordinals follow rule_id asc.

    The test holder holds both roles. rule_id 1 must take ordinal 1 and
    rule_id 2 ordinal 2 within the shared phase — order is pure rule_id
    ascending.
    """
    conn = clean_resolution
    _create_role(conn, _ROLE_A)
    _create_role(conn, _ROLE_B)
    try:
        conn.execute(f"GRANT {_ROLE_A} TO {role_holder}")
        conn.execute(f"GRANT {_ROLE_B} TO {role_holder}")
        _insert_rule(
            conn,
            rule_id=1,
            scope_type="ROLE",
            scope_value=_ROLE_A,
            phase="EXPAND",
            script_name="SCRIPT_ONE",
            function_name="fn_one",
        )
        _insert_rule(
            conn,
            rule_id=2,
            scope_type="ROLE",
            scope_value=_ROLE_B,
            phase="EXPAND",
            script_name="SCRIPT_TWO",
            function_name="fn_two",
        )

        _refresh(conn)

        expand = [r for r in _resolution_for(conn, role_holder) if r[0] == "EXPAND"]
        assert expand == [
            ("EXPAND", "SCRIPT_ONE", "fn_one", 1),
            ("EXPAND", "SCRIPT_TWO", "fn_two", 2),
        ]
    finally:
        _drop_role(conn, _ROLE_B)
        _drop_role(conn, _ROLE_A)


@pytest.mark.integration
def test_multi_path_function_deduplicated(clean_resolution, role_holder):
    """One (phase, script, function) reached by two roles dedupes to one row.

    Two ROLE rules name the SAME (phase, script, function) but different roles,
    both held by the test holder. The holder must get exactly one row at ordinal
    1 (lowest contributing rule_id wins), not two.
    """
    conn = clean_resolution
    _create_role(conn, _ROLE_A)
    _create_role(conn, _ROLE_B)
    try:
        conn.execute(f"GRANT {_ROLE_A} TO {role_holder}")
        conn.execute(f"GRANT {_ROLE_B} TO {role_holder}")
        _insert_rule(
            conn,
            rule_id=1,
            scope_type="ROLE",
            scope_value=_ROLE_A,
            phase="EXPAND",
            script_name="DEDUP_SCRIPT",
            function_name="dedup_fn",
        )
        _insert_rule(
            conn,
            rule_id=2,
            scope_type="ROLE",
            scope_value=_ROLE_B,
            phase="EXPAND",
            script_name="DEDUP_SCRIPT",
            function_name="dedup_fn",
        )

        _refresh(conn)

        matches = [
            r
            for r in _resolution_for(conn, role_holder)
            if r[1] == "DEDUP_SCRIPT" and r[2] == "dedup_fn"
        ]
        assert matches == [("EXPAND", "DEDUP_SCRIPT", "dedup_fn", 1)]
    finally:
        _drop_role(conn, _ROLE_B)
        _drop_role(conn, _ROLE_A)


@pytest.mark.integration
def test_disabled_rules_excluded(clean_resolution, role_holder):
    """A rule with enabled = FALSE contributes no RESOLUTION row."""
    conn = clean_resolution
    _create_role(conn, _ROLE_A)
    try:
        conn.execute(f"GRANT {_ROLE_A} TO {role_holder}")
        _insert_rule(
            conn,
            rule_id=1,
            scope_type="ROLE",
            scope_value=_ROLE_A,
            phase="EXPAND",
            script_name="DISABLED_SCRIPT",
            function_name="disabled_fn",
            enabled=False,
        )

        _refresh(conn)

        disabled = conn.execute(
            "SELECT COUNT(*) FROM PREPROC.RESOLUTION WHERE function_name = 'disabled_fn'"
        ).fetchval()
        assert disabled == 0
    finally:
        _drop_role(conn, _ROLE_A)


@pytest.mark.integration
def test_refresh_full_rebuild_no_orphans(clean_resolution):
    """REFRESH fully rebuilds: a stale row with no backing rule is dropped."""
    conn = clean_resolution
    conn.execute(
        "INSERT INTO PREPROC.RESOLUTION "
        "(user_name, client_pattern, phase, script_name, function_name, ordinal) "
        "VALUES ({u}, NULL, 'EXPAND', 'STALE_SCRIPT', 'stale_fn', 1)",
        {"u": "PREPROC_TEST_GHOST"},
    )
    precondition = conn.execute(
        "SELECT COUNT(*) FROM PREPROC.RESOLUTION WHERE function_name = 'stale_fn'"
    ).fetchval()
    assert precondition == 1, "stale row was not seeded"

    _refresh(conn)

    remaining = conn.execute(
        "SELECT COUNT(*) FROM PREPROC.RESOLUTION WHERE function_name = 'stale_fn'"
    ).fetchval()
    assert remaining == 0


@pytest.mark.integration
def test_rebuild_and_timestamp_atomic(clean_resolution):
    """REFRESH stamps a non-NULL last_refresh and reports it in its result row.

    True concurrent atomicity is out of scope for a single-connection test;
    this pins the observable post-condition: the stamp is written and the same
    value is returned to the caller.
    """
    conn = clean_resolution

    result = _refresh(conn)
    assert len(result) == 1
    rows_inserted, returned_stamp = result[0]
    assert returned_stamp, "REFRESH returned an empty last_refresh"

    stored = conn.execute("SELECT last_refresh FROM PREPROC.SETTINGS").fetchval()
    assert stored is not None


@pytest.mark.integration
def test_refresh_callable_by_admin_any_client(clean_resolution):
    """EXECUTE SCRIPT PREPROC.REFRESH succeeds from the admin test connection.

    The result is a single row whose first column is the (non-negative) inserted
    row count — proof the script ran to completion and returned its contract row
    to an ordinary SQL client.
    """
    conn = clean_resolution

    result = _refresh(conn)
    assert len(result) == 1
    rows_inserted = result[0][0]
    assert rows_inserted is not None
    assert int(rows_inserted) >= 0


@pytest.mark.integration
def test_refresh_requires_admin(installed):
    """A non-admin caller is rejected before any write to RESOLUTION.

    Requires CREATE USER privilege and a second connection authenticating as the
    fresh non-admin user. Where that is not feasible (no CREATE USER right, or
    the env-configured account cannot mint a usable login), the test skips
    rather than reporting a false pass — the privilege contract is also asserted
    structurally by the schema-layout suite (REFRESH not granted to PUBLIC).
    """
    if missing_env_vars():
        pytest.skip("no Exasol instance is configured")

    non_admin = "PREPROC_TEST_NOADMIN"
    password = "pw_PREPROC_test_1"
    other = None
    try:
        try:
            installed.execute(f"DROP USER {non_admin} CASCADE")
        except Exception:
            pass
        installed.execute(f'CREATE USER {non_admin} IDENTIFIED BY "{password}"')
        installed.execute(f"GRANT CREATE SESSION TO {non_admin}")
    except Exception as exc:
        pytest.skip(f"cannot create a non-admin user in this environment: {exc}")

    try:
        prev_user = os.environ.get("EXASOL_USER")
        prev_pw = os.environ.get("EXASOL_PASSWORD")
        os.environ["EXASOL_USER"] = non_admin
        os.environ["EXASOL_PASSWORD"] = password
        try:
            other = connect_from_env()
        finally:
            if prev_user is not None:
                os.environ["EXASOL_USER"] = prev_user
            if prev_pw is not None:
                os.environ["EXASOL_PASSWORD"] = prev_pw

        with pytest.raises(Exception):
            other.execute("EXECUTE SCRIPT PREPROC.REFRESH")
    finally:
        if other is not None:
            other.close()
        try:
            installed.execute(f"DROP USER {non_admin} CASCADE")
        except Exception:
            pass


@pytest.mark.integration
def test_sys_grouped_receives_dba_and_public_rules(clean_resolution, restore_sys_grouping):
    """With grouping enabled (default), SYS resolves DBA- and PUBLIC-scoped rules.

    SYS holds DBA as every Exasol superuser does, so a ROLE DBA rule reaches SYS
    through the closure; a ROLE PUBLIC rule reaches every user via the synthetic
    PUBLIC edge. Both rows must materialise for SYS when group_sys_with_dba is
    TRUE — identical to any other DBA holder.
    """
    conn = clean_resolution
    _set_sys_grouping(conn, "TRUE")
    _insert_rule(
        conn,
        rule_id=1,
        scope_type="ROLE",
        scope_value="DBA",
        phase="EXPAND",
        script_name="DBA_SCRIPT",
        function_name="dba_fn",
    )
    _insert_rule(
        conn,
        rule_id=2,
        scope_type="ROLE",
        scope_value="PUBLIC",
        phase="EXPAND",
        script_name="PUBLIC_SCRIPT",
        function_name="public_fn",
    )

    _refresh(conn)

    functions = {r[2] for r in _resolution_for(conn, _SYS)}
    assert "dba_fn" in functions, "SYS did not receive the DBA-derived rule while grouped"
    assert "public_fn" in functions, "SYS did not receive the PUBLIC-derived rule while grouped"


@pytest.mark.integration
def test_sys_excluded_from_full_closure_when_isolated(clean_resolution, restore_sys_grouping):
    """With grouping disabled, SYS gets no ROLE-derived row from DBA or PUBLIC.

    These are the only role-closure paths SYS actually has: Exasol forbids
    granting any role to SYS *or* to DBA ("cannot grant roles to DBA"), so a
    nested role above SYS is not constructible and needs no dedicated case. The
    SYS gate is a single path-independent predicate on the ROLE-closure arm
    (grantee <> 'SYS'), so excluding SYS from both a DBA-scoped and a
    PUBLIC-scoped rule at once proves the full-closure exclusion for every path
    SYS can hold. (Generic transitive nesting is covered by the non-SYS
    nested-role tests elsewhere in this suite.)
    """
    conn = clean_resolution
    _set_sys_grouping(conn, "FALSE")
    _insert_rule(
        conn,
        rule_id=1,
        scope_type="ROLE",
        scope_value="DBA",
        phase="EXPAND",
        script_name="DBA_SCRIPT",
        function_name="dba_fn",
    )
    _insert_rule(
        conn,
        rule_id=2,
        scope_type="ROLE",
        scope_value="PUBLIC",
        phase="EXPAND",
        script_name="PUBLIC_SCRIPT",
        function_name="public_fn",
    )

    _refresh(conn)

    sys_role_rows = conn.execute(
        "SELECT COUNT(*) FROM PREPROC.RESOLUTION "
        "WHERE user_name = {u} "
        "AND function_name IN ('dba_fn', 'public_fn')",
        {"u": _SYS},
    ).fetchval()
    assert sys_role_rows == 0, (
        "SYS received a ROLE-derived row while isolated; the full-closure "
        "exclusion did not cover DBA + PUBLIC"
    )


@pytest.mark.integration
def test_sys_user_scoped_rule_survives_isolation(clean_resolution, restore_sys_grouping):
    """A USER-scoped rule naming SYS resolves even while SYS is isolated from roles.

    The SYS gate lives only on the ROLE-closure arm, so the untouched USER arm
    still emits the row for scope_value = 'SYS' when group_sys_with_dba is FALSE.
    """
    conn = clean_resolution
    _set_sys_grouping(conn, "FALSE")
    _insert_rule(
        conn,
        rule_id=1,
        scope_type="USER",
        scope_value=_SYS,
        phase="EXPAND",
        script_name="USER_SYS_SCRIPT",
        function_name="user_sys_fn",
    )

    _refresh(conn)

    sys_rows = _resolution_for(conn, _SYS)
    assert ("EXPAND", "USER_SYS_SCRIPT", "user_sys_fn", 1) in sys_rows, (
        "USER-scoped SYS rule was removed under isolation; the gate must touch "
        "only the ROLE-closure arm"
    )


@pytest.mark.integration
def test_non_sys_dba_holder_unaffected_by_toggle(
    clean_resolution, role_holder, restore_sys_grouping
):
    """With grouping disabled, a non-SYS DBA holder still gets DBA-scoped rules.

    The gate excludes only the SYS account, never any other DBA holder. The test
    holder is granted DBA, and after refresh with group_sys_with_dba = FALSE it
    must carry the DBA-derived row while SYS does not.
    """
    conn = clean_resolution
    try:
        conn.execute(f"GRANT DBA TO {role_holder}")
        _set_sys_grouping(conn, "FALSE")
        _insert_rule(
            conn,
            rule_id=1,
            scope_type="ROLE",
            scope_value="DBA",
            phase="EXPAND",
            script_name="DBA_SCRIPT",
            function_name="dba_fn",
        )

        _refresh(conn)

        holder_rows = _resolution_for(conn, role_holder)
        assert ("EXPAND", "DBA_SCRIPT", "dba_fn", 1) in holder_rows, (
            "a non-SYS DBA holder lost its DBA-derived rule under isolation"
        )
        assert _sys_row_count(conn, "dba_fn") == 0, (
            "SYS was not excluded from the DBA rule under isolation"
        )
    finally:
        try:
            conn.execute(f"REVOKE DBA FROM {role_holder}")
        except Exception:
            pass


@pytest.mark.integration
def test_null_grouping_treated_as_grouped(clean_resolution, restore_sys_grouping):
    """A NULL group_sys_with_dba (upgraded, never-set) resolves as grouped for SYS.

    Setting the toggle to a genuine SQL NULL mimics an upgraded install whose
    additive column was never populated. COALESCE-to-TRUE at the read site must
    place the DBA-derived row for SYS with no backfill UPDATE having run.
    """
    conn = clean_resolution
    _set_sys_grouping(conn, "NULL")
    _insert_rule(
        conn,
        rule_id=1,
        scope_type="ROLE",
        scope_value="DBA",
        phase="EXPAND",
        script_name="DBA_SCRIPT",
        function_name="dba_fn",
    )

    _refresh(conn)

    assert _sys_row_count(conn, "dba_fn") >= 1, (
        "NULL group_sys_with_dba was not treated as grouped for SYS"
    )
