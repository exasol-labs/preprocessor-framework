"""Tests for the federated module registry.

Covers index generation (deterministic merge of ``modules/*/module.toml`` and
``registry/external/*.toml``), registry reads (local checkout and injected
online reader), sha256 verification, and the CI sync-drift check
(``src/preproc/module/registry.py``). All unit tests, fully offline: they
build a throwaway library tree under ``tmp_path`` and never touch a real
network or database.
"""

import hashlib
import json

import pytest

from preproc.module.registry import (
    RegistryEntry,
    RegistryError,
    RegistryIndex,
    generate_index,
    index_drift,
    read_registry,
    render_index,
    verify_entry_artifact,
)

_ARTIFACT_TEMPLATE = (
    "CREATE OR REPLACE LUA SCRIPT PREPROC_RT.{upper_name}_V{version}() AS\n"
    "function {function}(sqltext)\n"
    "    return sqltext\n"
    "end\n"
)


def _artifact_text(name: str, version: int, function: str) -> str:
    return _ARTIFACT_TEMPLATE.format(
        upper_name=name.upper().replace("-", "_"), version=version, function=function
    )


def _write_library_module(
    tmp_path,
    name: str,
    version: int = 1,
    *,
    function: str | None = None,
    phase: str = "TRANSLATE",
    deploy_mode: str = "library-deployed",
    sha256: str | None = "__auto__",
    source: str | None = None,
) -> None:
    """Write a conformant modules/<name>/{module.toml,<name>_v<N>.sql} pair."""
    function = function or name.replace("-", "_")
    module_dir = tmp_path / "modules" / name
    module_dir.mkdir(parents=True, exist_ok=True)

    artifact_text = _artifact_text(name, version, function)
    artifact_path = module_dir / f"{name}_v{version}.sql"
    artifact_path.write_text(artifact_text, encoding="utf-8")

    if sha256 == "__auto__":
        sha256 = hashlib.sha256(artifact_text.encode("utf-8")).hexdigest()

    lines = [
        f'name = "{name}"',
        f'description = "{name} module."',
        f'phase = "{phase}"',
        f'script_name = "PREPROC_RT.{name.upper().replace("-", "_")}_V{version}"',
        f'function = "{function}"',
        f"version = {version}",
        'min_framework = "1.0.0"',
        f'deploy_mode = "{deploy_mode}"',
    ]
    if sha256:
        lines.append(f'sha256 = "{sha256}"')
    if source:
        repo, ref = source
        lines.append("")
        lines.append("[source]")
        lines.append(f'repo = "{repo}"')
        lines.append(f'ref = "{ref}"')
    lines += [
        "",
        "[suggested_scope]",
        'type = "ROLE"',
        'value = "PREPROC_ADMIN"',
    ]
    (module_dir / "module.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_external_entry(
    tmp_path,
    name: str,
    *,
    version: int = 3,
    phase: str = "EXPAND",
    deploy_mode: str = "self-deployed",
    repo: str = "exasol/semantic_views",
    ref: str = "v3.0.0",
) -> None:
    external_dir = tmp_path / "registry" / "external"
    external_dir.mkdir(parents=True, exist_ok=True)
    upper_name = name.upper().replace("-", "_")
    text = (
        f'name = "{name}"\n'
        f'description = "{name} external module."\n'
        f'phase = "{phase}"\n'
        f'script_name = "{upper_name}_RT.ENTRY_V{version}"\n'
        'function = "expand"\n'
        f"version = {version}\n"
        'min_framework = "1.0.0"\n'
        f'deploy_mode = "{deploy_mode}"\n'
        "\n"
        "[suggested_scope]\n"
        'type = "ROLE"\n'
        f'value = "{upper_name}_USER"\n'
        "\n"
        "[source]\n"
        f'repo = "{repo}"\n'
        f'ref = "{ref}"\n'
    )
    (external_dir / f"{name}.toml").write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# Index generation
# ---------------------------------------------------------------------------


