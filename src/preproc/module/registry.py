"""Federated module registry: read, generate, and verify ``registry/index.json``.

The registry is a generated JSON index over every discoverable module: modules
hosted in the central library repo (auto-indexed by CI from each
``modules/<name>/module.toml``) and modules hosted in external repos (added by
a curated ``registry/external/<name>.toml`` block, one per human-reviewed PR).
The operator's CLI is the only reader — the database never sees the registry.

Index schema (``registry/index.json``)
---------------------------------------
::

    {
      "schema_version": 1,
      "modules": [
        {
          "name": "cast-shorthand",
          "version": 1,
          "phase": "TRANSLATE",
          "script_name": "PREPROC_RT.CAST_SHORTHAND_V1",
          "function": "cast_shorthand",
          "description": "...",
          "min_framework": "1.0.0",
          "suggested_scope": {"type": "ROLE", "value": "PREPROC_ADMIN"},
          "deploy_mode": "library-deployed",
          "sha256": "<64 hex chars>",
          "artifact_path": "modules/cast-shorthand/cast-shorthand_v1.sql",
          "source": {"repo": "exasol-labs/preprocessor-library", "ref": "main"}
        }
      ]
    }

``modules`` is sorted by ``(name, version)`` so the file is byte-stable across
regenerations of the same inputs. ``sha256`` and ``artifact_path`` are ``null``
for ``self-deployed`` entries (no artifact is fetched through the registry for
those — the module's own installer deploys it; the CLI only registers the
rule). ``artifact_path`` is the artifact's path relative to its own
``source.repo`` checkout, following the standard module layout
(``modules/<name>/<name>_v<N>.sql``, see docs/module-authoring.md).

Public API
----------
``generate_index(library_root, repo=..., ref=...)``
    Deterministically build a ``RegistryIndex`` from a library checkout: every
    ``modules/*/module.toml`` (auto-indexed, sha256-checked against its local
    artifact) plus every ``registry/external/*.toml`` (curated, pinned).
``render_index(index)``
    Serialize a ``RegistryIndex`` to the canonical JSON text CI commits.
``index_drift(library_root, repo=..., ref=...)``
    Compare a freshly generated index against the committed
    ``registry/index.json``; returns drift descriptions (empty means in sync).
``read_registry(registry, reader=None)``
    Read the federated index either from a local checkout path or, for an
    ``https://`` URL, online — with an injectable ``reader`` so callers (and
    tests) never need real network access.
``verify_entry_artifact(entry, artifact_bytes)``
    Verify a fetched library-deployed artifact's bytes against the entry's
    declared ``sha256`` (reuses ``manifest.verify_artifact_sha256``); a no-op
    for entries without one (self-deployed).
"""

from __future__ import annotations

import json
import tomllib
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from preproc.module.manifest import (
    ArtifactError,
    ManifestError,
    ModuleManifest,
    ScopeSpec,
    SourceSpec,
    load_manifest,
    parse_manifest,
    verify_artifact_sha256,
)

SCHEMA_VERSION = 1
DEFAULT_LIBRARY_REPO = "exasol-labs/preprocessor-library"
DEFAULT_LIBRARY_REF = "main"

ReaderFn = Callable[[str], bytes]


class RegistryError(Exception):
    """Raised when a registry index cannot be read, parsed, generated, or verified."""


