"""Integration tests for the MASTER dispatch loop (PREPROC_RT.MASTER).

These layer ON TOP of ``test_master_skeleton.py``, which already pins the safety
base: installation, byte-for-byte pass-through, the ALTER guard, and fail-open on
a broken pipeline lookup. This file does NOT re-test those; it covers the NEW
dispatch behaviour added in the refresh slice — phase ordering, within-phase
ordinal ordering, client_pattern matching, and per-rule fail-open.

How dispatch is observed without a result-returning preprocessor
----------------------------------------------------------------
MASTER is a SQL preprocessor: it rewrites a statement's text and has no return
value of its own. So the connection cannot read "what MASTER produced" directly.
The technique here is to make each test module function rewrite a sentinel string
literal inside the submitted statement, appending a per-rule tag. The statement

    SELECT 'SEED' AS PROBE

becomes e.g. ``SELECT 'SEED|T|E|R' AS PROBE`` after dispatch, and the scalar the
connection fetches back is exactly that literal. The fetched string therefore
encodes both WHICH functions ran and in WHAT ORDER — which is precisely what the
phase-order and ordinal-order assertions need. A function that does not run, or a
rule that does not match, simply leaves no tag.

The functions edit the text via ``string.gsub`` on the closing ``'`` of the
sentinel, so chaining is associative: each appends its tag immediately before the
quote, preserving submission order through the pipeline.

Why these tests can run here
----------------------------
``test_master_skeleton.py`` proves the test environment activates the
preprocessor with SESSION scope (``ALTER SESSION SET sql_preprocessor_script =
PREPROC_RT.MASTER``) — never ALTER SYSTEM. We mirror that exact activate/
deactivate pattern, so dispatch is testable in-session; nothing here needs to be
skipped for "requires active preprocessor".

Shared-state discipline (the ``installed`` connection is session-scoped)
------------------------------------------------------------------------
Two pieces of global state are mutated and MUST be restored unconditionally or
the rest of the suite breaks:

* PREPROC.RESOLUTION rows for CURRENT_USER — ``test_master_skeleton`` asserts the
  current user has ZERO rules, so every seeded rule is deleted in ``finally``.
* The test module scripts deployed in PREPROC_RT — dropped in ``finally``.
* The session preprocessor — deactivated in ``finally`` even if a restore raises.

All seeding/teardown is funnelled through the ``dispatch_env`` fixture so no test
body can leak a rule or leave the preprocessor active.
"""

import os
import ssl

import pyexasol
import pytest

_ACTIVATE = "ALTER SESSION SET sql_preprocessor_script = PREPROC_RT.MASTER"
_DEACTIVATE = "ALTER SESSION SET sql_preprocessor_script = ''"

_SEED = "SEED"
_PROBE = f"SELECT '{_SEED}' AS PROBE"

_MODULE = "PREPROC_RT.TEST_DISPATCH_MOD"


