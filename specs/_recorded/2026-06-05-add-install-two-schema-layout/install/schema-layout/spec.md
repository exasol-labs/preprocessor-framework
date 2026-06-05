# Feature: Two-Schema Database Layout

Establishes the foundational database objects the preprocessor framework dispatches from: a locked admin schema (`PREPROC`) holding configuration and a PUBLIC-accessible runtime schema (`PREPROC_RT`) holding the dispatcher, views, and (later) generated scripts. The layout encodes the privilege bridge — definer-rights views in `PREPROC_RT` read admin tables that invoker-rights scripts cannot — and the schema-level PUBLIC grants that are a hard framework invariant.

## Background

* The DDL source of truth lives in numbered files under `sql/install/` and is applied in filename order.
* All object creation is idempotent: schemas, tables, and views use `IF NOT EXISTS`; views and scripts use `CREATE OR REPLACE`. Re-applying the layout against an up-to-date database is a no-op.
* The `PREPROC_ADMIN` role owns the `PREPROC` schema and every object in it.
* Schema-level grants (`GRANT EXECUTE ON SCHEMA PREPROC_RT TO PUBLIC` and `GRANT SELECT ON SCHEMA PREPROC_RT TO PUBLIC`) cover all current and future objects in the runtime schema. Missing grants cause complete user lockout in enforced mode.

## Scenarios

<!-- DELTA:NEW -->
### Scenario: Admin and runtime schemas are created

* *GIVEN* a database with neither `PREPROC` nor `PREPROC_RT` present
* *WHEN* the install DDL is applied
* *THEN* the system MUST create schema `PREPROC`
* *AND* the system MUST create schema `PREPROC_RT`
* *AND* both schemas SHALL be visible in `EXA_ALL_SCHEMAS`
<!-- /DELTA:NEW -->

<!-- DELTA:NEW -->
### Scenario: PREPROC_ADMIN role owns the admin schema

* *GIVEN* the install DDL is being applied
* *WHEN* schema `PREPROC` is created
* *THEN* the system MUST ensure role `PREPROC_ADMIN` exists
* *AND* the system MUST make `PREPROC_ADMIN` the owner of schema `PREPROC`
* *AND* the system MUST make `PREPROC_ADMIN` the owner of every table in `PREPROC`
<!-- /DELTA:NEW -->

<!-- DELTA:NEW -->
### Scenario: Admin configuration tables are created

* *GIVEN* schema `PREPROC` exists
* *WHEN* the install DDL is applied
* *THEN* the system MUST create table `PREPROC.CONFIG` with columns for role/user/client_pattern, phase, module script, function, enabled, and version
* *AND* the system MUST create table `PREPROC.RESOLUTION` with columns user_name, client_pattern, phase, script, function
* *AND* the system MUST create table `PREPROC.SETTINGS` with columns mode, self_disable_allowed, last_refresh
* *AND* the system MUST create table `PREPROC.PROFILES` mapping a hash to a manifest
<!-- /DELTA:NEW -->

<!-- DELTA:NEW -->
### Scenario: SETTINGS is seeded with a single default row

* *GIVEN* table `PREPROC.SETTINGS` has just been created and is empty
* *WHEN* the install DDL is applied
* *THEN* the system MUST insert exactly one row into `PREPROC.SETTINGS`
* *AND* the row SHALL default `mode` to convenience
* *AND* re-applying the DDL MUST NOT insert a second row
<!-- /DELTA:NEW -->

<!-- DELTA:NEW -->
### Scenario: The CURRENT_USER-filtered pipeline view is created

* *GIVEN* schemas `PREPROC` and `PREPROC_RT` exist with the admin tables present
* *WHEN* the install DDL is applied
* *THEN* the system MUST create view `PREPROC_RT.MY_PIPELINE`
* *AND* the view SHALL join `PREPROC.SETTINGS` with `PREPROC.RESOLUTION` filtered on `r.user_name = CURRENT_USER`
* *AND* the view SHALL order results by phase
* *AND* the view SHALL read the admin base tables with definer (owner) rights
<!-- /DELTA:NEW -->

<!-- DELTA:NEW -->
### Scenario: Schema-level PUBLIC grants cover the runtime schema

* *GIVEN* schema `PREPROC_RT` exists
* *WHEN* the install DDL is applied
* *THEN* the system MUST grant EXECUTE on schema `PREPROC_RT` to PUBLIC
* *AND* the system MUST grant SELECT on schema `PREPROC_RT` to PUBLIC
* *AND* a non-admin user SHALL be able to select from `PREPROC_RT.MY_PIPELINE`
<!-- /DELTA:NEW -->

<!-- DELTA:NEW -->
### Scenario: A non-admin user reads only their own pipeline rows

* *GIVEN* `PREPROC.RESOLUTION` contains rows for several distinct users
* *AND* the schema-level SELECT grant is in place
* *WHEN* a non-admin user queries `PREPROC_RT.MY_PIPELINE`
* *THEN* the result MUST contain only rows whose `user_name` equals that user
* *AND* the user MUST NOT be able to select directly from `PREPROC.RESOLUTION`
<!-- /DELTA:NEW -->

<!-- DELTA:NEW -->
### Scenario: Re-applying the layout is idempotent

* *GIVEN* a database where the full layout has already been applied
* *WHEN* the install DDL is applied a second time without changes
* *THEN* the operation MUST succeed without error
* *AND* the schemas, tables, and grants MUST remain unchanged
* *AND* no duplicate `PREPROC.SETTINGS` row SHALL be created
<!-- /DELTA:NEW -->
