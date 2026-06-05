"""Integration tests for the demo config seed and teardown scripts.

``sql/demo/seed.sql`` stands up two demo roles, two demo users with different
role sets, and CONFIG rules across all three phases (one client_pattern-scoped),
then runs PREPROC.REFRESH so the two users resolve to visibly different
pipelines. ``sql/demo/teardown.sql`` removes exactly that and re-runs REFRESH;
it is idempotent.

Why a local SQL applier instead of install._apply_sql_file
    install._split_statements strips ``--`` comments and splits on ``;``. That
    is correct for the install DDL but WRONG for teardown.sql, which contains a
    ``/``-delimited transient Lua script whose body the ``;``-splitter would
    merge with the following ``EXECUTE SCRIPT`` into one malformed statement
    (verified). ``_apply_demo_sql`` below understands the standalone-``/`` script
    terminator (interactive-client convention), so both seed.sql and
    teardown.sql apply correctly. The seed's ``--@tolerate-exists`` markers are
    irrelevant when applied through this helper because the fixture tears down
    first, guaranteeing a clean slate; the helper does not tolerate errors.
"""

import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SEED_PATH = _REPO_ROOT / "sql" / "demo" / "seed.sql"
_TEARDOWN_PATH = _REPO_ROOT / "sql" / "demo" / "teardown.sql"

_DEMO_ROLES = ("DEMO_ANALYST", "DEMO_ETL")
_DEMO_USERS = ("DEMO_USER_ANALYST", "DEMO_USER_ETL")
_DEMO_RULE_IDS = (9001, 9002, 9003, 9004, 9005)
_DEMO_RULE_ID_LIST = ", ".join(str(rid) for rid in _DEMO_RULE_IDS)

_CREATE_WITH_BODY = re.compile(r"^\s*CREATE\b.*\bAS\s*$", re.IGNORECASE)


def _split_demo_sql(text):
    """Split a demo SQL file into statements, honouring ``/`` script terminators.

    A line that is exactly ``/`` terminates the accumulated statement (used for
    CREATE ... AS script bodies). Outside a script body, ``;`` terminates a
    statement and ``--`` comments are stripped first. Inside a script body
    (entered on a ``CREATE ... AS`` line), only the ``/`` terminates, so the body
    may freely contain ``;`` and ``--``.
    """
    statements = []
    buf = []
    in_body = False

    def flush():
        stmt = "\n".join(buf).strip()
        buf.clear()
        if stmt:
            statements.append(stmt)

    for raw in text.splitlines():
        if raw.strip() == "/":
            flush()
            in_body = False
            continue
        if in_body:
            buf.append(raw)
            continue
        code = re.sub(r"--.*", "", raw).rstrip()
        if not code.strip():
            continue
        if _CREATE_WITH_BODY.match(code):
            in_body = True
            buf.append(code)
            continue
        while ";" in code:
            head, code = code.split(";", 1)
            buf.append(head)
            flush()
        if code.strip():
            buf.append(code)
    flush()
    return statements


def _apply_demo_sql(connection, path):
    """Apply every statement in a demo SQL file in order."""
    for statement in _split_demo_sql(path.read_text(encoding="utf-8")):
        connection.execute(statement)


@pytest.fixture(scope="module")
def seeded(installed):
    """Apply a clean seed once per module; tear down on exit.

    Tears down first so the seed always lands on a clean slate (the seed's
    CREATE ROLE/USER have no IF-NOT-EXISTS form), then applies the seed. On exit
    it applies teardown so no demo objects leak into other test modules.
    """
    _apply_demo_sql(installed, _TEARDOWN_PATH)
    _apply_demo_sql(installed, _SEED_PATH)
    try:
        yield installed
    finally:
        try:
            _apply_demo_sql(installed, _TEARDOWN_PATH)
        except Exception:
            pass


def _pipeline_phases(connection, user_name):
    """Return the set of phases resolved for a demo user in PREPROC.RESOLUTION."""
    rows = connection.execute(
        "SELECT phase FROM PREPROC.RESOLUTION WHERE user_name = {u}",
        {"u": user_name},
    ).fetchall()
    return {row[0] for row in rows}


@pytest.mark.integration
def test_seed_creates_roles_users_distinct_pipelines(seeded):
    """The seed creates both demo roles and both demo users."""
    for role in _DEMO_ROLES:
        row = seeded.execute(
            "SELECT ROLE_NAME FROM EXA_DBA_ROLES WHERE ROLE_NAME = {r}", {"r": role}
        ).fetchone()
        assert row is not None, f"demo role {role} not created"

    for user in _DEMO_USERS:
        row = seeded.execute(
            "SELECT USER_NAME FROM EXA_DBA_USERS WHERE USER_NAME = {u}", {"u": user}
        ).fetchone()
        assert row is not None, f"demo user {user} not created"


