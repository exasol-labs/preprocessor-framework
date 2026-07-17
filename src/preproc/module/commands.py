"""The ``preproc module`` command group: the operator's air-gap bridge.

Wires the ``module`` subcommand tree into the ``preproc`` CLI and dispatches each
verb. ``list``, ``search``, ``bundle``, and static ``validate`` are pure registry
reads and never open a database connection. ``add``, ``update``, ``remove``,
``sync``, and live ``validate`` resolve a connection from the environment and
exit with the missing-environment code (matching ``preproc install``) when it is
absent.

Exit codes:
    0  — success
    1  — registry/SQL/validation failure
    2  — missing environment variables (mutating verbs only; no connection made)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from preproc.connection import MissingEnvError, connect_from_env, missing_env_vars
from preproc.module import sync as sync_mod
from preproc.module.deploy import deploy_module
from preproc.module.offline_bundle import run_offline_bundle
from preproc.module.registry import RegistryError, RegistryIndex, read_registry
from preproc.module.validate import ValidateError, validate_module

DEFAULT_REGISTRY = (
    "https://raw.githubusercontent.com/exasol-labs/preprocessor-library/main/registry/index.json"
)

_MISSING_ENV_EXIT = 2
_FAILURE_EXIT = 1


def _parse_scope_arg(value: str | None) -> tuple[str, str] | None:
    """Parse a ``--scope`` override string ``TYPE:value`` into (type, value)."""
    if value is None:
        return None
    scope_type, sep, name = value.partition(":")
    if not sep or not name:
        raise ValueError(f"--scope must be 'ROLE:<name>' or 'USER:<name>', got {value!r}")
    return scope_type.upper(), name


def _latest_entry(index: RegistryIndex, name: str):
    """Return the highest-version registry entry for ``name`` or raise."""
    matches = sorted((e for e in index.entries if e.name == name), key=lambda e: e.version)
    if not matches:
        raise RegistryError(f"module {name!r} not found in registry")
    return matches[-1]


def _format_entry(entry) -> str:
    return f"{entry.name}  v{entry.version}  {entry.phase}  {entry.deploy_mode}"


def _close(connection: Any) -> None:
    try:
        connection.close()
    except Exception:
        pass


def _connect_or_exit() -> tuple[Any | None, int]:
    """Open a connection, or return (None, missing-env exit code) when unconfigured."""
    try:
        return connect_from_env(), 0
    except MissingEnvError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return None, _MISSING_ENV_EXIT


# --- read-only verbs (no database) -----------------------------------------


def _cmd_list(args: argparse.Namespace) -> int:
    try:
        index = read_registry(args.registry)
    except RegistryError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return _FAILURE_EXIT
    entries = sorted(index.entries, key=lambda e: (e.name, e.version))
    if not entries:
        print("No modules in registry.")
        return 0
    for entry in entries:
        print(_format_entry(entry))
    return 0


def _cmd_search(args: argparse.Namespace) -> int:
    try:
        index = read_registry(args.registry)
    except RegistryError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return _FAILURE_EXIT
    term = args.term.lower()
    matches = sorted(
        (e for e in index.entries if term in e.name.lower() or term in e.description.lower()),
        key=lambda e: (e.name, e.version),
    )
    for entry in matches:
        print(_format_entry(entry))
    return 0


def _cmd_bundle(args: argparse.Namespace) -> int:
    try:
        index = read_registry(args.registry)
        entries = [_latest_entry(index, name) for name in args.names]
        count = run_offline_bundle(entries, args.registry, Path(args.output))
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return _FAILURE_EXIT
    print(f"Wrote {count} statements to {args.output}")
    return 0


def _cmd_validate(args: argparse.Namespace) -> int:
    try:
        index = read_registry(args.registry)
        entry = _latest_entry(index, args.name)
    except RegistryError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return _FAILURE_EXIT

    connection = None
    if not missing_env_vars():
        try:
            connection = connect_from_env()
        except Exception as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return _FAILURE_EXIT

    try:
        results = validate_module(entry, args.registry, connection=connection)
    except ValidateError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return _FAILURE_EXIT
    finally:
        if connection is not None:
            _close(connection)

    for result in results:
        print(f"  ok: {result}")
    print(f"validate: {entry.name} v{entry.version} OK")
    return 0


# --- mutating verbs (require a database) ------------------------------------


def _cmd_add(args: argparse.Namespace) -> int:
    connection, code = _connect_or_exit()
    if connection is None:
        return code
    try:
        index = read_registry(args.registry)
        entry = _latest_entry(index, args.name)
        scope = _parse_scope_arg(args.scope)
        result = deploy_module(connection, entry, args.registry, scope=scope)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return _FAILURE_EXIT
    finally:
        _close(connection)
    print(
        f"Added {result.name} v{result.version} at {result.scope_type} "
        f"{result.scope_value} (rule {result.rule_id})"
    )
    return 0


def _cmd_update(args: argparse.Namespace) -> int:
    connection, code = _connect_or_exit()
    if connection is None:
        return code
    try:
        scope = _parse_scope_arg(args.scope)
        result = sync_mod.update_module(connection, args.name, args.registry, scope=scope)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return _FAILURE_EXIT
    finally:
        _close(connection)
    if result.updated:
        print(f"Updated {args.name}")
    else:
        print(f"{args.name} already at the latest version; no changes")
    return 0


def _cmd_remove(args: argparse.Namespace) -> int:
    connection, code = _connect_or_exit()
    if connection is None:
        return code
    try:
        result = sync_mod.remove_module(connection, args.name)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return _FAILURE_EXIT
    finally:
        _close(connection)
    if result.retired:
        print(f"Removed {args.name}")
    else:
        print(f"{args.name} is not deployed; no changes")
    return 0


def _cmd_sync(args: argparse.Namespace) -> int:
    connection, code = _connect_or_exit()
    if connection is None:
        return code
    try:
        result = sync_mod.sync(connection, Path(args.manifest), args.registry)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return _FAILURE_EXIT
    finally:
        _close(connection)
    if not result.changed:
        print("sync: no changes")
    else:
        print(
            f"sync: added {result.added}, updated {result.updated}, "
            f"retired {result.retired}, unchanged {result.unchanged}"
        )
    return 0


_DISPATCH = {
    "list": _cmd_list,
    "search": _cmd_search,
    "bundle": _cmd_bundle,
    "validate": _cmd_validate,
    "add": _cmd_add,
    "update": _cmd_update,
    "remove": _cmd_remove,
    "sync": _cmd_sync,
}


def add_module_parser(subparsers: Any) -> argparse.ArgumentParser:
    """Attach the ``module`` command group and its subcommands to ``subparsers``."""
    module = subparsers.add_parser(
        "module",
        help="Discover, deploy, update, bundle, validate, and sync preprocessor modules.",
        description=(
            "The operator's air-gap bridge for preprocessor modules. Reads a "
            "federated registry (local checkout or pinned https index) and, for "
            "mutating verbs, talks to the Exasol database over SQL. The database "
            "never fetches code."
        ),
    )
    module_subs = module.add_subparsers(dest="module_command", metavar="<subcommand>")

    registry_parent = argparse.ArgumentParser(add_help=False)
    registry_parent.add_argument(
        "--registry",
        default=DEFAULT_REGISTRY,
        help="Registry index path or https URL (default: the central library index).",
    )

    module_subs.add_parser(
        "list", parents=[registry_parent], help="List registry modules (no database)."
    )

    p_search = module_subs.add_parser(
        "search", parents=[registry_parent], help="Search registry modules by term (no database)."
    )
    p_search.add_argument("term", help="Substring to match against module name or description.")

    p_add = module_subs.add_parser(
        "add", parents=[registry_parent], help="Deploy a module and register its rule."
    )
    p_add.add_argument("name", help="Module name to add.")
    p_add.add_argument(
        "--scope",
        default=None,
        help="Override the suggested scope: 'ROLE:<name>' or 'USER:<name>'.",
    )

    p_update = module_subs.add_parser(
        "update", parents=[registry_parent], help="Update a deployed module to the latest version."
    )
    p_update.add_argument("name", help="Module name to update.")
    p_update.add_argument(
        "--scope", default=None, help="Scope for a re-created rule if none exists to repoint."
    )

    p_remove = module_subs.add_parser(
        "remove", parents=[registry_parent], help="Remove a module's rule and retire it."
    )
    p_remove.add_argument("name", help="Module name to remove.")

    p_bundle = module_subs.add_parser(
        "bundle",
        parents=[registry_parent],
        help="Emit a self-contained offline .sql for the named modules (no database).",
    )
    p_bundle.add_argument("names", nargs="+", help="Module names to bundle.")
    p_bundle.add_argument("--output", required=True, help="Path to write the offline bundle .sql.")

    p_validate = module_subs.add_parser(
        "validate",
        parents=[registry_parent],
        help="Validate a module statically; live smoke-test when a database is configured.",
    )
    p_validate.add_argument("name", help="Module name to validate.")

    p_sync = module_subs.add_parser(
        "sync", parents=[registry_parent], help="Converge the database to a desired-state file."
    )
    p_sync.add_argument("manifest", help="Path to the desired-state TOML file.")

    return module


def run_module(args: argparse.Namespace) -> int:
    """Dispatch a ``preproc module`` subcommand; return the process exit code."""
    command = getattr(args, "module_command", None)
    if command is None:
        print(
            "Error: a module subcommand is required "
            "(list, search, add, update, remove, bundle, validate, sync).",
            file=sys.stderr,
        )
        return _FAILURE_EXIT
    return _DISPATCH[command](args)
