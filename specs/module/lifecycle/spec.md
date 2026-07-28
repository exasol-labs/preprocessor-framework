# Feature: Module DB Lifecycle and Provenance

Records which module versions are installed, where they came from, and drives
the update flow that honours the never-mutate-a-live-module discipline. A
`PREPROC.MODULES` provenance table (plus a PUBLIC-readable runtime view) is the
database's record of every deployed module version; the update flow deploys
`_V<N+1>`, repoints the config rule, and retires `_V<N>` — mirroring the
profile generate/retire/reclaim machinery so a live version is never dropped out
from under a pinned session. Rule registration is idempotent so declarative
re-application never stacks duplicates (BUG-008).

Provenance and reclaim are extended from a single deployed script to a
module's full object inventory. `PREPROC.MODULES` records the inventory at
install time so `MODULE_RECLAIM` can drop every object the module created
WITHOUT re-parsing the artifact — which it could not do anyway, since the
artifact is not retained in the database.

## Background

* `PREPROC.MODULES` lives in the locked admin schema and records one row per
  deployed module version: `name`, `version`, `source`, `ref`, `source_url`,
  `library_version`, `sha256`, `script_name`, `function_name`, `phase`,
  `deploy_mode`, `status` (`deployed` | `retired`), `installed_at`, and
  `retired_at` (nullable). `source_url` (the effective source resolved from) and
  `library_version` (the release the resolved index declared) are the audit anchor
  that ties an install performed through a mutable `latest` source to a concrete
  release; both are nullable (a manual/offline registration may omit them). A
  PUBLIC-readable view `PREPROC_RT.INSTALLED_MODULES` exposes provenance to
  operators through the same definer-rights bridge pattern as
  `PREPROC_RT.MY_PIPELINE`, so it is covered by the schema-level grants (ADR-002).
* `PREPROC.MODULES` gains a nullable `objects` column recording the deployed
  object inventory as declared at install time: each entry's type and
  fully-qualified name. It is added by the same idempotent additive-column
  upgrade pattern as `source_url` and `library_version`. A NULL `objects` value
  denotes a row written before this column existed, or a single-statement
  module, and is interpreted as the single object named by `script_name` — so
  existing provenance rows keep reclaiming correctly.
* Provenance is written through a supported admin surface, not direct DML:
  `PREPROC.MODULE_REGISTER` upserts the row for a `(name, version)` and is
  idempotent — re-registering an identical version MUST NOT create a second row.
* Rule registration is idempotent. `PREPROC.RULE_ENSURE` adds a rule only when
  no enabled rule with the same `(scope_type, scope_value, phase, script_name,
  function_name, client_pattern)` already exists, and returns the existing
  `rule_id` otherwise. `PREPROC.RULE_ADD` retains its allocate-a-new-rule
  behaviour; `RULE_ENSURE` is the declarative surface `sync` uses. This closes
  BUG-008: identical rules never stack, so a TRANSLATE/REWRITE module never runs
  N times for N accidental re-adds.
* The update flow is deploy → repoint → retire, and NEVER `CREATE OR REPLACE` a
  live `_V<N>` (ADR-019):
  1. **Deploy** `PREPROC_RT.<NAME>_V<N+1>` and register its provenance row with
     `status = 'deployed'`.
  2. **Repoint** the config rule that named `<NAME>_V<N>` so it names
     `<NAME>_V<N+1>`, then refresh. Because MASTER dispatches through the rule,
     the repoint is the atomic cutover; profile hashes change and the profile
     lifecycle regenerates and retires profiles exactly as for any rule change.
  3. **Retire** `_V<N>`: mark its provenance row `status = 'retired'` and stamp
     `retired_at`. The `_V<N>` script is NOT dropped at retirement — a
     convenience-mode session pinned to a profile that baked `<NAME>_V<N>` still
     imports it until that session's next statement re-resolves.
