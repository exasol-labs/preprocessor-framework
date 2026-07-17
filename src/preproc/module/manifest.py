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
``verify_artifact_script_name(manifest, artifact_text)``
    Confirm the artifact is a single ``CREATE OR REPLACE LUA SCRIPT``
    statement whose script name matches the manifest.
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
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_CREATE_SCRIPT_RE = re.compile(
    r"CREATE\s+OR\s+REPLACE\s+LUA\s+SCRIPT\s+([A-Za-z_][A-Za-z0-9_.\"]*)",
    re.IGNORECASE,
)


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


def _missing_and_typed_fields(data: dict[str, Any], errors: list[str]) -> None:
    for field_name in _REQUIRED_FIELDS:
        if field_name not in data or data[field_name] in (None, ""):
            errors.append(f"missing required field '{field_name}'")


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


def parse_manifest(data: dict[str, Any]) -> ModuleManifest:
    """Validate a decoded ``module.toml`` mapping against the module contract.

    Raises:
        ManifestError: with every violation found, if the mapping does not
            satisfy the contract (missing fields, unknown phase, version/
            script_name suffix mismatch, or a deploy-mode field rule).
    """
    errors: list[str] = []
    _missing_and_typed_fields(data, errors)
    _validate_phase(data, errors)
    _validate_deploy_mode(data, errors)
    _validate_version_matches_script_name(data, errors)
    scope = _validate_suggested_scope(data, errors)
    sha256, source = _validate_deploy_mode_fields(data, errors)

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


def _extract_script_names(artifact_text: str) -> list[str]:
    """Return every script name introduced by a CREATE OR REPLACE LUA SCRIPT statement."""
    return _CREATE_SCRIPT_RE.findall(artifact_text)


def verify_artifact_script_name(manifest: ModuleManifest, artifact_text: str) -> None:
    """Confirm the artifact is a single self-contained CREATE OR REPLACE statement.

    The canonical module artifact is exactly one
    ``CREATE OR REPLACE LUA SCRIPT <script_name> AS <body>`` statement; see
    docs/module-authoring.md § The canonical artifact.

    Raises:
        ArtifactError: if the artifact contains zero or more than one
            ``CREATE OR REPLACE LUA SCRIPT`` statement, or if the statement's
            script name does not match ``manifest.script_name``.
    """
    names = _extract_script_names(artifact_text)
    if len(names) != 1:
        raise ArtifactError(
            "artifact must contain exactly one CREATE OR REPLACE LUA SCRIPT "
            f"statement; found {len(names)}"
        )
    actual = names[0]
    if actual.upper() != manifest.script_name.upper():
        raise ArtifactError(
            f"artifact script name {actual!r} does not match manifest "
            f"script_name {manifest.script_name!r}"
        )


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