def _deploy_module(connection):
    """Deploy a Lua module script whose functions tag a sentinel literal.

    Each ``tag_*`` function appends its marker immediately before the closing
    quote of the ``'SEED...'`` literal, so chaining them through the pipeline
    records call order inside the literal the connection fetches back. ``boom``
    raises so the per-rule fail-open path can be exercised.
    """
    # pyexasol sends the bare CREATE statement WITHOUT a trailing '/' delimiter
    # (mirrors src/preproc/install.py), so none is appended here.
    connection.execute(
        f"""CREATE OR REPLACE LUA SCRIPT {_MODULE}() AS
            local function append(sqltext, tag)
                -- Insert tag just before the closing quote of the sentinel literal.
                local out = string.gsub(sqltext, "({_SEED}[^']*)'", "%1|" .. tag .. "'", 1)
                return out
            end
            function translate(sqltext) return append(sqltext, "T") end
            function expand(sqltext)    return append(sqltext, "E") end
            function rewrite(sqltext)   return append(sqltext, "R") end
            function expand1(sqltext)   return append(sqltext, "E1") end
            function expand2(sqltext)   return append(sqltext, "E2") end
            function tag_match(sqltext) return append(sqltext, "M") end
            function boom(sqltext)      error("boom: deliberate module failure") end
            -- EXPAND-phase functions exercising the nil/string return contract.
            -- A match function returns a FULL replacement statement that still
            -- carries a 'SEED...' literal, so a later REWRITE rule can tag it and
            -- the connection fetches an order-encoding scalar. expand_match_a and
            -- expand_match_b carry distinct markers (XA / XB) so a first-match
            -- test can tell WHICH expand rule produced the surviving expansion.
            function rewrite1(sqltext)   return append(sqltext, "R1") end
            function rewrite2(sqltext)   return append(sqltext, "R2") end
            -- expand_echo proves the EXPAND function receives the text as it arrived
            -- (post-TRANSLATE), not the original: it appends its own ECHO tag to
            -- whatever is already in the sentinel literal and returns the result as
            -- the expansion (terminal, first-match EXPAND semantics).
            function expand_echo(sqltext) return append(sqltext, "ECHO") end
            -- expand_returns_number exercises the non-string coercion path: returning
            -- a number is not a valid expansion; dispatch_expand_rule must treat it as
            -- nil (no-match) and continue scanning rather than threading the number
            -- into setsqltext.
            function expand_returns_number(sqltext) return 42 end
            function expand_match_a(sqltext) return "SELECT 'SEED|XA' AS PROBE" end
            function expand_match_b(sqltext) return "SELECT 'SEED|XB' AS PROBE" end
            -- decline returns nil for every statement: "not my command, keep
            -- scanning" — the non-terminal EXPAND path.
            function expand_decline(sqltext) return nil end
        """
    )


def _seed_rule(connection, *, phase, function_name, ordinal, client_pattern=None):
    """Insert one RESOLUTION rule for CURRENT_USER pointing at the test module."""
    connection.execute(
        "INSERT INTO PREPROC.RESOLUTION "
        "(user_name, client_pattern, phase, script_name, function_name, ordinal) "
        "VALUES (CURRENT_USER, {cp}, {ph}, {sn}, {fn}, {ord})",
        {
            "cp": client_pattern,
            "ph": phase,
            "sn": _MODULE,
            "fn": function_name,
            "ord": ordinal,
        },
    )


def _delete_rules(connection):
    """Remove every RESOLUTION rule for CURRENT_USER (restore no-rules invariant)."""
    connection.execute("DELETE FROM PREPROC.RESOLUTION WHERE user_name = CURRENT_USER")


@pytest.fixture
def dispatch_env(installed):
    """Provide a clean dispatch sandbox and guarantee teardown.

    Asserts the no-rules precondition up front (a leftover rule from another test
    would silently corrupt every assertion here), deploys the test module, and on
    teardown unconditionally deactivates the preprocessor, deletes seeded rules,
    and drops the module — in that order, each guarded — so a failure mid-test
    cannot poison the session-scoped connection.
    """
    # Clear CURRENT_USER's resolution first: install now seeds a DBA-scoped
    # control-sugar bootstrap rule, and this connection (SYS) holds DBA, so it
    # resolves that rule. These tests drive RESOLUTION directly, so start from a
    # truly empty sandbox (this also clears any leftover from another test).
    _delete_rules(installed)
    rule_count = installed.execute(
        "SELECT COUNT(*) FROM PREPROC.RESOLUTION WHERE user_name = CURRENT_USER"
    ).fetchval()
    assert rule_count == 0, (
        f"Expected 0 rules for CURRENT_USER before a dispatch test, got {rule_count} "
        f"— RESOLUTION could not be cleared"
    )

    _deploy_module(installed)
    try:
        yield installed
    finally:
        try:
            installed.execute(_DEACTIVATE)
        finally:
            try:
                _delete_rules(installed)
            finally:
                installed.execute(f"DROP SCRIPT {_MODULE}")


def _probe(connection):
    """Activate MASTER, run the sentinel SELECT, return the (rewritten) literal."""
    return _probe_statement(connection, _PROBE)


def _probe_statement(connection, statement):
    """Activate MASTER, run an arbitrary statement, return its fetched scalar.

    Used by EXPAND command tests that submit a non-sentinel statement (e.g.
    ``PING``) and need to observe the scalar the rewritten statement returns —
    the only externally visible evidence of what dispatch produced.
    """
    connection.execute(_ACTIVATE)
    try:
        return connection.execute(statement).fetchval()
    finally:
        connection.execute(_DEACTIVATE)


