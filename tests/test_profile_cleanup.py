"""Integration tests for the profile retire-then-reclaim lifecycle.

REFRESH_CORE.rebuild() manages a profile script's full lifecycle across refreshes
(see the module header in ``sql/lua/refresh_core.lua``):

  GENERATE  CREATE OR REPLACE the real baked body for every current chain, and
            clear ``orphaned_at`` for any re-adopted hash.
  RETIRE    For each PROFILES hash no longer in any current RESOLUTION row and
            not already retired, CREATE OR REPLACE the script with the MASTER
            STUB and stamp ``orphaned_at = CURRENT_TIMESTAMP``. The retirement
            stub is built by reading PREPROC_RT.MASTER's own body back from
            EXA_ALL_SCRIPTS, so the stub is byte-for-byte MASTER — one source of
            truth. The script is NEVER dropped on the refresh that orphans it.
  RECLAIM   For each retired hash orphaned on a PRIOR refresh (``orphaned_at <
            reclaim_cutoff``) with no active session that logged in before it was
            retired, DROP SCRIPT PREPROC_RT.P_<hash> and delete its PROFILES row.

The two hard invariants under test:
  * never-same-refresh-drop — a hash is retired (stub + stamp) first and reclaimed
    only on a LATER refresh once provably unpinned;
  * the retirement stub IS MASTER — a session pinned to a retired profile re-runs
    the full inline MASTER dispatch for its next statement (correct transform,
    then a re-point to the current target), so there is no untransformed gap.

Stub vs. baked body
    The stub is byte-for-byte MASTER, whose inline dispatch reads the live
    pipeline via ``SELECT ... FROM PREPROC_RT.MY_PIPELINE``. A real baked profile
    body, by contrast, carries its rules in an inline ``local rules`` table and
    NEVER queries MY_PIPELINE. The presence of ``MY_PIPELINE`` in a script body
    is therefore the reliable discriminator: stub ⇒ MY_PIPELINE present; baked
    profile ⇒ MY_PIPELINE absent.

    The re-point is verified BEHAVIOURALLY (this Exasol build's
    EXA_ALL_SESSIONS has no SQL_PREPROCESSOR column): a session pinned to a
    retired stub, on its next statement, transforms that statement under the
    NOW-CURRENT pipeline (proving the stub re-ran MASTER and re-pointed itself).

Connection discipline
    The session-scoped ``installed`` connection logged in at suite start, so its
    LOGIN_TIME predates any hash this suite orphans; that always-older session
    PREVENTS reclaim. The reclaim tests therefore open a SEPARATE short-lived
    connection (``connect_from_env``) for the orphaning/reclaiming refreshes and
    close every connection older than the orphaning point before the reclaiming
    refresh, so no session older than ``orphaned_at`` remains active. The pinning
    test (6.9) uses a dedicated second connection pinned to the old profile while
    ``installed`` performs the orphaning refresh from another session.

Isolation
    CONFIG/RESOLUTION/PROFILES are global tables wiped before and after every
    test. A final refresh restores a clean (empty) state, and every pinned or
    extra connection is deactivated/closed in ``finally``.
"""

import pytest

from preproc.connection import connect_from_env

_DEACTIVATE = "ALTER SESSION SET sql_preprocessor_script = ''"

_SEED = "SEED"
_PROBE = f"SELECT '{_SEED}' AS PROBE"

_MODULE = "PREPROC_RT.TEST_CLEANUP_MOD"


def _deploy_module(conn):
    """Deploy a Lua module whose REWRITE functions tag a sentinel literal.

    Mirrors the test_profile_dispatch module: ``tag_a`` appends ``|A`` just
    before the closing quote of the ``'SEED...'`` literal, so the scalar fetched
    back encodes whether the rule fired. ``tag_b`` tags ``|B`` to distinguish a
    second chain.
    """
    conn.execute(
        f"""CREATE OR REPLACE LUA SCRIPT {_MODULE}() AS
            local function append(sqltext, tag)
                local out = string.gsub(sqltext, "({_SEED}[^']*)'", "%1|" .. tag .. "'", 1)
                return out
            end
            function tag_a(sqltext)  return append(sqltext, "A") end
            function tag_b(sqltext)  return append(sqltext, "B") end
        """
    )


