"""Connection helpers for the preprocessor framework.

Reads Exasol connection parameters from environment variables:
  EXASOL_DSN      — host:port of the Exasol cluster
  EXASOL_USER     — database user
  EXASOL_PASSWORD — database password (never logged or surfaced in errors)

Optional:
  EXASOL_TLS_VERIFY — set to '0' or 'false' to disable TLS certificate verification
"""

import os
import ssl
from typing import Any

import pyexasol

_REQUIRED_ENV_VARS = ("EXASOL_DSN", "EXASOL_USER", "EXASOL_PASSWORD")


class MissingEnvError(Exception):
    """Raised when one or more required connection environment variables are absent.

    Attributes:
        missing: names of the unset or empty variables.
    """

    def __init__(self, missing: list[str]) -> None:
        self.missing = missing
        names = ", ".join(missing)
        super().__init__(f"Missing required environment variable(s): {names}")


def missing_env_vars() -> list[str]:
    """Return the names of required env vars that are unset or empty."""
    return [var for var in _REQUIRED_ENV_VARS if not os.environ.get(var)]


def connect_from_env() -> Any:
    """Open a pyexasol connection using environment variables.

    Raises:
        MissingEnvError: if any of EXASOL_DSN, EXASOL_USER, EXASOL_PASSWORD
            are unset or empty.  All missing variables are reported at once.
        pyexasol.ExaConnectionError: if the connection attempt fails.
    """
    absent = missing_env_vars()
    if absent:
        raise MissingEnvError(absent)

    dsn = os.environ["EXASOL_DSN"]
    user = os.environ["EXASOL_USER"]
    password = os.environ["EXASOL_PASSWORD"]

    kwargs: dict[str, Any] = {
        "dsn": dsn,
        "user": user,
        "password": password,
    }

    tls_verify = os.environ.get("EXASOL_TLS_VERIFY", "").lower()
    if tls_verify in ("0", "false"):
        kwargs["websocket_sslopt"] = {"cert_reqs": ssl.CERT_NONE}

    return pyexasol.connect(**kwargs)
