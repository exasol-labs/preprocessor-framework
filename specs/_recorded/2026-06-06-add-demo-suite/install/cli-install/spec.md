# Feature: preproc install CLI Verb

Provides the `preproc install` command that connects to an Exasol instance and applies the full two-schema layout plus the MASTER skeleton and demo modules, then prints the activation statement for the DBA to run manually. The verb is the operator's single entry point for bootstrapping the framework and is safe to re-run. This slice extends the install to additionally deploy the demo suite — the `DEMO_TRANSLATE_V1` and `DEMO_AUDIT_V1` Lua modules and the `DEMO_SQLGLOT_TRANSPILE_V1` Python UDF — alongside the existing `DEMO_PING_V1` module.

## Background

* The install applies all `sql/install/*.sql` files in sorted filename order, then deploys the Lua scripts and the demo UDF into `PREPROC_RT` and the admin scripts into `PREPROC`.
* All demo modules are deployed under versioned `DEMO_*_V1` names via `CREATE OR REPLACE LUA SCRIPT`; the demo transpile UDF is deployed via `CREATE OR REPLACE PYTHON3 SCALAR SCRIPT`. Re-deploying is idempotent.
* The runtime schema `PREPROC_RT` carries schema-level PUBLIC EXECUTE grants, so newly deployed scripts and the UDF are automatically executable by PUBLIC without per-object grants.
* The install never activates the preprocessor; it prints the `ALTER SYSTEM SET sql_preprocessor_script = PREPROC_RT.MASTER` statement for the DBA to run manually.

## Scenarios

<!-- DELTA:CHANGED -->
### Scenario: Install bootstraps a clean database

* *GIVEN* a reachable Exasol instance with the framework not yet installed and connection environment variables set
* *WHEN* the operator runs `preproc install`
* *THEN* the command MUST create schemas `PREPROC` and `PREPROC_RT` with their tables, view, and grants
* *AND* the command MUST deploy the MASTER script plus the demo modules `DEMO_PING_V1`, `DEMO_TRANSLATE_V1`, and `DEMO_AUDIT_V1` into `PREPROC_RT`
* *AND* the command MUST deploy the scalar UDF `PREPROC_RT.DEMO_SQLGLOT_TRANSPILE_V1`
* *AND* the command MUST exit with status 0
<!-- /DELTA:CHANGED -->

<!-- DELTA:NEW -->
### Scenario: Installed demo modules and UDF are executable by PUBLIC

* *GIVEN* a database where `preproc install` has completed
* *WHEN* the runtime schema objects are inspected
* *THEN* the scripts `PREPROC_RT.DEMO_TRANSLATE_V1` and `PREPROC_RT.DEMO_AUDIT_V1` MUST be executable by PUBLIC via the schema-level grant
* *AND* the UDF `PREPROC_RT.DEMO_SQLGLOT_TRANSPILE_V1` MUST be executable by PUBLIC via the schema-level grant
* *AND* re-running `preproc install` MUST redeploy all demo objects without error
<!-- /DELTA:NEW -->
