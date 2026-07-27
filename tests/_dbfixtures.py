"""Shared building blocks for the in-database module-install integration suite.

Groups A, B, and C reuse these helpers so the BucketFS-staged release tarball,
the operator-side CONNECTION, and the resolver-UDF deployment are defined once.

Two independent concerns live here:

* **A throwaway release tarball built in-test.** Phase 2 (the library repo's
  release packaging) has not shipped a real ``preproc-lib-*.tar.gz`` yet, so
  the fixture builds one from throwaway module sources using the very same
  ``registry.generate_index`` / ``render_index`` the CLI uses. The tarball
  carries ``registry/index.json`` plus each library-deployed module's artifact
  at its standard-layout ``modules/<name>/<name>_v<N>.sql`` path.

* **A hermetic harness for the resolver UDF body.** The resolver ships as a
  ``CREATE ... PYTHON3 SET SCRIPT`` in ``sql/udf/module_resolver_v1.sql``. Its
  Python body is stdlib-only and depends on the runtime only through ``exa`` and
  the emit/iterate ``ctx`` protocol, so it can be executed off-database against
  fakes — proving the resolve algorithm deterministically without a cluster.
"""

from __future__ import annotations

import base64
import hashlib
import io
import os
import ssl
import tarfile
import tempfile
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from preproc.module.registry import RegistryIndex, generate_index, render_index

# ---------------------------------------------------------------------------
# The resolver UDF's EMITS contract (Groups B and C consume this shape).
# ---------------------------------------------------------------------------

RESOLVER_COLUMNS: tuple[str, ...] = (
    "status",
    "error_kind",
    "error_message",
    "name",
    "version",
    "phase",
    "script_name",
    "function_name",
    "deploy_mode",
    "sha256",
    "suggested_scope_type",
    "suggested_scope_value",
    "source_repo",
    "source_ref",
    "library_version",
    "chunk_index",
    "chunk_count",
    "artifact_chunk",
)

_REPO_ROOT = Path(__file__).parent.parent
RESOLVER_SQL_PATH = _REPO_ROOT / "sql" / "udf" / "module_resolver_v1.sql"

RESOLVER_SCRIPT = "PREPROC.MODULE_RESOLVER_V1"

# A library-deployed artifact whose text exceeds this fits a single EMITS value;
# a larger one is emitted as ordered chunks (see the resolver's chunk fallback).
EMITS_CHUNK_CHARS = 2_000_000

_DEFAULT_REPO = "exasol-labs/preprocessor-library"
_DEFAULT_REF = "v0.1.0"

_ARTIFACT_TEMPLATE = (
    "CREATE OR REPLACE LUA SCRIPT PREPROC_RT.{upper}_V{version}() AS\n"
    "function {function}(sqltext)\n"
    "    return sqltext\n"
    "end\n"
)


def _upper(name: str) -> str:
    return name.upper().replace("-", "_")


def default_artifact_text(name: str, version: int, function: str) -> str:
    return _ARTIFACT_TEMPLATE.format(upper=_upper(name), version=version, function=function)


@dataclass(frozen=True)
class ModuleSpec:
    """One module version to place in a throwaway release tarball."""

    name: str
    version: int = 1
    phase: str = "TRANSLATE"
    function: str | None = None
    deploy_mode: str = "library-deployed"
    source: tuple[str, str] | None = None
    artifact_text: str | None = None

    @property
    def resolved_function(self) -> str:
        return self.function or self.name.replace("-", "_")

    @property
    def script_name(self) -> str:
        return f"PREPROC_RT.{_upper(self.name)}_V{self.version}"

    def artifact(self) -> str:
        if self.deploy_mode != "library-deployed":
            return ""
        if self.artifact_text is not None:
            return self.artifact_text
        return default_artifact_text(self.name, self.version, self.resolved_function)


def library_module(name: str, version: int = 1, **kwargs: Any) -> ModuleSpec:
    return ModuleSpec(name=name, version=version, deploy_mode="library-deployed", **kwargs)


def self_deployed_module(
    name: str, *, version: int, phase: str, source: tuple[str, str]
) -> ModuleSpec:
    return ModuleSpec(
        name=name, version=version, phase=phase, deploy_mode="self-deployed", source=source
    )


