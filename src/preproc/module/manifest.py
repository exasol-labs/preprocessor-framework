"""Module contract: parse and validate ``module.toml`` manifests and artifacts.

This is the reference implementation of the contract documented in
docs/module-authoring.md. A module is `fn(text) -> nil|string` in a versioned
Lua script, plus a `module.toml` manifest describing the artifact and how it
is deployed. There is exactly ONE contract regardless of `deploy_mode` — that
field is deployment metadata, not a second contract.

Validation is fail-loud and collects every violation before raising, so an
author sees the whole list of problems in one pass rather than fixing them one
at a time (mirrors the style of ``src/preproc/connection.py``'s
``MissingEnvError``, which reports all missing env vars together).

Public API
----------
``parse_manifest(data)`` / ``load_manifest(path)``
    Parse and validate a manifest, returning a ``ModuleManifest``.
``verify_artifact_inventory(manifest, artifact_text)``
    Split the artifact, derive the object inventory it creates, and confirm it
    agrees with the manifest's declared ``[[objects]]`` in both directions.
    Returns the derived inventory.
``verify_artifact_sha256(manifest, artifact_bytes)``
    For a ``library-deployed`` module, confirm the artifact's bytes hash to
    the manifest's declared ``sha256``.
"""

from __future__ import annotations

import hashlib
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from preproc.module.artifact import (
    UNQUALIFIED_TYPES,
    ArtifactSyntaxError,
    derive_objects,
    normalize_object_name,
    qualified_parts,
    split_statements,
)

PHASES: tuple[str, ...] = ("TRANSLATE", "EXPAND", "REWRITE")
DEPLOY_MODES: tuple[str, ...] = ("library-deployed", "self-deployed")
SCOPE_TYPES: tuple[str, ...] = ("ROLE", "USER")

_REQUIRED_FIELDS = (
    "name",
    "description",
    "phase",
    "script_name",
    "function",
    "version",
    "min_framework",
    "suggested_scope",
    "deploy_mode",
)

_VERSION_SUFFIX_RE = re.compile(r"_V(\d+)$", re.IGNORECASE)
# The PREPROC.MODULES.objects column encodes each object as a 'TYPE|NAME'
# line; these are exactly the characters that would corrupt that encoding, so
# a declared type or name is rejected up front instead of ever being escaped.
_FORBIDDEN_OBJECT_CHARS: tuple[tuple[str, str], ...] = (
    ("\n", "a newline"),
    ("\r", "a carriage return"),
    ("|", "a pipe ('|')"),
)
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
# A module name must be a plain SQL-style identifier: a letter followed by
# letters, digits, or underscores. Hyphens are disallowed so the name can be
# used unquoted in the `PREPROC INSTALL MODULE <name>` sugar (an unquoted Exasol
# identifier cannot contain a hyphen), and it is matched case-insensitively.
_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")

# The entry script is always an EXECUTE-SCRIPT Lua script; a companion may be
# any object type the artifact's DDL creates.
ENTRY_OBJECT_TYPE = "LUA SCRIPT"


class ManifestError(Exception):
    """Raised when a ``module.toml`` fails contract validation.

    Attributes:
        errors: one human-readable message per violation found. All checks
            run before raising, so every problem is reported at once.
    """

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("Invalid module.toml: " + "; ".join(errors))


class ArtifactError(Exception):
    """Raised when a module's ``.sql`` artifact fails a structural or integrity check."""


@dataclass(frozen=True)
class ScopeSpec:
    """A suggested default rule scope: ``FOR ROLE|USER <value>``."""

    type: str
    value: str


@dataclass(frozen=True)
class SourceSpec:
    """Where a self-deployed module's own project lives, pinned to a ref."""

    repo: str
    ref: str


@dataclass(frozen=True)
class ObjectSpec:
    """One database object an artifact creates: its DDL type and its canonical name.

    ``type`` is the normalised DDL form (``LUA SCRIPT``,
    ``PYTHON3 SCALAR SCRIPT``, ``TABLE``, …) and ``name`` is the canonical
    fully-qualified name, so a declared entry and a parsed one compare directly.
    """

    type: str
    name: str


@dataclass(frozen=True)
class ModuleManifest:
    """A parsed, contract-validated ``module.toml``.

    Construct via ``parse_manifest`` or ``load_manifest`` — both run the full
    validation described in docs/module-authoring.md. There is no public
    constructor path that skips validation.
    """

    name: str
    description: str
    phase: str
    script_name: str
    function: str
    version: int
    min_framework: str
    suggested_scope: ScopeSpec
    deploy_mode: str
    sha256: str | None = None
    source: SourceSpec | None = None
    objects: tuple[ObjectSpec, ...] | None = None


