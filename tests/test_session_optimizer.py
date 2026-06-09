"""Integration tests for the convenience-mode session self-optimization.

These tests verify the swap/self-disable behaviour of ``PREPROC_RT.MASTER`` — the
single canonical inline dispatcher. After dispatching the FIRST statement of a
session, a convenience-mode MASTER repoints the session preprocessor:

  * no-rules + self_disable_allowed → clears the session preprocessor (~0 ms/stmt)
  * has-rules → swaps to the generated ``PREPROC_RT.P_<hash>`` profile script

The swap is gated on ``mode == 'convenience'`` (the system posture; the other
recognised value is ``'strict'``, under which no swap is ever attempted) and is
wrapped in pcall so a failure leaves the session on its current script
(fail-open, ADR-004).

Behavioural verification (no SQL_PREPROCESSOR column)
-----------------------------------------------------
This Exasol version does NOT expose ``SQL_PREPROCESSOR`` on EXA_ALL_SESSIONS, so
the bound preprocessor cannot be read directly. The swap is instead observed by
its EFFECT. A profile script bakes the user's rule chain in STATICALLY; MASTER
re-reads MY_PIPELINE on every statement. So:

  * If a session is SWAPPED to ``P_<hash>``, changing the rules from a separate
    admin connection and refreshing does NOT change what the pinned session does
    on its next statement — it keeps applying the OLD baked rule.
  * If a session is still on MASTER (no swap / strict mode), its next statement
    does a fresh MY_PIPELINE lookup and picks up the NEW rule.

That divergence is the behavioural proof of the swap. The observable is a REWRITE
rule that appends a tag to a ``'SEED'`` literal: the scalar fetched back encodes
which rule ran. ``rw`` appends ``|R``.

Keeping the profile alive across the rule change (retire-immune)
----------------------------------------------------------------
A naive "change the pinned user's rule and refresh" does NOT work as a swap
probe: changing the user's chain orphans the old hash, and the refresh RETIREs
``P_<hash>`` by replacing its body with the live MASTER stub — which re-reads
MY_PIPELINE and applies the NEW rule, erasing the staleness. So a retired pinned
session would show the current rule whether or not a swap happened.

The fix keeps the OLD chain referenced so its profile is never retired: from the
admin connection, ADD an identical rule for a second (dummy) keeper user so the
``{rw}`` chain — and thus ``P_<hash>`` — stays current, then DELETE the pinned
user's own rule. After the refresh the pinned user resolves to ZERO rules, yet
``P_<hash>`` is still a live baked profile. Now the divergence is sharp:

  * a SWAPPED session keeps applying the baked ``rw`` (``SEED|R``) even though the
    pinned user now has no rules;
  * a still-on-MASTER session does a fresh MY_PIPELINE lookup, finds no rule, and
    returns the statement unchanged (``SEED``).

The session-under-test is the shared session-scoped ``installed`` connection. A
SEPARATE admin connection (``connect_from_env``) mutates CONFIG + refreshes mid
session. Because the session-under-test sat idle in one read transaction across
the admin refresh, it must ``commit()`` to release that MVCC snapshot before its
next statement sees the committed refresh (a real session gets a fresh snapshot
per autocommitted statement).

Isolation
    SETTINGS, RESOLUTION, CONFIG, and PROFILES are global tables on the shared
    ``installed`` connection. Every test activates MASTER with SESSION scope only
    — never ALTER SYSTEM — and the fixture unconditionally restores the
    preprocessor (cleared), the mode (convenience), the self_disable flag (FALSE,
    the install default asserted by test_schema_layout), the rule tables, and the
    test module in ``finally``, and refreshes to a clean state. The admin
    connection is closed in teardown.
"""

import pytest

from preproc.connection import connect_from_env

_ACTIVATE = "ALTER SESSION SET sql_preprocessor_script = PREPROC_RT.MASTER"
_DEACTIVATE = "ALTER SESSION SET sql_preprocessor_script = ''"

_SEED = "SEED"
_PROBE = f"SELECT '{_SEED}' AS PROBE"

_MODULE = "PREPROC_RT.TEST_OPTIMIZER_MOD"

_KEEPER_USER = "PREPROC_OPTIMIZER_KEEPER"


