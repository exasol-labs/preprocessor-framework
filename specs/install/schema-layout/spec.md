# Feature: Two-Schema Database Layout

Establishes the foundational database objects the preprocessor framework dispatches from: a locked admin schema (`PREPROC`) holding configuration and a PUBLIC-accessible runtime schema (`PREPROC_RT`) holding the dispatcher, views, and (later) generated scripts. This slice extends the layout for resolution: `RESOLUTION` gains an `ordinal` column for deterministic within-phase dispatch order, `SETTINGS` gains a state-fingerprint column for staleness detection, `MY_PIPELINE` exposes `client_pattern` and orders by `phase, ordinal`, and the `PREPROC_ADMIN` role gains `SELECT ANY DICTIONARY` so the admin-owned refresh script can read the role-grant dictionary tables. This delta extends the layout to add a `profile_hash` column to `PREPROC.RESOLUTION`, an `orphaned_at` column to `PREPROC.PROFILES` (for the retire-then-reclaim cleanup lifecycle), and exposes `mode` and `profile_hash` from `PREPROC_RT.MY_PIPELINE` to support session self-optimization.

## Background

* The DDL source of truth lives in numbered files under `sql/install/` and is applied in filename order.
* All object creation is idempotent: schemas, tables, and views use `IF NOT EXISTS` or `CREATE OR REPLACE`; column additions on already-installed databases ship as guarded `ALTER TABLE ... ADD COLUMN` statements that tolerate "already exists" via the `--@tolerate-exists` applier contract.
* The `PREPROC_ADMIN` role owns the `PREPROC` schema and every object in it, including the new refresh and status scripts. Objects in `PREPROC` are not granted to PUBLIC. All framework administration (refresh, status) is exposed as these DB-side scripts, callable from any SQL client by an admin; there is no external (Python) administration path. The CLI's only involvement is deployment — shipping the DDL and these Lua scripts during install.
* `SELECT ANY DICTIONARY` is required to read `EXA_DBA_ROLE_PRIVS` and `EXA_DBA_USERS`; it is granted to `PREPROC_ADMIN` only.

## Scenarios

### Scenario: Admin and runtime schemas are created

* *GIVEN* a database with neither `PREPROC` nor `PREPROC_RT` present
* *WHEN* the install DDL is applied
* *THEN* the system MUST create schema `PREPROC`
* *AND* the system MUST create schema `PREPROC_RT`
* *AND* both schemas SHALL be visible in `EXA_ALL_SCHEMAS`

### Scenario: PREPROC_ADMIN role owns the admin schema

* *GIVEN* the install DDL is being applied
* *WHEN* schema `PREPROC` is created
* *THEN* the system MUST ensure role `PREPROC_ADMIN` exists
* *AND* the system MUST make `PREPROC_ADMIN` the owner of schema `PREPROC`
* *AND* the system MUST make `PREPROC_ADMIN` the owner of every table in `PREPROC`

### Scenario: Admin configuration tables are created

* *GIVEN* schema `PREPROC` exists
* *WHEN* the install DDL is applied
* *THEN* the system MUST create table `PREPROC.CONFIG` with columns for role/user/client_pattern, phase, module script, function, enabled, and version
* *AND* the system MUST create table `PREPROC.RESOLUTION` with columns user_name, client_pattern, phase, script, function, an `ordinal` column that records the deterministic within-phase dispatch order, and a `profile_hash` column (VARCHAR, nullable) that records the hash of the profile script generated for that user's chain
* *AND* the system MUST create table `PREPROC.SETTINGS` with columns mode, self_disable_allowed, last_refresh, and a state-fingerprint column capturing the user/role-grant inputs seen at the last refresh
* *AND* the system MUST create table `PREPROC.PROFILES` mapping a hash to a manifest, including a nullable `orphaned_at` TIMESTAMP column that records when a profile's hash stopped being referenced by any current resolved chain (NULL while the profile is live/current)

### Scenario: SETTINGS is seeded with a single default row

* *GIVEN* table `PREPROC.SETTINGS` has just been created and is empty
* *WHEN* the install DDL is applied
* *THEN* the system MUST insert exactly one row into `PREPROC.SETTINGS`
* *AND* the row SHALL default `mode` to convenience
* *AND* re-applying the DDL MUST NOT insert a second row

