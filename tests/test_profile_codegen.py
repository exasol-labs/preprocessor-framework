"""Integration tests for profile codegen in PREPROC.REFRESH_CORE.

These tests exercise the eager, DB-side profile generation folded into the
atomic resolution rebuild (``REFRESH_CORE.rebuild()``): after RESOLUTION is
reinserted, every row of a user's chain is stamped with a content-addressed
``profile_hash`` (HASH_MD5 over the user's ordered manifest), a hash-named
``PREPROC_RT.P_<hash>`` profile script is generated via CREATE OR REPLACE, and a
``PREPROC.PROFILES`` row is upserted -- all inside the single commit, so no
committed RESOLUTION row ever references a not-yet-created script.

Isolation
    RESOLUTION, CONFIG, and PROFILES are global tables on a session-scoped
    connection shared with the rest of the suite, and REFRESH does a full DELETE
    of RESOLUTION before reinserting. The ``clean_profiles`` fixture wipes
    RESOLUTION, CONFIG, and PROFILES before and after every test so no rule or
    profile leaks across tests. Generated P_<hash> scripts are content-addressed
    and idempotent; they are left in PREPROC_RT (DROP is Group 6's reclaim path,
    out of scope here) but cannot collide because the name is the hash.

Current-user strategy
    MY_PIPELINE filters on CURRENT_USER, and a USER-scoped CONFIG rule resolves
    directly to its scope_value with no role grant required. Tests therefore add
    USER-scoped rules for CURRENT_USER so RESOLUTION populates for the connecting
    session and the same session can read its own MY_PIPELINE rows.
"""

import pytest

_REAL_SCRIPT = "PREPROC_RT.DEMO_PING_V1"


@pytest.fixture
def clean_profiles(installed):
    """Empty RESOLUTION, CONFIG, and PROFILES around the test.

    Wiping before guards against a prior crashed test; wiping after keeps the
    shared session clean for the next test.
    """
    installed.execute("DELETE FROM PREPROC.RESOLUTION")
    installed.execute("DELETE FROM PREPROC.CONFIG")
    installed.execute("DELETE FROM PREPROC.PROFILES")
    yield installed
    installed.execute("DELETE FROM PREPROC.RESOLUTION")
    installed.execute("DELETE FROM PREPROC.CONFIG")
    installed.execute("DELETE FROM PREPROC.PROFILES")


def _current_user(conn):
    return conn.execute("SELECT CURRENT_USER").fetchval()


def _insert_user_rule(
    conn,
    *,
    rule_id,
    user_name,
    phase,
    script_name,
    function_name,
    client_pattern=None,
):
    """Insert one USER-scoped PREPROC.CONFIG rule for the given user."""
    conn.execute(
        "INSERT INTO PREPROC.CONFIG "
        "(rule_id, scope_type, scope_value, client_pattern, phase, "
        "script_name, function_name, enabled, version) "
        "VALUES ({rid}, 'USER', {sv}, {cp}, {ph}, {sn}, {fn}, TRUE, 1)",
        {
            "rid": rule_id,
            "sv": user_name,
            "cp": client_pattern,
            "ph": phase,
            "sn": script_name,
            "fn": function_name,
        },
    )


def _refresh(conn):
    return conn.execute("EXECUTE SCRIPT PREPROC.REFRESH").fetchall()


def _hash_for(conn, user_name):
    """Return the (single) distinct non-NULL profile_hash for a user, or None."""
    rows = conn.execute(
        "SELECT DISTINCT profile_hash FROM PREPROC.RESOLUTION "
        "WHERE user_name = {u} AND profile_hash IS NOT NULL",
        {"u": user_name},
    ).fetchall()
    return rows[0][0] if rows else None


