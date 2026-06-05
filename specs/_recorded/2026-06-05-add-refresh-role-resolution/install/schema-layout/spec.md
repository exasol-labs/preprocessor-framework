# Feature: Two-Schema Database Layout

Establishes the foundational database objects the preprocessor framework dispatches from: a locked admin schema (`PREPROC`) holding configuration and a PUBLIC-accessible runtime schema (`PREPROC_RT`) holding the dispatcher, views, and (later) generated scripts. This slice extends the layout for resolution: `RESOLUTION` gains an `ordinal` column for deterministic within-phase dispatch order, `SETTINGS` gains a state-fingerprint column for staleness detection, `MY_PIPELINE` exposes `client_pattern` and orders by `phase, ordinal`, and the `PREPROC_ADMIN` role gains `SELECT ANY DICTIONARY` so the admin-owned refresh script can read the role-grant dictionary tables.

## Background

* The DDL source of truth lives in numbered files under `sql/install/` and is applied in filename order.
* All object creation is idempotent: schemas, tables, and views use `IF NOT EXISTS` or `CREATE OR REPLACE`; column additions on already-installed databases ship as guarded `ALTER TABLE ... ADD COLUMN` statements that tolerate "already exists" via the `--@tolerate-exists` applier contract.
* The `PREPROC_ADMIN` role owns the `PREPROC` schema and every object in it, including the new refresh and status scripts. Objects in `PREPROC` are not granted to PUBLIC. All framework administration (refresh, status) is exposed as these DB-side scripts, callable from any SQL client by an admin; there is no external (Python) administration path. The CLI's only involvement is deployment — shipping the DDL and these Lua scripts during install.
* `SELECT ANY DICTIONARY` is required to read `EXA_DBA_ROLE_PRIVS` and `EXA_DBA_USERS`; it is granted to `PREPROC_ADMIN` only.

## Scenarios

<!-- DELTA:CHANGED -->
### Scenario: Admin configuration tables are created

* *GIVEN* schema `PREPROC` exists
* *WHEN* the install DDL is applied
* *THEN* the system MUST create table `PREPROC.CONFIG` with columns for role/user/client_pattern, phase, module script, function, enabled, and version
* *AND* the system MUST create table `PREPROC.RESOLUTION` with columns user_name, client_pattern, phase, script, function, and an `ordinal` column that records the deterministic within-phase dispatch order
* *AND* the system MUST create table `PREPROC.SETTINGS` with columns mode, self_disable_allowed, last_refresh, and a state-fingerprint column capturing the user/role-grant inputs seen at the last refresh
* *AND* the system MUST create table `PREPROC.PROFILES` mapping a hash to a manifest
<!-- /DELTA:CHANGED -->

<!-- DELTA:CHANGED -->
### Scenario: The CURRENT_USER-filtered pipeline view is created

* *GIVEN* schemas `PREPROC` and `PREPROC_RT` exist with the admin tables present
* *WHEN* the install DDL is applied
* *THEN* the system MUST create view `PREPROC_RT.MY_PIPELINE`
* *AND* the view SHALL join `PREPROC.SETTINGS` with `PREPROC.RESOLUTION` filtered on `r.user_name = CURRENT_USER`, ordered by phase then `ordinal` for deterministic within-phase dispatch
* *AND* the view SHALL expose the resolution `script_name`, `function_name`, and `client_pattern` so MASTER can apply per-rule client matching
* *AND* the view SHALL read the admin base tables with definer (owner) rights
<!-- /DELTA:CHANGED -->

<!-- DELTA:NEW -->
### Scenario: The refresh and status scripts are created in the admin schema

* *GIVEN* schema `PREPROC` exists with its admin tables
* *WHEN* the install DDL and Lua deployment are applied
* *THEN* the system MUST create the Lua scripts `PREPROC.REFRESH` and `PREPROC.STATUS` owned by `PREPROC_ADMIN`
* *AND* re-deploying with `CREATE OR REPLACE` MUST succeed without error
* *AND* neither script MUST be executable by PUBLIC
* *NOTE* deployment of these DB-side scripts is the install path's only involvement with refresh/status; there is no external (Python) refresh or status verb
<!-- /DELTA:NEW -->

<!-- DELTA:NEW -->
### Scenario: PREPROC_ADMIN can read the dictionary tables refresh requires

* *GIVEN* the install DDL is applied
* *WHEN* the grant statements run
* *THEN* the system MUST grant `SELECT ANY DICTIONARY` to role `PREPROC_ADMIN`
* *AND* a holder of `PREPROC_ADMIN` SHALL be able to select from `EXA_DBA_ROLE_PRIVS` and `EXA_DBA_USERS`
* *AND* the grant MUST NOT be extended to PUBLIC
<!-- /DELTA:NEW -->