def _current_user(conn):
    return conn.execute("SELECT CURRENT_USER").fetchval()


def _set_mode(conn, mode):
    conn.execute(f"EXECUTE SCRIPT PREPROC.SET_MODE('{mode}', FALSE)")


def _insert_user_rewrite_rule(conn, *, user_name, function_name, rule_id=1):
    """Insert one enabled USER-scoped REWRITE rule pointing at the test module."""
    conn.execute(
        "INSERT INTO PREPROC.CONFIG "
        "(rule_id, scope_type, scope_value, client_pattern, phase, "
        "script_name, function_name, enabled, version) "
        "VALUES ({rid}, 'USER', {sv}, NULL, 'REWRITE', {sn}, {fn}, TRUE, 1)",
        {"rid": rule_id, "sv": user_name, "sn": _MODULE, "fn": function_name},
    )


def _refresh(conn):
    conn.execute("EXECUTE SCRIPT PREPROC.REFRESH").fetchall()


def _hash_for(conn, user_name):
    """Return the single distinct non-NULL profile_hash for a user, or None."""
    rows = conn.execute(
        "SELECT DISTINCT profile_hash FROM PREPROC.RESOLUTION "
        "WHERE user_name = {u} AND profile_hash IS NOT NULL",
        {"u": user_name},
    ).fetchall()
    return rows[0][0] if rows else None


def _script_exists(conn, hash_value):
    """True if PREPROC_RT.P_<hash> exists (case-insensitive name comparison)."""
    return (
        conn.execute(
            "SELECT COUNT(*) FROM EXA_ALL_SCRIPTS "
            "WHERE SCRIPT_SCHEMA = 'PREPROC_RT' AND SCRIPT_NAME = UPPER({n})",
            {"n": "P_" + str(hash_value)},
        ).fetchval()
        > 0
    )


def _script_text_by_name(conn, script_name):
    """The SCRIPT_TEXT of PREPROC_RT.<script_name>, or '' if absent.

    Names are stored upper-cased in EXA_ALL_SCRIPTS, so the comparison is made
    case-insensitively.
    """
    value = conn.execute(
        "SELECT SCRIPT_TEXT FROM EXA_ALL_SCRIPTS "
        "WHERE SCRIPT_SCHEMA = 'PREPROC_RT' AND SCRIPT_NAME = UPPER({n})",
        {"n": script_name},
    ).fetchval()
    return "" if value is None else str(value)


def _script_text(conn, hash_value):
    """The SCRIPT_TEXT of PREPROC_RT.P_<hash>, or '' if absent."""
    return _script_text_by_name(conn, "P_" + str(hash_value))


def _body_without_header(script_text):
    """Strip the leading ``CREATE ... AS`` header line from a stored script body.

    EXA_ALL_SCRIPTS stores the full ``CREATE [OR REPLACE] LUA SCRIPT name() AS\\n``
    statement; everything up to and including the first newline is the header.
    Two scripts with different names but identical bodies compare equal after
    this strip.
    """
    newline = script_text.find("\n")
    return "" if newline < 0 else script_text[newline + 1 :]


_STUB_MARKER = "MY_PIPELINE"
_BAKED_MARKER = "local rules"


def _is_stub_body(script_text):
    """True if a script body is the retirement stub (i.e. the inline MASTER body).

    The stub is byte-for-byte MASTER, whose inline dispatch reads the live
    pipeline via PREPROC_RT.MY_PIPELINE. A baked profile body never queries that
    view, so the marker's presence uniquely identifies the stub.
    """
    return _STUB_MARKER in script_text


def _is_baked_body(script_text):
    """True if a script body is a real baked profile (inline rules, no MY_PIPELINE)."""
    return _BAKED_MARKER in script_text and _STUB_MARKER not in script_text


def _orphaned_at(conn, hash_value):
    """The orphaned_at timestamp for a hash, or None (NULL or row absent)."""
    row = conn.execute(
        "SELECT orphaned_at FROM PREPROC.PROFILES WHERE profile_hash = {h}",
        {"h": hash_value},
    ).fetchone()
    return None if row is None else row[0]


