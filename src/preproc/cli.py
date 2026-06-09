"""CLI entry point for the preprocessor framework.

Usage:
    preproc install [--verbose]
    preproc bundle [--output PATH]

Exit codes:
    0  — success
    1  — connection or SQL failure, or bundle generation failure
    2  — missing environment variables (no connection attempted)
"""

import argparse
import sys
from pathlib import Path

from preproc.bundle import run_bundle, run_release_bundle
from preproc.connection import MissingEnvError, connect_from_env
from preproc.install import run_install


def _cmd_install(args: argparse.Namespace) -> int:
    """Handle the 'install' subcommand.

    Returns the process exit code.
    """
    try:
        connection = connect_from_env()
    except MissingEnvError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    try:
        run_install(connection, verbose=args.verbose)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    finally:
        try:
            connection.close()
        except Exception:
            pass

    return 0


def _cmd_bundle(args: argparse.Namespace) -> int:
    """Handle the 'bundle' subcommand.

    Generates the single-file deploy bundle offline (no database connection).
    With --release, generates the framework-only release.sql (no demo modules);
    otherwise the full deploy_all.sql (framework + demos + UDF). Returns the
    process exit code.
    """
    default_output = "release.sql" if args.release else "deploy_all.sql"
    output_path = Path(args.output if args.output is not None else default_output)
    try:
        count = run_release_bundle(output_path) if args.release else run_bundle(output_path)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(f"Wrote {count} statements to {output_path}")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="preproc",
        description="Exasol SQL preprocessor framework management tool.",
    )
    subparsers = parser.add_subparsers(dest="command", metavar="<command>")

    install_parser = subparsers.add_parser(
        "install",
        help="Bootstrap the preprocessor framework in an Exasol instance.",
        description=(
            "Apply the two-schema database layout and deploy the MASTER Lua script. "
            "Safe to re-run. Does not activate the preprocessor — prints the "
            "activation statement for the DBA to run manually."
        ),
    )
    install_parser.add_argument(
        "--verbose",
        action="store_true",
        default=False,
        help="Print each SQL statement before executing it.",
    )

    bundle_parser = subparsers.add_parser(
        "bundle",
        help="Generate a single-file deploy bundle (offline).",
        description=(
            "Assemble the install DDL and CREATE OR REPLACE wrappers into one "
            "slash-delimited SQL file. Offline: no database connection is made. "
            "Excludes the activation ALTER SYSTEM statement and demo seed data. "
            "Default output is the full deploy_all.sql (framework + demo modules "
            "+ UDF); pass --release for the framework-only release.sql."
        ),
    )
    bundle_parser.add_argument(
        "--release",
        action="store_true",
        default=False,
        help=(
            "Generate the framework-only release.sql (clean-install deploy: no "
            "demo modules, omits upgrade-only column ALTERs, ends with REFRESH)."
        ),
    )
    bundle_parser.add_argument(
        "--output",
        default=None,
        help=(
            "Path to write the bundle (default: release.sql with --release, "
            "otherwise deploy_all.sql, in the current directory)."
        ),
    )

    return parser


def main() -> None:
    """Dispatch preproc sub-commands."""
    parser = _build_parser()
    args = parser.parse_args()

    if args.command is None:
        parser.print_help(sys.stderr)
        sys.exit(1)

    if args.command == "install":
        sys.exit(_cmd_install(args))
    elif args.command == "bundle":
        sys.exit(_cmd_bundle(args))
    else:
        parser.error(f"unknown command: {args.command}")