def _deploy_module(conn):
    """Deploy a Lua module whose REWRITE function tags a sentinel literal.

    ``rw`` appends ``|R`` just before the closing quote of the ``'SEED...'``
    literal, so the scalar fetched back from ``SELECT 'SEED' AS PROBE`` encodes
    whether the rule ran. The module persists across refreshes so a profile baked
    against ``rw`` can still import it after the pinned user's own rule is removed.
    """
    conn.execute(
        f"""CREATE OR REPLACE LUA SCRIPT {_MODULE}() AS
            local function append(sqltext, tag)
                local out = string.gsub(sqltext, "({_SEED}[^']*)'", "%1|" .. tag .. "'", 1)
                return out
            end
            function rw(sqltext)  return append(sqltext, "R") end
        """
    )


def _current_user(conn):
    return conn.execute("SELECT CURRENT_USER").fetchval()


def _set_mode(conn, mode):
    """Set PREPROC.SETTINGS.mode (with refresh) to 'convenience' or 'strict'."""
    conn.execute(f"EXECUTE SCRIPT PREPROC.SET_MODE('{mode}', FALSE)")


def _set_self_disable(conn, allowed):
    """Set PREPROC.SETTINGS.self_disable_allowed to a SQL boolean literal."""
    literal = "TRUE" if allowed else "FALSE"
    conn.execute(f"EXECUTE SCRIPT PREPROC.SET_SELF_DISABLE({literal})")


def _insert_user_rewrite_rule(conn, *, user_name, function_name, rule_id=1):
    """Insert one enabled USER-scoped REWRITE rule pointing at the test module.

    A USER-scoped rule resolves directly to its scope_value with no role grant,
    so the connecting session populates its own RESOLUTION and MY_PIPELINE rows.
    """
    conn.execute(
        "INSERT INTO PREPROC.CONFIG "
        "(rule_id, scope_type, scope_value, client_pattern, phase, "
        "script_name, function_name, enabled, version) "
        "VALUES ({rid}, 'USER', {sv}, NULL, 'REWRITE', {sn}, {fn}, TRUE, 1)",
        {"rid": rule_id, "sv": user_name, "sn": _MODULE, "fn": function_name},
    )


def _retire_proof_remove_rule(admin, *, user_name):
    """Remove the pinned user's rule while keeping its profile hash alive.

    Adds an identical ``rw`` rule for a dummy keeper user (so the ``{rw}`` chain —
    and thus ``P_<hash>`` — stays referenced and is NOT retired to the live MASTER
    stub), then deletes the pinned user's own rule and refreshes. After this the
    pinned user resolves to ZERO rules, but ``P_<hash>`` remains a live baked
    profile. A swapped session keeps applying the baked ``rw``; a still-on-MASTER
    session does a fresh lookup and finds no rule.
    """
    _insert_user_rewrite_rule(admin, user_name=_KEEPER_USER, function_name="rw", rule_id=2)
    admin.execute("DELETE FROM PREPROC.CONFIG WHERE rule_id = 1")
    _refresh(admin)


def _refresh(conn):
    conn.execute("EXECUTE SCRIPT PREPROC.REFRESH").fetchall()


def _hash_for(conn, user_name):
    """Return the (single) distinct non-NULL profile_hash for a user, or None."""
    rows = conn.execute(
        "SELECT DISTINCT profile_hash FROM PREPROC.RESOLUTION "
        "WHERE user_name = {u} AND profile_hash IS NOT NULL",
        {"u": user_name},
    ).fetchall()
    return rows[0][0] if rows else None


def _wipe(conn):
    for table in ("RESOLUTION", "CONFIG", "PROFILES"):
        conn.execute(f"DELETE FROM PREPROC.{table}")


@pytest.fixture
def optimizer_env(installed):
    """Provide a clean rule/profile state and an admin connection; restore after.

    Yields ``(session_conn, admin_conn)`` where ``session_conn`` is the shared
    ``installed`` connection (the session under test) and ``admin_conn`` is a
    separate connection used to mutate rules mid-session. Wipes
    RESOLUTION/CONFIG/PROFILES and deploys the test module before the test, and
    in teardown unconditionally clears the preprocessor on the session, restores
    convenience mode + self_disable_allowed=FALSE (the install default), wipes the
    rule tables, refreshes to a clean state (reclaiming any orphaned profiles),
    drops the test module, and closes the admin connection — so a failure cannot
    poison the shared session for the rest of the suite.
    """
    conn = installed
    _wipe(conn)
    _deploy_module(conn)
    admin = connect_from_env()
    try:
        yield conn, admin
    finally:
        try:
            conn.execute(_DEACTIVATE)
        finally:
            try:
                _wipe(conn)
                _set_mode(conn, "convenience")
                _set_self_disable(conn, False)
                _refresh(conn)
                conn.execute(f"DROP SCRIPT {_MODULE}")
            finally:
                admin.close()