@pytest.mark.integration
def test_dispatch_phase_order(dispatch_env):
    """Phases apply TRANSLATE -> EXPAND -> REWRITE regardless of view (lexical) order.

    The seeded rows are TRANSLATE/EXPAND/REWRITE. MY_PIPELINE orders by phase
    *lexically* (EXPAND < REWRITE < TRANSLATE), so a dispatcher that trusted the
    view's row order would produce ``SEED|E|R|T``. MASTER instead walks the
    semantic phase list, so the literal must read ``SEED|T|E|R``. Asserting the
    exact semantic sequence — not just set membership — is what distinguishes the
    correct explicit ordering from naive view-order trust.
    """
    _seed_rule(dispatch_env, phase="REWRITE", function_name="rewrite", ordinal=1)
    _seed_rule(dispatch_env, phase="EXPAND", function_name="expand", ordinal=1)
    _seed_rule(dispatch_env, phase="TRANSLATE", function_name="translate", ordinal=1)

    assert _probe(dispatch_env) == f"{_SEED}|T|E|R"


@pytest.mark.integration
def test_dispatch_within_phase_ordinal_order(dispatch_env):
    """Within one phase, rules run in ascending ``ordinal`` order, threading text.

    Two REWRITE rules with ordinals 1 and 2 must produce ``SEED|R1|R2``. Seeding
    them in reverse ordinal order (the ordinal=2 row inserted first) guards
    against an implementation that happened to rely on insertion/row order rather
    than the ordinal the view sorts on. REWRITE has run-all threading semantics,
    so both rules always run regardless of what each returns.
    """
    _seed_rule(dispatch_env, phase="REWRITE", function_name="rewrite2", ordinal=2)
    _seed_rule(dispatch_env, phase="REWRITE", function_name="rewrite1", ordinal=1)

    assert _probe(dispatch_env) == f"{_SEED}|R1|R2"


@pytest.mark.integration
def test_dispatch_client_pattern_match(dispatch_env):
    """A rule whose client_pattern is a literal substring of the client string is dispatched.

    The matching pattern is derived from the live session's CLIENT (Exasol has
    no CURRENT_CLIENT function; EXA_ALL_SESSIONS is the source) so the test is
    independent of the exact client string pyexasol reports: any literal substring of
    it is a valid pattern that ``string.find(..., 1, true)`` will match. The rule then
    runs and tags the literal.
    """
    current_client = dispatch_env.execute(
        "SELECT CLIENT FROM EXA_ALL_SESSIONS WHERE SESSION_ID = CURRENT_SESSION"
    ).fetchval()
    assert current_client, "session CLIENT returned empty; cannot derive a match pattern"
    match_pattern = current_client[:3]

    _seed_rule(
        dispatch_env,
        phase="EXPAND",
        function_name="tag_match",
        ordinal=1,
        client_pattern=match_pattern,
    )

    assert _probe(dispatch_env) == f"{_SEED}|M"


@pytest.mark.integration
def test_dispatch_client_pattern_no_match(dispatch_env):
    """A rule whose client_pattern is not a literal substring of the client is skipped (no tag).

    A literal string that cannot occur in any real client identifier means the rule is
    not dispatched, and the statement passes through byte-for-byte — the literal
    stays ``SEED`` with no marker appended. This is the negative half of the
    client_pattern contract and confirms non-null patterns actually gate dispatch.
    """
    _seed_rule(
        dispatch_env,
        phase="EXPAND",
        function_name="tag_match",
        ordinal=1,
        client_pattern="ZZZ_NO_SUCH_CLIENT_ZZZ",
    )

    assert _probe(dispatch_env) == _SEED


@pytest.mark.integration
def test_dispatch_failing_function_skipped(dispatch_env):
    """A throwing module function is skipped fail-open; surrounding rules still run.

    The middle rule (``boom``) raises inside dispatch. Per-rule pcall must swallow
    that and continue with the text unchanged, so the two healthy REWRITE rules on
    either side still tag the literal: the result is ``SEED|R1|R2`` exactly as if
    ``boom`` were absent. Placing the failure BETWEEN two good rules proves the
    skip is isolated to the failing rule and does not abort the remaining pipeline
    or corrupt the threaded text. REWRITE has run-all threading semantics, so all
    three rules are visited and the two healthy ones always execute.
    """
    _seed_rule(dispatch_env, phase="REWRITE", function_name="rewrite1", ordinal=1)
    _seed_rule(dispatch_env, phase="REWRITE", function_name="boom", ordinal=2)
    _seed_rule(dispatch_env, phase="REWRITE", function_name="rewrite2", ordinal=3)

    assert _probe(dispatch_env) == f"{_SEED}|R1|R2"


