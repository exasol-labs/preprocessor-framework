"""Tests for the CLI deploy path's composite-artifact execution (``deploy.py``).

Exasol DDL implicitly commits and cannot be rolled back, so all-or-nothing is
CONSTRUCTED here rather than inherited from a transaction. These tests pin the
four mechanisms that construct it — abort-before-write pre-flight, file-order
execution, reverse-order compensating cleanup, and rule-registration-last — plus
the error the operator actually sees when a statement fails mid-artifact.

They run entirely offline against a recording fake connection: the ordering and
cleanup logic is pure sequencing, and a fake makes the negative assertions
("nothing was executed", "no rule was registered") provable rather than
inferred. The end-state parity with ``PREPROC.MODULE_INSTALL`` needs a live
database and lives in ``tests/test_db_module_install.py``.
"""

import hashlib

import pytest

from preproc.module.artifact import derive_object
from preproc.module.deploy import (
    ArtifactDeployError,
    ArtifactPlan,
    _wire_form,
    deploy_artifact,
    deploy_module,
    drop_sql,
    fetch_and_verify_artifact,
    plan_artifact,
)
from preproc.module.manifest import ArtifactError, ObjectSpec, parse_manifest
from preproc.module.registry import (
    OBJECTS_MAX_CHARS,
    RegistryEntry,
    RegistryError,
    encode_objects,
)

# ---------------------------------------------------------------------------
# a recording fake connection
# ---------------------------------------------------------------------------


class FakeDbError(Exception):
    """Stands in for the driver error a failing statement raises."""


class _FakeResult:
    def fetchall(self):
        return [(7, True, None, True)]

    def fetchval(self):
        return 0


class FakeConnection:
    """Records every SQL it is given and fails the statements told to fail.

    Covers exactly the slice of ``pyexasol.ExaConnection`` the deploy path uses.
    ``fail_on`` holds substrings whose statement must raise, which is how a
    mid-artifact failure — and a compensating DROP that itself fails — are
    provoked with no database.
    """

    def __init__(self, *, fail_on=()):
        self.executed: list[str] = []
        self._fail_on = tuple(fail_on)

    def execute(self, query, query_params=None):
        self.executed.append(query)
        for needle in self._fail_on:
            if needle in query:
                raise FakeDbError(f"syntax error near {needle!r}")
        return _FakeResult()

    def sql_of(self, prefix: str) -> list[str]:
        """Every recorded statement starting with ``prefix``, in execution order."""
        return [sql for sql in self.executed if sql.startswith(prefix)]


# ---------------------------------------------------------------------------
# the composite fixture: 6 statements, 5 objects, one of them outside PREPROC_RT
# ---------------------------------------------------------------------------

_COMPOSITE_ARTIFACT = """\
--/
CREATE OR REPLACE LUA SCRIPT PREPROC_RT.WIDGET_V1 AS
function widget(sqltext)
    local parts = {};
    return table.concat(parts);
end;
/

CREATE TABLE PREPROC_RT.WIDGET_CACHE_V1 (k VARCHAR(64));

--/
CREATE OR REPLACE PYTHON3 SCALAR SCRIPT ANALYTICS.WIDGET_HASH_V1 (s VARCHAR(200))
RETURNS VARCHAR(64) AS
def run(ctx):
    parts = [ctx.s];
    return parts[0]
/

CREATE VIEW PREPROC_RT.WIDGET_VIEW_V1 AS SELECT k FROM PREPROC_RT.WIDGET_CACHE_V1;

INSERT INTO PREPROC_RT.WIDGET_CACHE_V1 (k) VALUES ('seed');

CREATE CONNECTION WIDGET_LINK_V1 TO 'https://example.invalid';
"""

_SINGLE_ARTIFACT = """\
--/
CREATE OR REPLACE LUA SCRIPT PREPROC_RT.WIDGET_V1 AS
function widget(sqltext)
    local parts = {};
    return table.concat(parts);
end
/
"""