def _profiles_row_exists(conn, hash_value):
    return (
        conn.execute(
            "SELECT COUNT(*) FROM PREPROC.PROFILES WHERE profile_hash = {h}",
            {"h": hash_value},
        ).fetchval()
        > 0
    )


def _wipe(conn):
    for table in ("RESOLUTION", "CONFIG", "PROFILES"):
        conn.execute(f"DELETE FROM PREPROC.{table}")


@pytest.fixture
def cleanup_env(installed):
    """Clean rule/profile state, convenience mode, deploy the test module.

    Wipes RESOLUTION/CONFIG/PROFILES before and after, restores convenience mode
    and a cleared preprocessor, refreshes to a clean state so any orphaned
    profiles are reclaimed, and drops the test module on teardown.
    """
    conn = installed
    _wipe(conn)
    _set_mode(conn, "convenience")
    _deploy_module(conn)
    try:
        yield conn
    finally:
        try:
            conn.execute(_DEACTIVATE)
        finally:
            _wipe(conn)
            try:
                conn.execute(f"DROP SCRIPT {_MODULE}")
            finally:
                _set_mode(conn, "convenience")
                _refresh(conn)


def _make_orphan(conn, *, function_name="tag_a", rule_id=1):
    """Create a profile for CURRENT_USER then orphan it, returning its hash.

    Inserts a REWRITE rule, refreshes (creating P_<hash>), then deletes the rule
    and refreshes again (orphaning the hash → stub + orphaned_at). The hash is
    returned for assertions. CONFIG is cleared so the hash is no longer in any
    current RESOLUTION row.
    """
    user = _current_user(conn)
    _insert_user_rewrite_rule(conn, user_name=user, function_name=function_name, rule_id=rule_id)
    _refresh(conn)
    hash_value = _hash_for(conn, user)
    assert hash_value is not None, "no profile_hash materialised for the rule"
    conn.execute("DELETE FROM PREPROC.CONFIG")
    _refresh(conn)
    return hash_value


# --- 6.8: retire -----------------------------------------------------------


@pytest.mark.integration
def test_orphan_retired_not_dropped(cleanup_env):
    """An orphaned hash is retired (stub + stamp), NOT dropped on that refresh.

    After the orphaning refresh the script still exists, orphaned_at is non-NULL,
    and the body is now the MASTER stub (the inline dispatch, which queries
    MY_PIPELINE) rather than the real baked body (which carries an inline
    ``rules`` table and never queries MY_PIPELINE).
    """
    conn = cleanup_env
    hash_value = _make_orphan(conn)

    assert _script_exists(conn, hash_value), "orphaning refresh dropped the script"
    assert _orphaned_at(conn, hash_value) is not None, "orphaned_at was not stamped"
    assert _is_stub_body(_script_text(conn, hash_value)), (
        "retired script body is not the MASTER stub"
    )


@pytest.mark.integration
def test_stub_equals_master_body(cleanup_env):
    """The retirement stub is byte-for-byte PREPROC_RT.MASTER's body.

    The stub is built by reading MASTER's own SCRIPT_TEXT back from the catalog
    and deploying it verbatim under the P_<hash> name, so the two bodies must be
    identical once each one's leading ``CREATE ... AS`` header line (the only
    place the script name appears) is stripped.
    """
    conn = cleanup_env
    hash_value = _make_orphan(conn)
    assert _orphaned_at(conn, hash_value) is not None, "orphaned_at was not stamped"

    stub_body = _body_without_header(_script_text(conn, hash_value))
    master_body = _body_without_header(_script_text_by_name(conn, "MASTER"))

    assert master_body, "PREPROC_RT.MASTER body could not be read from the catalog"
    assert stub_body == master_body, (
        "retired stub body is not byte-for-byte identical to MASTER's body"
    )