def _script_exists(conn, hash_value):
    """True if PREPROC_RT.P_<hash> exists.

    Exasol folds the unquoted CREATE SCRIPT identifier to upper case, so the
    stored SCRIPT_NAME is P_<HASH-UPPER> while the RESOLUTION hash is lower-case
    hex; the comparison is therefore case-insensitive. An unquoted ALTER SESSION
    reference resolves identically regardless of case, so the swap still binds.
    """
    return (
        conn.execute(
            "SELECT COUNT(*) FROM EXA_ALL_SCRIPTS "
            "WHERE SCRIPT_SCHEMA = 'PREPROC_RT' AND SCRIPT_NAME = UPPER({n})",
            {"n": "P_" + str(hash_value)},
        ).fetchval()
        > 0
    )


def _profiles_count(conn, hash_value):
    return conn.execute(
        "SELECT COUNT(*) FROM PREPROC.PROFILES WHERE profile_hash = {h}",
        {"h": hash_value},
    ).fetchval()


@pytest.mark.integration
def test_profile_generated_after_refresh(clean_profiles):
    """Refresh stamps the hash, creates P_<hash>, and records PROFILES."""
    conn = clean_profiles
    user = _current_user(conn)
    _insert_user_rule(
        conn,
        rule_id=1,
        user_name=user,
        phase="REWRITE",
        script_name=_REAL_SCRIPT,
        function_name="ping_fn",
    )

    _refresh(conn)

    hash_value = _hash_for(conn, user)
    assert hash_value is not None, "RESOLUTION carries no profile_hash for the user"
    assert _script_exists(conn, hash_value), f"P_{hash_value} not in EXA_ALL_SCRIPTS"
    assert _profiles_count(conn, hash_value) == 1, "PROFILES has no row for the hash"


@pytest.mark.integration
def test_profile_idempotent_on_re_refresh(clean_profiles):
    """Re-refreshing an unchanged chain keeps the same hash and script."""
    conn = clean_profiles
    user = _current_user(conn)
    _insert_user_rule(
        conn,
        rule_id=1,
        user_name=user,
        phase="REWRITE",
        script_name=_REAL_SCRIPT,
        function_name="ping_fn",
    )

    _refresh(conn)
    hash1 = _hash_for(conn, user)
    _refresh(conn)
    hash2 = _hash_for(conn, user)

    assert hash1 == hash2, "an unchanged chain produced a different hash"
    assert _script_exists(conn, hash2), "profile script vanished after re-refresh"
    assert _profiles_count(conn, hash2) == 1, "PROFILES gained a duplicate row"


@pytest.mark.integration
def test_hash_changes_when_chain_changes(clean_profiles):
    """Adding a rule to the chain changes the user's profile_hash."""
    conn = clean_profiles
    user = _current_user(conn)
    _insert_user_rule(
        conn,
        rule_id=1,
        user_name=user,
        phase="REWRITE",
        script_name=_REAL_SCRIPT,
        function_name="fn_a",
    )
    _refresh(conn)
    hash1 = _hash_for(conn, user)

    _insert_user_rule(
        conn,
        rule_id=2,
        user_name=user,
        phase="REWRITE",
        script_name=_REAL_SCRIPT,
        function_name="fn_b",
    )
    _refresh(conn)
    hash2 = _hash_for(conn, user)

    assert hash1 is not None and hash2 is not None
    assert hash1 != hash2, "hash did not change when the chain changed"


@pytest.mark.integration
def test_no_profile_for_empty_chain(clean_profiles):
    """A user with no rules gets no RESOLUTION row, hash, or PROFILES entry."""
    conn = clean_profiles
    user = _current_user(conn)

    _refresh(conn)

    rows = conn.execute(
        "SELECT COUNT(*) FROM PREPROC.RESOLUTION WHERE user_name = {u}",
        {"u": user},
    ).fetchval()
    assert rows == 0, "empty chain produced RESOLUTION rows"
    assert _hash_for(conn, user) is None
    assert conn.execute("SELECT COUNT(*) FROM PREPROC.PROFILES").fetchval() == 0, (
        "empty chain produced a PROFILES row"
    )