_COMPOSITE_OBJECTS = (
    {"type": "LUA SCRIPT", "name": "PREPROC_RT.WIDGET_V1"},
    {"type": "TABLE", "name": "PREPROC_RT.WIDGET_CACHE_V1"},
    {"type": "PYTHON3 SCALAR SCRIPT", "name": "ANALYTICS.WIDGET_HASH_V1"},
    {"type": "VIEW", "name": "PREPROC_RT.WIDGET_VIEW_V1"},
    {"type": "CONNECTION", "name": "WIDGET_LINK_V1"},
)


def _manifest_data(objects=_COMPOSITE_OBJECTS, *, sha256=None):
    data = {
        "name": "widget",
        "description": "widget module.",
        "phase": "TRANSLATE",
        "script_name": "PREPROC_RT.WIDGET_V1",
        "function": "widget",
        "version": 1,
        "min_framework": "1.0.0",
        "deploy_mode": "library-deployed",
        "sha256": sha256 or ("a" * 64),
        "suggested_scope": {"type": "ROLE", "value": "PREPROC_ADMIN"},
    }
    if objects is not None:
        data["objects"] = [dict(obj) for obj in objects]
    return data


def _manifest(objects=_COMPOSITE_OBJECTS):
    return parse_manifest(_manifest_data(objects))


def _entry(artifact_text, objects=_COMPOSITE_OBJECTS):
    return RegistryEntry(
        name="widget",
        version=1,
        phase="TRANSLATE",
        script_name="PREPROC_RT.WIDGET_V1",
        function="widget",
        description="widget module.",
        min_framework="1.0.0",
        suggested_scope={"type": "ROLE", "value": "PREPROC_ADMIN"},
        deploy_mode="library-deployed",
        source={"repo": "exasol-labs/preprocessor-library", "ref": "main"},
        sha256=hashlib.sha256(artifact_text.encode("utf-8")).hexdigest(),
        artifact_path="modules/widget/widget_v1.sql",
        objects=objects,
    )


def _reader(text):
    def read(_url):
        return text.encode("utf-8")

    return read


_REGISTRY = (
    "https://raw.githubusercontent.com/exasol-labs/preprocessor-library/main/registry/index.json"
)


# ---------------------------------------------------------------------------
# pre-flight: nothing reaches the database until every check has passed
# ---------------------------------------------------------------------------


def test_plan_pairs_every_statement_with_the_object_it_creates():
    """The plan is the undo list: file order, and a statement creating nothing pairs with None."""
    plan = plan_artifact(_manifest(), _COMPOSITE_ARTIFACT)

    assert [stmt.index for stmt in plan.statements] == [1, 2, 3, 4, 5, 6]
    assert [None if s.creates is None else s.creates.name for s in plan.statements] == [
        "PREPROC_RT.WIDGET_V1",
        "PREPROC_RT.WIDGET_CACHE_V1",
        "ANALYTICS.WIDGET_HASH_V1",
        "PREPROC_RT.WIDGET_VIEW_V1",
        None,
        "WIDGET_LINK_V1",
    ]
    assert plan.objects == tuple(
        ObjectSpec(type=obj["type"], name=obj["name"]) for obj in _COMPOSITE_OBJECTS
    )
    assert plan.text == _COMPOSITE_ARTIFACT


def test_undeclared_object_is_rejected_before_any_statement_is_planned():
    """An artifact object absent from [[objects]] aborts at pre-flight, naming it."""
    declared = tuple(obj for obj in _COMPOSITE_OBJECTS if obj["type"] != "VIEW")
    with pytest.raises(ArtifactError, match="WIDGET_VIEW_V1"):
        plan_artifact(_manifest(declared), _COMPOSITE_ARTIFACT)


