# Feature: preproc install CLI Verb

Provides the `preproc install` command that connects to an Exasol instance and applies the full two-schema layout plus the MASTER skeleton, then prints the activation statement for the DBA to run manually. The verb is the operator's single entry point for bootstrapping the framework and is safe to re-run.

## Background

* Connection parameters are read from the environment: `EXASOL_DSN`, `EXASOL_USER`, `EXASOL_PASSWORD`. No connection secrets are passed on the command line.
* The CLI is a thin pyexasol wrapper; it applies the SQL DDL files (source of truth) and deploys the MASTER Lua script, in dependency order.
* Install is idempotent — re-running against an installed database completes successfully and changes nothing.
* Install creates all objects but does NOT activate the preprocessor. Activation (`ALTER SYSTEM SET sql_preprocessor_script = PREPROC_RT.MASTER`) is left to the DBA.

## Scenarios

<!-- DELTA:CHANGED -->
### Scenario: Install bootstraps a clean database

* *GIVEN* a reachable Exasol instance with the framework not yet installed
* *AND* `EXASOL_DSN`, `EXASOL_USER`, and `EXASOL_PASSWORD` are set in the environment
* *WHEN* the operator runs `preproc install`
* *THEN* the command MUST create schemas `PREPROC` and `PREPROC_RT` with their tables, view, and grants
* *AND* the command MUST deploy the MASTER script into `PREPROC_RT`
* *AND* the command MUST deploy the `DEMO_PING_V1` module script into `PREPROC_RT`
* *AND* the command MUST exit with status 0
<!-- /DELTA:CHANGED -->
