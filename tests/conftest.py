"""Pytest fixtures for integration tests against an env-configured Exasol instance.

Connection parameters are resolved from the environment by ``preproc.connection``
so that the same suite runs unchanged against an ephemeral ``exasol/docker-db``
container in CI or any live instance locally. When the required variables are
unset the database fixtures skip cleanly rather than erroring.
"""

import pytest

from preproc.connection import connect_from_env, missing_env_vars
from preproc.install import run_install


@pytest.fixture(scope="session")
def db():
    """Yield an open pyexasol connection to the env-configured instance.

    Skips the requesting test when the connection environment is not configured,
    naming the missing variables, and closes the connection on teardown.
    """
    missing = missing_env_vars()
    if missing:
        pytest.skip("no Exasol instance is configured; set " + ", ".join(missing))

    connection = connect_from_env()
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture(scope="session")
def installed(db):
    """Run the idempotent CLI install once per session before layout assertions."""
    run_install(db)
    return db