@dataclass(frozen=True)
class RegistryEntry:
    """One module version as it appears in the federated index."""

    name: str
    version: int
    phase: str
    script_name: str
    function: str
    description: str
    min_framework: str
    suggested_scope: dict[str, str]
    deploy_mode: str
    source: dict[str, str]
    sha256: str | None = None
    artifact_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "phase": self.phase,
            "script_name": self.script_name,
            "function": self.function,
            "description": self.description,
            "min_framework": self.min_framework,
            "suggested_scope": dict(self.suggested_scope),
            "deploy_mode": self.deploy_mode,
            "sha256": self.sha256,
            "artifact_path": self.artifact_path,
            "source": dict(self.source),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RegistryEntry:
        return cls(
            name=data["name"],
            version=data["version"],
            phase=data["phase"],
            script_name=data["script_name"],
            function=data["function"],
            description=data["description"],
            min_framework=data["min_framework"],
            suggested_scope=dict(data["suggested_scope"]),
            deploy_mode=data["deploy_mode"],
            sha256=data.get("sha256"),
            artifact_path=data.get("artifact_path"),
            source=dict(data["source"]),
        )

    def to_manifest(self) -> ModuleManifest:
        """Reconstruct the ``ModuleManifest`` this entry was generated from.

        Lets registry-level code reuse ``manifest.py``'s validated
        verification helpers (e.g. ``verify_artifact_sha256``) instead of
        re-implementing them against a different type.
        """
        return ModuleManifest(
            name=self.name,
            description=self.description,
            phase=self.phase,
            script_name=self.script_name,
            function=self.function,
            version=self.version,
            min_framework=self.min_framework,
            suggested_scope=ScopeSpec(**self.suggested_scope),
            deploy_mode=self.deploy_mode,
            sha256=self.sha256,
            source=SourceSpec(**self.source) if self.source else None,
        )


