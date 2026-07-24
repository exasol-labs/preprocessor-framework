"""Deploy a registry module version into a live database (the ``add`` path).

The operator's CLI is the air-gap bridge: it fetches and verifies the artifact
on the CLI machine, then issues SQL to the database (ADR-006/007). The database
never fetches code.

A library-deployed module is deployed atomically in a fixed order that keeps the
never-mutate-a-live-module discipline and the "leave the DB consistent on
failure" invariant from the CLI spec:

1. Fetch the artifact and verify its ``sha256`` — BEFORE any DB write. A
   mismatch aborts here, so nothing is deployed and no rule/provenance is
   written.
2. ``CREATE OR REPLACE`` the ``PREPROC_RT.<NAME>_V<N>`` script — now the version
   is present.
3. ``PREPROC.RULE_ENSURE`` — register the rule idempotently (the script exists,
   so the rule never points at a missing script; RULE_ENSURE also validates the
   script's existence itself).
4. ``PREPROC.MODULE_REGISTER`` — write the provenance row (the version is
   present, so there is never provenance for an absent version).
5. A single ``PREPROC.REFRESH`` (skippable, so a batch caller such as ``sync``
   issues exactly one refresh for the whole convergence).

A self-deployed module skips step 2 entirely — its own installer deployed the
backing script — and only ensures the rule and writes provenance.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from preproc.module.manifest import verify_artifact_script_name
from preproc.module.registry import (
    RegistryEntry,
    fetch_artifact_bytes,
    verify_entry_artifact,
)


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


def resolve_scope(entry: RegistryEntry, override: tuple[str, str] | None) -> tuple[str, str]:
    """Return the (scope_type, scope_value) to register the rule at.

    Uses the CLI ``--scope`` override when given, otherwise the module's
    ``suggested_scope``.
    """
    if override is not None:
        return override
    scope = entry.suggested_scope
    return scope["type"], scope["value"]


def strip_script_terminator(artifact_text: str) -> str:
    """Return the artifact CREATE with any interactive-client ``/`` line removed.

    The canonical artifact is a bare ``CREATE OR REPLACE LUA SCRIPT`` statement,
    but some authored ``.sql`` files ship a trailing lone ``/`` for direct
    EXAplus use; pyexasol rejects that terminator. The ``sha256`` is verified on
    the file's raw bytes before this strip, so removing the terminator here does
    not affect integrity checking.
    """
    lines = [line for line in artifact_text.splitlines() if line.strip() != "/"]
    return "\n".join(lines).strip()


def fetch_and_verify_artifact(entry: RegistryEntry, registry: str, *, reader=None) -> str:
    """Fetch a library-deployed entry's artifact, verify it, and return runnable text.

    Verifies both the ``sha256`` (integrity) and that the artifact is a single
    ``CREATE OR REPLACE LUA SCRIPT`` for the declared ``script_name`` (structure)
    before returning the terminator-stripped statement text ready to execute.

    Raises:
        RegistryError: if the artifact cannot be fetched or its sha256 mismatches.
        ArtifactError: if the artifact is not a single CREATE for ``script_name``.
    """
    artifact_bytes = fetch_artifact_bytes(entry, registry, reader=reader)
    verify_entry_artifact(entry, artifact_bytes)
    text = artifact_bytes.decode("utf-8")
    verify_artifact_script_name(entry.to_manifest(), text)
    return strip_script_terminator(text)


def deploy_artifact(connection: Any, script_text: str) -> None:
    """Execute a module artifact's CREATE OR REPLACE in the runtime schema.

    Opens ``PREPROC_RT`` first (Exasol needs an active schema context to compile
    a script), mirroring ``install.py``. The Lua body contains braces, so it is
    sent with no query parameters — pyexasol only formats when parameters are
    passed.
    """
    connection.execute("OPEN SCHEMA PREPROC_RT")
    connection.execute(script_text)


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
) -> bool:
    """Upsert the provenance row for ``entry`` via ``PREPROC.MODULE_REGISTER``.

    Returns whether the row was freshly created (True) or an existing row was
    updated (False). ``sha256`` is emitted as a SQL ``NULL`` literal for a
    self-deployed module (which carries none). ``source_url`` (the effective
    registry the artifact was fetched from) and ``library_version`` (the release
    the index declared) are recorded for audit and emitted as SQL ``NULL`` when
    absent.
    """
    sha_sql = "{sh}" if entry.sha256 is not None else "NULL"
    url_sql = "{su}" if source_url is not None else "NULL"
    libver_sql = "{lv}" if library_version is not None else "NULL"
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
    rows = connection.execute(
        "EXECUTE SCRIPT PREPROC.MODULE_REGISTER("
        "{n}, {v}, {src}, {rf}, "
        + sha_sql
        + ", {scn}, {fnn}, {ph}, {dm}, "
        + url_sql
        + ", "
        + libver_sql
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

    For a ``library-deployed`` module: fetch + verify the artifact, deploy the
    ``_V<N>`` script, ensure the rule, write provenance, and (optionally)
    refresh. For a ``self-deployed`` module: ensure the rule and write provenance
    only — the backing script is installed by the module's own project.

    Ordering guarantees the CLI's consistency invariant: the artifact is present
    before any rule or provenance names it, and a sha256 mismatch aborts before
    any DB write.

    Args:
        do_refresh: issue the single ``PREPROC.REFRESH`` at the end. A batch
            caller (``sync``) passes False and refreshes once for the whole run.
        library_version: the resolved library release (the index's
            ``library_version``), recorded as provenance alongside the effective
            ``registry`` source URL.
    """
    scope_type, scope_value = resolve_scope(entry, scope)

    artifact_deployed = False
    if entry.deploy_mode == "library-deployed":
        script_text = fetch_and_verify_artifact(entry, registry, reader=reader)
        deploy_artifact(connection, script_text)
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
        connection, entry, source_url=registry, library_version=library_version
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
    )