@pytest.mark.integration
def test_dispatch_missing_module_skipped(dispatch_env):
    """A rule pointing at a non-existent module is skipped fail-open.

    ``require`` on an undeployed script throws; the per-rule pcall must treat that
    identically to a throwing function and pass the statement through unchanged.
    No module ships in this slice, so a dangling RESOLUTION row pointing at a
    not-yet-deployed module is a realistic state that must never block SQL.
    """
    dispatch_env.execute(
        "INSERT INTO PREPROC.RESOLUTION "
        "(user_name, client_pattern, phase, script_name, function_name, ordinal) "
        "VALUES (CURRENT_USER, NULL, 'EXPAND', "
        "'PREPROC_RT.NO_SUCH_MODULE_XYZ', 'expand', 1)"
    )

    assert _probe(dispatch_env) == _SEED


# --- EXPAND first-match-terminal dispatch -----------------------------------


@pytest.mark.integration
def test_expand_terminal_first_match(dispatch_env):
    """The first matching EXPAND module owns the expansion; later EXPAND rules never run.

    Both seeded EXPAND rules return a (distinct) expansion string, so BOTH would
    match. First-match-terminal means only ordinal 1 (``expand_match_a`` → marker
    XA) may run; ordinal 2 (``expand_match_b`` → marker XB) must be skipped by the
    ``break``. Asserting the result carries XA and NOT XB is what distinguishes
    terminal semantics from a run-all-matching loop, which would have produced the
    last writer's XB (or threaded both).
    """
    _seed_rule(dispatch_env, phase="EXPAND", function_name="expand_match_a", ordinal=1)
    _seed_rule(dispatch_env, phase="EXPAND", function_name="expand_match_b", ordinal=2)

    assert _probe(dispatch_env) == f"{_SEED}|XA"


@pytest.mark.integration
def test_expand_nil_continues_to_next(dispatch_env):
    """A nil EXPAND return is non-terminal: dispatch keeps scanning in ordinal order.

    Ordinal 1 (``expand_decline``) returns nil — "not my command" — which must NOT
    terminate the EXPAND scan. Ordinal 2 (``expand_match_b`` → marker XB) then
    matches and supplies the expansion. The result carrying XB proves nil was
    treated as continue, not as a (terminal) empty expansion.
    """
    _seed_rule(dispatch_env, phase="EXPAND", function_name="expand_decline", ordinal=1)
    _seed_rule(dispatch_env, phase="EXPAND", function_name="expand_match_b", ordinal=2)

    assert _probe(dispatch_env) == f"{_SEED}|XB"


@pytest.mark.integration
def test_expand_failing_module_skipped_continues(dispatch_env):
    """A throwing EXPAND module is skipped fail-open and the scan continues.

    Ordinal 1 (``boom``) raises inside dispatch. ``dispatch_expand_rule`` must map
    that pcall failure to nil — NOT to the original text — so the EXPAND scan does
    not terminate on a broken module (returning the original text would be
    indistinguishable from a real expansion and would wrongly stop the scan).
    Ordinal 2 (``expand_match_b`` → XB) then supplies the expansion. The XB result
    proves the throw was swallowed as "keep looking", not as a terminal match.
    """
    _seed_rule(dispatch_env, phase="EXPAND", function_name="boom", ordinal=1)
    _seed_rule(dispatch_env, phase="EXPAND", function_name="expand_match_b", ordinal=2)

    assert _probe(dispatch_env) == f"{_SEED}|XB"