@pytest.mark.integration
def test_retired_profile_not_restamped(cleanup_env):
    """A still-retired hash keeps its original orphaned_at across a later refresh.

    The hash is orphaned (orphaned_at = t1). A long-lived connection that logged
    in BEFORE t1 is kept open across a second refresh, so the hash is not
    reclaimable; it stays retired. RETIRE must skip it (orphaned_at non-NULL), so
    orphaned_at is unchanged — not re-stamped to the second refresh's timestamp.
    """
    conn = cleanup_env
    pinner = connect_from_env()
    try:
        # pinner logs in now (t0); the orphan below is stamped at t1 > t0, so the
        # pinner keeps the hash unreclaimable across the second refresh.
        pinner.execute("SELECT 1").fetchval()
        hash_value = _make_orphan(conn)
        t1 = _orphaned_at(conn, hash_value)
        assert t1 is not None

        _refresh(conn)

        assert _script_exists(conn, hash_value), "still-retired hash was dropped while pinnable"
        assert _orphaned_at(conn, hash_value) == t1, "orphaned_at was re-stamped on a later refresh"
    finally:
        pinner.close()


@pytest.mark.integration
def test_current_profile_never_cleaned(cleanup_env):
    """A hash referenced by a current RESOLUTION row is never retired or dropped.

    Across two refreshes with the rule still present, orphaned_at stays NULL and
    the script keeps its real baked body (an inline ``rules`` table, never the
    MY_PIPELINE-querying stub).
    """
    conn = cleanup_env
    user = _current_user(conn)
    _insert_user_rewrite_rule(conn, user_name=user, function_name="tag_a")
    _refresh(conn)
    hash_value = _hash_for(conn, user)

    _refresh(conn)

    assert _script_exists(conn, hash_value), "current profile script was dropped"
    assert _orphaned_at(conn, hash_value) is None, "current profile was stamped orphaned"
    assert _is_baked_body(_script_text(conn, hash_value)), (
        "current profile body was replaced by the retirement stub"
    )


# --- 6.9: retired-profile correctness path (stub == MASTER) ----------------


@pytest.mark.integration
def test_retired_session_reruns_master_logic(cleanup_env):
    """A session pinned to a retired stub transforms its next statement and re-points.

    Setup: rule A → P_<oldhash>; pin a dedicated session to P_<oldhash>. From
    ``installed`` (a different session) change the rule to B and refresh — this
    orphans oldhash (→ stub) and creates P_<newhash> for chain B. On the pinned
    session submit the sentinel SELECT. Because the stub IS MASTER, the statement
    is transformed under the NOW-CURRENT chain (tag B, ``SEED|B``, NOT returned
    unchanged) and the session is re-pointed to P_<newhash>.

    The re-point is verified BEHAVIOURALLY: this build's EXA_ALL_SESSIONS has no
    SQL_PREPROCESSOR column, so the proof is that the triggering statement comes
    back ``SEED|B`` — the stub re-ran the full MASTER dispatch under the
    now-current pipeline. A ``SEED|A`` or untransformed ``SEED`` would mean the
    stub failed to re-run MASTER.
    """
    conn = cleanup_env
    user = _current_user(conn)
    _insert_user_rewrite_rule(conn, user_name=user, function_name="tag_a", rule_id=1)
    _refresh(conn)
    old_hash = _hash_for(conn, user)
    assert old_hash is not None

    pinned = connect_from_env()
    try:
        pinned.execute(f"ALTER SESSION SET sql_preprocessor_script = PREPROC_RT.P_{old_hash}")

        # Re-resolve the user to chain B from a DIFFERENT session, orphaning oldhash.
        conn.execute("DELETE FROM PREPROC.CONFIG")
        _insert_user_rewrite_rule(conn, user_name=user, function_name="tag_b", rule_id=2)
        _refresh(conn)
        new_hash = _hash_for(conn, user)
        assert new_hash is not None and new_hash != old_hash
        assert _is_stub_body(_script_text(conn, old_hash)), "oldhash was not retired to the stub"

        # The pinned session's read transaction began at its ALTER SESSION, before
        # the orphaning refresh committed; commit() releases that snapshot so the
        # next statement's stub lookup sees the now-current chain. (A real session
        # gets a fresh snapshot per autocommitted statement; here the session sat
        # idle in one transaction across the refresh.)
        pinned.commit()

        result = pinned.execute(_PROBE).fetchval()
        assert result == f"{_SEED}|B", (
            "retired stub did not transform the triggering statement under the current chain"
        )
    finally:
        try:
            pinned.execute(_DEACTIVATE)
        finally:
            pinned.close()


