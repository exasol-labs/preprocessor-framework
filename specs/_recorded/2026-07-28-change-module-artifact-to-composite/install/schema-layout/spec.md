# Feature: Two-Schema Install Layout

Creates the framework's admin (`PREPROC`) and runtime (`PREPROC_RT`) schemas,
their tables, views, and grants, via numbered idempotent DDL files.

This delta adds the one storage change the composite artifact contract needs:
`PREPROC.MODULES` must record each installed version's object inventory so
`MODULE_RECLAIM` can drop every object the module created without re-parsing an
artifact the database does not retain.

## Background

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

<!-- DELTA:NEW -->
### Scenario: PREPROC.MODULES gains a nullable objects column via an idempotent upgrade

* *GIVEN* an existing install whose `PREPROC.MODULES` table has no `objects` column
* *WHEN* the install DDL is applied
* *THEN* `PREPROC.MODULES` MUST have a nullable `objects` column
* *AND* existing provenance rows MUST be preserved with a NULL `objects` value
* *AND* re-applying the install DDL MUST NOT fail and MUST NOT duplicate the column
<!-- /DELTA:NEW -->

<!-- DELTA:NEW -->
### Scenario: PREPROC.MODULES.objects is widened in place over an existing install

* *GIVEN* an existing install whose `PREPROC.MODULES.objects` column is narrower than the 200,000 characters the install DDL declares
* *AND* `PREPROC.MODULES` holds provenance rows, at least one with a non-NULL `objects` value written through the narrower column
* *WHEN* the install DDL is applied
* *THEN* `PREPROC.MODULES.objects` MUST be 200,000 characters wide
* *AND* every existing provenance row MUST be preserved with its recorded `objects` value unchanged
* *AND* the column MUST remain nullable
* *AND* the widening MUST NOT drop and recreate the column
* *AND* re-applying the install DDL over the already-widened column MUST NOT fail, MUST NOT narrow the column, and MUST NOT alter any recorded `objects` value
<!-- /DELTA:NEW -->

<!-- DELTA:NEW -->
### Scenario: The installed-modules view exposes the object inventory to a non-admin user

* *GIVEN* an installed framework with a composite module whose provenance row records four objects
* *WHEN* a non-admin user selects from `PREPROC_RT.INSTALLED_MODULES`
* *THEN* the view MUST expose the module's recorded object inventory
* *AND* the user MUST NOT require any grant on the `PREPROC.MODULES` admin table
<!-- /DELTA:NEW -->