@pytest.mark.integration
def test_expand_no_match_passes_through(dispatch_env):
    """When every EXPAND rule declines (nil), the statement passes through unchanged.

    Two ``expand_decline`` rules both return nil, so the EXPAND phase produces no
    expansion and never breaks early. With no other phase active, the sentinel
    must arrive byte-for-byte — the literal stays ``SEED`` with no marker. This is
    the all-decline floor of the EXPAND contract: declining is the normal,
    non-blocking path.
    """
    _seed_rule(dispatch_env, phase="EXPAND", function_name="expand_decline", ordinal=1)
    _seed_rule(dispatch_env, phase="EXPAND", function_name="expand_decline", ordinal=2)

    assert _probe(dispatch_env) == _SEED


@pytest.mark.integration
def test_expand_non_string_return_treated_as_no_match(dispatch_env):
    """A non-string EXPAND return is coerced to nil (no-match); scanning continues.

    Ordinal 1 (``expand_returns_number``) returns the integer ``42``. The
    ``dispatch_expand_rule`` helper must check ``type(result) == "string"`` and
    treat any other type as nil — "not my command" — so the EXPAND scan is NOT
    terminated. Ordinal 2 (``expand_match_b`` → marker XB) then matches and
    supplies the expansion. The XB result proves the number was never threaded into
    setsqltext and did not terminate the scan: a naive implementation that omitted
    the type guard would have treated 42 as a truthy (non-nil) expansion and
    broken early, leaving no XB.
    """
    _seed_rule(dispatch_env, phase="EXPAND", function_name="expand_returns_number", ordinal=1)
    _seed_rule(dispatch_env, phase="EXPAND", function_name="expand_match_b", ordinal=2)

    assert _probe(dispatch_env) == f"{_SEED}|XB"


@pytest.mark.integration
def test_expand_rewrite_still_runs_after_match(dispatch_env):
    """REWRITE runs on the expanded text after EXPAND terminates on a match.

    The EXPAND rule (``expand_match_a``) replaces the statement with
    ``SELECT 'SEED|XA' AS PROBE`` and terminates the EXPAND phase. The terminal
    ``break`` exits only the inner row loop, so the outer phase loop proceeds to
    REWRITE, whose ``rewrite`` rule tags the EXPANDED literal — yielding
    ``SEED|XA|R``. The ordered ``XA`` then ``R`` proves both phases ran AND that
    EXPAND's terminal semantics do not short-circuit REWRITE.
    """
    _seed_rule(dispatch_env, phase="EXPAND", function_name="expand_match_a", ordinal=1)
    _seed_rule(dispatch_env, phase="REWRITE", function_name="rewrite", ordinal=1)

    assert _probe(dispatch_env) == f"{_SEED}|XA|R"


@pytest.mark.integration
def test_translate_output_reaches_expand_module(dispatch_env):
    """EXPAND receives the post-TRANSLATE text, not the original statement.

    A TRANSLATE rule tags the sentinel literal first (appending ``|T``), then an
    EXPAND rule (``expand_echo``) receives the already-translated text and appends
    ``|ECHO`` to whatever is in the literal at that point. The ordered result
    ``SEED|T|ECHO`` is only reachable if ``expand_echo`` received the
    post-TRANSLATE text: had it received the original text the result would be
    ``SEED|ECHO`` (no ``T``), and had TRANSLATE run after EXPAND the order would
    be ``SEED|ECHO|T``. The exact sequence proves the TRANSLATE→EXPAND handoff
    threads text correctly through the phase boundary.
    """
    _seed_rule(dispatch_env, phase="TRANSLATE", function_name="translate", ordinal=1)
    _seed_rule(dispatch_env, phase="EXPAND", function_name="expand_echo", ordinal=1)

    assert _probe(dispatch_env) == f"{_SEED}|T|ECHO"


# --- DEMO_PING_V1 end-to-end ------------------------------------------------


@pytest.mark.integration
def test_demo_ping_expands(dispatch_env):
    """DEMO_PING_V1 expands a ``PING`` statement to ``SELECT 'PONG'`` end-to-end.

    The ``installed`` fixture (via ``run_install``) has already deployed
    PREPROC_RT.DEMO_PING_V1, so this test only seeds an EXPAND rule pointing at its
    ``ping`` function and submits the bare command ``PING`` — which is not valid
    SQL on its own. The mechanism proof is end-to-end: ``ping`` recognises the
    statement, returns ``SELECT 'PONG'``, MASTER rewrites the text, and the engine
    executes the REWRITTEN statement, so the scalar the connection fetches is
    exactly ``PONG``. A fetched ``PONG`` is only reachable if the real expansion
    fired (a bare ``PING`` would otherwise error), making this the strongest
    single proof of the EXPAND command mechanism.
    """
    dispatch_env.execute(
        "INSERT INTO PREPROC.RESOLUTION "
        "(user_name, client_pattern, phase, script_name, function_name, ordinal) "
        "VALUES (CURRENT_USER, NULL, 'EXPAND', "
        "'PREPROC_RT.DEMO_PING_V1', 'ping', 1)"
    )

    assert _probe_statement(dispatch_env, "PING") == "PONG"


