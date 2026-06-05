# Feature: preproc install CLI Verb

Provides the `preproc install` command that connects to an Exasol instance and applies the full two-schema layout plus the MASTER skeleton, then prints the activation statement for the DBA to run manually. The verb is the operator's single entry point for bootstrapping the framework and is safe to re-run.

## Background

* Connection parameters are read from the environment: `EXASOL_DSN`, `EXASOL_USER`, `EXASOL_PASSWORD`. No connection secrets are passed on the command line.
* The CLI is a thin pyexasol wrapper; it applies the SQL DDL files (source of truth) and deploys the MASTER Lua script, in dependency order.
* Install is idempotent — re-running against an installed database completes successfully and changes nothing.
* Install creates all objects but does NOT activate the preprocessor. Activation (`ALTER SYSTEM SET sql_preprocessor_script = PREPROC_RT.MASTER`) is left to the DBA.

## Scenarios

### Scenario: Install bootstraps a clean database

* *GIVEN* a reachable Exasol instance with the framework not yet installed
* *AND* `EXASOL_DSN`, `EXASOL_USER`, and `EXASOL_PASSWORD` are set in the environment
* *WHEN* the operator runs `preproc install`
* *THEN* the command MUST create schemas `PREPROC` and `PREPROC_RT` with their tables, view, and grants
* *AND* the command MUST deploy the MASTER script into `PREPROC_RT`
* *AND* the command MUST exit with status 0

### Scenario: Install prints the activation statement without executing it

* *GIVEN* a reachable Exasol instance with the environment configured
* *WHEN* the operator runs `preproc install`
* *THEN* the command MUST print the statement `ALTER SYSTEM SET sql_preprocessor_script = PREPROC_RT.MASTER`
* *AND* the command MUST NOT execute that statement
* *AND* the system-wide `sql_preprocessor_script` setting MUST remain unchanged after the command completes

### Scenario: Install is safe to re-run

* *GIVEN* an Exasol instance where `preproc install` has already completed
* *WHEN* the operator runs `preproc install` a second time
* *THEN* the command MUST exit with status 0
* *AND* the command MUST NOT raise an error about existing objects
* *AND* the database objects MUST be unchanged from the first run

### Scenario: Missing connection environment variables fail fast

* *GIVEN* one or more of `EXASOL_DSN`, `EXASOL_USER`, `EXASOL_PASSWORD` is unset
* *WHEN* the operator runs `preproc install`
* *THEN* the command MUST exit with a non-zero status
* *AND* the command MUST report which environment variable is missing
* *AND* the command MUST NOT attempt a database connection

### Scenario: Unreachable instance reports a connection error

* *GIVEN* the connection environment variables point to an unreachable instance
* *WHEN* the operator runs `preproc install`
* *THEN* the command MUST exit with a non-zero status
* *AND* the command MUST report a connection failure
* *AND* the command MUST NOT leave a partially created schema layout