@pytest.mark.integration
def test_stub_transforms_triggering_statement(cleanup_env):
    """Focused: the triggering statement under a retired stub is transformed.

    A session pinned to a retired stub whose current chain tags ``A`` must yield
    ``SEED|A`` for the triggering statement — proving the stub does not leave the
    statement untransformed even though the pinned script is now a stub.
    """
    conn = cleanup_env
    user = _current_user(conn)
    # Build oldhash from a throwaway chain, then keep chain A current.
    _insert_user_rewrite_rule(conn, user_name=user, function_name="tag_b", rule_id=1)
    _refresh(conn)
    old_hash = _hash_for(conn, user)

    pinned = connect_from_env()
    try:
        pinned.execute(f"ALTER SESSION SET sql_preprocessor_script = PREPROC_RT.P_{old_hash}")

        conn.execute("DELETE FROM PREPROC.CONFIG")
        _insert_user_rewrite_rule(conn, user_name=user, function_name="tag_a", rule_id=2)
        _refresh(conn)
        assert _is_stub_body(_script_text(conn, old_hash))

        pinned.commit()  # release the pre-refresh snapshot (see 6.9 reruns test)
        assert pinned.execute(_PROBE).fetchval() == f"{_SEED}|A"
    finally:
        try:
            pinned.execute(_DEACTIVATE)
        finally:
            pinned.close()


@pytest.mark.integration
def test_stub_honors_control_guard(cleanup_env):
    """A retired stub passes ALTER SESSION through and does not re-point on it.

    With a session pinned to a retired stub, an ALTER SESSION control statement
    must be guarded (returned early, no pipeline lookup, no re-point), so the
    session preprocessor is unchanged by the control statement and the session
    stays usable.
    """
    conn = cleanup_env
    user = _current_user(conn)
    _insert_user_rewrite_rule(conn, user_name=user, function_name="tag_a", rule_id=1)
    _refresh(conn)
    old_hash = _hash_for(conn, user)

    pinned = connect_from_env()
    try:
        pinned.execute(f"ALTER SESSION SET sql_preprocessor_script = PREPROC_RT.P_{old_hash}")

        conn.execute("DELETE FROM PREPROC.CONFIG")
        _insert_user_rewrite_rule(conn, user_name=user, function_name="tag_b", rule_id=2)
        _refresh(conn)
        assert _is_stub_body(_script_text(conn, old_hash))

        pinned.commit()  # release the pre-refresh snapshot (see 6.9 reruns test)

        # The control statement must be guarded: returned early WITHOUT a pipeline
        # lookup or a re-point. It must therefore succeed under the stub. A
        # subsequent normal statement still works, confirming the guard left the
        # session usable.
        pinned.execute("ALTER SESSION SET QUERY_TIMEOUT = 600")
        try:
            assert pinned.execute("SELECT 314159 AS PROBE").fetchval() == 314159
        finally:
            pinned.execute("ALTER SESSION SET QUERY_TIMEOUT = 0")
    finally:
        try:
            pinned.execute(_DEACTIVATE)
        finally:
            pinned.close()


@pytest.mark.integration
def test_stub_fail_open(cleanup_env):
    """Re-point failure under a retired stub is fail-open: the statement is not lost.

    The re-point target P_<newhash> is dropped before the pinned session runs, so
    the stub's ALTER SESSION swap (issued by the inline MASTER body before
    setsqltext) fails.
    The fail-open guarantee is at the STATEMENT level: the swap is wrapped in
    pcall so the failure never raises into the user's statement, which still
    executes and returns its value. (Whether the in-flight rewrite still lands
    when the pre-setsqltext swap pquery fails is engine-dependent and NOT part of
    the fail-open contract — mirrors test_session_optimizer.test_swap_failure_fail_open,
    which likewise asserts only that the statement survives.)
    """
    conn = cleanup_env
    user = _current_user(conn)
    _insert_user_rewrite_rule(conn, user_name=user, function_name="tag_b", rule_id=1)
    _refresh(conn)
    old_hash = _hash_for(conn, user)

    pinned = connect_from_env()
    try:
        pinned.execute(f"ALTER SESSION SET sql_preprocessor_script = PREPROC_RT.P_{old_hash}")

        conn.execute("DELETE FROM PREPROC.CONFIG")
        _insert_user_rewrite_rule(conn, user_name=user, function_name="tag_a", rule_id=2)
        _refresh(conn)
        new_hash = _hash_for(conn, user)
        assert _is_stub_body(_script_text(conn, old_hash))

        # Remove the swap target so the stub's re-point ALTER SESSION fails.
        conn.execute(f"DROP SCRIPT PREPROC_RT.P_{new_hash}")
        pinned.commit()  # release the pre-refresh snapshot (see 6.9 reruns test)
        try:
            # The triggering statement must survive the failed re-point. The
            # probe carries a sentinel value so a lost statement (None) is
            # distinguishable from a surviving one.
            assert pinned.execute("SELECT 9090 AS PROBE").fetchval() == 9090, (
                "the triggering statement was lost when the re-point target was missing"
            )
        finally:
            _refresh(conn)
    finally:
        try:
            pinned.execute(_DEACTIVATE)
        finally:
            pinned.close()


