"""Integration tests for the generated profile script dispatch (PREPROC_RT.P_<hash>).

A profile script is the zero-overhead session path: REFRESH_CORE bakes a user's
resolved chain into a static Lua rule table inside ``PREPROC_RT.P_<hash>`` and
that script dispatches it WITHOUT the per-statement MY_PIPELINE pquery MASTER
runs. These tests pin the dispatch SEMANTICS of the generated body — the same
safety contract MASTER carries (control-statement guard, per-rule pcall
fail-open, EXPAND first-match-terminal, literal client_pattern matching) — by
activating ``P_<hash>`` directly as the session preprocessor (never MASTER) and
observing the rewritten statement.

How dispatch is observed without a result-returning preprocessor
----------------------------------------------------------------
Identical technique to ``test_master_dispatch.py``: a test module function tags a
sentinel string literal inside the submitted statement, so the scalar the
connection fetches back encodes WHICH rules ran and in WHAT ORDER. The profile
path differs only in HOW the rules get there — they are baked into P_<hash> by a
real PREPROC.REFRESH over PREPROC.CONFIG, not seeded straight into RESOLUTION —
so these tests drive the genuine codegen, not a hand-rolled body.

Shared-state discipline (the ``installed`` connection is session-scoped)
------------------------------------------------------------------------
CONFIG, RESOLUTION, and PROFILES are global tables on the shared session-scoped
connection. The ``profile_env`` fixture wipes all three before and after every
test and deploys/drops the test module, and every probe deactivates the session
preprocessor in ``finally`` so a mid-test failure cannot leave P_<hash> bound to
the shared connection (which would corrupt every later test).
"""

import pytest

_DEACTIVATE = "ALTER SESSION SET sql_preprocessor_script = ''"

_SEED = "SEED"
_PROBE = f"SELECT '{_SEED}' AS PROBE"

_MODULE = "PREPROC_RT.TEST_PROFILE_MOD"


def _deploy_module(connection):
    """Deploy a Lua module whose functions tag a sentinel literal.

    Mirrors the test_master_dispatch module: each function appends its marker
    just before the closing quote of the ``'SEED...'`` literal so chaining
    records order. ``boom`` raises to exercise per-rule fail-open; the
    ``expand_match_*`` functions return a full replacement statement to exercise
    EXPAND first-match-terminal semantics.
    """
    connection.execute(
        f"""CREATE OR REPLACE LUA SCRIPT {_MODULE}() AS
            local function append(sqltext, tag)
                local out = string.gsub(sqltext, "({_SEED}[^']*)'", "%1|" .. tag .. "'", 1)
                return out
            end
            function tag_a(sqltext)  return append(sqltext, "A") end
            function tag_b(sqltext)  return append(sqltext, "B") end
            function boom(sqltext)   error("boom: deliberate module failure") end
            function expand_match_a(sqltext) return "SELECT 'SEED|XA' AS PROBE" end
            function expand_match_b(sqltext) return "SELECT 'SEED|XB' AS PROBE" end
        """
    )