def _missing_and_typed_fields(data: dict[str, Any], errors: list[str]) -> None:
    for field_name in _REQUIRED_FIELDS:
        if field_name not in data or data[field_name] in (None, ""):
            errors.append(f"missing required field '{field_name}'")


def _validate_name(data: dict[str, Any], errors: list[str]) -> None:
    name = data.get("name")
    if isinstance(name, str) and name and not _NAME_RE.match(name):
        errors.append(
            f"name {name!r} must be an identifier: a letter followed by letters, digits, "
            "or underscores (no hyphens), so it can be used unquoted in PREPROC INSTALL MODULE"
        )


def _validate_phase(data: dict[str, Any], errors: list[str]) -> None:
    phase = data.get("phase")
    if phase is not None and phase not in PHASES:
        errors.append(f"phase must be one of {', '.join(PHASES)}; got {phase!r}")


def _validate_deploy_mode(data: dict[str, Any], errors: list[str]) -> None:
    deploy_mode = data.get("deploy_mode")
    if deploy_mode is not None and deploy_mode not in DEPLOY_MODES:
        errors.append(f"deploy_mode must be one of {', '.join(DEPLOY_MODES)}; got {deploy_mode!r}")


def _validate_version_matches_script_name(data: dict[str, Any], errors: list[str]) -> None:
    version = data.get("version")
    script_name = data.get("script_name")
    if not isinstance(version, int) or not isinstance(script_name, str):
        return
    match = _VERSION_SUFFIX_RE.search(script_name)
    if match is None:
        errors.append(f"script_name {script_name!r} must end with a _V<N> version suffix")
        return
    suffix_version = int(match.group(1))
    if suffix_version != version:
        errors.append(
            f"version {version} does not match the _V{suffix_version} suffix "
            f"in script_name {script_name!r}"
        )


def _validate_suggested_scope(data: dict[str, Any], errors: list[str]) -> ScopeSpec | None:
    raw = data.get("suggested_scope")
    if not isinstance(raw, dict):
        return None
    scope_type = raw.get("type")
    scope_value = raw.get("value")
    if scope_type not in SCOPE_TYPES:
        errors.append(
            f"suggested_scope.type must be one of {', '.join(SCOPE_TYPES)}; got {scope_type!r}"
        )
    if not scope_value:
        errors.append("suggested_scope.value is required")
    if scope_type in SCOPE_TYPES and scope_value:
        return ScopeSpec(type=scope_type, value=scope_value)
    return None


def _validate_deploy_mode_fields(
    data: dict[str, Any], errors: list[str]
) -> tuple[str | None, SourceSpec | None]:
    """Enforce the deploy-mode field rules and return (sha256, source).

    library-deployed REQUIRES a well-formed sha256 hex digest.
    self-deployed REQUIRES a source table (repo + ref); sha256 is not
    required for self-deployed (per the contract, an artifact sha256 is not
    part of a self-deployed module's obligations) but is not rejected if
    present — it simply plays no role in verification for that mode.
    """
    deploy_mode = data.get("deploy_mode")
    sha256 = data.get("sha256")
    source_raw = data.get("source")

    if sha256 is not None and not (isinstance(sha256, str) and _SHA256_RE.match(sha256)):
        errors.append("sha256 must be a 64-character hex string")
        sha256 = None

    source: SourceSpec | None = None
    if source_raw is not None:
        if (
            not isinstance(source_raw, dict)
            or not source_raw.get("repo")
            or not source_raw.get("ref")
        ):
            errors.append("source must declare both 'repo' and 'ref'")
        else:
            source = SourceSpec(repo=source_raw["repo"], ref=source_raw["ref"])

    if deploy_mode == "library-deployed" and not sha256:
        errors.append(
            "library-deployed modules require a 'sha256' field naming the artifact's hex digest"
        )
    if deploy_mode == "self-deployed" and source is None:
        errors.append("self-deployed modules require a 'source' table with 'repo' and 'ref'")

    return sha256, source


def _delimiter_error(name: str, field: str, raw: str) -> str | None:
    """Return an error message if ``raw`` (the object's ``field``) carries a forbidden character.

    ``name`` identifies the offending object in the message even when
    ``name`` itself is the field that is invalid (there is no other handle to
    name it by).
    """
    for char, label in _FORBIDDEN_OBJECT_CHARS:
        if char in raw:
            return (
                f"object {name!r} {field} {raw!r} must not contain {label}: the "
                "PREPROC.MODULES.objects column encodes each object as a "
                "'TYPE|NAME' line, and a delimiter in a declared type or name "
                "would corrupt that encoding"
            )
    return None