# --- 6.10: reclaim ---------------------------------------------------------


@pytest.mark.integration
def test_not_reclaimed_while_pinnable_session_active(cleanup_env):
    """A retired hash is not dropped while a session older than orphaned_at is active.

    A long-lived connection logs in at t0, the hash is orphaned at t1 > t0, and
    that connection stays open across a second refresh. Its LOGIN_TIME < t1 makes
    the hash unreclaimable, so the script and PROFILES row survive.
    """
    conn = cleanup_env
    pinner = connect_from_env()
    try:
        pinner.execute("SELECT 1").fetchval()
        hash_value = _make_orphan(conn)
        assert _orphaned_at(conn, hash_value) is not None

        _refresh(conn)

        assert _script_exists(conn, hash_value), "hash dropped despite an older active session"
        assert _profiles_row_exists(conn, hash_value), (
            "PROFILES row deleted despite a pinnable session"
        )
    finally:
        pinner.close()


@pytest.mark.integration
def test_reclaimed_when_no_pinnable_session(cleanup_env):
    """A retired hash is dropped once no session older than orphaned_at is active.

    The reclaim criterion counts EVERY active session: any session that logged in
    before orphaned_at prevents reclaim. The orphaning refresh is issued from a
    worker connection that is then CLOSED, and the reclaiming refresh from a fresh
    connection that logged in AFTER the orphan — so no session this test controls
    predates orphaned_at. If another suite session (e.g. ``installed``) is older
    than orphaned_at it correctly prevents reclaim and the test skips rather than
    asserting a false negative.
    """
    conn = cleanup_env
    user = _current_user(conn)
    worker = connect_from_env()
    worker.execute("DELETE FROM PREPROC.CONFIG")
    worker.execute(
        "INSERT INTO PREPROC.CONFIG "
        "(rule_id, scope_type, scope_value, client_pattern, phase, "
        "script_name, function_name, enabled, version) "
        "VALUES (1, 'USER', {sv}, NULL, 'REWRITE', {sn}, 'tag_a', TRUE, 1)",
        {"sv": user, "sn": _MODULE},
    )
    worker.execute("EXECUTE SCRIPT PREPROC.REFRESH").fetchall()
    hash_value = worker.execute(
        "SELECT DISTINCT profile_hash FROM PREPROC.RESOLUTION "
        "WHERE user_name = {u} AND profile_hash IS NOT NULL",
        {"u": user},
    ).fetchval()
    worker.execute("DELETE FROM PREPROC.CONFIG")
    worker.execute("EXECUTE SCRIPT PREPROC.REFRESH").fetchall()  # orphan at t1
    worker.close()

    # If installed (the suite connection) logged in before t1, it confounds the
    # reclaim criterion. Skip rather than assert a false negative.
    orphaned_at = conn.execute(
        "SELECT orphaned_at FROM PREPROC.PROFILES WHERE profile_hash = {h}",
        {"h": hash_value},
    ).fetchval()
    older = conn.execute(
        "SELECT COUNT(*) FROM EXA_ALL_SESSIONS WHERE LOGIN_TIME < {t}",
        {"t": orphaned_at},
    ).fetchval()
    if older > 0:
        pytest.skip(
            "a session older than orphaned_at is active (e.g. the suite connection); "
            "reclaim is correctly prevented and cannot be asserted here"
        )

    reclaimer = connect_from_env()  # logs in after t1
    try:
        reclaimer.execute("EXECUTE SCRIPT PREPROC.REFRESH").fetchall()
        assert not _script_exists(conn, hash_value), "unpinned retired hash was not dropped"
        assert not _profiles_row_exists(conn, hash_value), "reclaimed hash kept its PROFILES row"
    finally:
        reclaimer.close()