* **Reclaim** (physical `DROP SCRIPT PREPROC_RT.<NAME>_V<N>`) is guarded and
  never same-flow as the retirement, mirroring the profile never-same-refresh-drop
  invariant (ADR-022). A retired version is reclaimable only when it is
  referenced by no enabled `PREPROC.CONFIG` rule AND by no live (non-orphaned)
  `PREPROC.PROFILES` manifest — i.e. every profile that baked it has itself been
  retired — so no dispatchable chain and no reachable profile can still import it.
  Those checks are evaluated against the module's entry script, as before — a
  profile bakes the entry script, not the companions.
* Reclaim is INVENTORY-DRIVEN. It drops every object in the retired version's
  recorded inventory rather than only the one script named by `script_name`,
  resolving each object by its declared type and fully-qualified name. Because
  the inventory was declared AND parse-verified at install time (ADR-055),
  reclaim always knows exactly which objects the module owns. Objects are
  dropped in reverse inventory order, so a dependent object is removed before
  the object it depends on.
* The previous `PREPROC_RT`-only drop guard is WIDENED accordingly (ADR-057): an
  object outside `PREPROC_RT` is reclaimable when it appears in the version's
  recorded inventory and the version is otherwise reclaimable. The inventory,
  not the schema name, is the ownership boundary. Reclaim MUST NOT drop an
  object absent from the recorded inventory, whatever schema it lives in.
* Because every companion carries the module's `_V<N>` suffix (ADR-056), a v1
  and a v2 generation share no object name, so reclaiming v1 can never drop an
  object v2 is using.
* All lifecycle mutations that change resolution run through the shared
  `REFRESH_CORE.rebuild()` so RESOLUTION, profiles, and settings stay atomic and
  consistent, exactly as the rule admin scripts already do.
* Validation precedes any write: an update that names a `<NAME>_V<N>` with no
  provenance row, or a repoint of a rule that does not exist, MUST fail before
  mutating anything.

## Scenarios

### Scenario: Registering a module writes a provenance row

* *GIVEN* an installed framework with an empty `PREPROC.MODULES`
* *WHEN* `PREPROC.MODULE_REGISTER` is called for `cast_shorthand` version 1 with its source, ref, sha256, script, function, phase, deploy mode, effective source URL, library version, and object inventory
* *THEN* `PREPROC.MODULES` MUST contain one row for `('cast_shorthand', 1)` with `status = 'deployed'` and a non-null `installed_at`
* *AND* the row MUST record the supplied `source_url`, `library_version`, and `objects` (each NULL when not supplied)
* *AND* `PREPROC_RT.INSTALLED_MODULES` MUST expose that row to a non-admin user

### Scenario: Re-registering an identical module version is idempotent

* *GIVEN* `PREPROC.MODULES` already holds `('cast_shorthand', 1)` with `status = 'deployed'`
* *WHEN* `PREPROC.MODULE_REGISTER` is called again with the same `(name, version)` and identical metadata
* *THEN* `PREPROC.MODULES` MUST still contain exactly one row for `('cast_shorthand', 1)`

### Scenario: RULE_ENSURE does not create a duplicate for an identical rule

* *GIVEN* an enabled rule already exists for `(ROLE, ANALYSTS, TRANSLATE, PREPROC_RT.CAST_SHORTHAND_V1, cast_shorthand, NULL)`
* *WHEN* `PREPROC.RULE_ENSURE` is called with that identical tuple
* *THEN* `PREPROC.CONFIG` MUST still contain exactly one matching rule
* *AND* the call MUST return the existing `rule_id`

### Scenario: RULE_ENSURE creates a rule when none matches

* *GIVEN* no enabled rule exists for a given `(scope, phase, script, function)` tuple
* *WHEN* `PREPROC.RULE_ENSURE` is called with that tuple
* *THEN* `PREPROC.CONFIG` MUST contain exactly one new enabled rule for the tuple
* *AND* the new rule MUST be resolved into `PREPROC.RESOLUTION` after the refresh