def _wide_object_name(index: int, qualified_len: int = 100) -> str:
    """A fully-qualified TABLE name exactly ``qualified_len`` characters long, ``_V1``-suffixed."""
    schema = f"BIGSCHEMA_{index:04d}"
    remaining = qualified_len - len(schema) - 1
    object_part = "X" * (remaining - 3) + "_V1"
    return f"{schema}.{object_part}"


def _wide_inventory_fixture(qualified_len: int = 100):
    """1 entry script + just enough ~100-char tables to encode past the objects column.

    The table count is DERIVED from ``OBJECTS_MAX_CHARS``, never hard-coded: a
    fixture pinned to an object count stops exceeding the limit the moment the
    column is widened, and the test asserting the refusal goes quietly vacuous
    (which is exactly what happened at 4,000). ~100-character qualified names
    model the real overflow — a subsystem module reaching the ceiling through
    name LENGTH as much as object COUNT — and both ``parse_manifest`` and
    ``generate_index`` accept the shape, so the gap this pins is reachable
    through the CLI's ``add`` path rather than being a resolver corner case.

    Every generated name is exactly ``qualified_len`` characters, so the count
    needed is arithmetic rather than a re-encode-per-candidate search: at the
    current width that is ~2,000 objects, and an O(n^2) search would dominate
    the suite's runtime for no benefit.

    Returns ``(manifest, artifact_text, encoded_length)``.
    """
    objects = [{"type": "LUA SCRIPT", "name": "PREPROC_RT.WIDGET_V1"}]
    statements = [
        "--/\nCREATE OR REPLACE LUA SCRIPT PREPROC_RT.WIDGET_V1 AS\n"
        "function widget(sqltext)\n    return sqltext\nend\n/"
    ]
    # encode_objects joins 'TYPE|NAME' lines with '\n', so each further table
    # costs its own line plus the newline that joins it.
    per_table = len("TABLE|") + qualified_len + len("\n")
    table_count = (OBJECTS_MAX_CHARS - len(encode_objects(objects))) // per_table + 1
    for index in range(table_count):
        name = _wide_object_name(index, qualified_len)
        objects.append({"type": "TABLE", "name": name})
        statements.append(f"CREATE TABLE {name} (c INT);")
    artifact_text = "\n\n".join(statements) + "\n"
    manifest = parse_manifest(
        _manifest_data(
            tuple(objects), sha256=hashlib.sha256(artifact_text.encode("utf-8")).hexdigest()
        )
    )
    encoded_length = len(encode_objects(objects))
    return manifest, artifact_text, encoded_length


def test_encoded_inventory_wider_than_the_objects_column_is_rejected_before_any_write():
    """An inventory whose encoding exceeds PREPROC.MODULES.objects's width aborts at pre-flight.

    This is the CLI-side sibling of the in-DB check ``module_install.lua`` and
    the resolver already enforce: registering the rule happens BEFORE the
    provenance write, so discovering the width only at ``MODULE_REGISTER`` would
    leave the module live and dispatching with no provenance row — un-retirable
    and un-reclaimable. Refusing here, in ``plan_artifact``, keeps the abort
    strictly before the first DDL.
    """
    manifest, artifact_text, encoded_length = _wide_inventory_fixture()
    assert encoded_length > OBJECTS_MAX_CHARS

    with pytest.raises(ArtifactError) as exc_info:
        plan_artifact(manifest, artifact_text)

    message = str(exc_info.value)
    assert str(OBJECTS_MAX_CHARS) in message
    assert str(encoded_length) in message


def test_sha256_mismatch_deploys_no_statement_of_the_artifact():
    """A tampered artifact aborts in fetch — before OPEN SCHEMA, let alone any DDL."""
    entry = _entry(_COMPOSITE_ARTIFACT)
    connection = FakeConnection()

    with pytest.raises(RegistryError, match="sha256"):
        deploy_module(
            connection,
            entry,
            _REGISTRY,
            reader=_reader(_COMPOSITE_ARTIFACT + "\n-- tampered\n"),
        )

    assert connection.executed == []


