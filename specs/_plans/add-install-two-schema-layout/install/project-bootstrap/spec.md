# Feature: Project Bootstrap

Establishes the development scaffolding the rest of the framework is built and tested on: the uv-managed Python project, the luacheck configuration for Lua linting, and the pytest integration fixture that yields a pyexasol connection driven by environment variables — so the same test suite runs against an ephemeral `exasol/docker-db` container in CI or any live instance locally.

## Background

* The project is managed with uv; `uv sync` produces a working environment with pyexasol, pytest, and ruff available.
* The repository layout follows the mission: `src/preproc/` for the CLI, `sql/install|lua|udf/` for DDL and scripts, `tests/` for the pytest suite.
* The integration test fixture resolves connection parameters from `EXASOL_DSN`, `EXASOL_USER`, `EXASOL_PASSWORD`; the connection abstraction is identical whether the target is Docker or a live instance.
* Lua sources are linted with luacheck using a checked-in configuration.

## Scenarios

<!-- DELTA:NEW -->
### Scenario: uv sync produces a working environment

* *GIVEN* a clone of the repository with `pyproject.toml` present
* *WHEN* `uv sync` is run
* *THEN* the command MUST exit with status 0
* *AND* the `preproc` console entry point MUST be importable and invokable
* *AND* pytest, pyexasol, and ruff MUST be available in the environment
<!-- /DELTA:NEW -->

<!-- DELTA:NEW -->
### Scenario: The connection fixture targets the env-configured instance

* *GIVEN* `EXASOL_DSN`, `EXASOL_USER`, `EXASOL_PASSWORD` are set
* *WHEN* the pytest connection fixture is requested by a test
* *THEN* the fixture MUST yield an open pyexasol connection to the configured instance
* *AND* the same fixture MUST work unchanged whether the target is a Docker container or a live instance
* *AND* the fixture MUST close the connection when the test completes
<!-- /DELTA:NEW -->

<!-- DELTA:NEW -->
### Scenario: Integration tests skip cleanly when no instance is configured

* *GIVEN* the connection environment variables are unset
* *WHEN* the integration test suite is collected and run
* *THEN* integration tests requiring a database MUST be skipped, not errored
* *AND* the skip reason MUST state that no Exasol instance is configured
<!-- /DELTA:NEW -->

<!-- DELTA:NEW -->
### Scenario: luacheck configuration is present and passes

* *GIVEN* the checked-in luacheck configuration and the `sql/lua/` directory
* *WHEN* `luacheck sql/lua/` is run
* *THEN* the command MUST exit with status 0
* *AND* the command MUST report zero warnings and zero errors
<!-- /DELTA:NEW -->

<!-- DELTA:NEW -->
### Scenario: Lint and format checks pass on the source tree

* *GIVEN* the bootstrapped project
* *WHEN* `uv run ruff check .` and `uv run ruff format --check .` are run
* *THEN* both commands MUST exit with status 0
* *AND* neither command SHALL report changes or violations
<!-- /DELTA:NEW -->
