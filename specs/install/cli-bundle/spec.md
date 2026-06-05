# Feature: Single-File Deploy Bundle Generator

Provides the `preproc bundle` CLI verb, which emits a single generated `deploy_all.sql` file an operator can run end-to-end against an Exasol instance. The bundle concatenates the numbered install DDL with `CREATE OR REPLACE` wrappers for every Lua script and the demo Python UDF. This slice extends the bundle to additionally wrap the admin control scripts and the `CONTROL_SUGAR_V1` convenience module so the generated file stays equivalent to `preproc install`.

## Background

* The bundle is always generated, never hand-edited. Its single source of truth is the existing `sql/install/*.sql` DDL files plus the `sql/lua/*.lua` and `sql/udf/*.sql` sources; `preproc bundle` reads those and assembles the output deterministically. A hand-edited bundle would drift from the sources and is explicitly disallowed.
* The output is delimited for interactive Exasol clients (EXAplus / DbVisualiser) using the `/` statement terminator on its own line after each statement. This is required because `CREATE LUA SCRIPT` and `CREATE PYTHON3 SCALAR SCRIPT` bodies contain semicolons; a plain `;`-split file would break those statements. The `/`-delimited form is the documented runnable format.
* The bundle assembles content in dependency order: schemas and roles first, then tables, views, grants (the numbered `sql/install/*.sql` order), then the Lua scripts wrapped as `CREATE OR REPLACE LUA SCRIPT <schema>.<name>(...) AS`, then the demo UDF wrapped as its `CREATE OR REPLACE PYTHON3 SCALAR SCRIPT`. The MASTER and admin (`REFRESH`, `STATUS`) script wrappers mirror exactly the wrappers `preproc install` already emits (same schema, same signature), so the bundle and the CLI install produce identical objects.
* The bundle MUST NOT include the activation `ALTER SYSTEM` statement nor any demo seed data: activation is a deliberate DBA action and demo config is a separate seed script. The bundle's job is structure + code only, matching what `preproc install` deploys.
* The bundle is written to a path the operator chooses (default `deploy_all.sql` in the working directory) and the command reports the path and statement count on success.

## Scenarios

### Scenario: Bundle emits a single deployable SQL file

* *GIVEN* a checked-out repository with the `sql/install`, `sql/lua`, and `sql/udf` sources present
* *WHEN* the operator runs `preproc bundle`
* *THEN* the command MUST write a single file (default `deploy_all.sql`)
* *AND* the command MUST exit with status 0
* *AND* the command MUST report the output path and the number of statements written

### Scenario: Bundle contains the full install DDL in numbered order

* *GIVEN* the numbered `sql/install/*.sql` files
* *WHEN* the operator runs `preproc bundle`
* *THEN* the generated file MUST contain every install statement
* *AND* the statements MUST appear in ascending filename order so schemas precede tables precede views precede grants

### Scenario: Bundle wraps every Lua script and the demo UDF as CREATE OR REPLACE

* *GIVEN* the Lua sources for MASTER, the demo modules, REFRESH, STATUS, the admin control scripts, and the convenience module, plus the demo transpile UDF source
* *WHEN* the operator runs `preproc bundle`
* *THEN* the generated file MUST contain a `CREATE OR REPLACE LUA SCRIPT` wrapper for each Lua source under the same schema and signature `preproc install` uses
* *AND* the generated file MUST contain wrappers for the admin control scripts in the `PREPROC` schema and for the `PREPROC_RT.CONTROL_SUGAR_V1` convenience module
* *AND* the generated file MUST contain a `CREATE OR REPLACE PYTHON3 SCALAR SCRIPT` wrapper for the demo transpile UDF

### Scenario: Bundle uses slash delimiters so script bodies survive

* *GIVEN* script bodies that contain semicolons
* *WHEN* the operator runs `preproc bundle`
* *THEN* each emitted statement MUST be terminated by a `/` on its own line
* *AND* the file MUST NOT rely on `;`-splitting that would break script bodies

### Scenario: The generated bundle deploys an equivalent layout to CLI install

* *GIVEN* a freshly generated `deploy_all.sql`
* *WHEN* the file is executed against a clean Exasol instance through an interactive client
* *THEN* the resulting schemas, tables, views, grants, Lua scripts (including the admin control scripts and the convenience module), and the demo UDF MUST match those produced by `preproc install`
* *AND* the bundle MUST NOT execute the activation `ALTER SYSTEM` statement
* *AND* the bundle MUST NOT insert any demo configuration rows

### Scenario: Bundle is reproducible and excludes activation and seed data

* *GIVEN* unchanged sources
* *WHEN* `preproc bundle` is run twice
* *THEN* the two generated files MUST be byte-for-byte identical
* *AND* neither file MUST contain an `ALTER SYSTEM SET sql_preprocessor_script` statement
* *AND* neither file MUST contain rows from the demo seed
