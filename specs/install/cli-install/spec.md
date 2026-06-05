# Feature: preproc install CLI Verb

Provides the `preproc install` command that connects to an Exasol instance and applies the full two-schema layout plus the MASTER skeleton, modules, and admin scripts, then prints the activation statement for the DBA to run manually. This slice extends the install to additionally deploy the admin control scripts and the production `CONTROL_SUGAR_V1` convenience module, and to seed the convenience EXPAND bootstrap rule.

## Background

* Connection parameters are read from the environment: `EXASOL_DSN`, `EXASOL_USER`, `EXASOL_PASSWORD`. No connection secrets are passed on the command line.
* The install applies all `sql/install/*.sql` files in sorted filename order, then deploys the Lua scripts and the demo UDF into `PREPROC_RT` and the admin scripts into `PREPROC`.
* All demo modules are deployed under versioned `DEMO_*_V1` names via `CREATE OR REPLACE LUA SCRIPT`; the demo transpile UDF is deployed via `CREATE OR REPLACE PYTHON3 SCALAR SCRIPT`. Re-deploying is idempotent.
* The runtime schema `PREPROC_RT` carries schema-level PUBLIC EXECUTE grants, so newly deployed scripts and the UDF are automatically executable by PUBLIC without per-object grants.
* Install creates all objects but does NOT activate the preprocessor. Activation (`ALTER SYSTEM SET sql_preprocessor_script = PREPROC_RT.MASTER`) is left to the DBA.

## Scenarios

### Scenario: Install bootstraps a clean database

* *GIVEN* a reachable Exasol instance with the framework not yet installed and connection environment variables set
* *WHEN* the operator runs `preproc install`
* *THEN* the command MUST create schemas `PREPROC` and `PREPROC_RT` with their tables, view, and grants
* *AND* the command MUST deploy the MASTER script plus the demo modules `DEMO_PING_V1`, `DEMO_TRANSLATE_V1`, and `DEMO_AUDIT_V1` into `PREPROC_RT`
* *AND* the command MUST deploy the scalar UDF `PREPROC_RT.DEMO_SQLGLOT_TRANSPILE_V1`
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

### Scenario: Installed demo modules and UDF are executable by PUBLIC

* *GIVEN* a database where `preproc install` has completed
* *WHEN* the runtime schema objects are inspected
* *THEN* the scripts `PREPROC_RT.DEMO_TRANSLATE_V1` and `PREPROC_RT.DEMO_AUDIT_V1` MUST be executable by PUBLIC via the schema-level grant
* *AND* the UDF `PREPROC_RT.DEMO_SQLGLOT_TRANSPILE_V1` MUST be executable by PUBLIC via the schema-level grant
* *AND* re-running `preproc install` MUST redeploy all demo objects without error

### Scenario: Install deploys the control scripts and convenience module

* *GIVEN* a clean Exasol instance reachable via the connection environment variables
* *WHEN* the operator runs `preproc install`
* *THEN* the install MUST deploy the admin control scripts (`RULE_ADD`, `RULE_DROP`, `RULE_ENABLE`, `RULE_DISABLE`, `RULE_LIST`, `SET_MODE`, `SET_SELF_DISABLE`) into the `PREPROC` admin schema alongside `REFRESH` and `STATUS`
* *AND* the install MUST deploy the production convenience module `PREPROC_RT.CONTROL_SUGAR_V1` into the runtime schema
* *AND* re-running `preproc install` MUST remain a safe no-op for these objects

### Scenario: Install seeds the convenience EXPAND bootstrap rule

* *GIVEN* a clean Exasol instance
* *WHEN* the operator runs `preproc install`
* *THEN* `PREPROC.CONFIG` MUST contain an enabled EXPAND rule scoped to `PREPROC_ADMIN` that points at the convenience module
* *AND* that seed MUST be guarded so re-running `preproc install` does not duplicate the rule
* *AND* the install MUST NOT activate the preprocessor (the activation `ALTER SYSTEM` statement is still only printed)