def test_inventory_disagreement_deploys_no_statement_of_the_artifact():
    """The declared-vs-derived comparison aborts before the first write, too."""
    declared = tuple(obj for obj in _COMPOSITE_OBJECTS if obj["type"] != "TABLE")
    entry = _entry(_COMPOSITE_ARTIFACT, objects=declared)
    connection = FakeConnection()

    with pytest.raises(ArtifactError, match="WIDGET_CACHE_V1"):
        deploy_module(connection, entry, _REGISTRY, reader=_reader(_COMPOSITE_ARTIFACT))

    assert connection.executed == []


def test_fetch_and_verify_artifact_returns_the_executable_plan():
    """The fetch step yields a plan, not a lone string — the composite path needs both."""
    entry = _entry(_COMPOSITE_ARTIFACT)

    plan = fetch_and_verify_artifact(entry, _REGISTRY, reader=_reader(_COMPOSITE_ARTIFACT))

    assert isinstance(plan, ArtifactPlan)
    assert len(plan.statements) == 6
    assert len(plan.objects) == 5


# ---------------------------------------------------------------------------
# execution: file order, and N=1 unchanged
# ---------------------------------------------------------------------------


def test_statements_execute_in_file_order_after_opening_the_runtime_schema():
    """Every statement lands after OPEN SCHEMA, in file order, in its WIRE form.

    Statement 1's block body legitimately keeps a trailing ``;`` (preserved
    verbatim by the splitter), so its ``sql`` and its wire form genuinely
    differ here — asserting the executed text against the raw ``stmt.sql``
    values would therefore catch ``deploy_artifact`` skipping the
    ``_wire_form`` normalisation, unlike comparing against a fixture where
    every statement happens to already be terminator-free.
    """
    plan = plan_artifact(_manifest(), _COMPOSITE_ARTIFACT)
    connection = FakeConnection()

    deploy_artifact(connection, plan)

    assert connection.executed[0] == "OPEN SCHEMA PREPROC_RT"
    assert plan.statements[0].sql.endswith("end;")
    assert connection.executed[1].endswith("end")
    assert not connection.executed[1].endswith("end;")
    assert connection.executed[1:] == [_wire_form(stmt.sql) for stmt in plan.statements]


def test_single_statement_artifact_deploys_the_marker_stripped_create():
    """N=1 stays a single OPEN SCHEMA + single CREATE, with no marker lines on the wire."""
    plan = plan_artifact(_manifest(None), _SINGLE_ARTIFACT)
    connection = FakeConnection()

    deploy_artifact(connection, plan)

    assert len(connection.executed) == 2
    assert connection.executed[1].startswith("CREATE OR REPLACE LUA SCRIPT PREPROC_RT.WIDGET_V1")
    assert "--/" not in connection.executed[1]
    assert not connection.executed[1].endswith("/")


def test_a_block_statements_trailing_semicolon_is_not_sent_to_the_database():
    """A block body legitimately keeps a trailing ``;``; the executor normalises it off.

    The block grammar preserves a body verbatim so a ``CREATE FUNCTION … END;``
    survives, which means a statement may arrive with a trailing terminator. The
    single-statement API takes a statement without one, so the executor — never
    the splitter — removes it.
    """
    artifact = (
        "--/\nCREATE OR REPLACE LUA SCRIPT PREPROC_RT.WIDGET_V1 AS\n"
        "function widget(s)\n    return s;\nend;\n/\n"
    )
    plan = plan_artifact(_manifest(None), artifact)
    connection = FakeConnection()

    assert plan.statements[0].sql.endswith(";")
    deploy_artifact(connection, plan)

    assert connection.executed[1].endswith("end")


# ---------------------------------------------------------------------------
# compensating cleanup: reverse order, best effort, never masking the failure
# ---------------------------------------------------------------------------