@pytest.mark.integration
def test_seed_inserts_rules_all_phases(seeded):
    """The seed inserts demo CONFIG rules covering all three phases."""
    rows = seeded.execute(
        f"SELECT DISTINCT phase FROM PREPROC.CONFIG WHERE rule_id IN ({_DEMO_RULE_ID_LIST})"
    ).fetchall()
    phases = {row[0] for row in rows}
    assert phases == {"TRANSLATE", "EXPAND", "REWRITE"}, (
        f"seed must span all three phases, got {phases}"
    )


@pytest.mark.integration
def test_seed_includes_client_pattern_rule(seeded):
    """The seed includes a client_pattern-scoped rule (rule_id 9004, EXAplus)."""
    row = seeded.execute(
        "SELECT client_pattern FROM PREPROC.CONFIG WHERE rule_id = 9004"
    ).fetchone()
    assert row is not None, "client-pattern rule 9004 not present"
    assert row[0] == "EXAplus", f"rule 9004 client_pattern must be EXAplus, got {row[0]!r}"


@pytest.mark.integration
def test_seed_runs_refresh(seeded):
    """After the seed, RESOLUTION carries rows for both demo users."""
    for user in _DEMO_USERS:
        count = seeded.execute(
            "SELECT COUNT(*) FROM PREPROC.RESOLUTION WHERE user_name = {u}",
            {"u": user},
        ).fetchval()
        assert count > 0, f"REFRESH did not resolve any rules for {user}"


@pytest.mark.integration
def test_demo_users_see_different_pipelines(seeded):
    """The two demo users resolve to visibly different pipelines.

    The analyst holds the full TRANSLATE/EXPAND/REWRITE pipeline; the ETL user
    has no EXPAND rule and only a client-scoped TRANSLATE, so the resolved phase
    sets must differ — the whole point of the demo.
    """
    analyst_phases = _pipeline_phases(seeded, "DEMO_USER_ANALYST")
    etl_phases = _pipeline_phases(seeded, "DEMO_USER_ETL")

    assert analyst_phases != etl_phases, (
        f"demo users must resolve to different pipelines; both saw {analyst_phases}"
    )
    assert "EXPAND" in analyst_phases, "analyst should have the EXPAND phase"
    assert "EXPAND" not in etl_phases, "ETL user should NOT have the EXPAND phase"


@pytest.mark.integration
def test_teardown_removes_demo_objects(seeded):
    """Teardown removes the demo roles, users, and CONFIG rules.

    Applied AFTER the seed (the module-scoped ``seeded`` fixture has run), so
    this test mutates shared state; the fixture's exit teardown is a harmless
    idempotent repeat.
    """
    _apply_demo_sql(seeded, _TEARDOWN_PATH)

    for role in _DEMO_ROLES:
        row = seeded.execute(
            "SELECT ROLE_NAME FROM EXA_DBA_ROLES WHERE ROLE_NAME = {r}", {"r": role}
        ).fetchone()
        assert row is None, f"demo role {role} survived teardown"

    for user in _DEMO_USERS:
        row = seeded.execute(
            "SELECT USER_NAME FROM EXA_DBA_USERS WHERE USER_NAME = {u}", {"u": user}
        ).fetchone()
        assert row is None, f"demo user {user} survived teardown"

    remaining = seeded.execute(
        f"SELECT COUNT(*) FROM PREPROC.CONFIG WHERE rule_id IN ({_DEMO_RULE_ID_LIST})"
    ).fetchval()
    assert remaining == 0, f"{remaining} demo CONFIG rule(s) survived teardown"


@pytest.mark.integration
def test_teardown_idempotent(seeded):
    """Teardown re-run on an already-clean database raises no error.

    Runs teardown twice in a row; the second run must complete without error and
    leave no demo objects (the existence-checked transient drop script is the
    mechanism that makes the missing-object drops safe).
    """
    _apply_demo_sql(seeded, _TEARDOWN_PATH)
    _apply_demo_sql(seeded, _TEARDOWN_PATH)

    remaining = seeded.execute(
        f"SELECT COUNT(*) FROM PREPROC.CONFIG WHERE rule_id IN ({_DEMO_RULE_ID_LIST})"
    ).fetchval()
    assert remaining == 0, "demo CONFIG rules present after idempotent teardown"
