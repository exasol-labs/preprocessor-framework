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
      "schema_version": 2,
      "library_version": "0.2.0",
      "modules": [
        {
          "name": "cast_shorthand",
          "version": 1,
          "phase": "TRANSLATE",
          "script_name": "PREPROC_RT.CAST_SHORTHAND_V1",
          "function": "cast_shorthand",
          "description": "...",
          "min_framework": "1.0.0",
          "suggested_scope": {"type": "ROLE", "value": "PREPROC_ADMIN"},
          "deploy_mode": "library-deployed",
          "sha256": "<64 hex chars>",
          "artifact_path": "modules/cast_shorthand/cast_shorthand_v1.sql",
          "objects": [{"type": "LUA SCRIPT", "name": "PREPROC_RT.CAST_SHORTHAND_V1"}],
          "source": {"repo": "exasol-labs/preprocessor-library", "ref": "main"}
        }
      ]
    }

``library_version`` is optional (omitted entirely when the generator is not
given one) and records the library release the index was built from, so an
install resolved through a mutable ``latest`` source can be audited back to a
concrete version. ``modules`` is sorted by ``(name, version)`` so the file is
byte-stable across regenerations of the same inputs. ``sha256`` and ``artifact_path`` are ``null``
for ``self-deployed`` entries (no artifact is fetched through the registry for
those — the module's own installer deploys it; the CLI only registers the
rule). ``artifact_path`` is the artifact's path relative to its own
``source.repo`` checkout, following the standard module layout
(``modules/<name>/<name>_v<N>.sql``, see docs/module-authoring.md).

``objects`` is the object inventory a ``library-deployed`` entry's artifact
creates: one ``{"type", "name"}`` element per object, copied from the
manifest's declared ``[[objects]]`` (in its declared order) or, when the
manifest declares none — legal only for a single-statement artifact — derived
as the single entry object named by ``script_name``. It is ``null`` for a
``self-deployed`` entry (no artifact travels through the registry for one, so
there is nothing to inventory here).

``schema_version`` is 2 as of this generator. A reader MUST also accept a
published ``schema_version: 1`` index, whose entries carry no ``objects``
key at all: each such entry's inventory is interpreted as the single object
named by its ``script_name`` (see ``RegistryIndex.from_dict``), which is what
lets an already-published version-1 index keep resolving unchanged.

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
``encode_objects(objects)`` / ``decode_objects(raw)``
    Serialize an object inventory to/from the ``PREPROC.MODULES.objects``
    VARCHAR form — see the format note on ``encode_objects``.
"""

from __future__ import annotations

import json
import tomllib
import urllib.request
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from preproc.module.artifact import normalize_object_name
from preproc.module.manifest import (
    ENTRY_OBJECT_TYPE,
    ArtifactError,
    ManifestError,
    ModuleManifest,
    ObjectSpec,
    ScopeSpec,
    SourceSpec,
    load_manifest,
    parse_manifest,
    verify_artifact_sha256,
)

SCHEMA_VERSION = 2
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
    objects: tuple[dict[str, str], ...] | None = None

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
            "objects": [dict(obj) for obj in self.objects] if self.objects is not None else None,
            "source": dict(self.source),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RegistryEntry:
        raw_objects = data.get("objects")
        objects = (
            tuple({"type": obj["type"], "name": obj["name"]} for obj in raw_objects)
            if raw_objects is not None
            else None
        )
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
            objects=objects,
            source=dict(data["source"]),
        )

    def to_manifest(self) -> ModuleManifest:
        """Reconstruct the ``ModuleManifest`` this entry was generated from.

        Lets registry-level code reuse ``manifest.py``'s validated
        verification helpers (e.g. ``verify_artifact_sha256`` and
        ``verify_artifact_inventory``) instead of re-implementing them against
        a different type. ``objects`` round-trips into ``ObjectSpec`` entries
        so a composite entry's declared inventory survives the reconstruction.
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
            objects=(
                tuple(ObjectSpec(type=obj["type"], name=obj["name"]) for obj in self.objects)
                if self.objects is not None
                else None
            ),
        )


