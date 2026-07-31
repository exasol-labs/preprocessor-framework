# Feature: Two-Schema Database Layout

Establishes the foundational database objects the preprocessor framework dispatches from: a locked admin schema (`PREPROC`) holding configuration and a PUBLIC-accessible runtime schema (`PREPROC_RT`) holding the dispatcher, views, and (later) generated scripts. This slice extends the layout for resolution: `RESOLUTION` gains an `ordinal` column for deterministic within-phase dispatch order, `SETTINGS` gains a state-fingerprint column for staleness detection, `MY_PIPELINE` exposes `client_pattern` and orders by `phase, ordinal`, and the `PREPROC_ADMIN` role gains `SELECT ANY DICTIONARY` so the admin-owned refresh script can read the role-grant dictionary tables. This delta extends the layout to add a `profile_hash` column to `PREPROC.RESOLUTION`, an `orphaned_at` column to `PREPROC.PROFILES` (for the retire-then-reclaim cleanup lifecycle), and exposes `mode` and `profile_hash` from `PREPROC_RT.MY_PIPELINE` to support session self-optimization. It further extends `PREPROC.SETTINGS` with a `group_sys_with_dba` BOOLEAN column that governs whether the `SYS` account is grouped with `DBA` during resolution. Fresh installs seed it `TRUE` (grouped — preserving today's behavior); existing installs gain it as a nullable additive upgrade whose `NULL` is treated as `TRUE` by the resolution `COALESCE`, so an upgrade changes no behavior. This delta further extends the layout for the in-database module installer: the locked admin schema `PREPROC` gains the admin-only resolver UDF(s), `PREPROC.MODULE_INSTALL`, and `PREPROC.MODULE_CATALOG` — owned by `PREPROC_ADMIN` and NEVER granted to PUBLIC. The BucketFS source used by the installer is an operator-created CONNECTION object; the framework never creates it. This delta adds the one storage change the composite artifact contract needs: `PREPROC.MODULES` gains a nullable `objects` column recording each installed version's object inventory, so `MODULE_RECLAIM` can drop every object the module created without re-parsing an artifact the database does not retain. This delta records that the BucketFS source string is now the plain location `bucketfs:<service>/<bucket>/<path>` and no longer names the operator-created CONNECTION: the CONNECTION remains an operator prerequisite the install DDL never creates, but it is a credential grant rather than a name the framework's surfaces carry.

## Background

* The DDL source of truth lives in numbered files under `sql/install/` and is applied in filename order.
* All object creation is idempotent: schemas, tables, and views use `IF NOT EXISTS` or `CREATE OR REPLACE`; column additions on already-installed databases ship as guarded `ALTER TABLE ... ADD COLUMN` statements that tolerate "already exists" via the `--@tolerate-exists` applier contract.
* The `PREPROC_ADMIN` role owns the `PREPROC` schema and every object in it, including the new refresh and status scripts. Objects in `PREPROC` are not granted to PUBLIC. All framework administration (refresh, status) is exposed as these DB-side scripts, callable from any SQL client by an admin; there is no external (Python) administration path. The CLI's only involvement is deployment — shipping the DDL and these Lua scripts during install.
* `SELECT ANY DICTIONARY` is required to read `EXA_DBA_ROLE_PRIVS` and `EXA_DBA_USERS`; it is granted to `PREPROC_ADMIN` only.
* The `group_sys_with_dba` column is nullable in both the fresh-install `CREATE TABLE` and the upgrade `ALTER TABLE`, matching the established additive-upgrade pattern (a NOT NULL add would fail on the existing single SETTINGS row); the seed sets the fresh-install value explicitly to `TRUE` and the resolution SQL `COALESCE`s a `NULL` to `TRUE`.
* The resolver Python3 UDF(s), `PREPROC.MODULE_INSTALL`, and `PREPROC.MODULE_CATALOG` are objects in the locked `PREPROC` admin schema owned by `PREPROC_ADMIN`. Unlike the runtime schema, `PREPROC` is not granted to PUBLIC (ADR-002), so these objects are admin-only — the resolver, which reaches BucketFS/network, is never invokable by an ordinary user.
* The BucketFS source is an operator responsibility: for a read-protected bucket the operator creates an Exasol CONNECTION `TO 'bucketfs:<service>/<bucket>' IDENTIFIED BY '<read-password>'` and grants it to `PREPROC_ADMIN`; the framework install DDL creates no CONNECTION. The CONNECTION's name is arbitrary and appears in NO framework surface — the source string is the plain BucketFS location `bucketfs:<service>/<bucket>/<path>`, and the grant is what makes Exasol mount the bucket into the UDF container for that caller.
* `PREPROC.MODULES` gains a nullable `objects` column holding the deployed
  object inventory as a serialised list of `type` + fully-qualified `name`
  entries. It is added through `sql/install/25_column_upgrades.sql` using the
  same idempotent additive-column pattern already used for
  `PREPROC.PROFILES.orphaned_at`, `PREPROC.SETTINGS.group_sys_with_dba`, and
  `PREPROC.MODULES.source_url` / `library_version`, so re-applying the layout
  over an existing install is a no-op.
* The column is 200,000 characters wide — roughly 2,000 objects at realistic
  ~100-character qualified names, and well under Exasol's 2,000,000-character
  VARCHAR ceiling. The 4,000 it was first added at held about 39 objects: a house
  default rather than a derived bound, and too tight for the subsystem module
  that legitimately owns its own schema (ADR-057). An Exasol VARCHAR is
  variable-length, so the headroom costs nothing in storage.
* That width is pinned in seven places that cannot import one another — the table
  DDL, the widening `ALTER` in `sql/install/25_column_upgrades.sql`,
  `MODULE_REGISTER`'s `CAST`, the resolver's and `MODULE_CATALOG`'s `objects`
  EMITS, and the `OBJECTS_MAX_CHARS` pre-flight guards in the resolver and
  `module_install.lua`, canonically
  `preproc.module.registry.OBJECTS_MAX_CHARS`. Because each one is read by a gate
  that refuses an over-wide inventory BEFORE the provenance write, the narrowest
  copy is the effective limit: a partial widening does not fail, it silently
  keeps the old ceiling. They therefore MUST move together.
* Widening the column over an existing install is a different operation from
  adding it, so it ships as an `ALTER TABLE ... MODIFY` rather than the additive
  `--@tolerate-exists` `ADD COLUMN` pattern. `MODIFY` states the target type
  absolutely, which is what makes re-application converge rather than error (a
  second run raises no "already exists" error for a marker to tolerate), and
  Exasol converts the stored values in place, so recorded inventories survive and
  the column is never dropped and recreated. The statement follows the additive
  `ADD COLUMN` in the same file, which guarantees the column exists by the time it
  runs. The release bundle is clean-install only and skips that file, so the
  widening path is reached exclusively by re-installing over an existing install.
* A NULL `objects` value denotes a row written before this delta or a
  single-statement module; readers interpret it as the single object named by
  the row's `script_name`.
* `PREPROC_RT.INSTALLED_MODULES` exposes the new column through the existing
  definer-rights view bridge, so an operator can read a module's object
  inventory without holding admin rights.

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
* *AND* the system MUST create table `PREPROC.SETTINGS` with columns mode, self_disable_allowed, last_refresh, a state-fingerprint column capturing the user/role-grant inputs seen at the last refresh, and a nullable `group_sys_with_dba` BOOLEAN column that governs whether the `SYS` account participates in the ROLE-grant closure
* *AND* the system MUST create table `PREPROC.PROFILES` mapping a hash to a manifest, including a nullable `orphaned_at` TIMESTAMP column that records when a profile's hash stopped being referenced by any current resolved chain (NULL while the profile is live/current)

### Scenario: SETTINGS is seeded with a single default row

* *GIVEN* table `PREPROC.SETTINGS` has just been created and is empty
* *WHEN* the install DDL is applied
* *THEN* the system MUST insert exactly one row into `PREPROC.SETTINGS`
* *AND* the row SHALL default `mode` to convenience
* *AND* the row SHALL default `group_sys_with_dba` to `TRUE` (grouped — preserving the pre-toggle behavior for fresh installs)
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

### Scenario: PREPROC.SETTINGS gains a group_sys_with_dba column via an idempotent upgrade

* *GIVEN* an existing install where `PREPROC.SETTINGS` does not yet have a `group_sys_with_dba` column
* *WHEN* the additive column-upgrade DDL is applied
* *THEN* the system MUST add a nullable `group_sys_with_dba` BOOLEAN column to `PREPROC.SETTINGS`
* *AND* the upgrade MUST be expressed via the existing `--@tolerate-exists` additive `ALTER TABLE ... ADD COLUMN` pattern so that re-applying it on an install that already has the column is a no-op (no error, column unchanged)
* *AND* the existing single `PREPROC.SETTINGS` row MUST retain its data with `group_sys_with_dba` defaulting to `NULL`, which resolution treats as `TRUE` (grouped) via `COALESCE`, so the upgrade changes no resolution behavior

### Scenario: The resolver UDF and install/catalog scripts are created admin-only in PREPROC

* *GIVEN* schema `PREPROC` exists with its admin tables
* *WHEN* the install DDL and UDF/Lua deployment are applied
* *THEN* the system MUST create the resolver Python3 UDF(s), the Lua script `PREPROC.MODULE_INSTALL`, and the Lua script `PREPROC.MODULE_CATALOG`, all owned by `PREPROC_ADMIN`
* *AND* none of them MUST be executable by PUBLIC
* *AND* re-deploying them with `CREATE OR REPLACE` MUST succeed without error

### Scenario: The BucketFS CONNECTION is a credential grant the framework never creates and never names

* *GIVEN* a DBA preparing an air-gapped in-database install from a release staged in a read-protected BucketFS bucket
* *WHEN* the framework install DDL is applied
* *THEN* the install DDL MUST NOT create any BucketFS CONNECTION object
* *AND* the in-database installer MUST derive the BucketFS mount from the source string's own `<service>` and `<bucket>` segments, with no CONNECTION lookup
* *AND* reading a read-protected bucket MUST require the operator to have created a CONNECTION `TO 'bucketfs:<service>/<bucket>' IDENTIFIED BY '<read-password>'` and granted it to `PREPROC_ADMIN`
* *AND* the CONNECTION's name MUST NOT appear in any framework surface — not the source string, not `PREPROC.MODULES.source_url`, not the install DDL

### Scenario: PREPROC.MODULES gains a nullable objects column via an idempotent upgrade

* *GIVEN* an existing install whose `PREPROC.MODULES` table has no `objects` column
* *WHEN* the install DDL is applied
* *THEN* `PREPROC.MODULES` MUST have a nullable `objects` column
* *AND* existing provenance rows MUST be preserved with a NULL `objects` value
* *AND* re-applying the install DDL MUST NOT fail and MUST NOT duplicate the column

### Scenario: PREPROC.MODULES.objects is widened in place over an existing install

* *GIVEN* an existing install whose `PREPROC.MODULES.objects` column is narrower than the 200,000 characters the install DDL declares
* *AND* `PREPROC.MODULES` holds provenance rows, at least one with a non-NULL `objects` value written through the narrower column
* *WHEN* the install DDL is applied
* *THEN* `PREPROC.MODULES.objects` MUST be 200,000 characters wide
* *AND* every existing provenance row MUST be preserved with its recorded `objects` value unchanged
* *AND* the column MUST remain nullable
* *AND* the widening MUST NOT drop and recreate the column
* *AND* re-applying the install DDL over the already-widened column MUST NOT fail, MUST NOT narrow the column, and MUST NOT alter any recorded `objects` value

### Scenario: The installed-modules view exposes the object inventory to a non-admin user

* *GIVEN* an installed framework with a composite module whose provenance row records four objects
* *WHEN* a non-admin user selects from `PREPROC_RT.INSTALLED_MODULES`
* *THEN* the view MUST expose the module's recorded object inventory
* *AND* the user MUST NOT require any grant on the `PREPROC.MODULES` admin table