def test_failure_at_statement_k_drops_the_objects_of_statements_1_to_k_minus_1():
    """DDL cannot roll back, so the inventory prefix is walked backwards as the undo list."""
    plan = plan_artifact(_manifest(), _COMPOSITE_ARTIFACT)
    connection = FakeConnection(fail_on=("CREATE VIEW",))

    with pytest.raises(ArtifactDeployError):
        deploy_artifact(connection, plan)

    assert connection.sql_of("DROP") == [
        'DROP SCRIPT IF EXISTS "ANALYTICS"."WIDGET_HASH_V1"',
        'DROP TABLE IF EXISTS "PREPROC_RT"."WIDGET_CACHE_V1"',
        'DROP SCRIPT IF EXISTS "PREPROC_RT"."WIDGET_V1"',
    ]


def test_a_failure_at_the_first_statement_drops_nothing():
    plan = plan_artifact(_manifest(), _COMPOSITE_ARTIFACT)
    connection = FakeConnection(fail_on=("CREATE OR REPLACE LUA SCRIPT",))

    with pytest.raises(ArtifactDeployError):
        deploy_artifact(connection, plan)

    assert connection.sql_of("DROP") == []


def test_a_statement_that_creates_nothing_is_never_dropped():
    """The INSERT at statement 5 contributes no object, so cleanup skips straight past it."""
    plan = plan_artifact(_manifest(), _COMPOSITE_ARTIFACT)
    connection = FakeConnection(fail_on=("CREATE CONNECTION",))

    with pytest.raises(ArtifactDeployError):
        deploy_artifact(connection, plan)

    drops = connection.sql_of("DROP")
    assert drops[0] == 'DROP VIEW IF EXISTS "PREPROC_RT"."WIDGET_VIEW_V1"'
    assert len(drops) == 4
    assert not any("SEED" in sql or "INSERT" in sql for sql in drops)


def test_cleanup_drops_an_object_in_whatever_schema_the_inventory_names():
    """Objects may live in any schema (ADR-057); cleanup follows the inventory, not PREPROC_RT."""
    plan = plan_artifact(_manifest(), _COMPOSITE_ARTIFACT)
    connection = FakeConnection(fail_on=("CREATE VIEW",))

    with pytest.raises(ArtifactDeployError):
        deploy_artifact(connection, plan)

    assert 'DROP SCRIPT IF EXISTS "ANALYTICS"."WIDGET_HASH_V1"' in connection.executed


def test_the_error_names_the_failing_statement_and_the_object_it_would_have_created():
    plan = plan_artifact(_manifest(), _COMPOSITE_ARTIFACT)
    connection = FakeConnection(fail_on=("CREATE VIEW",))

    with pytest.raises(ArtifactDeployError) as excinfo:
        deploy_artifact(connection, plan)

    error = excinfo.value
    assert error.index == 4
    assert error.creates == ObjectSpec(type="VIEW", name="PREPROC_RT.WIDGET_VIEW_V1")
    assert "statement 4 of 6" in str(error)
    assert "VIEW PREPROC_RT.WIDGET_VIEW_V1" in str(error)


def test_a_failing_statement_that_creates_nothing_is_still_named_by_its_index():
    """ "Where derivable" — a DML statement has no object to name, but it has a position."""
    plan = plan_artifact(_manifest(), _COMPOSITE_ARTIFACT)
    connection = FakeConnection(fail_on=("INSERT INTO",))

    with pytest.raises(ArtifactDeployError) as excinfo:
        deploy_artifact(connection, plan)

    error = excinfo.value
    assert error.index == 5
    assert error.creates is None
    assert "statement 5 of 6 failed:" in str(error)
    assert len(error.dropped) == 4


