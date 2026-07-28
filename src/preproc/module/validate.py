"""Validate a module against the contract — statically, and live when a DB exists.

``preproc module validate <name>`` runs two layers:

* **Static** (always, no DB): the manifest already conformed when the registry
  index parsed it; validate additionally fetches the library-deployed artifact,
  confirms its ``sha256``, pre-flights it exactly like ``add`` does
  (``plan_artifact``: split into statements, derive the object inventory, and
  confirm it agrees with the declared ``[[objects]]`` in both directions), and
  — when ``luacheck`` is on PATH — lints the entry script's Lua body. Any
  failure raises ``ValidateError``.
* **Live** (only when a database is configured): compiles the artifact's SCRIPT
  statements in a throwaway schema and smoke-tests the entry function through a
  real ``import``. See ``validate_live`` for exactly what that does and does not
  cover.

Live validation applies only to library-deployed modules (a self-deployed
module's artifact is not carried through the registry, so there is nothing to
scratch-deploy).

Why live validation is deliberately narrow (ADR-059 territory)
--------------------------------------------------------------
A composite artifact's statements reference each other BY NAME — a ``VIEW``
selecting from a companion table, a Lua ``import`` of a companion script — and
those names are the only names under which the references resolve. That leaves
no sound way to smoke-test arbitrary artifact DDL:

* Executing a statement as written creates the module's REAL objects in a live
  schema. ``validate`` is not ``add``: it must never do that, and it must never
  drop what an installed generation already owns.
* Renaming each statement's own creation site leaves every cross-reference
  pointing at the real object, so a conformant composite fails validation on a
  clean database and reads live data on an installed one.
* Rewriting every occurrence of every declared name is string surgery on SQL,
  Lua, and Python bodies: it mangles a name that appears in a string literal or
  a comment, and cannot see one assembled at runtime.
* A throwaway scratch SCHEMA does not help either, because the contract
  requires every name to be fully qualified — the artifact names its schema,
  and an artifact may legitimately create one.

So the live layer promises less and keeps its promise: it executes NOTHING the
artifact wrote, only statements this module composes, all of them inside one
disposable schema it created and drops. What that buys, and what it gives up,
is spelled out on ``validate_live``.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from preproc.module.artifact import ArtifactSyntaxError, derive_object, normalize_object_name
from preproc.module.deploy import (
    ArtifactPlan,
    ArtifactStatement,
    drop_objects,
    plan_artifact,
    quote_qualified,
)
from preproc.module.manifest import ENTRY_OBJECT_TYPE, ArtifactError, ObjectSpec
from preproc.module.registry import (
    RegistryEntry,
    RegistryError,
    fetch_artifact_bytes,
    verify_entry_artifact,
)

_BODY_RE = re.compile(r"\bAS\b[ \t]*\r?\n(.*)", re.IGNORECASE | re.DOTALL)
# One possibly-quoted, possibly-qualified SQL name, as a span in a statement.
# Deliberately NOT imported from artifact.py: that file is embedded verbatim in
# the resolver UDF under a byte-identity check, so nothing outside it may depend
# on its internals. Nothing rests on this pattern being exactly the canonical
# grammar either — every span it finds is re-derived through the public
# ``derive_object`` before the statement is allowed anywhere near a database.
_NAME_SPAN = r'(?:"(?:[^"]|"")*"|[A-Za-z_][A-Za-z0-9_]*)'
_NAME_SPAN_RE = re.compile(_NAME_SPAN + r"(?:\." + _NAME_SPAN + r")*")

_PHASE_RETURNS = ("nil", "string")
_SCRATCH_SCHEMA_PREFIX = "PREPROC_VALIDATE_"
_SCRATCH_SCHEMA_ENTROPY_CHARS = 12
_SCRATCH_SCHEMA_TYPE = "SCHEMA"
# Fixed local names inside the disposable schema. Neither can collide with a
# module's own object: every declared object must carry a ``_V<N>`` suffix, and
# neither of these does.
_SCRATCH_ENTRY_NAME = "MODULE_ENTRY"
_SCRATCH_PROBE_NAME = "MODULE_PROBE"
_SCRIPT_TYPE_SUFFIX = "SCRIPT"
_COMPILE_CONTEXT_SCHEMA = "PREPROC_RT"
_NOOP_INPUT = "SELECT 1"
_TERMINATOR_CHARS = " \n;"


class ValidateError(Exception):
    """Raised when a module fails a static or live validation check."""


def validate_static(
    entry: RegistryEntry, registry: str, *, reader=None, run_luacheck: bool = True
) -> list[str]:
    """Run the static checks and return the list of checks that passed.

    Raises:
        ValidateError: on a sha256 mismatch, an artifact/inventory mismatch (in
            either direction — an undeclared object or a declared one the
            artifact never creates), or a ``luacheck`` failure.
    """
    passed: list[str] = ["manifest conforms to the contract"]

    if entry.deploy_mode != "library-deployed":
        passed.append("self-deployed: no artifact to verify")
        return passed

    try:
        artifact_bytes = fetch_artifact_bytes(entry, registry, reader=reader)
    except RegistryError as exc:
        raise ValidateError(str(exc)) from exc

    try:
        verify_entry_artifact(entry, artifact_bytes)
    except RegistryError as exc:
        raise ValidateError(f"sha256 mismatch: {exc}") from exc
    passed.append("artifact sha256 matches the manifest")

    text = artifact_bytes.decode("utf-8")
    try:
        plan = plan_artifact(entry.to_manifest(), text)
    except ArtifactError as exc:
        raise ValidateError(f"artifact / inventory mismatch: {exc}") from exc
    passed.append(
        f"artifact inventory matches the manifest ({len(plan.objects)} "
        f"object{'' if len(plan.objects) == 1 else 's'})"
    )

    if run_luacheck:
        entry_statement = _entry_statement(plan, normalize_object_name(entry.script_name))
        if entry_statement is not None:
            result = _run_luacheck(entry_statement.sql, entry.function)
            if result is not None:
                passed.append(result)

    return passed


def _entry_statement(plan: ArtifactPlan, entry_name: str) -> ArtifactStatement | None:
    """The plan statement that creates the entry script, or None (should not happen post-plan)."""
    for statement in plan.statements:
        if statement.creates is not None and statement.creates.name == entry_name:
            return statement
    return None


# luacheck config for a module body, mirroring the framework's own .luacheckrc:
# the Exasol preprocessor injects these globals, and a module MUST define its
# entry function as a global for import() to bind it — so both must be declared
# or luacheck flags every conformant module. Embedded SQL makes lines long, so
# line-length is disabled, and 212 (unused argument) is Exasol-script noise.
_LUACHECKRC_TEMPLATE = """\
std = "lua51"
max_line_length = false
read_globals = {{
    "sqlparsing", "pquery", "query", "import", "output", "exit", "exa", "null",
}}
globals = {{ "preproc_module", "{function_name}" }}
ignore = {{ "212" }}
"""


def _run_luacheck(entry_statement_sql: str, function_name: str) -> str | None:
    """Lint the entry script's Lua body with ``luacheck`` when it is available.

    ``entry_statement_sql`` is the entry ``CREATE OR REPLACE LUA SCRIPT``
    statement ALONE (never the whole composite artifact text) — a composite
    module's other statements may be Python UDFs or plain DDL, which are not
    Lua source and would corrupt the linted body if the whole file were fed in.

    Lints against the preprocessor's global environment (the injected globals plus
    the module's own entry function, which the contract requires be a global), so a
    conformant module lints clean. Returns a pass description, or None when
    ``luacheck`` is not installed. Raises ``ValidateError`` when the linter reports
    problems.
    """
    if shutil.which("luacheck") is None:
        return None
    match = _BODY_RE.search(entry_statement_sql)
    if match is None:
        return None
    body = match.group(1)

    with tempfile.TemporaryDirectory() as tmp:
        lua_path = Path(tmp) / "module.lua"
        lua_path.write_text(body, encoding="utf-8")
        rc_path = Path(tmp) / ".luacheckrc"
        rc_path.write_text(
            _LUACHECKRC_TEMPLATE.format(function_name=function_name), encoding="utf-8"
        )
        completed = subprocess.run(
            ["luacheck", "--no-color", "--config", str(rc_path), str(lua_path)],
            capture_output=True,
            text=True,
            check=False,
        )
    if completed.returncode != 0:
        raise ValidateError(f"luacheck reported problems:\n{completed.stdout}{completed.stderr}")
    return "luacheck clean"


@dataclass(frozen=True)
class _ScratchScript:
    """One artifact script statement, rewritten to create a disposable copy of itself.

    ``declared`` is the object the artifact statement creates and is what an
    operator must be told about; ``creates`` is the disposable object inside the
    validation schema that the rewritten ``sql`` actually creates.
    """

    sql: str
    declared: ObjectSpec
    creates: ObjectSpec


def _scratch_schema_name() -> str:
    """A fresh disposable schema name, unqualified and unquoted-safe by construction."""
    return _SCRATCH_SCHEMA_PREFIX + uuid.uuid4().hex[:_SCRATCH_SCHEMA_ENTROPY_CHARS].upper()


def _created_object(statement: str) -> ObjectSpec | None:
    """The object a statement creates per the canonical grammar, or None if it reads none."""
    try:
        derived = derive_object(statement)
    except ArtifactSyntaxError:
        return None
    if derived is None:
        return None
    return ObjectSpec(type=derived["type"], name=derived["name"])


def _relocated(obj: ObjectSpec, schema: str, identifier: str | None = None) -> ObjectSpec:
    """The same object type, in the disposable schema, keeping (or replacing) its identifier.

    Only ever applied to a schema-qualified object. The canonical name is split
    on its FIRST dot only — ``quote_qualified``'s own convention — because a
    canonical name is not an identifier chain: a quoted object part may carry a
    dot of its own, which the tail absorbs.
    """
    _, _, own = obj.name.partition(".")
    return ObjectSpec(type=obj.type, name=f"{schema}.{identifier if identifier else own}")


def _scratch_statement(sql: str, declared: ObjectSpec, scratch: ObjectSpec) -> str | None:
    """``sql`` rewritten to create ``scratch`` instead of ``declared``, or None.

    The one name span that canonicalises to the declared object's name is
    replaced by the freshly quoted disposable name — replaced WHOLE, never
    suffixed, so an author's quoted identifier keeps its quotes and stays a
    name at all.

    The verification, not the search, is what makes this safe: a candidate is
    accepted only when the canonical grammar re-derives EXACTLY the disposable
    object from the rewritten text. A span mislocated (a name mentioned in a
    leading comment) or a shape this search does not anticipate therefore yields
    no candidate at all, and the statement is left unexecuted rather than sent
    to a database in some half-rewritten form.
    """
    replacement = quote_qualified(scratch)
    for match in _NAME_SPAN_RE.finditer(sql):
        if normalize_object_name(match.group()) != declared.name:
            continue
        candidate = sql[: match.start()] + replacement + sql[match.end() :]
        if _created_object(candidate) == scratch:
            return candidate
    return None


def _scratch_scripts(
    plan: ArtifactPlan, entry_name: str, schema: str
) -> tuple[_ScratchScript, ...]:
    """Every SCRIPT statement of the artifact, rewritten into the disposable schema.

    SCRIPT statements and ONLY SCRIPT statements are eligible, because a script
    body is opaque to the database at creation time: Exasol resolves an
    ``import`` or an embedded query when the script RUNS, so a disposable copy
    of a script cannot fail merely because a companion it names is absent. Every
    other statement is left out on purpose:

    * ``TABLE``/``VIEW``/``FUNCTION`` resolve their references AT creation, so a
      disposable copy of one that references a companion would fail spuriously.
    * ``SCHEMA``/``VIRTUAL SCHEMA``/``CONNECTION`` have no schema part to
      redirect — a copy would land in a live namespace.
    * A statement that creates nothing (``GRANT``, ``INSERT``, ``COMMIT``) would
      write to whatever live object it names.

    The entry script is relocated under a fixed local name so the probe's
    ``import`` string is a plain identifier whatever the author called it.
    """
    scripts = []
    for statement in plan.statements:
        declared = statement.creates
        if declared is None or not declared.type.endswith(_SCRIPT_TYPE_SUFFIX):
            continue
        identifier = _SCRATCH_ENTRY_NAME if declared.name == entry_name else None
        scratch = _relocated(declared, schema, identifier)
        rewritten = _scratch_statement(statement.sql, declared, scratch)
        if rewritten is not None:
            scripts.append(_ScratchScript(sql=_wire(rewritten), declared=declared, creates=scratch))
    return tuple(scripts)


def _wire(statement: str) -> str:
    """Strip any trailing terminator so a statement suits the single-statement API.

    Mirrors ``deploy._wire_form``: the splitter preserves a block body
    verbatim, which may carry a trailing ``;`` its own syntax needs, so the
    terminator is removed HERE, never before this point.
    """
    return statement.rstrip(_TERMINATOR_CHARS)


def _probe_statement(probe: ObjectSpec, scratch_entry: ObjectSpec, function: str) -> str:
    """The throwaway script that imports the disposable entry copy and reports a Lua type.

    Mirrors ``master.lua``'s dispatch exactly — ``import(name, "preproc_module")``
    then an index by function name — so what is smoke-tested is the binding the
    preprocessor actually performs. ``{{`` … ``}}`` is a one-row, one-column
    result table for ``exit``, not a format placeholder: no query parameters are
    passed, so the driver never formats the text.
    """
    body = (
        'import("' + scratch_entry.name + '", "preproc_module")\n'
        'local out = preproc_module["' + function + '"]("' + _NOOP_INPUT + '")\n'
        'exit({{type(out)}}, "rettype VARCHAR(16)")\n'
    )
    return f"CREATE OR REPLACE LUA SCRIPT {quote_qualified(probe)}() RETURNS TABLE AS\n{body}"


def _create_scratch_schema(connection: Any, schema: ObjectSpec) -> None:
    """Create the disposable validation schema.

    Deliberately not ``IF NOT EXISTS``: the name carries fresh entropy, so a
    collision means the schema is someone else's, and validate must never adopt
    — and then drop — a schema it did not create.

    Raises:
        ValidateError: if the schema cannot be created, stating that the module
            itself was not judged.
    """
    try:
        connection.execute(f"CREATE SCHEMA {quote_qualified(schema)}")
    except Exception as error:
        raise ValidateError(
            f"live validation could not create its disposable schema {schema.name}: {error}. "
            "The module was NOT judged — live validation needs the privilege to create a "
            "throwaway schema, because it refuses to smoke-test inside a live one."
        ) from error


def _compile_scratch_script(connection: Any, script: _ScratchScript) -> None:
    """Create one disposable script copy.

    Raises:
        ValidateError: if the database rejects it. A script's creation never
            depends on a companion, so a rejection here is the artifact's own
            defect and ``add`` would hit it too.
    """
    try:
        connection.execute(script.sql)
    except Exception as error:
        raise ValidateError(
            f"{script.declared.type} {script.declared.name} was rejected by the database: {error}"
        ) from error


def _coverage(plan: ArtifactPlan, scripts: tuple[_ScratchScript, ...]) -> str:
    """What the disposable deploy covered and what it deliberately left alone."""
    compiled = len(scripts)
    skipped = len(plan.statements) - compiled
    text = f"{compiled} script statement{_plural(compiled)} compiled in a throwaway schema"
    if skipped:
        text += f", {skipped} non-script statement{_plural(skipped)} not deployed"
    return text


def _plural(count: int) -> str:
    return "" if count == 1 else "s"


def validate_live(connection: Any, entry: RegistryEntry, registry: str, *, reader=None) -> str:
    """Compile the artifact's scripts in a disposable schema and smoke-test the entry function.

    What this GUARANTEES:

    * Every statement it sends is one this module composed. No statement of the
      artifact is executed as written, so no object of the module — and nothing
      in any schema the artifact names — is ever created, replaced, or dropped.
    * Everything it creates lives in one disposable schema
      (``PREPROC_VALIDATE_<entropy>``) created for this run, and the undo list
      is built BEFORE each statement, so the ``finally`` tears down the whole
      namespace even when a statement failed halfway. ``drop_objects`` walks it
      in reverse and never raises, so teardown can never mask the real error.
    * Every SCRIPT statement of the artifact is compiled by the real database
      under a disposable name — catching a Lua syntax error, a bad UDF
      signature, or an unregistered script language. A rejection is a failure.
    * The entry function is invoked through the same ``import`` binding
      ``master.lua`` uses, and its returned Lua type must honour the phase
      contract (``nil`` or a string). A forbidden return type is a failure
      whatever the artifact's shape.

    What it deliberately does NOT cover:

    * Non-script statements (``TABLE``, ``VIEW``, ``FUNCTION``, ``SCHEMA``,
      ``CONNECTION``) and statements that create nothing are not executed at
      all — see ``_scratch_scripts`` for why each is unsound to copy. Their
      conformance rests on the static layer: the split, the declared-vs-derived
      inventory, and the type/qualification rules.
    * For a composite artifact, an invocation that ERRORS is reported as
      inconclusive rather than as a failure: the entry function may legitimately
      reach for a companion that exists only under its real name, which this
      path refuses to create. Only for a single-statement artifact — where the
      disposable deploy reproduces the whole artifact — is an invocation error
      the module's own and reported as a failure.

    Returns:
        A description of what was checked, for the operator's ``ok:`` line.

    Raises:
        ValidateError: if a script statement is rejected, if the entry function
            returns a type the phase contract forbids, if a single-statement
            artifact's entry function cannot be invoked, or if the disposable
            schema cannot be created.
    """
    if entry.deploy_mode != "library-deployed":
        return "self-deployed: no artifact to smoke-test"

    artifact_bytes = fetch_artifact_bytes(entry, registry, reader=reader)
    verify_entry_artifact(entry, artifact_bytes)
    plan = plan_artifact(entry.to_manifest(), artifact_bytes.decode("utf-8"))

    schema = _scratch_schema_name()
    scripts = _scratch_scripts(plan, normalize_object_name(entry.script_name), schema)
    scratch_entry = ObjectSpec(type=ENTRY_OBJECT_TYPE, name=f"{schema}.{_SCRATCH_ENTRY_NAME}")
    if all(script.creates != scratch_entry for script in scripts):
        return (
            "live smoke skipped: the entry script's creation target could not be redirected "
            "into a throwaway schema, so nothing was executed"
        )

    schema_object = ObjectSpec(type=_SCRATCH_SCHEMA_TYPE, name=schema)
    probe = ObjectSpec(type=ENTRY_OBJECT_TYPE, name=f"{schema}.{_SCRATCH_PROBE_NAME}")
    _create_scratch_schema(connection, schema_object)
    created = [schema_object]
    try:
        # Exasol opens a freshly created schema implicitly, so this moves the
        # session back off the one teardown has to drop, and gives scripts the
        # same compilation context install.py and deploy.py use. Every name
        # below is fully qualified, so the open schema is context only.
        connection.execute(f"OPEN SCHEMA {_COMPILE_CONTEXT_SCHEMA}")
        for script in scripts:
            created.append(script.creates)
            _compile_scratch_script(connection, script)
        created.append(probe)
        connection.execute(_wire(_probe_statement(probe, scratch_entry, entry.function)))
        return _smoke(connection, probe, plan, scripts)
    finally:
        drop_objects(connection, tuple(created))


def _smoke(
    connection: Any,
    probe: ObjectSpec,
    plan: ArtifactPlan,
    scripts: tuple[_ScratchScript, ...],
) -> str:
    """Invoke the entry function through the probe and judge the phase contract.

    Raises:
        ValidateError: on a forbidden return type, or on an invocation error
            that the artifact's shape makes conclusive.
    """
    coverage = _coverage(plan, scripts)
    try:
        rettype = connection.execute(f"EXECUTE SCRIPT {quote_qualified(probe)}").fetchall()[0][0]
    except Exception as error:
        if len(plan.statements) == 1:
            raise ValidateError(
                f"entry function could not be invoked: {error}. The artifact is a single "
                "statement, so the throwaway deploy reproduced all of it and nothing was "
                "missing from the module's own environment."
            ) from error
        return (
            f"live smoke: {coverage}; entry function not conclusively invocable — {error} "
            "(a composite module's companions exist only under their real names, which "
            "validation never creates)"
        )
    if rettype not in _PHASE_RETURNS:
        raise ValidateError(
            f"entry function returned Lua type {rettype!r}; the phase contract "
            f"requires one of {_PHASE_RETURNS}"
        )
    return (
        f"live smoke: {coverage}; entry function honoured the phase contract (returned {rettype})"
    )


def validate_module(
    entry: RegistryEntry,
    registry: str,
    *,
    connection: Any | None = None,
    reader=None,
) -> list[str]:
    """Run static validation always, then live validation when a connection is given."""
    results = validate_static(entry, registry, reader=reader)
    if connection is not None:
        results.append(validate_live(connection, entry, registry, reader=reader))
    return results
