# Feature: Single-File Deploy Bundle Generator

Provides the `preproc bundle` CLI verb, which emits a single generated `deploy_all.sql` file an operator can run end-to-end against an Exasol instance. The bundle concatenates the numbered install DDL with `CREATE OR REPLACE` wrappers for every Lua script and the demo Python UDF. This slice extends the bundle to additionally wrap the admin control scripts and the `CONTROL_SUGAR_V1` convenience module so the generated file stays equivalent to `preproc install`.

## Background

* The bundle is always generated, never hand-edited; its source of truth is `sql/install/*.sql` plus the `sql/lua/*.lua` and `sql/udf/*.sql` sources.
* The Lua and UDF wrappers are byte-identical to the `CREATE OR REPLACE` strings `preproc install` sends, so the bundle and the CLI install produce identical objects (ADR-016).
* The bundle MUST NOT include the activation `ALTER SYSTEM` statement nor any demo seed data: structure + code only.

## Scenarios

<!-- DELTA:CHANGED -->
### Scenario: Bundle wraps every Lua script and the demo UDF as CREATE OR REPLACE

* *GIVEN* the Lua sources for MASTER, the demo modules, REFRESH, STATUS, the admin control scripts, and the convenience module, plus the demo transpile UDF source
* *WHEN* the operator runs `preproc bundle`
* *THEN* the generated file MUST contain a `CREATE OR REPLACE LUA SCRIPT` wrapper for each Lua source under the same schema and signature `preproc install` uses
* *AND* the generated file MUST contain wrappers for the admin control scripts in the `PREPROC` schema and for the `PREPROC_RT.CONTROL_SUGAR_V1` convenience module
* *AND* the generated file MUST contain a `CREATE OR REPLACE PYTHON3 SCALAR SCRIPT` wrapper for the demo transpile UDF
<!-- /DELTA:CHANGED -->

<!-- DELTA:CHANGED -->
### Scenario: The generated bundle deploys an equivalent layout to CLI install

* *GIVEN* a freshly generated `deploy_all.sql`
* *WHEN* the file is executed against a clean Exasol instance through an interactive client
* *THEN* the resulting schemas, tables, views, grants, Lua scripts (including the admin control scripts and the convenience module), and the demo UDF MUST match those produced by `preproc install`
* *AND* the bundle MUST NOT execute the activation `ALTER SYSTEM` statement
* *AND* the bundle MUST NOT insert any demo configuration rows
<!-- /DELTA:CHANGED -->