def test_cleanup_does_not_mask_the_original_statement_error():
    """A DROP that fails is reported alongside — never instead of — the real failure."""
    plan = plan_artifact(_manifest(), _COMPOSITE_ARTIFACT)
    connection = FakeConnection(fail_on=("CREATE VIEW", "DROP TABLE"))

    with pytest.raises(ArtifactDeployError) as excinfo:
        deploy_artifact(connection, plan)

    error = excinfo.value
    assert isinstance(error.__cause__, FakeDbError)
    assert "CREATE VIEW" in str(error.__cause__)
    assert "syntax error near 'CREATE VIEW'" in str(error)
    assert [obj.name for obj in error.cleanup_failures] == ["PREPROC_RT.WIDGET_CACHE_V1"]
    assert 'DROP SCRIPT IF EXISTS "PREPROC_RT"."WIDGET_V1"' in connection.executed


def test_a_quoted_object_name_is_requoted_for_its_drop():
    """The canonical inventory name is unquoted; a DROP must put the quotes back."""
    artifact = (
        "--/\nCREATE OR REPLACE LUA SCRIPT PREPROC_RT.WIDGET_V1 AS\n"
        "function widget(s)\n    return s;\nend\n/\n\n"
        'CREATE TABLE PREPROC_RT."odd.name_V1" (c INT);\n\n'
        "CREATE VIEW PREPROC_RT.WIDGET_VIEW_V1 AS SELECT 1;\n"
    )
    objects = (
        {"type": "LUA SCRIPT", "name": "PREPROC_RT.WIDGET_V1"},
        {"type": "TABLE", "name": 'PREPROC_RT."odd.name_V1"'},
        {"type": "VIEW", "name": "PREPROC_RT.WIDGET_VIEW_V1"},
    )
    plan = plan_artifact(_manifest(objects), artifact)
    connection = FakeConnection(fail_on=("CREATE VIEW",))

    with pytest.raises(ArtifactDeployError):
        deploy_artifact(connection, plan)

    assert connection.sql_of("DROP") == [
        'DROP TABLE IF EXISTS "PREPROC_RT"."odd.name_V1"',
        'DROP SCRIPT IF EXISTS "PREPROC_RT"."WIDGET_V1"',
    ]


def test_a_schema_is_never_dropped_with_cascade():
    """Best-effort compensation must not destroy data a pre-existing schema might hold."""
    artifact = (
        "CREATE SCHEMA WIDGET_HOME_V1;\n\n"
        "--/\nCREATE OR REPLACE LUA SCRIPT PREPROC_RT.WIDGET_V1 AS\n"
        "function widget(s)\n    return s;\nend\n/\n"
    )
    objects = (
        {"type": "SCHEMA", "name": "WIDGET_HOME_V1"},
        {"type": "LUA SCRIPT", "name": "PREPROC_RT.WIDGET_V1"},
    )
    plan = plan_artifact(_manifest(objects), artifact)
    connection = FakeConnection(fail_on=("CREATE OR REPLACE LUA SCRIPT",))

    with pytest.raises(ArtifactDeployError):
        deploy_artifact(connection, plan)

    assert connection.sql_of("DROP") == ['DROP SCHEMA IF EXISTS "WIDGET_HOME_V1"']


_ADMITTED_CREATES = (
    "CREATE OR REPLACE LUA SCRIPT PREPROC_RT.A_V1 AS x",
    "CREATE OR REPLACE PYTHON3 SCALAR SCRIPT PREPROC_RT.A_V1 () RETURNS INT AS x",
    "CREATE OR REPLACE JAVA SET SCRIPT PREPROC_RT.A_V1 () EMITS (c INT) AS x",
    "CREATE TABLE PREPROC_RT.A_V1 (c INT)",
    "CREATE VIEW PREPROC_RT.A_V1 AS SELECT 1",
    "CREATE OR REPLACE FUNCTION PREPROC_RT.A_V1 () RETURN INT IS BEGIN RETURN 1; END",
    "CREATE SCHEMA A_V1",
    "CREATE VIRTUAL SCHEMA A_V1 USING PREPROC_RT.ADAPTER",
    "CREATE CONNECTION A_V1 TO 'x'",
)