@pytest.mark.integration
def test_resolution_profile_hash_populated(clean_profiles):
    """Every RESOLUTION row for a non-empty chain carries a non-NULL hash."""
    conn = clean_profiles
    user = _current_user(conn)
    _insert_user_rule(
        conn,
        rule_id=1,
        user_name=user,
        phase="TRANSLATE",
        script_name=_REAL_SCRIPT,
        function_name="fn_t",
    )
    _insert_user_rule(
        conn,
        rule_id=2,
        user_name=user,
        phase="REWRITE",
        script_name=_REAL_SCRIPT,
        function_name="fn_r",
    )

    _refresh(conn)

    null_count = conn.execute(
        "SELECT COUNT(*) FROM PREPROC.RESOLUTION WHERE user_name = {u} AND profile_hash IS NULL",
        {"u": user},
    ).fetchval()
    total = conn.execute(
        "SELECT COUNT(*) FROM PREPROC.RESOLUTION WHERE user_name = {u}",
        {"u": user},
    ).fetchval()
    assert total >= 2, "expected the two inserted rules to materialise"
    assert null_count == 0, "some RESOLUTION rows lack a profile_hash"


@pytest.mark.integration
def test_profile_hash_in_view(clean_profiles):
    """MY_PIPELINE surfaces the non-NULL profile_hash for the current user."""
    conn = clean_profiles
    user = _current_user(conn)
    _insert_user_rule(
        conn,
        rule_id=1,
        user_name=user,
        phase="REWRITE",
        script_name=_REAL_SCRIPT,
        function_name="fn_view",
    )

    _refresh(conn)

    hashes = conn.execute(
        "SELECT DISTINCT profile_hash FROM PREPROC_RT.MY_PIPELINE WHERE profile_hash IS NOT NULL"
    ).fetchall()
    assert len(hashes) == 1, "MY_PIPELINE did not surface exactly one profile_hash"
    assert hashes[0][0] == _hash_for(conn, user)


@pytest.mark.integration
def test_hash_manifest_distinctness(clean_profiles):
    """Different chains for the same user produce distinct hashes.

    Two refreshes with different rule sets must yield different hashes, proving
    the manifest distinguishes chains by their content.
    """
    conn = clean_profiles
    user = _current_user(conn)

    _insert_user_rule(
        conn,
        rule_id=1,
        user_name=user,
        phase="REWRITE",
        script_name=_REAL_SCRIPT,
        function_name="alpha",
    )
    _refresh(conn)
    hash_alpha = _hash_for(conn, user)

    conn.execute("DELETE FROM PREPROC.CONFIG")
    _insert_user_rule(
        conn,
        rule_id=1,
        user_name=user,
        phase="REWRITE",
        script_name=_REAL_SCRIPT,
        function_name="beta",
    )
    _refresh(conn)
    hash_beta = _hash_for(conn, user)

    assert hash_alpha is not None and hash_beta is not None
    assert hash_alpha != hash_beta, "distinct chains collided to one hash"


@pytest.mark.integration
def test_rebuild_atomicity_with_profiles(clean_profiles):
    """Every RESOLUTION hash has a PROFILES row and a generated script.

    After a single refresh the three artefacts are mutually consistent: no
    RESOLUTION hash references a missing PROFILES row or missing script.
    """
    conn = clean_profiles
    user = _current_user(conn)
    _insert_user_rule(
        conn,
        rule_id=1,
        user_name=user,
        phase="REWRITE",
        script_name=_REAL_SCRIPT,
        function_name="fn_atomic",
    )

    _refresh(conn)

    hashes = conn.execute(
        "SELECT DISTINCT profile_hash FROM PREPROC.RESOLUTION WHERE profile_hash IS NOT NULL"
    ).fetchall()
    assert hashes, "no profile_hash materialised"
    for (hash_value,) in hashes:
        assert _profiles_count(conn, hash_value) == 1, (
            f"RESOLUTION hash {hash_value} has no PROFILES row"
        )
        assert _script_exists(conn, hash_value), (
            f"RESOLUTION hash {hash_value} has no generated script"
        )