@dataclass(frozen=True)
class RegistryIndex:
    """The full federated index: a schema version plus every module entry.

    ``library_version`` is the optional release version of the library this
    index was generated from (e.g. ``"0.2.0"``, read from the library's
    ``VERSION``). When present it lets an install performed against a mutable
    ``latest`` source record which concrete release it actually resolved,
    independent of the (mutable) tag in the URL. It is omitted from the
    serialized JSON when ``None`` so indexes that predate the field stay
    byte-identical.
    """

    schema_version: int
    entries: tuple[RegistryEntry, ...]
    library_version: str | None = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"schema_version": self.schema_version}
        if self.library_version is not None:
            result["library_version"] = self.library_version
        result["modules"] = [entry.to_dict() for entry in self.entries]
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RegistryIndex:
        """Parse an index mapping, accepting both ``schema_version`` 1 and 2.

        A ``schema_version: 1`` index carries no ``objects`` key on any entry;
        each such entry's inventory is filled in as the single object named by
        its ``script_name`` (``ENTRY_OBJECT_TYPE``), so an already-published
        version-1 index keeps resolving unchanged. A ``schema_version: 2``
        entry's ``objects`` (including an explicit ``null`` for a
        self-deployed entry) is taken as-is.
        """
        try:
            modules = data["modules"]
            schema_version = data["schema_version"]
        except KeyError as exc:
            raise RegistryError(f"registry index is missing required key {exc}") from exc
        entries = tuple(RegistryEntry.from_dict(entry) for entry in modules)
        if schema_version == 1:
            entries = tuple(
                entry
                if entry.objects is not None
                else replace(entry, objects=_fallback_objects(entry))
                for entry in entries
            )
        return cls(
            schema_version=schema_version,
            entries=entries,
            library_version=data.get("library_version"),
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


def _fallback_objects(entry: RegistryEntry) -> tuple[dict[str, str], ...]:
    """The single-object inventory a schema-version-1 entry is read as.

    Mirrors ``verify_artifact_inventory``'s N=1 fallback: the entry object is
    the ``ENTRY_OBJECT_TYPE`` object named by the entry's ``script_name``.
    """
    return ({"type": ENTRY_OBJECT_TYPE, "name": normalize_object_name(entry.script_name)},)


def _entry_objects(manifest: ModuleManifest) -> tuple[dict[str, str], ...] | None:
    """Derive the ``objects`` an index entry carries for ``manifest``.

    ``None`` for a ``self-deployed`` module: no artifact travels through the
    registry for one, so there is nothing to inventory. For a
    ``library-deployed`` module this is the manifest's declared
    ``[[objects]]`` (in declared order) or, when the manifest declares none —
    legal only for a single-statement artifact — the single entry object named
    by ``script_name``, exactly ``verify_artifact_inventory``'s N=1 fallback.
    """
    if manifest.deploy_mode != "library-deployed":
        return None
    if manifest.objects is not None:
        return tuple({"type": obj.type, "name": obj.name} for obj in manifest.objects)
    return ({"type": ENTRY_OBJECT_TYPE, "name": normalize_object_name(manifest.script_name)},)


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
        objects=_entry_objects(manifest),
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
    library_version: str | None = None,
) -> RegistryIndex:
    """Deterministically build the federated index from a library checkout.

    Merges every ``modules/<name>/module.toml`` (stamped with ``repo``/``ref``
    as their source) with every ``registry/external/<name>.toml`` (each
    keeping its own pinned source), sorted by ``(name, version)``. Running
    this twice against the same checkout produces the same ``RegistryIndex``
    (and, via ``render_index``, byte-identical JSON).

    ``library_version`` (e.g. the library's ``VERSION``) is stamped as the
    index's top-level ``library_version`` so an install resolved through a
    mutable ``latest`` source can record the concrete release. When omitted,
    the field is left out of the serialized index.
    """
    entries = _library_entries(library_root, repo, ref) + _external_entries(library_root)
    entries.sort(key=lambda entry: (entry.name, entry.version))
    return RegistryIndex(
        schema_version=SCHEMA_VERSION,
        entries=tuple(entries),
        library_version=library_version,
    )


def index_drift(
    library_root: Path,
    *,
    repo: str = DEFAULT_LIBRARY_REPO,
    ref: str = DEFAULT_LIBRARY_REF,
    library_version: str | None = None,
) -> list[str]:
    """Compare a freshly generated index against the committed ``registry/index.json``.

    Returns a list of human-readable drift descriptions; an empty list means
    the committed file is in sync with its sources. Intended for CI: a
    non-empty result should fail the build. ``repo``/``ref``/``library_version``
    must match what the committed index was generated with, or the comparison
    reports (real) drift.
    """
    committed_path = library_root / "registry" / "index.json"
    fresh_text = render_index(
        generate_index(library_root, repo=repo, ref=ref, library_version=library_version)
    )
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