@pytest.mark.integration
def test_reclaim_drops_provably_unpinned_hash(cleanup_env):
    """The RECLAIM path drops a hash whose orphaned_at predates every live session.

    Connection age is the confound for the live-orphan reclaim tests (the suite
    connection logged in first and prevents reclaim, forcing a skip). This test
    removes that confound deterministically: it orphans a hash, then backdates its
    orphaned_at to the distant past, so NO currently-open session can have
    LOGIN_TIME < orphaned_at and the reclaim criterion is satisfied for certain.
    The next refresh must DROP P_<hash> and delete its PROFILES row, exercising
    the RECLAIM DROP + delete on every CI database regardless of session timing.
    """
    conn = cleanup_env
    hash_value = _make_orphan(conn)
    assert _orphaned_at(conn, hash_value) is not None

    # Backdate the stamp below any possible live LOGIN_TIME so the criterion
    # NOT EXISTS (LOGIN_TIME < orphaned_at) holds for the whole cluster, while
    # still satisfying orphaned_at < reclaim_cutoff (captured at refresh start).
    conn.execute(
        "UPDATE PREPROC.PROFILES SET orphaned_at = TIMESTAMP '2000-01-01 00:00:00' "
        "WHERE profile_hash = {h}",
        {"h": hash_value},
    )

    _refresh(conn)

    assert not _script_exists(conn, hash_value), "provably-unpinned retired hash was not dropped"
    assert not _profiles_row_exists(conn, hash_value), "reclaimed hash kept its PROFILES row"


@pytest.mark.integration
def test_zero_sessions_reclaims_all(cleanup_env):
    """No session older than orphaned_at ⇒ every retired hash is reclaimed.

    Same connection-age technique as the single-hash reclaim: orphan from a worker
    that then disconnects, and (when no older session confounds) a reclaiming
    refresh from a fresh connection drops the retired hash and clears its PROFILES
    row, leaving zero orphaned PROFILES rows.
    """
    conn = cleanup_env
    user = _current_user(conn)

    worker = connect_from_env()
    worker.execute("DELETE FROM PREPROC.CONFIG")
    worker.execute(
        "INSERT INTO PREPROC.CONFIG "
        "(rule_id, scope_type, scope_value, client_pattern, phase, "
        "script_name, function_name, enabled, version) "
        "VALUES (1, 'USER', {sv}, NULL, 'REWRITE', {sn}, 'tag_a', TRUE, 1)",
        {"sv": user, "sn": _MODULE},
    )
    worker.execute("EXECUTE SCRIPT PREPROC.REFRESH").fetchall()
    hash_value = worker.execute(
        "SELECT DISTINCT profile_hash FROM PREPROC.RESOLUTION "
        "WHERE user_name = {u} AND profile_hash IS NOT NULL",
        {"u": user},
    ).fetchval()
    worker.execute("DELETE FROM PREPROC.CONFIG")
    worker.execute("EXECUTE SCRIPT PREPROC.REFRESH").fetchall()
    orphaned_at = worker.execute(
        "SELECT orphaned_at FROM PREPROC.PROFILES WHERE profile_hash = {h}",
        {"h": hash_value},
    ).fetchval()
    worker.close()

    older = conn.execute(
        "SELECT COUNT(*) FROM EXA_ALL_SESSIONS WHERE LOGIN_TIME < {t}",
        {"t": orphaned_at},
    ).fetchval()
    if older > 0:
        pytest.skip("a session older than orphaned_at is active; reclaim is correctly prevented")

    reclaimer = connect_from_env()
    try:
        reclaimer.execute("EXECUTE SCRIPT PREPROC.REFRESH").fetchall()
        retired = conn.execute(
            "SELECT COUNT(*) FROM PREPROC.PROFILES WHERE orphaned_at IS NOT NULL"
        ).fetchval()
        assert retired == 0, "retired profiles remained after a zero-older-session reclaim"
        assert not _script_exists(conn, hash_value), "retired hash script was not dropped"
    finally:
        reclaimer.close()


