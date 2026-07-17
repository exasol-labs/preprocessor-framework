"""Validate a module against the contract — statically, and live when a DB exists.

``preproc module validate <name>`` runs two layers:

* **Static** (always, no DB): the manifest already conformed when the registry
  index parsed it; validate additionally fetches the library-deployed artifact,
  confirms its ``sha256``, confirms it is a single ``CREATE OR REPLACE LUA
  SCRIPT`` for the declared ``script_name``, and — when ``luacheck`` is on PATH —
  lints the Lua body. Any failure raises ``ValidateError``.
* **Live** (only when a database is configured): deploys the artifact to a
  scratch script, imports it through a throwaway probe script, invokes the entry
  function with a no-op input, and asserts the return honours the phase contract
  (``nil`` or a string). The scratch and probe scripts are always dropped
  afterward, pass or fail.

Live validation applies only to library-deployed modules (a self-deployed
module's artifact is not carried through the registry, so there is nothing to
scratch-deploy).
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from preproc.module.deploy import strip_script_terminator
from preproc.module.manifest import ArtifactError, verify_artifact_script_name
from preproc.module.registry import (
    RegistryEntry,
    RegistryError,
    fetch_artifact_bytes,
    verify_entry_artifact,
)

_CREATE_NAME_RE = re.compile(
    r'(CREATE\s+OR\s+REPLACE\s+LUA\s+SCRIPT\s+)([A-Za-z_][A-Za-z0-9_."]*)',
    re.IGNORECASE,
)
_BODY_RE = re.compile(r"\bAS\b[ \t]*\r?\n(.*)", re.IGNORECASE | re.DOTALL)

_PHASE_RETURNS = ("nil", "string")
_SCRATCH_SUFFIX = "__VALIDATE"
_PROBE_SUFFIX = "__VALIDATE_PROBE"


class ValidateError(Exception):
    """Raised when a module fails a static or live validation check."""


def validate_static(
    entry: RegistryEntry, registry: str, *, reader=None, run_luacheck: bool = True
) -> list[str]:
    """Run the static checks and return the list of checks that passed.

    Raises:
        ValidateError: on a sha256 mismatch, an artifact/``script_name``
            mismatch, or a ``luacheck`` failure.
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
        verify_artifact_script_name(entry.to_manifest(), text)
    except ArtifactError as exc:
        raise ValidateError(f"artifact / script_name mismatch: {exc}") from exc
    passed.append("artifact is a single CREATE OR REPLACE for script_name")

    if run_luacheck:
        result = _run_luacheck(text, entry.function)
        if result is not None:
            passed.append(result)

    return passed


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


def _run_luacheck(artifact_text: str, function_name: str) -> str | None:
    """Lint the artifact's Lua body with ``luacheck`` when it is available.

    Lints against the preprocessor's global environment (the injected globals plus
    the module's own entry function, which the contract requires be a global), so a
    conformant module lints clean. Returns a pass description, or None when
    ``luacheck`` is not installed. Raises ``ValidateError`` when the linter reports
    problems.
    """
    if shutil.which("luacheck") is None:
        return None
    match = _BODY_RE.search(artifact_text)
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


def validate_live(connection: Any, entry: RegistryEntry, registry: str, *, reader=None) -> str:
    """Scratch-deploy the artifact, smoke-test the entry function, and tear it down.

    Deploys the artifact under a scratch name, imports it through a probe script,
    invokes the entry function with a no-op input, and asserts the returned Lua
    type honours the phase contract (``nil`` or ``string``). Both scratch objects
    are dropped in a ``finally``.

    Raises:
        ValidateError: if the return does not honour the phase contract.
    """
    if entry.deploy_mode != "library-deployed":
        return "self-deployed: no artifact to smoke-test"

    artifact_bytes = fetch_artifact_bytes(entry, registry, reader=reader)
    verify_entry_artifact(entry, artifact_bytes)
    text = strip_script_terminator(artifact_bytes.decode("utf-8"))

    scratch = entry.script_name + _SCRATCH_SUFFIX
    probe = entry.script_name + _PROBE_SUFFIX
    scratch_create = _CREATE_NAME_RE.sub(lambda m: m.group(1) + scratch, text, count=1)

    probe_body = (
        'import("' + scratch + '", "m")\n'
        "local out = m." + entry.function + '("SELECT 1")\n'
        'exit({{type(out)}}, "rettype VARCHAR(16)")\n'
    )
    probe_create = f"CREATE OR REPLACE LUA SCRIPT {probe}() RETURNS TABLE AS\n{probe_body}"

    try:
        connection.execute("OPEN SCHEMA PREPROC_RT")
        connection.execute(scratch_create)
        connection.execute(probe_create)
        rettype = connection.execute(f"EXECUTE SCRIPT {probe}").fetchall()[0][0]
        if rettype not in _PHASE_RETURNS:
            raise ValidateError(
                f"entry function returned Lua type {rettype!r}; the phase contract "
                f"requires one of {_PHASE_RETURNS}"
            )
        return f"live smoke: entry function honoured the phase contract (returned {rettype})"
    finally:
        for qualified in (probe, scratch):
            _drop_script_if_present(connection, qualified)


def _drop_script_if_present(connection: Any, qualified: str) -> None:
    schema, _, name = qualified.partition(".")
    exists = connection.execute(
        "SELECT COUNT(*) FROM EXA_ALL_SCRIPTS WHERE SCRIPT_SCHEMA = {s} AND SCRIPT_NAME = {n}",
        {"s": schema, "n": name},
    ).fetchval()
    if exists:
        connection.execute(f"DROP SCRIPT {qualified}")


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