def test_index_deterministic(tmp_path):
    """Re-running the generator on the same inputs produces byte-identical JSON."""
    _write_library_module(tmp_path, "trailing_comma", version=1)
    _write_library_module(tmp_path, "cast_shorthand", version=1)
    _write_external_entry(tmp_path, "semantic_views")

    first = render_index(generate_index(tmp_path, repo="exasol-labs/preprocessor-library"))
    second = render_index(generate_index(tmp_path, repo="exasol-labs/preprocessor-library"))

    assert first == second

    index = generate_index(tmp_path, repo="exasol-labs/preprocessor-library")
    assert len(index.entries) == 3
    assert [entry.name for entry in index.entries] == [
        "cast_shorthand",
        "semantic_views",
        "trailing_comma",
    ]


def test_index_autoindex_library_module(tmp_path):
    """A modules/cast_shorthand/module.toml is auto-indexed with the manifest's fields."""
    _write_library_module(tmp_path, "cast_shorthand", version=1)

    index = generate_index(
        tmp_path, repo="exasol-labs/preprocessor-library", ref="cast_shorthand-v1"
    )

    assert len(index.entries) == 1
    entry = index.entries[0]
    assert entry.name == "cast_shorthand"
    assert entry.version == 1
    assert entry.phase == "TRANSLATE"
    assert entry.script_name == "PREPROC_RT.CAST_SHORTHAND_V1"
    assert entry.function == "cast_shorthand"
    assert entry.deploy_mode == "library-deployed"
    assert entry.sha256 is not None
    assert entry.artifact_path == "modules/cast_shorthand/cast_shorthand_v1.sql"
    assert entry.source == {
        "repo": "exasol-labs/preprocessor-library",
        "ref": "cast_shorthand-v1",
    }


def test_index_library_version_stamped_and_roundtrips(tmp_path):
    """generate_index stamps a top-level library_version when given one, and it round-trips."""
    _write_library_module(tmp_path, "cast_shorthand", version=1)

    index = generate_index(
        tmp_path, repo="exasol-labs/preprocessor-library", library_version="0.2.0"
    )
    assert index.library_version == "0.2.0"
    rendered = json.loads(render_index(index))
    assert rendered["library_version"] == "0.2.0"
    # Round-trips through from_dict.
    assert RegistryIndex.from_dict(rendered).library_version == "0.2.0"


def test_index_library_version_omitted_when_absent(tmp_path):
    """With no library_version the field is omitted entirely — indexes stay byte-identical."""
    _write_library_module(tmp_path, "cast_shorthand", version=1)

    index = generate_index(tmp_path, repo="exasol-labs/preprocessor-library")
    assert index.library_version is None
    assert "library_version" not in json.loads(render_index(index))
    assert RegistryIndex.from_dict(json.loads(render_index(index))).library_version is None


def test_index_merge_external_entry(tmp_path):
    """A curated registry/external/<name>.toml block is merged alongside library entries."""
    _write_library_module(tmp_path, "cast_shorthand", version=1)
    _write_external_entry(tmp_path, "semantic_views", repo="exasol/semantic_views", ref="v3.0.0")

    index = generate_index(tmp_path, repo="exasol-labs/preprocessor-library")

    names = {entry.name for entry in index.entries}
    assert names == {"cast_shorthand", "semantic_views"}

    external = next(entry for entry in index.entries if entry.name == "semantic_views")
    assert external.deploy_mode == "self-deployed"
    assert external.source == {"repo": "exasol/semantic_views", "ref": "v3.0.0"}
    assert external.artifact_path is None


def test_index_generate_rejects_sha_mismatch(tmp_path):
    """Index generation fails loudly when a manifest's sha256 disagrees with its artifact."""
    _write_library_module(tmp_path, "cast_shorthand", version=1)
    artifact_path = tmp_path / "modules" / "cast_shorthand" / "cast_shorthand_v1.sql"
    artifact_path.write_text(artifact_path.read_text(encoding="utf-8") + "\n-- tampered\n")

    with pytest.raises(RegistryError):
        generate_index(tmp_path, repo="exasol-labs/preprocessor-library")


# ---------------------------------------------------------------------------
# Registry reads
# ---------------------------------------------------------------------------