def _insert_user_rule(
    connection,
    *,
    rule_id,
    user_name,
    phase,
    function_name,
    script_name=_MODULE,
    client_pattern=None,
):
    """Insert one USER-scoped PREPROC.CONFIG rule so refresh bakes it into P_<hash>."""
    connection.execute(
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


def _refresh(connection):
    connection.execute("EXECUTE SCRIPT PREPROC.REFRESH")


def _profile_hash(connection):
    """Return the single distinct profile_hash baked for CURRENT_USER after refresh."""
    row = connection.execute(
        "SELECT DISTINCT profile_hash FROM PREPROC.RESOLUTION "
        "WHERE user_name = CURRENT_USER AND profile_hash IS NOT NULL"
    ).fetchone()
    assert row is not None, "no profile_hash baked for CURRENT_USER after refresh"
    return row[0]


@pytest.fixture
def profile_env(installed):
    """Clean CONFIG/RESOLUTION/PROFILES around the test and deploy the test module.

    Wipes before (guarding a crashed predecessor) and after (keeping the shared
    session clean), and drops the module on teardown.
    """
    for table in ("RESOLUTION", "CONFIG", "PROFILES"):
        installed.execute(f"DELETE FROM PREPROC.{table}")
    _deploy_module(installed)
    try:
        yield installed
    finally:
        try:
            for table in ("RESOLUTION", "CONFIG", "PROFILES"):
                installed.execute(f"DELETE FROM PREPROC.{table}")
        finally:
            installed.execute(f"DROP SCRIPT {_MODULE}")


def _probe_with_profile(connection, profile_hash, statement=_PROBE):
    """Activate P_<hash>, run a statement, return its scalar; always deactivate."""
    connection.execute(f"ALTER SESSION SET sql_preprocessor_script = PREPROC_RT.P_{profile_hash}")
    try:
        return connection.execute(statement).fetchval()
    finally:
        connection.execute(_DEACTIVATE)


@pytest.mark.integration
def test_profile_full_dispatch_semantics(profile_env):
    """A baked REWRITE rule rewrites the statement via the static rule table.

    The rule is installed in CONFIG and a real refresh bakes it into P_<hash>;
    activating P_<hash> and submitting the sentinel SELECT must yield the tagged
    literal ``SEED|A``. The transform proves the baked static rules dispatch
    correctly with NO MY_PIPELINE lookup: the profile script never queries
    PREPROC_RT.MY_PIPELINE (its body has no such pquery), so a correct rewrite is
    the externally observable proof the zero-overhead path works end-to-end.
    """
    conn = profile_env
    user = conn.execute("SELECT CURRENT_USER").fetchval()
    _insert_user_rule(conn, rule_id=1, user_name=user, phase="REWRITE", function_name="tag_a")
    _refresh(conn)

    assert _probe_with_profile(conn, _profile_hash(conn)) == f"{_SEED}|A"


@pytest.mark.integration
def test_profile_client_pattern_literal_match(profile_env):
    """A NULL-pattern rule always fires; a non-matching client_pattern rule never does.

    Two REWRITE rules are baked: ordinal 1 has client_pattern NULL (apply-always)
    and tags ``A``; ordinal 2 carries a client_pattern that cannot occur in any
    real client identifier and tags ``B``. The result ``SEED|A`` (A present, B
    absent) proves the baked ``client_matches`` treats NULL as apply-always and
    gates a non-NULL pattern by literal substring — exactly MASTER's contract,
    reproduced from the static table rather than a live view row.
    """
    conn = profile_env
    user = conn.execute("SELECT CURRENT_USER").fetchval()
    _insert_user_rule(conn, rule_id=1, user_name=user, phase="REWRITE", function_name="tag_a")
    _insert_user_rule(
        conn,
        rule_id=2,
        user_name=user,
        phase="REWRITE",
        function_name="tag_b",
        client_pattern="ZZZ_NO_SUCH_CLIENT_ZZZ",
    )
    _refresh(conn)

    assert _probe_with_profile(conn, _profile_hash(conn)) == f"{_SEED}|A"


@pytest.mark.integration
def test_profile_control_guard(profile_env):
    """The baked guard returns early on ALTER SESSION; the session stays usable.

    With P_<hash> active, an ``ALTER SESSION`` must pass through untouched (the
    guard returns before any dispatch, so the control statement is not
    intercepted) and a following ordinary SELECT must still execute. Asserting
    both succeed pins the guard reproduced inside the generated body.
    """
    conn = profile_env
    user = conn.execute("SELECT CURRENT_USER").fetchval()
    _insert_user_rule(conn, rule_id=1, user_name=user, phase="REWRITE", function_name="tag_a")
    _refresh(conn)
    profile_hash = _profile_hash(conn)

    conn.execute(f"ALTER SESSION SET sql_preprocessor_script = PREPROC_RT.P_{profile_hash}")
    try:
        conn.execute("ALTER SESSION SET QUERY_TIMEOUT = 600")
        assert conn.execute("SELECT 424242 AS PROBE").fetchval() == 424242
    finally:
        try:
            conn.execute("ALTER SESSION SET QUERY_TIMEOUT = 0")
        finally:
            conn.execute(_DEACTIVATE)


@pytest.mark.integration
def test_profile_missing_script_passes_through(profile_env):
    """A rule pointing at a non-existent script fails open: the statement is unchanged.

    Exasol resolves ``import`` of an absent script as a fatal load error that
    aborts the whole preprocessor invocation (not a Lua throw the per-rule pcall
    can catch), so the engine receives the statement unchanged — the sentinel
    arrives as ``SEED`` with no tag. This is exactly MASTER's missing-module
    behaviour (``test_master_dispatch.test_dispatch_missing_module_skipped``): a
    dangling RESOLUTION/baked rule never blocks SQL. Per-rule isolation of a
    *throwing function* (a deployed script whose function raises) is covered
    separately by ``test_profile_failing_function_skipped``; a missing SCRIPT is
    a coarser, whole-invocation fail-open.
    """
    conn = profile_env
    user = conn.execute("SELECT CURRENT_USER").fetchval()
    _insert_user_rule(
        conn,
        rule_id=1,
        user_name=user,
        phase="REWRITE",
        function_name="absent",
        script_name="PREPROC_RT.NONEXISTENT_SCRIPT_XYZ",
    )
    _refresh(conn)

    assert _probe_with_profile(conn, _profile_hash(conn)) == _SEED


@pytest.mark.integration
def test_profile_failing_function_skipped(profile_env):
    """A throwing module function is skipped fail-open; surrounding rules still run.

    The middle rule (``boom``) raises inside its dispatch. The per-rule pcall in
    the baked body must swallow that and continue with the text unchanged, so the
    two healthy REWRITE rules on either side still tag the literal: the result is
    ``SEED|A|B`` exactly as if ``boom`` were absent. Placing the failure BETWEEN
    two good rules proves the skip is isolated to the failing rule — the genuine
    per-rule fail-open guarantee (a deployed script, function raises), distinct
    from the whole-invocation abort of a *missing* script.
    """
    conn = profile_env
    user = conn.execute("SELECT CURRENT_USER").fetchval()
    _insert_user_rule(conn, rule_id=1, user_name=user, phase="REWRITE", function_name="tag_a")
    _insert_user_rule(conn, rule_id=2, user_name=user, phase="REWRITE", function_name="boom")
    _insert_user_rule(conn, rule_id=3, user_name=user, phase="REWRITE", function_name="tag_b")
    _refresh(conn)

    assert _probe_with_profile(conn, _profile_hash(conn)) == f"{_SEED}|A|B"


@pytest.mark.integration
def test_profile_expand_first_match_terminal(profile_env):
    """The first matching EXPAND rule owns the expansion; later EXPAND rules never run.

    Two baked EXPAND rules each return a distinct full replacement. First-match-
    terminal means only ordinal 1 (``expand_match_a`` → XA) runs and ordinal 2
    (``expand_match_b`` → XB) is skipped by the ``break``. The result carrying XA
    and NOT XB distinguishes terminal semantics from a run-all loop (which would
    surface the last writer's XB) inside the generated profile body.
    """
    conn = profile_env
    user = conn.execute("SELECT CURRENT_USER").fetchval()
    _insert_user_rule(
        conn, rule_id=1, user_name=user, phase="EXPAND", function_name="expand_match_a"
    )
    _insert_user_rule(
        conn, rule_id=2, user_name=user, phase="EXPAND", function_name="expand_match_b"
    )
    _refresh(conn)

    assert _probe_with_profile(conn, _profile_hash(conn)) == f"{_SEED}|XA"