@pytest.mark.integration
def test_no_rule_session_self_disables(optimizer_env):
    """Convenience + self_disable_allowed + no rules → session clears its preprocessor.

    Behavioural proof: after the first statement triggers the self-disable
    (``ALTER SESSION SET sql_preprocessor_script = ''``), a rule added from a
    separate admin connection and refreshed does NOT apply to a subsequent
    statement — because the session no longer has any preprocessor to do the
    fresh MY_PIPELINE lookup.
    """
    conn, admin = optimizer_env
    user = _current_user(conn)
    _set_mode(conn, "convenience")
    _set_self_disable(conn, True)

    conn.execute(_ACTIVATE)
    conn.execute("SELECT 1").fetchval()

    _insert_user_rewrite_rule(admin, user_name=user, function_name="rw")
    _refresh(admin)
    conn.commit()

    assert conn.execute(_PROBE).fetchval() == _SEED, (
        "a rule added after self-disable still applied — the session was not cleared"
    )


@pytest.mark.integration
def test_self_disable_not_attempted_when_disabled(optimizer_env):
    """Convenience + self_disable_allowed=FALSE + no rules → stays on MASTER.

    Behavioural contrast to self-disable: with the toggle off the session keeps
    MASTER, so a rule added from a separate admin connection and refreshed DOES
    apply to a subsequent statement (MASTER does a fresh MY_PIPELINE lookup).
    """
    conn, admin = optimizer_env
    user = _current_user(conn)
    _set_mode(conn, "convenience")
    _set_self_disable(conn, False)

    conn.execute(_ACTIVATE)
    conn.execute("SELECT 1").fetchval()

    _insert_user_rewrite_rule(admin, user_name=user, function_name="rw")
    _refresh(admin)
    conn.commit()

    assert conn.execute(_PROBE).fetchval() == f"{_SEED}|R", (
        "self-disable fired even though self_disable_allowed is FALSE — "
        "the later rule did not apply"
    )


@pytest.mark.integration
def test_rule_bearing_session_swaps_to_profile(optimizer_env):
    """Convenience + a resolved rule → session swaps to its baked profile.

    Behavioural proof: the first statement swaps the session to P_<hash> (baked
    with ``rw``). The pinned user's own rule is then removed from a separate admin
    connection (the hash kept alive by a keeper user, so the profile is NOT
    retired). The pinned session keeps applying the baked ``rw`` (``SEED|R``) even
    though the pinned user now has no rules — proving it is pinned to the profile
    rather than re-reading MY_PIPELINE (which would now yield ``SEED`` unchanged).
    """
    conn, admin = optimizer_env
    user = _current_user(conn)
    _set_mode(conn, "convenience")
    _insert_user_rewrite_rule(conn, user_name=user, function_name="rw")
    _refresh(conn)
    assert _hash_for(conn, user) is not None, "no profile_hash materialised for the rule"

    conn.execute(_ACTIVATE)
    assert conn.execute(_PROBE).fetchval() == f"{_SEED}|R", "first statement was not dispatched"
    conn.commit()

    _retire_proof_remove_rule(admin, user_name=user)
    conn.commit()

    assert conn.execute(_PROBE).fetchval() == f"{_SEED}|R", (
        "session stopped applying the baked rule — it was not pinned to the profile"
    )


@pytest.mark.integration
def test_master_reads_mode_and_hash_from_view(optimizer_env):
    """The swap proves MASTER read mode and profile_hash from the single view read.

    Both the gating mode and the swap target hash come from MASTER's single
    MY_PIPELINE read. In convenience mode a rule-bearing session swaps to
    P_<hash>: after the pinned user's rule is removed (hash kept alive by a keeper
    user) the session keeps applying the baked ``rw`` (``SEED|R``) instead of the
    fresh-lookup result (``SEED``). A successful swap to P_<hash> is only possible
    if MASTER surfaced both the mode and the profile_hash from that single view
    read, so this is the behavioural proof of the view-sourced swap.
    """
    conn, admin = optimizer_env
    user = _current_user(conn)
    _set_mode(conn, "convenience")
    _insert_user_rewrite_rule(conn, user_name=user, function_name="rw")
    _refresh(conn)
    assert _hash_for(conn, user) is not None

    conn.execute(_ACTIVATE)
    assert conn.execute(_PROBE).fetchval() == f"{_SEED}|R"
    conn.commit()

    _retire_proof_remove_rule(admin, user_name=user)
    conn.commit()

    assert conn.execute(_PROBE).fetchval() == f"{_SEED}|R", (
        "convenience session did not swap on the view's mode + profile_hash"
    )