@dataclass(frozen=True)
class RegistryIndex:
    """The full federated index: a schema version plus every module entry."""

    schema_version: int
    entries: tuple[RegistryEntry, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "modules": [entry.to_dict() for entry in self.entries],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RegistryIndex:
        try:
            modules = data["modules"]
            schema_version = data["schema_version"]
        except KeyError as exc:
            raise RegistryError(f"registry index is missing required key {exc}") from exc
        return cls(
            schema_version=schema_version,
            entries=tuple(RegistryEntry.from_dict(entry) for entry in modules),
        )


def render_index(index: RegistryIndex) -> str:
    """Serialize a ``RegistryIndex`` to the canonical JSON text committed as ``index.json``.

    Field order is fixed (``to_dict``'s literal key order) and entries are
    assumed already sorted, so calling this on two ``RegistryIndex`` values
    built from the same inputs produces byte-identical text.
    """
    return json.dumps(index.to_dict(), indent=2) + "\n"


def _artifact_relpath(name: str, version: int) -> str:
    return f"modules/{name}/{name}_v{version}.sql"


def _entry_from_manifest(
    manifest: ModuleManifest, *, source: SourceSpec, artifact_path: str | None
) -> RegistryEntry:
    return RegistryEntry(
        name=manifest.name,
        version=manifest.version,
        phase=manifest.phase,
        script_name=manifest.script_name,
        function=manifest.function,
        description=manifest.description,
        min_framework=manifest.min_framework,
        suggested_scope={
            "type": manifest.suggested_scope.type,
            "value": manifest.suggested_scope.value,
        },
        deploy_mode=manifest.deploy_mode,
        sha256=manifest.sha256,
        artifact_path=artifact_path,
        source={"repo": source.repo, "ref": source.ref},
    )


def _library_entries(library_root: Path, repo: str, ref: str) -> list[RegistryEntry]:
    """Auto-index every ``modules/<name>/module.toml`` in a library checkout.

    For a ``library-deployed`` module, also reads its artifact off disk at the
    standard-layout path and verifies the manifest's declared ``sha256``
    against it (reusing ``manifest.verify_artifact_sha256``) — an authoring
    mistake (stale sha256, missing artifact) fails index generation rather
    than silently shipping a bad entry.

    Directories whose name begins with ``_`` (e.g. ``modules/_template``) are
    scaffolding, not shippable modules, and are skipped: they must never appear
    in the operator-facing registry or be installable via ``preproc module add``.
    """
    entries: list[RegistryEntry] = []
    modules_dir = library_root / "modules"
    if not modules_dir.is_dir():
        return entries

    for manifest_path in sorted(modules_dir.glob("*/module.toml")):
        if manifest_path.parent.name.startswith("_"):
            continue
        try:
            manifest = load_manifest(manifest_path)
        except ManifestError as exc:
            raise RegistryError(
                f"{manifest_path}: invalid module.toml: {'; '.join(exc.errors)}"
            ) from exc

        artifact_path: str | None = None
        if manifest.deploy_mode == "library-deployed":
            artifact_path = _artifact_relpath(manifest.name, manifest.version)
            artifact_abspath = library_root / artifact_path
            try:
                artifact_bytes = artifact_abspath.read_bytes()
            except FileNotFoundError as exc:
                raise RegistryError(
                    f"{manifest_path}: declared artifact {artifact_path} is missing"
                ) from exc
            try:
                verify_artifact_sha256(manifest, artifact_bytes)
            except ArtifactError as exc:
                raise RegistryError(f"{artifact_abspath}: {exc}") from exc

        entries.append(
            _entry_from_manifest(
                manifest, source=SourceSpec(repo=repo, ref=ref), artifact_path=artifact_path
            )
        )
    return entries


def _external_entries(library_root: Path) -> list[RegistryEntry]:
    """Merge every curated ``registry/external/<name>.toml`` block.

    Each block is the same schema as a ``module.toml`` plus its own pinned
    ``source`` (required — an external entry always names where it lives).
    Human review of the PR adding this file is the trust gate; this function
    performs no crawling, only reads what is already committed.
    """
    entries: list[RegistryEntry] = []
    external_dir = library_root / "registry" / "external"
    if not external_dir.is_dir():
        return entries

    for toml_path in sorted(external_dir.glob("*.toml")):
        with toml_path.open("rb") as handle:
            data = tomllib.load(handle)
        try:
            manifest = parse_manifest(data)
        except ManifestError as exc:
            raise RegistryError(
                f"{toml_path}: invalid external registry entry: {'; '.join(exc.errors)}"
            ) from exc
        if manifest.source is None:
            raise RegistryError(f"{toml_path}: external registry entries must declare 'source'")

        artifact_path = (
            _artifact_relpath(manifest.name, manifest.version)
            if manifest.deploy_mode == "library-deployed"
            else None
        )
        entries.append(
            _entry_from_manifest(manifest, source=manifest.source, artifact_path=artifact_path)
        )
    return entries


def generate_index(
    library_root: Path,
    *,
    repo: str = DEFAULT_LIBRARY_REPO,
    ref: str = DEFAULT_LIBRARY_REF,
) -> RegistryIndex:
    """Deterministically build the federated index from a library checkout.

    Merges every ``modules/<name>/module.toml`` (stamped with ``repo``/``ref``
    as their source) with every ``registry/external/<name>.toml`` (each
    keeping its own pinned source), sorted by ``(name, version)``. Running
    this twice against the same checkout produces the same ``RegistryIndex``
    (and, via ``render_index``, byte-identical JSON).
    """
    entries = _library_entries(library_root, repo, ref) + _external_entries(library_root)
    entries.sort(key=lambda entry: (entry.name, entry.version))
    return RegistryIndex(schema_version=SCHEMA_VERSION, entries=tuple(entries))


def index_drift(
    library_root: Path,
    *,
    repo: str = DEFAULT_LIBRARY_REPO,
    ref: str = DEFAULT_LIBRARY_REF,
) -> list[str]:
    """Compare a freshly generated index against the committed ``registry/index.json``.

    Returns a list of human-readable drift descriptions; an empty list means
    the committed file is in sync with its sources. Intended for CI: a
    non-empty result should fail the build.
    """
    committed_path = library_root / "registry" / "index.json"
    fresh_text = render_index(generate_index(library_root, repo=repo, ref=ref))
    if not committed_path.exists():
        return ["registry/index.json is missing; run the index generator"]
    committed_text = committed_path.read_text(encoding="utf-8")
    if committed_text == fresh_text:
        return []
    return [
        "registry/index.json is out of sync with modules/*/module.toml "
        "and registry/external/*.toml; regenerate it"
    ]


def _read_local_index_bytes(checkout_path: Path) -> bytes:
    index_path = checkout_path / "registry" / "index.json"
    try:
        return index_path.read_bytes()
    except FileNotFoundError as exc:
        raise RegistryError(f"no registry/index.json found under {checkout_path}") from exc


def _fetch_url(url: str) -> bytes:
    with urllib.request.urlopen(url) as response:  # noqa: S310 - https-only, see read_registry
        return response.read()


def parse_index(raw: bytes) -> RegistryIndex:
    """Parse raw ``index.json`` bytes into a ``RegistryIndex``."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RegistryError(f"registry index is not valid JSON: {exc}") from exc
    return RegistryIndex.from_dict(data)


def read_registry(registry: str, *, reader: ReaderFn | None = None) -> RegistryIndex:
    """Read the federated index from ``--registry <path-or-https-url>``.

    A value starting with ``https://`` is treated as a pinned index URL and
    fetched online (or via ``reader``, if given — tests inject a fake reader
    so no real network call is ever made). Anything else is treated as a
    local checkout path and read straight off disk as
    ``<registry>/registry/index.json``; that path never touches the network,
    regardless of whether ``reader`` was supplied.

    Raises:
        RegistryError: for a plain ``http://`` URL (registry endpoints must be
            HTTPS), a missing/unreadable index, or invalid JSON/schema.
    """
    if registry.startswith("https://"):
        fetch = reader or _fetch_url
        raw = fetch(registry)
    elif registry.startswith("http://"):
        raise RegistryError(f"registry URL must use https, not http: {registry!r}")
    else:
        raw = _read_local_index_bytes(Path(registry))
    return parse_index(raw)


def verify_entry_artifact(entry: RegistryEntry, artifact_bytes: bytes) -> None:
    """Verify fetched artifact bytes against a registry entry's declared ``sha256``.

    Reuses ``manifest.verify_artifact_sha256`` against the entry's
    reconstructed manifest, so the hashing/compare rule lives in exactly one
    place. A no-op when the entry carries no ``sha256`` (self-deployed).

    Raises:
        RegistryError: if the artifact's sha256 digest does not match the
            entry's declared ``sha256``. Callers MUST NOT deploy the artifact
            when this raises.
    """
    try:
        verify_artifact_sha256(entry.to_manifest(), artifact_bytes)
    except ArtifactError as exc:
        raise RegistryError(str(exc)) from exc


def _artifact_url(registry: str, artifact_path: str) -> str:
    """Derive an artifact's raw URL from the pinned index URL and its relative path.

    The index URL ends in ``registry/index.json`` (or ``index.json``); the
    artifact lives at ``<repo-root>/<artifact_path>``, so the trailing index
    filename is stripped to recover the repo-root base.
    """
    base = registry
    for suffix in ("registry/index.json", "index.json"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            break
    return base.rstrip("/") + "/" + artifact_path.lstrip("/")


def fetch_artifact_bytes(
    entry: RegistryEntry, registry: str, *, reader: ReaderFn | None = None
) -> bytes:
    """Fetch a library-deployed entry's artifact bytes from the registry source.

    For an ``https://`` registry the artifact is fetched from the raw URL
    derived from the index URL (or via ``reader`` when injected, so tests never
    touch the network); for a local checkout path it is read straight off disk
    at ``<registry>/<artifact_path>``, which never touches the network. The
    caller MUST ``verify_entry_artifact`` the returned bytes before deploying.

    Raises:
        RegistryError: for a self-deployed entry (no artifact to fetch), a
            plain ``http://`` registry, or an unreadable artifact.
    """
    if entry.artifact_path is None:
        raise RegistryError(
            f"module {entry.name!r} is {entry.deploy_mode}; it has no artifact to fetch"
        )
    if registry.startswith("https://"):
        fetch = reader or _fetch_url
        return fetch(_artifact_url(registry, entry.artifact_path))
    if registry.startswith("http://"):
        raise RegistryError(f"registry URL must use https, not http: {registry!r}")
    artifact_abspath = Path(registry) / entry.artifact_path
    try:
        return artifact_abspath.read_bytes()
    except FileNotFoundError as exc:
        raise RegistryError(f"artifact not found at {artifact_abspath}") from exc