### Scenario: Update deploys V+1, repoints the rule, and marks V retired

* *GIVEN* `cast_shorthand` version 1 is deployed and its rule dispatches at a role scope
* *WHEN* the update flow deploys version 2, repoints the rule to `PREPROC_RT.CAST_SHORTHAND_V2`, and retires version 1
* *THEN* `PREPROC.CONFIG` MUST name `PREPROC_RT.CAST_SHORTHAND_V2` for that rule
* *AND* `PREPROC.MODULES` MUST show version 2 `deployed` and version 1 `retired` with a `retired_at`
* *AND* an in-scope statement MUST be transformed by version 2

### Scenario: A retired version is not dropped while still referenced by a live profile

* *GIVEN* version 1 has just been retired and a profile manifest that baked `PREPROC_RT.CAST_SHORTHAND_V1` is not yet orphaned-and-reclaimed
* *WHEN* the reclaim check runs for version 1
* *THEN* the reclaim MUST NOT drop `PREPROC_RT.CAST_SHORTHAND_V1`
* *AND* the version 1 provenance row MUST remain with `status = 'retired'`

### Scenario: A retired version is reclaimed once no rule and no live profile reference it

* *GIVEN* a composite module's version 1 is retired, no enabled `PREPROC.CONFIG` rule names its entry script, and no non-orphaned `PREPROC.PROFILES` manifest contains it
* *WHEN* the reclaim runs for version 1
* *THEN* every object in the version's recorded inventory MUST be dropped
* *AND* the objects MUST be dropped in reverse inventory order
* *AND* its `PREPROC.MODULES` row MUST be removed or its reclamation recorded

### Scenario: Updating a version with no provenance row is rejected before any write

* *GIVEN* `PREPROC.MODULES` has no row for `cast_shorthand` version 1
* *WHEN* an update from version 1 to version 2 is attempted
* *THEN* the update MUST fail
* *AND* `PREPROC.CONFIG` and `PREPROC.MODULES` MUST be unchanged

### Scenario: Provenance records the full object inventory of a composite module

* *GIVEN* an installed framework and a composite module whose artifact creates a Lua entry script, two Python UDFs, and one table
* *WHEN* the module is installed and its provenance row is written
* *THEN* the `PREPROC.MODULES` row MUST record all four objects with their types and fully-qualified names
* *AND* the recorded inventory MUST equal the inventory declared in the module's manifest

### Scenario: Reclaim drops a composite module's objects outside PREPROC_RT

* *GIVEN* a reclaimable retired version whose recorded inventory includes an object in a schema other than `PREPROC_RT`
* *WHEN* the reclaim runs for that version
* *THEN* the reclaim MUST drop the object in the non-`PREPROC_RT` schema
* *AND* the reclaim MUST NOT be blocked by the former `PREPROC_RT`-only guard

### Scenario: Reclaim never drops an object absent from the recorded inventory

* *GIVEN* a reclaimable retired version and an unrelated object in the same schema that the module never created
* *WHEN* the reclaim runs for that version
* *THEN* the unrelated object MUST still exist afterwards
* *AND* the reclaim MUST resolve its drop set solely from the version's recorded inventory

### Scenario: A provenance row with no recorded inventory reclaims its entry script

* *GIVEN* a retired, reclaimable version whose `PREPROC.MODULES` row was written before this delta and whose `objects` value is NULL
* *WHEN* the reclaim runs for that version
* *THEN* the reclaim MUST drop the single object named by the row's `script_name`
* *AND* the reclaim MUST NOT fail on the absent inventory

### Scenario: Reclaiming version 1 leaves a live version 2 companion untouched

* *GIVEN* a composite module with version 1 retired and reclaimable and version 2 deployed, each declaring its own `_V<N>`-suffixed companion objects
* *WHEN* the reclaim runs for version 1
* *THEN* every version 1 object MUST be dropped
* *AND* every version 2 object MUST still exist
* *AND* an in-scope statement MUST still be transformed by version 2
