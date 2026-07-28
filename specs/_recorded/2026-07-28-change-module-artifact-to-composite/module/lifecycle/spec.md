# Feature: Module DB Lifecycle and Provenance

Records which module versions are installed, where they came from, and drives
the update flow that honours the never-mutate-a-live-module discipline.

This delta extends provenance and reclaim from a single deployed script to a
module's full object inventory. `PREPROC.MODULES` records the inventory at
install time so `MODULE_RECLAIM` can drop every object the module created
WITHOUT re-parsing the artifact — which it could not do anyway, since the
artifact is not retained in the database.

## Background

* `PREPROC.MODULES` gains a nullable `objects` column recording the deployed
  object inventory as declared at install time: each entry's type and
  fully-qualified name. It is added by the same idempotent additive-column
  upgrade pattern as `source_url` and `library_version`. A NULL `objects` value
  denotes a row written before this delta, or a single-statement module, and is
  interpreted as the single object named by `script_name` — so existing
  provenance rows keep reclaiming correctly.
* Reclaim is INVENTORY-DRIVEN. It drops every object in the retired version's
  recorded inventory rather than only the one script named by `script_name`,
  resolving each object by its declared type and fully-qualified name. Because
  the inventory was declared AND parse-verified at install time (ADR-055),
  reclaim always knows exactly which objects the module owns.
* The previous `PREPROC_RT`-only drop guard is WIDENED accordingly (ADR-057): an
  object outside `PREPROC_RT` is reclaimable when it appears in the version's
  recorded inventory and the version is otherwise reclaimable. The inventory,
  not the schema name, is the ownership boundary. Reclaim MUST NOT drop an
  object absent from the recorded inventory, whatever schema it lives in.
* Reclaim remains guarded by the existing liveness conditions and is never
  same-flow as retirement: a retired version is reclaimable only when no enabled
  `PREPROC.CONFIG` rule references it AND no live `PREPROC.PROFILES` manifest
  contains it. Those checks are evaluated against the module's entry script, as
  before — a profile bakes the entry script, not the companions.
* Because every companion carries the module's `_V<N>` suffix (ADR-056), a v1
  and a v2 generation share no object name, so reclaiming v1 can never drop an
  object v2 is using.
* Objects are dropped in reverse inventory order, so a dependent object is
  removed before the object it depends on.

## Scenarios

<!-- DELTA:CHANGED -->
### Scenario: Registering a module writes a provenance row

* *GIVEN* an installed framework with an empty `PREPROC.MODULES`
* *WHEN* `PREPROC.MODULE_REGISTER` is called for `cast_shorthand` version 1 with its source, ref, sha256, script, function, phase, deploy mode, effective source URL, library version, and object inventory
* *THEN* `PREPROC.MODULES` MUST contain one row for `('cast_shorthand', 1)` with `status = 'deployed'` and a non-null `installed_at`
* *AND* the row MUST record the supplied `source_url`, `library_version`, and `objects` (each NULL when not supplied)
* *AND* `PREPROC_RT.INSTALLED_MODULES` MUST expose that row to a non-admin user
<!-- /DELTA:CHANGED -->

<!-- DELTA:NEW -->
### Scenario: Provenance records the full object inventory of a composite module

* *GIVEN* an installed framework and a composite module whose artifact creates a Lua entry script, two Python UDFs, and one table
* *WHEN* the module is installed and its provenance row is written
* *THEN* the `PREPROC.MODULES` row MUST record all four objects with their types and fully-qualified names
* *AND* the recorded inventory MUST equal the inventory declared in the module's manifest
<!-- /DELTA:NEW -->

<!-- DELTA:CHANGED -->
### Scenario: A retired version is reclaimed once no rule and no live profile reference it

* *GIVEN* a composite module's version 1 is retired, no enabled `PREPROC.CONFIG` rule names its entry script, and no non-orphaned `PREPROC.PROFILES` manifest contains it
* *WHEN* the reclaim runs for version 1
* *THEN* every object in the version's recorded inventory MUST be dropped
* *AND* the objects MUST be dropped in reverse inventory order
* *AND* its `PREPROC.MODULES` row MUST be removed or its reclamation recorded
<!-- /DELTA:CHANGED -->

<!-- DELTA:NEW -->
### Scenario: Reclaim drops a composite module's objects outside PREPROC_RT

* *GIVEN* a reclaimable retired version whose recorded inventory includes an object in a schema other than `PREPROC_RT`
* *WHEN* the reclaim runs for that version
* *THEN* the reclaim MUST drop the object in the non-`PREPROC_RT` schema
* *AND* the reclaim MUST NOT be blocked by the former `PREPROC_RT`-only guard
<!-- /DELTA:NEW -->

<!-- DELTA:NEW -->
### Scenario: Reclaim never drops an object absent from the recorded inventory

* *GIVEN* a reclaimable retired version and an unrelated object in the same schema that the module never created
* *WHEN* the reclaim runs for that version
* *THEN* the unrelated object MUST still exist afterwards
* *AND* the reclaim MUST resolve its drop set solely from the version's recorded inventory
<!-- /DELTA:NEW -->

<!-- DELTA:NEW -->
### Scenario: A provenance row with no recorded inventory reclaims its entry script

* *GIVEN* a retired, reclaimable version whose `PREPROC.MODULES` row was written before this delta and whose `objects` value is NULL
* *WHEN* the reclaim runs for that version
* *THEN* the reclaim MUST drop the single object named by the row's `script_name`
* *AND* the reclaim MUST NOT fail on the absent inventory
<!-- /DELTA:NEW -->

<!-- DELTA:NEW -->
### Scenario: Reclaiming version 1 leaves a live version 2 companion untouched

* *GIVEN* a composite module with version 1 retired and reclaimable and version 2 deployed, each declaring its own `_V<N>`-suffixed companion objects
* *WHEN* the reclaim runs for version 1
* *THEN* every version 1 object MUST be dropped
* *AND* every version 2 object MUST still exist
* *AND* an in-scope statement MUST still be transformed by version 2
<!-- /DELTA:NEW -->