# The width of PREPROC.MODULES.objects (sql/install/22_modules_table.sql), of
# MODULE_REGISTER's CAST, of the resolver's and MODULE_CATALOG's own `objects`
# EMITS column, and of the widening ALTER in sql/install/25_column_upgrades.sql.
# This is the CANONICAL copy: the resolver (sql/udf/module_resolver_v1.sql
# `OBJECTS_MAX_CHARS`) and `module_install.lua` (`OBJECTS_MAX_CHARS`) each carry
# a pinned copy because neither can import from here. All of them must move
# together — every one is read by a gate that refuses an over-wide inventory
# before the provenance write, so the NARROWEST copy is the effective limit and a
# partial widening silently keeps the old ceiling instead of failing.
# tests/test_module_registry.py::test_every_pinned_objects_width_equals_the_canonical_constant
# is what keeps them in step; it scans the shipped tree rather than trusting a
# hand-maintained list.
#
# 200,000 characters is roughly 2,000 objects at realistic ~100-character
# qualified names, well past any plausible module (including the schema-owning
# subsystem module ADR-057 blesses) and well under Exasol's 2,000,000-character
# VARCHAR ceiling. An Exasol VARCHAR is variable-length, so the headroom costs
# nothing in storage.
#
# Counted in CHARACTERS, not bytes: a quoted object identifier may legitimately
# hold non-ASCII, and byte-counting would refuse an install the column itself
# accepts.
OBJECTS_MAX_CHARS = 200000


def encode_objects(objects: Iterable[Mapping[str, str] | ObjectSpec] | None) -> str | None:
    """Serialize an object inventory into the ``PREPROC.MODULES.objects`` VARCHAR form.

    The wire format is newline-separated ``TYPE|NAME`` lines, one per object,
    in file order::

        LUA SCRIPT|PREPROC_RT.CONTROL_DATABASE_V1
        PYTHON3 SCALAR SCRIPT|PREPROC_RT.CONFD_JOB_V1
        TABLE|PREPROC_RT.CONFD_STATE_V1

    This column is the one place ``PREPROC_RT.INSTALLED_MODULES`` lets an
    operator without admin rights read a module's object inventory (see
    ``install/schema-layout``), so the wire form itself must be readable, not
    just decodable. Delimiter-safety is NOT handled by escaping here: a
    declared object ``type`` or ``name`` containing ``\\n``, ``\\r``, or ``|``
    is rejected at manifest parse time (``manifest.py``'s ``[[objects]]``
    validation), so those characters can never occur in a value this function
    encodes, and no escaping scheme is needed. ``module_reclaim.lua`` parses
    this text back with ``line:match("^(.-)|(.*)$")`` per line — splitting
    each line on its first ``|``, matching ``decode_objects`` below.

    Returns ``None`` for ``None`` or an empty inventory, which callers bind as
    SQL ``NULL`` — the same nullability the recorded inventory already has.
    """
    if not objects:
        return None
    lines = []
    for obj in objects:
        obj_type = obj["type"] if isinstance(obj, Mapping) else obj.type
        name = obj["name"] if isinstance(obj, Mapping) else obj.name
        lines.append(f"{obj_type}|{name}")
    return "\n".join(lines)


def decode_objects(raw: str | None) -> list[dict[str, str]] | None:
    """Parse the ``PREPROC.MODULES.objects`` VARCHAR form back into a list.

    The exact inverse of ``encode_objects``; see its docstring for the wire
    format. Each line is split on its FIRST ``|`` (matching the Lua
    ``(.-)|(.*)`` non-greedy/greedy pattern), so the split is unambiguous even
    though it is a plain single-character delimiter — safe only because
    ``manifest.py`` already rejects a declared type or name containing ``|``.

    Returns ``None`` for ``None`` (no recorded inventory — a NULL column
    reader falls back to the row's ``script_name``, same as a
    schema-version-1 registry entry).

    Raises:
        RegistryError: if a non-empty line carries no ``|`` delimiter.
    """
    if raw is None:
        return None
    objects: list[dict[str, str]] = []
    for line in raw.split("\n"):
        if not line:
            continue
        obj_type, sep, name = line.partition("|")
        if not sep:
            raise RegistryError(f"malformed objects encoding: line {line!r} has no '|' delimiter")
        objects.append({"type": obj_type, "name": name})
    return objects