@pytest.mark.integration
def test_profile_regen_on_mutation(clean_profiles):
    """A CONFIG mutation via RULE_ADD triggers profile codegen.

    RULE_ADD calls REFRESH_CORE.rebuild() on success, so the profile artefacts
    must appear without an explicit PREPROC.REFRESH.
    """
    conn = clean_profiles
    user = _current_user(conn)

    conn.execute(
        "EXECUTE SCRIPT PREPROC.RULE_ADD('USER', {u}, 'REWRITE', {sn}, 'mutate_fn', NULL, FALSE)",
        {"u": user, "sn": _REAL_SCRIPT},
    )

    hash_value = _hash_for(conn, user)
    assert hash_value is not None, "RULE_ADD did not stamp a profile_hash"
    assert _script_exists(conn, hash_value), "RULE_ADD did not generate the script"
    assert _profiles_count(conn, hash_value) == 1, "RULE_ADD did not record PROFILES"


@pytest.mark.integration
def test_refresh_never_drops_profile(clean_profiles):
    """A refresh that orphans a chain does not drop its profile script.

    The never-same-refresh-drop rule: after the rule is removed and a refresh
    rebuilds RESOLUTION without it, the previously generated P_<hash> must still
    exist (reclaim is Group 6, never the orphaning refresh).
    """
    conn = clean_profiles
    user = _current_user(conn)
    _insert_user_rule(
        conn,
        rule_id=1,
        user_name=user,
        phase="REWRITE",
        script_name=_REAL_SCRIPT,
        function_name="fn_keep",
    )
    _refresh(conn)
    hash1 = _hash_for(conn, user)
    assert _script_exists(conn, hash1)

    conn.execute("DELETE FROM PREPROC.CONFIG")
    _refresh(conn)

    assert _script_exists(conn, hash1), "orphaning refresh dropped the profile script"


@pytest.mark.integration
def test_profile_codegen_atomicity(clean_profiles):
    """No PROFILES row exists without its generating script, and vice versa.

    Consistency check companion to test_rebuild_atomicity_with_profiles, viewed
    from the PROFILES side: every recorded hash has a script.
    """
    conn = clean_profiles
    user = _current_user(conn)
    _insert_user_rule(
        conn,
        rule_id=1,
        user_name=user,
        phase="REWRITE",
        script_name=_REAL_SCRIPT,
        function_name="fn_consistency",
    )

    _refresh(conn)

    profile_hashes = conn.execute("SELECT profile_hash FROM PREPROC.PROFILES").fetchall()
    assert profile_hashes, "PROFILES is empty after a non-empty refresh"
    for (hash_value,) in profile_hashes:
        assert _script_exists(conn, hash_value), (
            f"PROFILES hash {hash_value} has no generated script"
        )


@pytest.mark.integration
def test_codegen_is_db_side(clean_profiles):
    """Codegen runs inside the DB-side Lua path, not the Python CLI.

    PREPROC.PROFILES is populated by EXECUTE SCRIPT PREPROC.REFRESH alone (the
    Lua rebuild), with no Python-side codegen step, proving the hash/UPDATE,
    CREATE SCRIPT, and PROFILES upsert live in refresh_core.lua.
    """
    conn = clean_profiles
    user = _current_user(conn)
    _insert_user_rule(
        conn,
        rule_id=1,
        user_name=user,
        phase="REWRITE",
        script_name=_REAL_SCRIPT,
        function_name="fn_dbside",
    )

    _refresh(conn)

    populated = conn.execute("SELECT COUNT(*) FROM PREPROC.PROFILES").fetchval()
    assert populated >= 1, "the Lua refresh path did not populate PROFILES"