@pytest.mark.integration
def test_readopted_hash_restored(cleanup_env):
    """Re-adding the exact rules restores the real body and clears orphaned_at.

    Orphan a hash (stub + orphaned_at), then re-add the identical rule and
    refresh: the hash resolves again, so GENERATE re-creates the REAL baked body
    (carries an inline ``rules`` table and never queries MY_PIPELINE), the
    re-adoption clear sets orphaned_at back to NULL, and the script is NOT
    dropped. The baked body is distinguished from the stub by MY_PIPELINE being
    absent (the stub, being MASTER, always queries it).
    """
    conn = cleanup_env
    user = _current_user(conn)
    _insert_user_rewrite_rule(conn, user_name=user, function_name="tag_a", rule_id=1)
    _refresh(conn)
    hash_value = _hash_for(conn, user)

    conn.execute("DELETE FROM PREPROC.CONFIG")
    _refresh(conn)
    assert _orphaned_at(conn, hash_value) is not None, "hash was not orphaned"
    assert _is_stub_body(_script_text(conn, hash_value)), "hash was not retired to the stub"

    # Re-add the identical rule so the SAME hash resolves again.
    _insert_user_rewrite_rule(conn, user_name=user, function_name="tag_a", rule_id=1)
    _refresh(conn)
    readopted = _hash_for(conn, user)

    assert readopted == hash_value, "re-adding the identical rule produced a different hash"
    assert _script_exists(conn, hash_value), "re-adopted hash was dropped"
    assert _orphaned_at(conn, hash_value) is None, "re-adopted hash kept its orphaned_at stamp"
    assert _is_baked_body(_script_text(conn, hash_value)), (
        "re-adopted body is still the stub, not the real baked-rule body"
    )


# --- 6.11: atomicity -------------------------------------------------------


@pytest.mark.integration
def test_cleanup_atomicity(cleanup_env):
    """After a reclaiming refresh, no RESOLUTION row references a missing script.

    Every distinct RESOLUTION.profile_hash must have a corresponding P_<hash>
    script: a reader resolving the committed RESOLUTION never names a dropped
    hash, so the DROP + RESOLUTION rebuild are consistent at the commit boundary.
    """
    conn = cleanup_env
    user = _current_user(conn)
    _insert_user_rewrite_rule(conn, user_name=user, function_name="tag_a", rule_id=1)
    _refresh(conn)

    hashes = conn.execute(
        "SELECT DISTINCT profile_hash FROM PREPROC.RESOLUTION WHERE profile_hash IS NOT NULL"
    ).fetchall()
    for (hash_value,) in hashes:
        assert _script_exists(conn, hash_value), (
            f"committed RESOLUTION hash {hash_value} has no generated script"
        )


@pytest.mark.integration
def test_cleanup_phase_order_atomic(cleanup_env):
    """Three-way consistency after a refresh: RESOLUTION ⊆ scripts; non-orphaned ⊆ scripts.

    Every current RESOLUTION hash has a script, and every non-orphaned PROFILES
    row has a script — the generate→retire→reclaim ordering leaves no committed
    dangling reference in either direction.
    """
    conn = cleanup_env
    user = _current_user(conn)
    _insert_user_rewrite_rule(conn, user_name=user, function_name="tag_a", rule_id=1)
    _refresh(conn)

    for (hash_value,) in conn.execute(
        "SELECT DISTINCT profile_hash FROM PREPROC.RESOLUTION WHERE profile_hash IS NOT NULL"
    ).fetchall():
        assert _script_exists(conn, hash_value), f"RESOLUTION hash {hash_value} lacks a script"

    for (hash_value,) in conn.execute(
        "SELECT profile_hash FROM PREPROC.PROFILES WHERE orphaned_at IS NULL"
    ).fetchall():
        assert _script_exists(conn, hash_value), (
            f"non-orphaned PROFILES hash {hash_value} lacks a script"
        )