### Scenario: The CURRENT_USER-filtered pipeline view is created

* *GIVEN* schemas `PREPROC` and `PREPROC_RT` exist with the admin tables present
* *WHEN* the install DDL is applied
* *THEN* the system MUST create view `PREPROC_RT.MY_PIPELINE`
* *AND* the view SHALL join `PREPROC.SETTINGS` with `PREPROC.RESOLUTION` filtered on `CURRENT_USER`, ordered by phase then `ordinal` for deterministic within-phase dispatch
* *AND* the view SHALL expose the resolution `phase`, `client_pattern`, `script_name`, `function_name`, and `ordinal` so MASTER can apply per-rule client matching
* *AND* the view SHALL expose `PREPROC.SETTINGS.self_disable_allowed` and `PREPROC.SETTINGS.mode` so MASTER can determine the swap/disable eligibility
* *AND* the view SHALL expose `PREPROC.RESOLUTION.profile_hash` (NULL for users with no rules) so MASTER can name the target profile script without re-hashing in Lua
* *AND* the view SHALL read the admin base tables with definer (owner) rights

### Scenario: Schema-level PUBLIC grants cover the runtime schema

* *GIVEN* schema `PREPROC_RT` exists
* *WHEN* the install DDL is applied
* *THEN* the system MUST grant EXECUTE on schema `PREPROC_RT` to PUBLIC
* *AND* the system MUST grant SELECT on schema `PREPROC_RT` to PUBLIC
* *AND* a non-admin user SHALL be able to select from `PREPROC_RT.MY_PIPELINE`

### Scenario: A non-admin user reads only their own pipeline rows

* *GIVEN* `PREPROC.RESOLUTION` contains rows for several distinct users
* *AND* the schema-level SELECT grant is in place
* *WHEN* a non-admin user queries `PREPROC_RT.MY_PIPELINE`
* *THEN* the result MUST contain only rows whose `user_name` equals that user
* *AND* the user MUST NOT be able to select directly from `PREPROC.RESOLUTION`

### Scenario: Re-applying the layout is idempotent

* *GIVEN* a database where the full layout has already been applied
* *WHEN* the install DDL is applied a second time without changes
* *THEN* the operation MUST succeed without error
* *AND* the schemas, tables, and grants MUST remain unchanged
* *AND* no duplicate `PREPROC.SETTINGS` row SHALL be created

### Scenario: The refresh and status scripts are created in the admin schema

* *GIVEN* schema `PREPROC` exists with its admin tables
* *WHEN* the install DDL and Lua deployment are applied
* *THEN* the system MUST create the Lua scripts `PREPROC.REFRESH` and `PREPROC.STATUS` owned by `PREPROC_ADMIN`
* *AND* re-deploying with `CREATE OR REPLACE` MUST succeed without error
* *AND* neither script MUST be executable by PUBLIC
* *NOTE* deployment of these DB-side scripts is the install path's only involvement with refresh/status; there is no external (Python) refresh or status verb

### Scenario: PREPROC_ADMIN can read the dictionary tables refresh requires

* *GIVEN* the install DDL is applied
* *WHEN* the grant statements run
* *THEN* the system MUST grant `SELECT ANY DICTIONARY` to role `PREPROC_ADMIN`
* *AND* a holder of `PREPROC_ADMIN` SHALL be able to select from `EXA_DBA_ROLE_PRIVS` and `EXA_DBA_USERS`
* *AND* the grant MUST NOT be extended to PUBLIC

### Scenario: PREPROC.PROFILES gains a nullable orphaned_at column via an idempotent upgrade

* *GIVEN* an existing install where `PREPROC.PROFILES` does not yet have an `orphaned_at` column
* *WHEN* the additive column-upgrade DDL is applied
* *THEN* the system MUST add a nullable `orphaned_at` TIMESTAMP column to `PREPROC.PROFILES`
* *AND* the upgrade MUST be expressed via the existing `--@tolerate-exists` additive `ALTER TABLE ... ADD COLUMN` pattern so that re-applying it on an install that already has the column is a no-op (no error, column unchanged)
* *AND* existing `PREPROC.PROFILES` rows MUST retain their data with `orphaned_at` defaulting to NULL (treated as live/current)
