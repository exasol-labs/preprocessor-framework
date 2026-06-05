"""CLI entry point for the preprocessor framework.

Usage:
    preproc install [--verbose]

Exit codes:
    0  — success
    1  — connection or SQL failure
    2  — missing environment variables (no connection attempted)
"""

import argparse
import sys

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