@pytest.mark.integration
def test_demo_ping_nil_for_non_ping(dispatch_env):
    """DEMO_PING_V1 returns nil for a non-PING statement, which passes through unchanged.

    With the same DEMO_PING_V1 EXPAND rule, the submitted statement is the ordinary
    sentinel SELECT (not ``PING``). ``ping`` must return nil — "not my command" —
    so the EXPAND phase produces no expansion and the SELECT reaches the engine
    unchanged, fetching the original ``SEED`` literal. This is the decline half of
    the demo contract: a real command module must not hijack unrelated SQL.
    """
    dispatch_env.execute(
        "INSERT INTO PREPROC.RESOLUTION "
        "(user_name, client_pattern, phase, script_name, function_name, ordinal) "
        "VALUES (CURRENT_USER, NULL, 'EXPAND', "
        "'PREPROC_RT.DEMO_PING_V1', 'ping', 1)"
    )

    assert _probe(dispatch_env) == _SEED


# --- client_pattern literal-substring semantics -----------------------------


def _connect_with_client_name(client_name):
    """Open a pyexasol connection whose reported CLIENT is exactly ``client_name``.

    pyexasol forwards ``client_name`` to the server's ``clientName`` login field,
    which is what EXA_ALL_SESSIONS.CLIENT exposes — so this is the only handle for
    making MASTER's ``current_client`` lookup return a controlled string. Reuses
    the same env-configured DSN/credentials as the suite (hence the same
    CURRENT_USER, so RESOLUTION rules seeded under that user apply here too).
    """
    kwargs = {
        "dsn": os.environ["EXASOL_DSN"],
        "user": os.environ["EXASOL_USER"],
        "password": os.environ["EXASOL_PASSWORD"],
        "client_name": client_name,
    }
    tls_verify = os.environ.get("EXASOL_TLS_VERIFY", "").lower()
    if tls_verify in ("0", "false"):
        kwargs["websocket_sslopt"] = {"cert_reqs": ssl.CERT_NONE}
    return pyexasol.connect(**kwargs)


@pytest.mark.integration
def test_client_pattern_literal_substring(dispatch_env):
    """client_pattern matches as a literal substring, never as a Lua pattern.

    The pattern ``EXA.Client`` contains ``.`` — a Lua "any character" metacharacter.
    Under the old (buggy) ``string.find`` pattern mode, a client string
    ``EXAXClient`` would MATCH (``.`` consuming the ``X``); under the corrected
    plain-find (``1, true``) it must NOT, because ``EXA.Client`` does not occur
    literally. A client string ``v8 EXA.Client 1.0`` contains the literal substring
    and MUST match. Two fresh sessions with controlled CLIENT values isolate the
    two halves; the rule is seeded once under CURRENT_USER (shared across both
    sessions) and torn down by ``dispatch_env``.

    This is the regression guard for the latent metacharacter bug: it fails RED
    against pattern-mode find and passes only with literal-substring matching.
    """
    _seed_rule(
        dispatch_env,
        phase="EXPAND",
        function_name="tag_match",
        ordinal=1,
        client_pattern="EXA.Client",
    )

    # Negative: the dot is literal, so a client that only matches the Lua pattern
    # must be rejected and the sentinel must pass through untagged.
    non_match_conn = _connect_with_client_name("EXAXClient")
    try:
        assert _probe(non_match_conn) == _SEED
    finally:
        non_match_conn.close()

    # Positive: the literal substring "EXA.Client" is present, so the rule fires.
    match_conn = _connect_with_client_name("v8 EXA.Client 1.0")
    try:
        assert _probe(match_conn) == f"{_SEED}|M"
    finally:
        match_conn.close()