def standard_library_specs() -> list[ModuleSpec]:
    """The canonical staged modules Groups A/B/C share: cast_shorthand + trailing_comma."""
    return [
        library_module("cast_shorthand", version=1, phase="TRANSLATE", function="cast_shorthand"),
        library_module("trailing_comma", version=1, phase="TRANSLATE", function="trailing_comma"),
    ]


def _write_module_tree(root: Path, spec: ModuleSpec) -> Path | None:
    """Write a conformant ``modules/<name>/{module.toml,<name>_v<N>.sql}`` pair.

    Returns the artifact path for a library-deployed module, else ``None``.
    """
    module_dir = root / "modules" / spec.name
    module_dir.mkdir(parents=True, exist_ok=True)

    artifact_path: Path | None = None
    lines = [
        f'name = "{spec.name}"',
        f'description = "{spec.name} module."',
        f'phase = "{spec.phase}"',
        f'script_name = "{spec.script_name}"',
        f'function = "{spec.resolved_function}"',
        f"version = {spec.version}",
        'min_framework = "1.0.0"',
        f'deploy_mode = "{spec.deploy_mode}"',
    ]
    if spec.deploy_mode == "library-deployed":
        artifact_text = spec.artifact()
        artifact_path = module_dir / f"{spec.name}_v{spec.version}.sql"
        artifact_path.write_text(artifact_text, encoding="utf-8")
        sha = hashlib.sha256(artifact_text.encode("utf-8")).hexdigest()
        lines.append(f'sha256 = "{sha}"')
    if spec.source is not None:
        repo, ref = spec.source
        lines += ["", "[source]", f'repo = "{repo}"', f'ref = "{ref}"']
    lines += ["", "[suggested_scope]", 'type = "ROLE"', 'value = "PREPROC_ADMIN"']
    (module_dir / "module.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return artifact_path


@dataclass
class BuiltTarball:
    """A built release tarball plus the metadata a test needs to assert on it."""

    path: Path
    index: RegistryIndex
    index_bytes: bytes


def build_library_tarball(
    dest: Path,
    specs: list[ModuleSpec],
    *,
    repo: str = _DEFAULT_REPO,
    ref: str = _DEFAULT_REF,
    library_version: str | None = None,
    tamper: dict[str, bytes] | None = None,
) -> BuiltTarball:
    """Build a ``preproc-lib-<ver>.tar.gz`` from throwaway module sources.

    The tarball carries ``registry/index.json`` (generated the same way CI
    generates it) plus each library-deployed artifact at its standard-layout
    ``modules/<name>/<name>_v<N>.sql`` path. ``tamper`` maps a module name to
    extra bytes appended to *only the tarball copy* of its artifact, leaving the
    index's ``sha256`` describing the clean bytes — used to force a mismatch.
    """
    tamper = tamper or {}
    work = Path(tempfile.mkdtemp(prefix="preproc-lib-"))
    artifacts: dict[str, Path] = {}
    for spec in specs:
        artifact_path = _write_module_tree(work, spec)
        if artifact_path is not None:
            artifacts[spec.name] = artifact_path

    index = generate_index(work, repo=repo, ref=ref, library_version=library_version)
    index_text = render_index(index)
    registry_dir = work / "registry"
    registry_dir.mkdir(exist_ok=True)
    (registry_dir / "index.json").write_text(index_text, encoding="utf-8")

    dest.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(dest, "w:gz") as tar:
        tar.add(registry_dir / "index.json", arcname="registry/index.json")
        for entry in index.entries:
            if entry.artifact_path is None:
                continue
            local = artifacts[entry.name]
            data = local.read_bytes() + tamper.get(entry.name, b"")
            info = tarfile.TarInfo(name=entry.artifact_path)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return BuiltTarball(path=dest, index=index, index_bytes=index_text.encode("utf-8"))


# ---------------------------------------------------------------------------
# Resolver UDF body extraction + off-database hermetic harness
# ---------------------------------------------------------------------------


def resolver_create_statement() -> str:
    """The resolver CREATE statement with the interactive trailing '/' stripped.

    Mirrors ``install.py``'s handling of the demo UDF: every lone ``/`` line is
    dropped so pyexasol receives a bare CREATE statement.
    """
    raw = RESOLVER_SQL_PATH.read_text(encoding="utf-8")
    return "\n".join(line for line in raw.splitlines() if line.strip() != "/").strip()


def resolver_python_body() -> str:
    """The Python body of the resolver UDF (everything after the ``) AS`` header)."""
    statement = resolver_create_statement()
    marker = ") AS\n"
    idx = statement.find(marker)
    if idx == -1:
        raise AssertionError("resolver CREATE statement has no ') AS' body marker")
    return statement[idx + len(marker) :]


def resolve_rows(connection: Any, source: str, module_name: str) -> list[dict[str, Any]]:
    """Invoke the deployed resolver over a single request and return rows as dicts.

    The SET script forms one group over the single-row driving select, so ``run``
    fires exactly once (verified live). pyexasol substitutes ``{name}`` params
    with safe quoting.
    """
    rows = connection.execute(
        f"SELECT {RESOLVER_SCRIPT}({{src}}, {{mod}})",
        {"src": source, "mod": module_name},
    ).fetchall()
    return [dict(zip(RESOLVER_COLUMNS, row)) for row in rows]


def wait_until_ok(
    connection: Any, source: str, module_name: str, *, timeout: float = 90.0, interval: float = 3.0
) -> bool:
    """Poll until BucketFS has extracted + replicated the release to the UDF mount.

    A freshly uploaded archive takes a moment to auto-extract and replicate;
    until then the resolver reports ``source_unreadable``. Returns True once the
    module resolves OK, False on timeout.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rows = resolve_rows(connection, source, module_name)
        if rows and rows[0]["status"] == "OK":
            return True
        time.sleep(interval)
    return False


def deploy_resolver_udf(connection: Any) -> None:
    """Deploy PREPROC.MODULE_RESOLVER_V1 into the locked admin schema.

    Group C wires this into ``install.py``; until then the integration suite
    deploys it directly, mirroring how the demo UDF is deployed. An OPEN SCHEMA
    is issued first because Exasol compiles a script against an active schema.
    """
    connection.execute("OPEN SCHEMA PREPROC")
    connection.execute(resolver_create_statement())


class FakeEmitCtx:
    """A stand-in for the Exasol SET-script ``ctx``: records emitted rows.

    The resolver processes the single request row and never advances, so
    ``next()`` always reports "no more rows".
    """

    def __init__(self, source: str | None, module_name: str | None) -> None:
        self.src = source
        self.module_name = module_name
        self.rows: list[tuple[Any, ...]] = []

    def emit(self, *values: Any) -> None:
        self.rows.append(values)

    def next(self) -> bool:  # noqa: A003 - mirrors the Exasol ctx protocol
        return False


def make_fake_exa(connections: dict[str, str]) -> Any:
    """A fake ``exa`` whose ``get_connection`` returns address-only connections."""

    def get_connection(name: str) -> Any:
        if name not in connections:
            raise RuntimeError(f"connection {name} does not exist or is not accessible")
        return SimpleNamespace(address=connections[name], user="", password="", type="password")

    return SimpleNamespace(get_connection=get_connection)


def run_resolver_body(
    source: str | None,
    module_name: str | None,
    *,
    exa: Any = None,
    urlopen: Callable[..., Any] | None = None,
) -> list[dict[str, Any]]:
    """Execute the resolver UDF body off-database and return emitted rows as dicts.

    ``exa`` supplies ``get_connection`` for the bucketfs path; ``urlopen``, when
    given, replaces ``urllib.request.urlopen`` for the https path so no real
    network is touched. The returned rows use ``RESOLVER_COLUMNS`` keys.
    """
    namespace: dict[str, Any] = {}
    exec(compile(resolver_python_body(), str(RESOLVER_SQL_PATH), "exec"), namespace)  # noqa: S102
    namespace["exa"] = exa if exa is not None else make_fake_exa({})

    ctx = FakeEmitCtx(source, module_name)
    if urlopen is not None:
        original = urllib.request.urlopen
        urllib.request.urlopen = urlopen  # type: ignore[assignment]
        try:
            namespace["run"](ctx)
        finally:
            urllib.request.urlopen = original  # type: ignore[assignment]
    else:
        namespace["run"](ctx)

    return [dict(zip(RESOLVER_COLUMNS, row)) for row in ctx.rows]


def fake_http_directory(files: dict[str, bytes]) -> Callable[..., Any]:
    """A fake ``urlopen`` serving ``files`` (URL -> bytes); 404s the rest."""

    def _urlopen(url: Any, *args: Any, **kwargs: Any) -> Any:
        target = url.full_url if hasattr(url, "full_url") else url
        if target not in files:
            raise urllib.error.HTTPError(target, 404, "Not Found", {}, None)  # type: ignore[arg-type]
        return _BytesResponse(files[target])

    return _urlopen


class _BytesResponse(io.BytesIO):
    """A minimal urlopen-response context manager over fixed bytes."""

    def __enter__(self) -> _BytesResponse:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


# ---------------------------------------------------------------------------
# BucketFS staging (used by the live integration fixture)
# ---------------------------------------------------------------------------

BFS_WRITE_PASSWORD_ENV = "EXASOL_BFS_WRITE_PASSWORD"
_DEFAULT_BUCKET = "default"
_DEFAULT_SERVICE = "bfsdefault"


@dataclass
class BucketFsConfig:
    host: str
    port: str
    bucket: str
    write_password: str
    service: str

    @property
    def base_url(self) -> str:
        """HTTPS endpoint used by the test harness to PUT/GET staging bytes."""
        return f"https://{self.host}:{self.port}/{self.bucket}"

    @property
    def bucketfs_address(self) -> str:
        """Native BucketFS CONNECTION address the resolver reads (no host/port).

        Mirrors the Exasol-documented ``bucketfs:<service>/<bucket>`` form that
        maps 1:1 to the UDF mount ``/buckets/<service>/<bucket>``.
        """
        return f"bucketfs:{self.service}/{self.bucket}"


def bucketfs_config() -> BucketFsConfig | None:
    """Resolve BucketFS write config from the environment, or ``None`` if absent.

    Reads/uploads use the BucketFS HTTPS endpoint; the write password lives in
    ``EXASOL_BFS_WRITE_PASSWORD`` (a distinct secret from the DB password). When
    it is unset the live BucketFS fixture skips rather than failing.
    """
    host = os.environ.get("EXASOL_BFS_HOST")
    write_password = os.environ.get(BFS_WRITE_PASSWORD_ENV)
    if not host or not write_password:
        return None
    return BucketFsConfig(
        host=host,
        port=os.environ.get("EXASOL_BFS_PORT", "2581"),
        bucket=os.environ.get("EXASOL_BFS_BUCKET", _DEFAULT_BUCKET),
        write_password=write_password,
        service=os.environ.get("EXASOL_BFS_SERVICE", _DEFAULT_SERVICE),
    )


def _insecure_ssl_context() -> ssl.SSLContext:
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def bucketfs_put(config: BucketFsConfig, bucket_path: str, data: bytes) -> None:
    """PUT ``data`` into BucketFS at ``<bucket>/<bucket_path>`` over HTTPS."""
    url = f"{config.base_url}/{bucket_path.lstrip('/')}"
    request = urllib.request.Request(url, data=data, method="PUT")
    token = base64.b64encode(f"w:{config.write_password}".encode()).decode()
    request.add_header("Authorization", f"Basic {token}")
    with urllib.request.urlopen(request, context=_insecure_ssl_context(), timeout=120):
        pass


def bucketfs_get(config: BucketFsConfig, bucket_path: str) -> bytes:
    """GET ``<bucket>/<bucket_path>`` over HTTPS (reads are public)."""
    url = f"{config.base_url}/{bucket_path.lstrip('/')}"
    with urllib.request.urlopen(url, context=_insecure_ssl_context(), timeout=60) as response:
        return response.read()


def bucketfs_delete(config: BucketFsConfig, bucket_path: str) -> None:
    """DELETE ``<bucket>/<bucket_path>`` over HTTPS (best-effort teardown)."""
    url = f"{config.base_url}/{bucket_path.lstrip('/')}"
    request = urllib.request.Request(url, method="DELETE")
    token = base64.b64encode(f"w:{config.write_password}".encode()).decode()
    request.add_header("Authorization", f"Basic {token}")
    with urllib.request.urlopen(request, context=_insecure_ssl_context(), timeout=60):
        pass
