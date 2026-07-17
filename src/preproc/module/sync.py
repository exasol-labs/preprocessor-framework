"""Declarative, idempotent convergence of the DB to a desired module set.

``preproc module sync <desired.toml>`` reads a desired-state manifest, diffs it
against the live provenance (``PREPROC_RT.INSTALLED_MODULES``), and converges the
database with the fewest possible mutations and exactly one refresh.

Convergence algorithm
---------------------
The diff is computed by module NAME (a module has one ``deployed`` version at a
time in steady state):

* **add**    — desired, not currently deployed → deploy the artifact, ensure the
  rule, write provenance.
* **update** — desired at a different version than deployed → deploy the new
  ``_V<N+1>``, repoint the existing rule off the old script onto the new one,
  and (after the refresh) mark the old version retired. Never a CREATE OR REPLACE
  of the live version.
* **no-op**  — desired at exactly the deployed version → RULE_ENSURE only, which
  is idempotent on the natural rule key (it makes no change when the rule already
  exists), so a re-run of an unchanged manifest touches nothing.
* **retire** — deployed but dropped from the desired set → drop its rule and
  (after the refresh) mark its provenance retired. Reclaim (the physical DROP) is
  a separate, guarded admin step and is deliberately NOT done here.

Ordering and the single refresh
-------------------------------
All mutations run first with the auto-refresh suppressed (``RULE_ENSURE`` /
``RULE_DROP`` with ``norefresh = TRUE``, and direct autocommitted CONFIG
repoints), so no script is ever named by a rule before it is deployed. Then a
SINGLE ``PREPROC.REFRESH`` rematerialises RESOLUTION for the whole convergence —
and only when something actually changed CONFIG, so an unchanged re-run issues no
refresh at all. Provenance retirement runs AFTER the refresh (it neither reads
nor writes CONFIG), mirroring the update flow's repoint → refresh → retire order.

Idempotency (closes BUG-008)
----------------------------
Rule registration always goes through ``PREPROC.RULE_ENSURE``, never a blind
add, so re-applying an unchanged desired file converges every module to the
no-op branch: RULE_ENSURE returns the existing rule id, nothing is inserted,
CONFIG is unchanged, and no refresh is issued.

Failure safety
--------------
Every desired ``module@version`` is resolved against the registry, and every
library-deployed artifact is fetched and sha256-verified, BEFORE any DB write —
so an unknown module or a tampered artifact aborts the whole convergence with the
database untouched.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from preproc.module.deploy import (
    deploy_artifact,
    fetch_and_verify_artifact,
    module_register,
    refresh,
    resolve_scope,
    rule_ensure,
)
from preproc.module.registry import RegistryEntry, RegistryError, read_registry


class SyncError(Exception):
    """Raised when a desired-state file is malformed or names an unresolvable module."""


@dataclass(frozen=True)
class DesiredModule:
    """One desired ``module@version`` plus an optional scope override."""

    name: str
    version: int
    scope: tuple[str, str] | None = None


@dataclass(frozen=True)
class SyncResult:
    """What a convergence did, for reporting without a re-query."""

    added: int = 0
    updated: int = 0
    retired: int = 0
    unchanged: int = 0
    refreshed: bool = False

    @property
    def changed(self) -> bool:
        """True when the convergence mutated the database."""
        return bool(self.added or self.updated or self.retired or self.refreshed)


def _parse_scope(raw: object) -> tuple[str, str] | None:
    """Parse a scope override from a string ``"TYPE:VALUE"`` or a ``{type, value}`` table."""
    if raw is None:
        return None
    if isinstance(raw, str):
        scope_type, sep, value = raw.partition(":")
        if not sep or not value:
            raise SyncError(f"scope {raw!r} must be 'ROLE:<name>' or 'USER:<name>'")
        return scope_type.strip().upper(), value.strip()
    if isinstance(raw, dict) and raw.get("type") and raw.get("value"):
        return str(raw["type"]).upper(), str(raw["value"])
    raise SyncError(f"scope {raw!r} must be a 'TYPE:value' string or a {{type, value}} table")


def parse_desired_state(path: Path) -> list[DesiredModule]:
    """Read a desired-state TOML file into a list of ``DesiredModule``.

    Accepted forms under the top-level ``modules`` key:

    * an array of ``"name@version"`` strings, or
    * an array of tables, each with ``name`` + ``version`` (or a combined
      ``module = "name@version"``) and an optional ``scope``
      (``"TYPE:value"`` string or ``{type, value}`` table).
    """
    with path.open("rb") as handle:
        data = tomllib.load(handle)

    raw = data.get("modules", [])
    if not isinstance(raw, list):
        raise SyncError("desired-state file must define a 'modules' array")

    desired: list[DesiredModule] = []
    for item in raw:
        if isinstance(item, str):
            desired.append(_desired_from_spec(item, None))
        elif isinstance(item, dict):
            if "name" in item and "version" in item:
                name, version = item["name"], int(item["version"])
            elif "module" in item:
                parsed = _desired_from_spec(str(item["module"]), None)
                name, version = parsed.name, parsed.version
            else:
                raise SyncError(f"module entry {item!r} needs 'name'+'version' or 'module'")
            desired.append(DesiredModule(name, version, _parse_scope(item.get("scope"))))
        else:
            raise SyncError(f"unsupported module entry: {item!r}")
    return desired


def _desired_from_spec(spec: str, scope: tuple[str, str] | None) -> DesiredModule:
    name, sep, version = spec.partition("@")
    if not sep or not version:
        raise SyncError(f"module spec {spec!r} must be 'name@version'")
    try:
        return DesiredModule(name.strip(), int(version), scope)
    except ValueError as exc:
        raise SyncError(f"module spec {spec!r} has a non-integer version") from exc


# --- live-state queries -----------------------------------------------------


def _current_deployed(connection: Any) -> dict[str, tuple[int, str]]:
    """Return ``{name: (version, script_name)}`` for every ``deployed`` module version.

    Reads the provenance view. When (transiently) more than one version of a
    name is deployed, the highest version wins as the current one.
    """
    rows = connection.execute(
        "SELECT name, version, script_name FROM PREPROC_RT.INSTALLED_MODULES "
        "WHERE status = 'deployed' ORDER BY name, version"
    ).fetchall()
    current: dict[str, tuple[int, str]] = {}
    for name, version, script_name in rows:
        current[name] = (int(version), script_name)
    return current


def _enabled_rule_ids_for_script(connection: Any, script_name: str) -> list[int]:
    """rule_ids of ENABLED CONFIG rules naming ``script_name``, ascending."""
    rows = connection.execute(
        "SELECT rule_id FROM PREPROC.CONFIG WHERE enabled = TRUE AND script_name = {sn} "
        "ORDER BY rule_id",
        {"sn": script_name},
    ).fetchall()
    return [int(row[0]) for row in rows]


def _repoint(connection: Any, rule_id: int, script_name: str, function_name: str) -> None:
    """Repoint one CONFIG rule onto a new script/function (autocommitted, no rebuild)."""
    connection.execute(
        "UPDATE PREPROC.CONFIG SET script_name = {sn}, function_name = {fn} WHERE rule_id = {rid}",
        {"sn": script_name, "fn": function_name, "rid": rule_id},
    )


def _drop_rules_for_script(connection: Any, script_name: str) -> int:
    """Drop every enabled rule naming ``script_name`` (norefresh); return the count."""
    rule_ids = _enabled_rule_ids_for_script(connection, script_name)
    for rule_id in rule_ids:
        connection.execute(
            "EXECUTE SCRIPT PREPROC.RULE_DROP({rid}, TRUE)", {"rid": rule_id}
        ).fetchall()
    return len(rule_ids)


def module_retire(connection: Any, name: str, version: int) -> None:
    """Mark a module version retired via ``PREPROC.MODULE_RETIRE`` (provenance only)."""
    connection.execute(
        "EXECUTE SCRIPT PREPROC.MODULE_RETIRE({n}, {v})", {"n": name, "v": version}
    ).fetchall()


# --- op appliers (all run norefresh; the caller drives the single refresh) ---


def _apply_add(
    connection: Any,
    entry: RegistryEntry,
    scope: tuple[str, str] | None,
    registry: str,
    reader,
    script_text: str | None,
) -> bool:
    """Deploy a new module version and register rule + provenance; return rule_created."""
    if entry.deploy_mode == "library-deployed":
        deploy_artifact(
            connection, script_text or fetch_and_verify_artifact(entry, registry, reader=reader)
        )
    scope_type, scope_value = resolve_scope(entry, scope)
    _rule_id, created = rule_ensure(
        connection,
        scope_type=scope_type,
        scope_value=scope_value,
        phase=entry.phase,
        script_name=entry.script_name,
        function_name=entry.function,
        norefresh=True,
    )
    module_register(connection, entry)
    return created


def _apply_update(
    connection: Any,
    old_script: str,
    entry: RegistryEntry,
    scope: tuple[str, str] | None,
    registry: str,
    reader,
    script_text: str | None,
) -> bool:
    """Deploy V+1, register it, and repoint the old rule onto it; return config_changed.

    When no enabled rule names the old script (e.g. the rule was removed out of
    band), a fresh rule is ensured at the desired scope so the new version is
    still dispatched.
    """
    if entry.deploy_mode == "library-deployed":
        deploy_artifact(
            connection, script_text or fetch_and_verify_artifact(entry, registry, reader=reader)
        )
    module_register(connection, entry)

    rule_ids = _enabled_rule_ids_for_script(connection, old_script)
    if rule_ids:
        for rule_id in rule_ids:
            _repoint(connection, rule_id, entry.script_name, entry.function)
        return True

    scope_type, scope_value = resolve_scope(entry, scope)
    _rule_id, created = rule_ensure(
        connection,
        scope_type=scope_type,
        scope_value=scope_value,
        phase=entry.phase,
        script_name=entry.script_name,
        function_name=entry.function,
        norefresh=True,
    )
    return created


def _index_by_name_version(entries) -> dict[tuple[str, int], RegistryEntry]:
    return {(entry.name, entry.version): entry for entry in entries}


def _latest_by_name(entries) -> dict[str, RegistryEntry]:
    latest: dict[str, RegistryEntry] = {}
    for entry in sorted(entries, key=lambda e: (e.name, e.version)):
        latest[entry.name] = entry
    return latest


def converge(
    connection: Any,
    desired: list[DesiredModule],
    registry: str,
    *,
    reader=None,
) -> SyncResult:
    """Converge the database to ``desired`` and return what changed.

    See the module docstring for the diff, ordering, single-refresh, and
    idempotency guarantees.
    """
    index = read_registry(registry, reader=reader)
    by_nv = _index_by_name_version(index.entries)

    # --- resolve every desired entry up front (fail before any DB write) ---
    resolved: list[tuple[DesiredModule, RegistryEntry]] = []
    for want in desired:
        entry = by_nv.get((want.name, want.version))
        if entry is None:
            raise SyncError(f"no registry entry for {want.name}@{want.version}")
        resolved.append((want, entry))

    current = _current_deployed(connection)
    desired_names = {want.name for want in desired}

    adds, updates, noops = [], [], []
    for want, entry in resolved:
        if want.name not in current:
            adds.append((want, entry))
        elif current[want.name][0] == want.version:
            noops.append((want, entry))
        else:
            updates.append((want, entry, current[want.name]))
    retires = [
        (name, version, script)
        for name, (version, script) in current.items()
        if name not in desired_names
    ]

    # --- pre-fetch + verify all library artifacts before any DB write ---
    prefetched: dict[tuple[str, int], str] = {}
    for _want, entry in adds:
        if entry.deploy_mode == "library-deployed":
            prefetched[(entry.name, entry.version)] = fetch_and_verify_artifact(
                entry, registry, reader=reader
            )
    for _want, entry, _cur in updates:
        if entry.deploy_mode == "library-deployed":
            prefetched[(entry.name, entry.version)] = fetch_and_verify_artifact(
                entry, registry, reader=reader
            )

    # --- deploy phase (norefresh); collect provenance retirements for later ---
    config_changed = False
    pending_retires: list[tuple[str, int]] = []

    for want, entry in adds:
        created = _apply_add(
            connection,
            entry,
            want.scope,
            registry,
            reader,
            prefetched.get((entry.name, entry.version)),
        )
        config_changed = config_changed or created

    for want, entry, (old_version, old_script) in updates:
        changed = _apply_update(
            connection,
            old_script,
            entry,
            want.scope,
            registry,
            reader,
            prefetched.get((entry.name, entry.version)),
        )
        config_changed = config_changed or changed
        pending_retires.append((entry.name, old_version))

    for want, entry in noops:
        scope_type, scope_value = resolve_scope(entry, want.scope)
        _rule_id, created = rule_ensure(
            connection,
            scope_type=scope_type,
            scope_value=scope_value,
            phase=entry.phase,
            script_name=entry.script_name,
            function_name=entry.function,
            norefresh=True,
        )
        config_changed = config_changed or created

    for name, version, script in retires:
        if _drop_rules_for_script(connection, script):
            config_changed = True
        pending_retires.append((name, version))

    # --- the single refresh (only when CONFIG actually changed) ---
    if config_changed:
        refresh(connection)

    # --- provenance retirement, after the refresh ---
    for name, version in pending_retires:
        module_retire(connection, name, version)

    return SyncResult(
        added=len(adds),
        updated=len(updates),
        retired=len(retires),
        unchanged=len(noops),
        refreshed=config_changed,
    )


def sync(connection: Any, desired_path: Path, registry: str, *, reader=None) -> SyncResult:
    """Parse a desired-state file and converge the database to it."""
    return converge(connection, parse_desired_state(desired_path), registry, reader=reader)


def update_module(
    connection: Any,
    name: str,
    registry: str,
    *,
    scope: tuple[str, str] | None = None,
    reader=None,
) -> SyncResult:
    """Update one deployed module to the registry's latest version via the update flow."""
    index = read_registry(registry, reader=reader)
    entry = _latest_by_name(index.entries).get(name)
    if entry is None:
        raise RegistryError(f"no registry entry for module {name!r}")

    current = _current_deployed(connection)
    if name not in current:
        raise SyncError(f"module {name!r} is not deployed; use 'add'")

    old_version, old_script = current[name]
    if old_version == entry.version:
        return SyncResult(unchanged=1)

    script_text = (
        fetch_and_verify_artifact(entry, registry, reader=reader)
        if entry.deploy_mode == "library-deployed"
        else None
    )
    changed = _apply_update(connection, old_script, entry, scope, registry, reader, script_text)
    if changed:
        refresh(connection)
    module_retire(connection, name, old_version)
    return SyncResult(updated=1, refreshed=changed)


def remove_module(connection: Any, name: str) -> SyncResult:
    """Remove a deployed module: drop its rule(s), single refresh, retire provenance."""
    current = _current_deployed(connection)
    if name not in current:
        return SyncResult(unchanged=1)

    version, script = current[name]
    dropped = _drop_rules_for_script(connection, script)
    if dropped:
        refresh(connection)
    module_retire(connection, name, version)
    return SyncResult(retired=1, refreshed=bool(dropped))