@pytest.mark.parametrize("statement", _ADMITTED_CREATES)
def test_every_object_type_the_contract_admits_has_a_drop_form(statement):
    """Cleanup can only be exact if the DROP map closes over what the parser admits."""
    derived = derive_object(statement)
    assert drop_sql(ObjectSpec(type=derived["type"], name=derived["name"])).startswith("DROP ")


def test_a_virtual_schema_is_dropped_cascade_but_a_plain_schema_is_not():
    """CASCADE on a VIRTUAL SCHEMA removes local metadata only; on a SCHEMA it removes data."""
    assert drop_sql(ObjectSpec(type="VIRTUAL SCHEMA", name="VS_V1")).endswith("CASCADE")
    assert drop_sql(ObjectSpec(type="SCHEMA", name="S_V1")) == 'DROP SCHEMA IF EXISTS "S_V1"'


def test_an_unknown_object_type_has_no_drop_form():
    with pytest.raises(ValueError, match="ROLE"):
        drop_sql(ObjectSpec(type="ROLE", name="R_V1"))


# ---------------------------------------------------------------------------
# rule registration last: a failed add leaves the module inert
# ---------------------------------------------------------------------------


def test_the_rule_is_registered_only_after_the_last_artifact_statement():
    entry = _entry(_COMPOSITE_ARTIFACT)
    connection = FakeConnection()

    deploy_module(connection, entry, _REGISTRY, reader=_reader(_COMPOSITE_ARTIFACT))

    last_artifact_statement = max(
        index for index, sql in enumerate(connection.executed) if sql.startswith("CREATE")
    )
    rule = next(
        index
        for index, sql in enumerate(connection.executed)
        if sql.startswith("EXECUTE SCRIPT PREPROC.RULE_ENSURE")
    )
    provenance = next(
        index
        for index, sql in enumerate(connection.executed)
        if sql.startswith("EXECUTE SCRIPT PREPROC.MODULE_REGISTER")
    )
    assert last_artifact_statement < rule < provenance


def test_a_failed_add_registers_no_rule_and_no_provenance_row():
    """A partial install is inert, not half-active: the rule is DML and never runs."""
    entry = _entry(_COMPOSITE_ARTIFACT)
    connection = FakeConnection(fail_on=("CREATE VIEW",))

    with pytest.raises(ArtifactDeployError):
        deploy_module(connection, entry, _REGISTRY, reader=_reader(_COMPOSITE_ARTIFACT))

    assert connection.sql_of("EXECUTE SCRIPT PREPROC.RULE_ENSURE") == []
    assert connection.sql_of("EXECUTE SCRIPT PREPROC.MODULE_REGISTER") == []
    assert connection.sql_of("EXECUTE SCRIPT PREPROC.REFRESH") == []


def test_deploy_result_reports_the_deployed_inventory():
    entry = _entry(_COMPOSITE_ARTIFACT)
    connection = FakeConnection()

    result = deploy_module(connection, entry, _REGISTRY, reader=_reader(_COMPOSITE_ARTIFACT))

    assert result.artifact_deployed is True
    assert [obj.name for obj in result.objects] == [obj["name"] for obj in _COMPOSITE_OBJECTS]


def test_a_self_deployed_module_deploys_no_artifact_and_still_registers_its_rule():
    entry = RegistryEntry(
        name="widget",
        version=1,
        phase="TRANSLATE",
        script_name="PREPROC_RT.WIDGET_V1",
        function="widget",
        description="widget module.",
        min_framework="1.0.0",
        suggested_scope={"type": "ROLE", "value": "PREPROC_ADMIN"},
        deploy_mode="self-deployed",
        source={"repo": "exasol/widget", "ref": "v1"},
    )
    connection = FakeConnection()

    result = deploy_module(connection, entry, _REGISTRY)

    assert result.artifact_deployed is False
    assert result.objects == ()
    assert connection.sql_of("OPEN SCHEMA") == []
    assert len(connection.sql_of("EXECUTE SCRIPT PREPROC.RULE_ENSURE")) == 1