def test_registry_read_local_offline(tmp_path, monkeypatch):
    """--registry <path> loads every entry from the local index.json, with no network fetch."""
    _write_library_module(tmp_path, "cast_shorthand", version=1)
    _write_library_module(tmp_path, "trailing_comma", version=1)
    index = generate_index(tmp_path, repo="exasol-labs/preprocessor-library")
    registry_dir = tmp_path / "registry"
    registry_dir.mkdir(exist_ok=True)
    (registry_dir / "index.json").write_text(render_index(index), encoding="utf-8")

    def _network_forbidden(*_args, **_kwargs):
        raise AssertionError("read_registry must not touch the network for a local checkout")

    monkeypatch.setattr("urllib.request.urlopen", _network_forbidden)

    loaded = read_registry(str(tmp_path))

    assert {entry.name for entry in loaded.entries} == {"cast_shorthand", "trailing_comma"}


def test_registry_read_online_injected_reader(tmp_path):
    """An https:// registry source is read through the injected reader (no real fetch)."""
    _write_library_module(tmp_path, "cast_shorthand", version=1)
    index = generate_index(tmp_path, repo="exasol-labs/preprocessor-library")
    payload = render_index(index).encode("utf-8")

    calls = []

    def _fake_reader(url: str) -> bytes:
        calls.append(url)
        return payload

    loaded = read_registry(
        "https://raw.githubusercontent.com/exasol-labs/preprocessor-library/main/registry/index.json",
        reader=_fake_reader,
    )

    assert calls == [
        "https://raw.githubusercontent.com/exasol-labs/preprocessor-library/main/registry/index.json"
    ]
    assert [entry.name for entry in loaded.entries] == ["cast_shorthand"]


def test_registry_read_online_rejects_plain_http():
    """A plain http:// registry URL is rejected before any fetch is attempted."""
    with pytest.raises(RegistryError):
        read_registry("http://example.com/registry/index.json", reader=lambda url: b"{}")


# ---------------------------------------------------------------------------
# sha256 verification
# ---------------------------------------------------------------------------


def _cast_shorthand_entry(tmp_path) -> tuple[RegistryEntry, bytes]:
    _write_library_module(tmp_path, "cast_shorthand", version=1)
    index = generate_index(tmp_path, repo="exasol-labs/preprocessor-library")
    entry = index.entries[0]
    artifact_bytes = (
        tmp_path / "modules" / "cast_shorthand" / "cast_shorthand_v1.sql"
    ).read_bytes()
    return entry, artifact_bytes


def test_registry_sha_match(tmp_path):
    """An artifact whose bytes match the entry's sha256 verifies cleanly."""
    entry, artifact_bytes = _cast_shorthand_entry(tmp_path)
    verify_entry_artifact(entry, artifact_bytes)  # does not raise


def test_registry_sha_mismatch_aborts(tmp_path):
    """An artifact hashing to a different digest raises and must not be deployed."""
    entry, artifact_bytes = _cast_shorthand_entry(tmp_path)
    tampered = artifact_bytes + b"\n-- tampered\n"
    with pytest.raises(RegistryError):
        verify_entry_artifact(entry, tampered)


# ---------------------------------------------------------------------------
# CI sync-drift check
# ---------------------------------------------------------------------------


def test_index_sync_check_detects_drift(tmp_path):
    """A committed index.json that predates a module.toml edit is reported as drifted."""
    _write_library_module(tmp_path, "cast_shorthand", version=1)
    index = generate_index(tmp_path, repo="exasol-labs/preprocessor-library")
    registry_dir = tmp_path / "registry"
    registry_dir.mkdir(exist_ok=True)
    (registry_dir / "index.json").write_text(render_index(index), encoding="utf-8")

    assert index_drift(tmp_path, repo="exasol-labs/preprocessor-library") == []

    _write_library_module(tmp_path, "trailing_comma", version=1)

    drift = index_drift(tmp_path, repo="exasol-labs/preprocessor-library")
    assert drift != []


def test_index_sync_check_missing_committed_index(tmp_path):
    """A missing registry/index.json is reported as drift rather than raising."""
    _write_library_module(tmp_path, "cast_shorthand", version=1)
    drift = index_drift(tmp_path, repo="exasol-labs/preprocessor-library")
    assert drift != []