def _validate_objects(data: dict[str, Any], errors: list[str]) -> tuple[ObjectSpec, ...] | None:
    """Validate the optional ``[[objects]]`` inventory and return it normalised.

    Returns None when the key is absent — which is legal only for a
    single-statement artifact, a condition ``verify_artifact_inventory``
    enforces because it needs the artifact to count its statements.
    """
    raw = data.get("objects")
    if raw is None:
        return None
    if not isinstance(raw, list) or not raw:
        errors.append("objects must be a non-empty array of tables, each with 'type' and 'name'")
        return None

    version = data.get("version")
    expected_suffix = f"_V{version}" if isinstance(version, int) else "_V<N>"
    specs: list[ObjectSpec] = []
    seen: set[str] = set()

    for index, item in enumerate(raw):
        if not isinstance(item, dict) or not item.get("type") or not item.get("name"):
            errors.append(f"objects[{index}] must declare both 'type' and 'name'")
            continue

        raw_type = str(item["type"])
        raw_name = str(item["name"])
        delimiter_error = _delimiter_error(raw_name, "type", raw_type) or _delimiter_error(
            raw_name, "name", raw_name
        )
        if delimiter_error is not None:
            errors.append(delimiter_error)
            continue

        object_type = " ".join(raw_type.upper().split())
        try:
            parts = qualified_parts(raw_name)
        except ArtifactSyntaxError as exc:
            errors.append(f"objects[{index}]: {exc}")
            continue
        name = ".".join(parts)

        if object_type in UNQUALIFIED_TYPES:
            if len(parts) != 1:
                errors.append(
                    f"object {name!r} is a {object_type} and must be a single "
                    "unqualified identifier"
                )
                continue
        elif len(parts) != 2:
            errors.append(
                f"object {name!r} must be fully qualified as <schema>.<object> so the "
                "inventory never depends on the session's open schema"
            )
            continue
        elif "." in parts[0]:
            errors.append(
                f"object {name!r} has a quoted schema part {parts[0]!r} containing a '.': "
                "deploy.py and module_reclaim.lua re-split the canonical <schema>.<object> "
                "name on its FIRST dot only, so a dot inside the schema part (unlike one in "
                "the object part, which the tail absorbs) would be misparsed as the "
                "schema/object separator"
            )
            continue

        suffix = _VERSION_SUFFIX_RE.search(parts[-1])
        if suffix is None or (isinstance(version, int) and int(suffix.group(1)) != version):
            errors.append(
                f"object {name!r} must carry the module's {expected_suffix} version suffix; "
                "EVERY object an artifact creates carries it, not only the entry script, so "
                "a v1 and a v2 generation coexist without collisions"
            )
            continue
        if name in seen:
            errors.append(f"object {name!r} is declared more than once in objects")
            continue
        seen.add(name)
        specs.append(ObjectSpec(type=object_type, name=name))

    return tuple(specs)


def parse_manifest(data: dict[str, Any]) -> ModuleManifest:
    """Validate a decoded ``module.toml`` mapping against the module contract.

    Raises:
        ManifestError: with every violation found, if the mapping does not
            satisfy the contract (missing fields, unknown phase, version/
            script_name suffix mismatch, a malformed ``[[objects]]`` entry, or a
            deploy-mode field rule).
    """
    errors: list[str] = []
    _missing_and_typed_fields(data, errors)
    _validate_name(data, errors)
    _validate_phase(data, errors)
    _validate_deploy_mode(data, errors)
    _validate_version_matches_script_name(data, errors)
    scope = _validate_suggested_scope(data, errors)
    sha256, source = _validate_deploy_mode_fields(data, errors)
    objects = _validate_objects(data, errors)

    if errors:
        raise ManifestError(errors)

    return ModuleManifest(
        name=data["name"],
        description=data["description"],
        phase=data["phase"],
        script_name=data["script_name"],
        function=data["function"],
        version=data["version"],
        min_framework=data["min_framework"],
        suggested_scope=scope,  # type: ignore[arg-type]
        deploy_mode=data["deploy_mode"],
        sha256=sha256,
        source=source,
        objects=objects,
    )


def load_manifest(path: Path) -> ModuleManifest:
    """Read and validate a ``module.toml`` file from disk.

    Raises:
        ManifestError: if the manifest does not satisfy the module contract.
        FileNotFoundError: if ``path`` does not exist.
    """
    with path.open("rb") as handle:
        data = tomllib.load(handle)
    return parse_manifest(data)