@pytest.mark.integration
def test_strict_mode_no_swap(optimizer_env):
    """Strict mode → swap is never attempted (covers the plan's 'enforced mode' scenario).

    The plan's wording calls this 'enforced mode'; the code's recognised
    non-convenience posture is ``strict``. In strict mode no swap happens, so the
    session does a fresh MY_PIPELINE lookup on every statement: after the pinned
    user's rule is removed from a separate admin connection (the hash kept alive
    by a keeper user), the next statement does a fresh lookup, finds no rule, and
    returns ``SEED`` unchanged — proving no swap occurred (a swapped session would
    still show the baked ``SEED|R``).
    """
    conn, admin = optimizer_env
    user = _current_user(conn)
    _insert_user_rewrite_rule(conn, user_name=user, function_name="rw")
    _set_mode(conn, "strict")

    conn.execute(_ACTIVATE)
    assert conn.execute(_PROBE).fetchval() == f"{_SEED}|R", "first statement was not dispatched"
    conn.commit()

    _retire_proof_remove_rule(admin, user_name=user)
    conn.commit()

    assert conn.execute(_PROBE).fetchval() == _SEED, (
        "strict-mode session still applied the removed rule — a swap occurred"
    )


@pytest.mark.integration
def test_first_statement_dispatched_before_swap(optimizer_env):
    """The first statement is dispatched (REWRITE applied) before the swap takes effect.

    The triggering statement must reflect the pipeline transform (``SEED|R``),
    proving dispatch happened on statement 1. After it, the swap must be in
    effect: removing the pinned user's rule from a separate admin connection (the
    hash kept alive by a keeper user) does NOT reach the pinned session, which
    keeps applying the baked ``rw`` (``SEED|R``) — proving the swap took effect
    AFTER dispatch, not instead of it (else the fresh lookup would return ``SEED``).
    """
    conn, admin = optimizer_env
    user = _current_user(conn)
    _set_mode(conn, "convenience")
    _insert_user_rewrite_rule(conn, user_name=user, function_name="rw")
    _refresh(conn)

    conn.execute(_ACTIVATE)
    assert conn.execute(_PROBE).fetchval() == f"{_SEED}|R", (
        "the REWRITE-dispatched first statement was not transformed"
    )
    conn.commit()

    _retire_proof_remove_rule(admin, user_name=user)
    conn.commit()

    assert conn.execute(_PROBE).fetchval() == f"{_SEED}|R", (
        "swap did not take effect after the first statement was dispatched"
    )


@pytest.mark.integration
def test_swap_failure_fail_open(optimizer_env):
    """A swap to a missing profile script fails open: the statement still runs.

    The generated profile script is dropped before activation, so MASTER's swap
    ALTER SESSION targets a non-existent script and the pquery fails. MASTER wraps
    the swap in pcall, so the triggering statement still succeeds (it is even
    correctly transformed, since dispatch precedes the swap). The script is
    restored via refresh in teardown.
    """
    conn, _admin = optimizer_env
    user = _current_user(conn)
    _set_mode(conn, "convenience")
    _insert_user_rewrite_rule(conn, user_name=user, function_name="rw")
    _refresh(conn)
    hash_value = _hash_for(conn, user)
    assert hash_value is not None

    conn.execute(f"DROP SCRIPT PREPROC_RT.P_{hash_value}")
    conn.execute(_ACTIVATE)

    assert conn.execute(_PROBE).fetchval() == f"{_SEED}|R", (
        "swap-failure path did not fail open — the statement was lost or not dispatched"
    )


@pytest.mark.integration
def test_self_disable_failure_fail_open(optimizer_env):
    """The no-rules self-disable path is structurally fail-open: pass-through holds.

    The self-disable ``ALTER SESSION SET sql_preprocessor_script = ''`` cannot be
    forced to fail safely without poisoning the shared connection, so this asserts
    the guarantee that does NOT depend on the ALTER outcome: a no-rules
    convenience self_disable session passes the triggering statement through
    byte-for-byte and keeps running. The fail-open guarantee itself is structural
    — the ALTER is wrapped in pcall in MASTER, so any failure leaves the session
    on its current script without ever raising into the user's statement.
    """
    conn, _admin = optimizer_env
    _set_mode(conn, "convenience")
    _set_self_disable(conn, True)

    conn.execute(_ACTIVATE)

    assert conn.execute("SELECT 555 AS PROBE").fetchval() == 555, (
        "no-rules self-disable path did not pass the statement through"
    )
