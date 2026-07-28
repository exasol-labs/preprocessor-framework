"""Deploy a registry module version into a live database (the ``add`` path).

The operator's CLI is the air-gap bridge: it fetches and verifies the artifact
on the CLI machine, then issues SQL to the database (ADR-006/007). The database
never fetches code.

A library-deployed module's artifact carries 1..N statements (ADR-054), and it
is deployed in a fixed order that keeps the never-mutate-a-live-module discipline
and the "leave the DB consistent on failure" invariant from the CLI spec:

1. Fetch the artifact, verify its ``sha256``, split it, and verify its object
   inventory — ALL of it BEFORE any DB write. Any failure aborts here, so
   nothing is deployed and no rule/provenance is written.
2. ``OPEN SCHEMA PREPROC_RT``, then execute the artifact's statements in FILE
   order — now the version's objects are present.
3. ``PREPROC.RULE_ENSURE`` — register the rule idempotently (the objects exist,
   so the rule never points at a missing script; RULE_ENSURE also validates the
   script's existence itself).
4. ``PREPROC.MODULE_REGISTER`` — write the provenance row (the version is
   present, so there is never provenance for an absent version).
5. A single ``PREPROC.REFRESH`` (skippable, so a batch caller such as ``sync``
   issues exactly one refresh for the whole convergence).

A self-deployed module skips step 2 entirely — its own installer deployed the
backing script — and only ensures the rule and writes provenance.

Atomicity is CONSTRUCTED, not inherited (ADR-059)
-------------------------------------------------
Exasol DDL implicitly commits and cannot be rolled back — ``refresh_core.lua``
and ``module_reclaim.lua`` document and work around this — so a transaction
around a composite artifact would be a false guarantee. All-or-nothing is built
from four mechanisms, and ``PREPROC.MODULE_INSTALL`` builds it from the same
four so both paths reach a byte-identical end state:

* **Abort-before-write pre-flight.** ``sha256``, the split, and the
  declared-vs-derived inventory (in both directions, every object carrying the
  module's ``_V<N>`` suffix) are all settled before the first statement runs.
* **File-order execution.** A companion may depend on an earlier statement.
* **Compensating cleanup.** DDL cannot roll back, so the inventory is the undo
  list: a failure at statement k drops the objects of statements 1..k-1 in
  REVERSE creation order. Cleanup is best-effort and never masks the original
  error — the operator sees the statement that actually failed.
* **Rule registration last.** The rule is DML and rollback-able, so a partial
  install is INERT rather than half-active: the grammar never activates.

Because every artifact statement is a ``CREATE OR REPLACE``, re-running an
install that failed part-way converges rather than colliding — including when
compensating cleanup could not drop everything it created.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from preproc.module.artifact import UNQUALIFIED_TYPES, derive_object, split_statements
from preproc.module.manifest import (
    ArtifactError,
    ModuleManifest,
    ObjectSpec,
    verify_artifact_inventory,
)
from preproc.module.registry import (
    OBJECTS_MAX_CHARS,
    RegistryEntry,
    encode_objects,
    fetch_artifact_bytes,
    verify_entry_artifact,
)

_SCRIPT_TYPE_SUFFIX = "SCRIPT"
_SCRIPT_DROP = "DROP SCRIPT IF EXISTS"
# A SCHEMA is dropped RESTRICT (Exasol's default): compensating cleanup is
# best-effort and must never destroy data, and `CREATE SCHEMA IF NOT EXISTS` may
# have found the schema already there. A non-empty schema therefore survives as
# a reported cleanup failure. A VIRTUAL SCHEMA is the deliberate exception — it
# always holds virtual tables, so RESTRICT could never drop one, and CASCADE
# removes only the local metadata, never anything at the remote source.
_DROP_VERBS = {
    "TABLE": "DROP TABLE IF EXISTS",
    "VIEW": "DROP VIEW IF EXISTS",
    "FUNCTION": "DROP FUNCTION IF EXISTS",
    "CONNECTION": "DROP CONNECTION IF EXISTS",
    "SCHEMA": "DROP SCHEMA IF EXISTS",
    "VIRTUAL SCHEMA": "DROP VIRTUAL SCHEMA IF EXISTS",
}
_CASCADE_TYPES = ("VIRTUAL SCHEMA",)
# pyexasol's own `_format_query` ends in `rstrip(" \n;")`; matching it exactly
# keeps this normalisation idempotent when the driver applies its own.
_TERMINATOR_CHARS = " \n;"


@dataclass(frozen=True)
class DeployResult:
    """What a single module deploy did, so a caller can report without a re-query."""

    name: str
    version: int
    scope_type: str
    scope_value: str
    rule_id: int
    rule_created: bool
    provenance_created: bool
    artifact_deployed: bool
    objects: tuple[ObjectSpec, ...] = ()


@dataclass(frozen=True)
class ArtifactStatement:
    """One statement of an artifact, with its file position and the object it creates.

    ``sql`` is the statement VERBATIM as the splitter yielded it, so a caller
    that has to reproduce the file (``offline_bundle``) emits exactly what the
    author wrote. ``creates`` is None for a statement that creates no object
    (``GRANT``, ``INSERT``, ``COMMIT``, …), which is what keeps such a statement
    out of the compensating-cleanup undo list.
    """

    index: int
    sql: str
    creates: ObjectSpec | None


@dataclass(frozen=True)
class ArtifactPlan:
    """A fetched, fully pre-flighted artifact, ready to execute.

    Holding a plan is the proof that the abort-before-write pre-flight passed:
    ``plan_artifact`` is the only way to build one, and it raises rather than
    return a plan whose inventory does not agree with its manifest.

    ``statements`` is in file order — the order the objects are created in, and
    the reverse of the order compensating cleanup and reclaim drop them in.
    """

    text: str
    statements: tuple[ArtifactStatement, ...]
    objects: tuple[ObjectSpec, ...]


class ArtifactDeployError(Exception):
    """Raised when an artifact statement fails, after compensating cleanup has run.

    Attributes:
        index: the failing statement's 1-based position in the artifact.
        total: how many statements the artifact carries.
        creates: the object the failing statement would have created, or None.
        dropped: the objects compensating cleanup removed, in the order dropped.
        cleanup_failures: the objects cleanup could not remove. The module is
            then partially present but still INERT — no rule was registered —
            and re-running the install converges.
    """

    def __init__(
        self,
        *,
        index: int,
        total: int,
        creates: ObjectSpec | None,
        cause: Exception,
        dropped: tuple[ObjectSpec, ...],
        failures: tuple[tuple[ObjectSpec, Exception], ...],
    ) -> None:
        self.index = index
        self.total = total
        self.creates = creates
        self.dropped = dropped
        self.cleanup_failures = tuple(obj for obj, _error in failures)
        super().__init__(_deploy_error_message(index, total, creates, cause, dropped, failures))


def _deploy_error_message(
    index: int,
    total: int,
    creates: ObjectSpec | None,
    cause: Exception,
    dropped: tuple[ObjectSpec, ...],
    failures: tuple[tuple[ObjectSpec, Exception], ...],
) -> str:
    """Compose the operator-facing message: the real failure first, cleanup after."""
    target = "" if creates is None else f" ({creates.type} {creates.name})"
    parts = [f"module artifact statement {index} of {total} failed{target}: {cause}"]
    if dropped:
        names = ", ".join(f"{obj.type} {obj.name}" for obj in dropped)
        parts.append(f"compensating cleanup dropped {len(dropped)} object(s) in reverse: {names}")
    if failures:
        unresolved = "; ".join(f"{obj.type} {obj.name} ({error})" for obj, error in failures)
        parts.append(
            f"compensating cleanup could NOT drop {unresolved} — the module is partially "
            "present but inert (no rule was registered) and re-running the install converges"
        )
    return ". ".join(parts)


def resolve_scope(entry: RegistryEntry, override: tuple[str, str] | None) -> tuple[str, str]:
    """Return the (scope_type, scope_value) to register the rule at.

    Uses the CLI ``--scope`` override when given, otherwise the module's
    ``suggested_scope``.
    """
    if override is not None:
        return override
    scope = entry.suggested_scope
    return scope["type"], scope["value"]


def plan_artifact(manifest: ModuleManifest, artifact_text: str) -> ArtifactPlan:
    """Pre-flight an artifact and return the plan that executes it.

    This is the abort-before-write gate: the artifact is split, the object each
    statement creates is derived independently of the manifest, and the derived
    inventory is compared with the declared ``[[objects]]`` in BOTH directions
    before a plan exists at all. Every declared object having the module's
    ``_V<N>`` suffix was already settled when the manifest parsed.

    Raises:
        ArtifactError: if the artifact cannot be split, creates an object twice,
            carries more than one statement with no ``[[objects]]`` declared,
            does not create the entry script the manifest names, its
            inventory disagrees with the declaration in either direction, or
            the inventory's encoded form is wider than ``PREPROC.MODULES.objects``
            can hold.
    """
    objects = verify_artifact_inventory(manifest, artifact_text)
    encoded_objects = encode_objects(objects)
    if encoded_objects is not None and len(encoded_objects) > OBJECTS_MAX_CHARS:
        raise ArtifactError(
            f"module {manifest.name!r}'s object inventory encodes to "
            f"{len(encoded_objects)} characters, over the {OBJECTS_MAX_CHARS}-character "
            "limit of PREPROC.MODULES.objects; registering the rule happens before the "
            "provenance write, so a module this size would deploy live with no "
            "provenance row — neither retirable nor reclaimable — so the install is "
            "refused before any DDL; split the module into smaller modules or shorten "
            "its object names"
        )
    statements = tuple(
        ArtifactStatement(index=position, sql=sql, creates=_created_object(sql))
        for position, sql in enumerate(split_statements(artifact_text), start=1)
    )
    return ArtifactPlan(text=artifact_text, statements=statements, objects=tuple(objects))


def _created_object(statement: str) -> ObjectSpec | None:
    """The object one statement creates, or None. Safe: the plan gate already parsed it."""
    derived = derive_object(statement)
    if derived is None:
        return None
    return ObjectSpec(type=derived["type"], name=derived["name"])


def fetch_and_verify_artifact(entry: RegistryEntry, registry: str, *, reader=None) -> ArtifactPlan:
    """Fetch a library-deployed entry's artifact, pre-flight it, and return its plan.

    Verifies the ``sha256`` (integrity) and then everything ``plan_artifact``
    verifies (structure), so the returned plan is executable and no DB write has
    happened yet.

    Raises:
        RegistryError: if the artifact cannot be fetched or its sha256 mismatches.
        ArtifactError: if the artifact's inventory disagrees with the manifest.
    """
    artifact_bytes = fetch_artifact_bytes(entry, registry, reader=reader)
    verify_entry_artifact(entry, artifact_bytes)
    return plan_artifact(entry.to_manifest(), artifact_bytes.decode("utf-8"))


def quote_qualified(obj: ObjectSpec) -> str:
    """Render an inventory object's canonical name as a quoted SQL identifier.

    The inventory name is canonical, not quoted: an unquoted identifier folded
    to upper case, a quoted one kept verbatim with its quotes stripped. Quoting
    every part on the way back out is what makes the name safe whatever it
    holds — a reserved word, a lower-case letter, or punctuation — and it is
    lossless, because an unquoted identifier and its upper-case quoted form name
    the same object. Only the schema separator is re-split, and only on the
    FIRST dot — safe because ``manifest._validate_objects`` rejects a dot inside
    a quoted SCHEMA part at parse time; a dot in the OBJECT part (the tail this
    partition absorbs) is unaffected and survives.
    """
    if obj.type in UNQUALIFIED_TYPES:
        return _quote(obj.name)
    schema, _, name = obj.name.partition(".")
    return f"{_quote(schema)}.{_quote(name)}"


def _quote(part: str) -> str:
    return '"' + part.replace('"', '""') + '"'


def drop_sql(obj: ObjectSpec) -> str:
    """The ``DROP … IF EXISTS`` statement that removes one inventory object.

    ``IF EXISTS`` keeps the drop idempotent, which matters because cleanup and
    reclaim both re-run over inventories that may be partly gone already.

    Raises:
        ValueError: for an object type the module contract does not admit — the
            admitted set is closed by ``artifact.derive_object``, so this only
            fires if the two ever drift apart.
    """
    if obj.type.endswith(_SCRIPT_TYPE_SUFFIX):
        verb = _SCRIPT_DROP
    elif obj.type in _DROP_VERBS:
        verb = _DROP_VERBS[obj.type]
    else:
        raise ValueError(f"no DROP form is known for object type {obj.type!r}")
    suffix = " CASCADE" if obj.type in _CASCADE_TYPES else ""
    return f"{verb} {quote_qualified(obj)}{suffix}"


def drop_objects(
    connection: Any, objects: tuple[ObjectSpec, ...]
) -> list[tuple[ObjectSpec, Exception]]:
    """Best-effort drop of ``objects`` in REVERSE of the order given.

    Reverse order because a companion may depend on an earlier object, so the
    creation order run backwards is the only order that can succeed. Every drop
    is attempted even when an earlier one fails, and no failure is raised: this
    is compensation for an error that has already happened, and it must never
    replace it.

    Returns:
        The ``(object, error)`` pairs that could not be dropped, in attempt
        order. Empty means every object is gone.
    """
    failures: list[tuple[ObjectSpec, Exception]] = []
    for obj in reversed(objects):
        try:
            connection.execute(drop_sql(obj))
        except Exception as error:  # noqa: BLE001 - compensation is best-effort by contract
            failures.append((obj, error))
    return failures


def deploy_artifact(connection: Any, plan: ArtifactPlan) -> None:
    """Execute a pre-flighted artifact's statements in file order, compensating on failure.

    Opens ``PREPROC_RT`` first (Exasol needs an active schema context to compile
    a script), mirroring ``install.py``; every object name in the artifact is
    fully qualified, so the open schema is compilation context only and objects
    may land in any schema. Statements are sent with no query parameters — a Lua
    or Python body contains braces, and pyexasol only formats when parameters are
    passed.

    On a failure at statement k the objects created by statements 1..k-1 are
    dropped in reverse, then ``ArtifactDeployError`` is raised naming statement
    k. Nothing is retried and nothing is rolled back: Exasol DDL cannot be.

    Raises:
        ArtifactDeployError: if any statement fails. Compensating cleanup has
            already run and its outcome is reported in the message.
    """
    connection.execute("OPEN SCHEMA PREPROC_RT")

    created: list[ObjectSpec] = []
    for statement in plan.statements:
        try:
            connection.execute(_wire_form(statement.sql))
        except Exception as cause:
            failures = drop_objects(connection, tuple(created))
            unresolved = {obj for obj, _error in failures}
            raise ArtifactDeployError(
                index=statement.index,
                total=len(plan.statements),
                creates=statement.creates,
                cause=cause,
                dropped=tuple(obj for obj in reversed(created) if obj not in unresolved),
                failures=tuple(failures),
            ) from cause
        if statement.creates is not None:
            created.append(statement.creates)


def _wire_form(statement: str) -> str:
    """Return a statement as the database's single-statement API takes it.

    A block-wrapped statement is preserved VERBATIM by the splitter so a body
    whose own syntax needs a trailing ``;`` survives, which means a statement can
    arrive carrying one. The single-statement API takes a statement with no
    terminator, so it is removed HERE and never in the splitter — stripping it
    there would corrupt every body that needs it. The in-database path applies
    the same normalisation before ``pquery`` so the two paths send identical text.
    """
    return statement.rstrip(_TERMINATOR_CHARS)


def rule_ensure(
    connection: Any,
    *,
    scope_type: str,
    scope_value: str,
    phase: str,
    script_name: str,
    function_name: str,
    client_pattern: str | None = None,
    norefresh: bool,
) -> tuple[int, bool]:
    """Idempotently register a rule via ``PREPROC.RULE_ENSURE``; return (rule_id, created).

    ``client_pattern`` is emitted as a SQL ``NULL`` literal when absent so the
    apply-always shape matches RULE_ENSURE's null-safe natural key. ``norefresh``
    is emitted as a ``TRUE``/``FALSE`` literal — the caller drives the single
    refresh.
    """
    cp_sql = "{cp}" if client_pattern is not None else "NULL"
    flag = "TRUE" if norefresh else "FALSE"
    params = {
        "st": scope_type,
        "sv": scope_value,
        "ph": phase,
        "sn": script_name,
        "fn": function_name,
    }
    if client_pattern is not None:
        params["cp"] = client_pattern
    rows = connection.execute(
        "EXECUTE SCRIPT PREPROC.RULE_ENSURE("
        "{st}, {sv}, {ph}, {sn}, {fn}, " + cp_sql + ", " + flag + ")",
        params,
    ).fetchall()
    rule_id, created = rows[0][0], rows[0][1]
    return int(rule_id), bool(created)


def module_register(
    connection: Any,
    entry: RegistryEntry,
    *,
    source_url: str | None = None,
    library_version: str | None = None,
    objects: tuple[ObjectSpec, ...] = (),
) -> bool:
    """Upsert the provenance row for ``entry`` via ``PREPROC.MODULE_REGISTER``.

    Returns whether the row was freshly created (True) or an existing row was
    updated (False). ``sha256`` is emitted as a SQL ``NULL`` literal for a
    self-deployed module (which carries none). ``source_url`` (the effective
    registry the artifact was fetched from) and ``library_version`` (the release
    the index declared) are recorded for audit and emitted as SQL ``NULL`` when
    absent. ``objects`` (the deployed inventory, ``DeployResult.objects``) is
    encoded via ``registry.encode_objects`` — never hand-rolled here, since the
    wire format is owned by that function — and emitted as SQL ``NULL`` when
    empty (a self-deployed module, which carries no artifact through the
    registry and so has nothing to inventory).
    """
    sha_sql = "{sh}" if entry.sha256 is not None else "NULL"
    url_sql = "{su}" if source_url is not None else "NULL"
    libver_sql = "{lv}" if library_version is not None else "NULL"
    encoded_objects = encode_objects(objects)
    objects_sql = "{ob}" if encoded_objects is not None else "NULL"
    params = {
        "n": entry.name,
        "v": entry.version,
        "src": entry.source["repo"],
        "rf": entry.source["ref"],
        "scn": entry.script_name,
        "fnn": entry.function,
        "ph": entry.phase,
        "dm": entry.deploy_mode,
    }
    if entry.sha256 is not None:
        params["sh"] = entry.sha256
    if source_url is not None:
        params["su"] = source_url
    if library_version is not None:
        params["lv"] = library_version
    if encoded_objects is not None:
        params["ob"] = encoded_objects
    rows = connection.execute(
        "EXECUTE SCRIPT PREPROC.MODULE_REGISTER("
        "{n}, {v}, {src}, {rf}, "
        + sha_sql
        + ", {scn}, {fnn}, {ph}, {dm}, "
        + url_sql
        + ", "
        + libver_sql
        + ", "
        + objects_sql
        + ")",
        params,
    ).fetchall()
    return bool(rows[0][3])


def refresh(connection: Any) -> None:
    """Run the single ``PREPROC.REFRESH`` that materialises RESOLUTION from CONFIG."""
    connection.execute("EXECUTE SCRIPT PREPROC.REFRESH").fetchall()


def deploy_module(
    connection: Any,
    entry: RegistryEntry,
    registry: str,
    *,
    scope: tuple[str, str] | None = None,
    do_refresh: bool = True,
    reader=None,
    library_version: str | None = None,
) -> DeployResult:
    """Deploy one registry module version and register its rule and provenance.

    For a ``library-deployed`` module: fetch + pre-flight the artifact, execute
    its statements in file order, ensure the rule, write provenance, and
    (optionally) refresh. For a ``self-deployed`` module: ensure the rule and
    write provenance only — the backing script is installed by the module's own
    project.

    Ordering guarantees the CLI's consistency invariant: every object of the
    artifact is present before any rule or provenance names it, a sha256 or
    inventory failure aborts before any DB write, and a mid-artifact failure
    propagates before the rule is registered — so a failed ``add`` leaves the
    module inert, with no rule and no provenance row.

    Args:
        do_refresh: issue the single ``PREPROC.REFRESH`` at the end. A batch
            caller (``sync``) passes False and refreshes once for the whole run.
        library_version: the resolved library release (the index's
            ``library_version``), recorded as provenance alongside the effective
            ``registry`` source URL.

    Raises:
        ArtifactDeployError: if an artifact statement fails; compensating
            cleanup has already run and no rule or provenance was written.
    """
    scope_type, scope_value = resolve_scope(entry, scope)

    objects: tuple[ObjectSpec, ...] = ()
    artifact_deployed = False
    if entry.deploy_mode == "library-deployed":
        plan = fetch_and_verify_artifact(entry, registry, reader=reader)
        deploy_artifact(connection, plan)
        objects = plan.objects
        artifact_deployed = True

    rule_id, rule_created = rule_ensure(
        connection,
        scope_type=scope_type,
        scope_value=scope_value,
        phase=entry.phase,
        script_name=entry.script_name,
        function_name=entry.function,
        norefresh=True,
    )
    provenance_created = module_register(
        connection, entry, source_url=registry, library_version=library_version, objects=objects
    )

    if do_refresh and rule_created:
        refresh(connection)

    return DeployResult(
        name=entry.name,
        version=entry.version,
        scope_type=scope_type,
        scope_value=scope_value,
        rule_id=rule_id,
        rule_created=rule_created,
        provenance_created=provenance_created,
        artifact_deployed=artifact_deployed,
        objects=objects,
    )