def _entry_script_errors(entry_name: str, derived: list[ObjectSpec], script_name: str) -> list[str]:
    """Confirm exactly one derived object is the entry Lua script the manifest names."""
    matches = [obj for obj in derived if obj.name == entry_name]
    if not matches:
        return [
            f"artifact creates no {ENTRY_OBJECT_TYPE} named {entry_name} for the manifest "
            f"script_name {script_name!r}"
        ]
    if matches[0].type != ENTRY_OBJECT_TYPE:
        return [
            f"artifact object {entry_name} is a {matches[0].type}, but the entry script named "
            f"by script_name must be a {ENTRY_OBJECT_TYPE}"
        ]
    return []


def _inventory_errors(declared: list[ObjectSpec], derived: list[ObjectSpec]) -> list[str]:
    """Compare the declared and parsed inventories in BOTH directions, naming each offender."""
    declared_keys = {(obj.type, obj.name) for obj in declared}
    derived_keys = {(obj.type, obj.name) for obj in derived}
    errors = [
        f"artifact creates {obj.type} {obj.name} but module.toml does not declare it"
        for obj in derived
        if (obj.type, obj.name) not in declared_keys
    ]
    errors.extend(
        f"module.toml declares {obj.type} {obj.name} but the artifact does not create it"
        for obj in declared
        if (obj.type, obj.name) not in derived_keys
    )
    return errors


def _duplicate_object_names(derived: list[ObjectSpec]) -> list[str]:
    seen: set[str] = set()
    duplicates: list[str] = []
    for obj in derived:
        if obj.name in seen and obj.name not in duplicates:
            duplicates.append(obj.name)
        seen.add(obj.name)
    return duplicates


def verify_artifact_inventory(manifest: ModuleManifest, artifact_text: str) -> list[ObjectSpec]:
    """Confirm the artifact's object inventory agrees with the manifest, and return it.

    The artifact is split into its statements, the object each statement creates
    is derived independently of the manifest, and the derived inventory is
    compared with the declared ``[[objects]]`` in BOTH directions — the same
    accept/reject discipline already applied to ``sha256``, which is what makes
    drift between manifest and artifact impossible.

    ``[[objects]]`` may be omitted only when the artifact carries exactly one
    statement, in which case the inventory is the single entry named by
    ``script_name``. That is precisely the pre-composite check, so existing
    single-statement manifests stay valid unedited.

    Returns:
        The derived inventory, in file order — the order an installer creates
        the objects in, and the reverse of the order reclaim drops them in.

    Raises:
        ArtifactError: if the artifact cannot be split, creates an object twice,
            carries more than one statement with no ``[[objects]]`` declared,
            does not create the entry script the manifest names, or its
            inventory disagrees with the declaration in either direction.
    """
    try:
        statements = split_statements(artifact_text)
        derived = [
            ObjectSpec(type=item["type"], name=item["name"]) for item in derive_objects(statements)
        ]
        entry_name = normalize_object_name(manifest.script_name)
    except ArtifactSyntaxError as exc:
        raise ArtifactError(str(exc)) from exc

    if not statements:
        raise ArtifactError("artifact carries no statements")

    duplicates = _duplicate_object_names(derived)
    if duplicates:
        raise ArtifactError(
            "artifact creates the same object more than once: " + ", ".join(duplicates)
        )

    if manifest.objects is not None:
        declared = list(manifest.objects)
    elif len(statements) == 1:
        declared = [ObjectSpec(type=ENTRY_OBJECT_TYPE, name=entry_name)]
    else:
        raise ArtifactError(
            "[[objects]] is REQUIRED once the artifact carries more than one statement; "
            f"this artifact carries {len(statements)} statements"
        )

    errors = _entry_script_errors(entry_name, derived, manifest.script_name)
    errors.extend(_inventory_errors(declared, derived))
    if errors:
        raise ArtifactError("; ".join(errors))
    return derived


def verify_artifact_sha256(manifest: ModuleManifest, artifact_bytes: bytes) -> None:
    """Verify a library-deployed artifact's bytes hash to the manifest's declared sha256.

    A no-op when ``manifest.sha256`` is not set (self-deployed modules do not
    carry a bundled artifact to verify).

    Raises:
        ArtifactError: if the artifact's sha256 digest does not match
            ``manifest.sha256``.
    """
    if manifest.sha256 is None:
        return
    digest = hashlib.sha256(artifact_bytes).hexdigest()
    if digest != manifest.sha256.lower():
        raise ArtifactError(
            f"sha256 mismatch: manifest declares {manifest.sha256}, artifact hashes to {digest}"
        )
