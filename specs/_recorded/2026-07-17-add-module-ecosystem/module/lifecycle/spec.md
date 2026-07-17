# Feature: Module DB Lifecycle and Provenance

Records which module versions are installed, where they came from, and drives
the update flow that honours the never-mutate-a-live-module discipline. A
`PREPROC.MODULES` provenance table (plus a PUBLIC-readable runtime view) is the
database's record of every deployed module version; the update flow deploys
`_V<N+1>`, repoints the config rule, and retires `_V<N>` — mirroring the
profile generate/retire/reclaim machinery so a live version is never dropped out
from under a pinned session. Rule registration is idempotent so declarative
re-application never stacks duplicates (BUG-008).

## Background

* `PREPROC.MODULES` lives in the locked admin schema and records one row per
  deployed module version: `name`, `version`, `source`, `ref`, `sha256`,
  `script_name`, `function_name`, `phase`, `deploy_mode`, `status`
  (`deployed` | `retired`), `installed_at`, and `retired_at` (nullable). A
  PUBLIC-readable view `PREPROC_RT.INSTALLED_MODULES` exposes provenance to
  operators through the same definer-rights bridge pattern as
  `PREPROC_RT.MY_PIPELINE`, so it is covered by the schema-level grants (ADR-002).
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
* All lifecycle mutations that change resolution run through the shared
  `REFRESH_CORE.rebuild()` so RESOLUTION, profiles, and settings stay atomic and
  consistent, exactly as the rule admin scripts already do.
* Validation precedes any write: an update that names a `<NAME>_V<N>` with no
  provenance row, or a repoint of a rule that does not exist, MUST fail before
  mutating anything.

## Scenarios

<!-- DELTA:NEW -->
### Scenario: Registering a module writes a provenance row

* *GIVEN* an installed framework with an empty `PREPROC.MODULES`
* *WHEN* `PREPROC.MODULE_REGISTER` is called for `cast-shorthand` version 1 with its source, ref, sha256, script, function, phase, and deploy mode
* *THEN* `PREPROC.MODULES` MUST contain one row for `('cast-shorthand', 1)` with `status = 'deployed'` and a non-null `installed_at`
* *AND* `PREPROC_RT.INSTALLED_MODULES` MUST expose that row to a non-admin user
<!-- /DELTA:NEW -->

<!-- DELTA:NEW -->
### Scenario: Re-registering an identical module version is idempotent

* *GIVEN* `PREPROC.MODULES` already holds `('cast-shorthand', 1)` with `status = 'deployed'`
* *WHEN* `PREPROC.MODULE_REGISTER` is called again with the same `(name, version)` and identical metadata
* *THEN* `PREPROC.MODULES` MUST still contain exactly one row for `('cast-shorthand', 1)`
<!-- /DELTA:NEW -->

<!-- DELTA:NEW -->
### Scenario: RULE_ENSURE does not create a duplicate for an identical rule

* *GIVEN* an enabled rule already exists for `(ROLE, ANALYSTS, TRANSLATE, PREPROC_RT.CAST_SHORTHAND_V1, cast_shorthand, NULL)`
* *WHEN* `PREPROC.RULE_ENSURE` is called with that identical tuple
* *THEN* `PREPROC.CONFIG` MUST still contain exactly one matching rule
* *AND* the call MUST return the existing `rule_id`
<!-- /DELTA:NEW -->

<!-- DELTA:NEW -->
### Scenario: RULE_ENSURE creates a rule when none matches

* *GIVEN* no enabled rule exists for a given `(scope, phase, script, function)` tuple
* *WHEN* `PREPROC.RULE_ENSURE` is called with that tuple
* *THEN* `PREPROC.CONFIG` MUST contain exactly one new enabled rule for the tuple
* *AND* the new rule MUST be resolved into `PREPROC.RESOLUTION` after the refresh
<!-- /DELTA:NEW -->

<!-- DELTA:NEW -->
### Scenario: Update deploys V+1, repoints the rule, and marks V retired

* *GIVEN* `cast-shorthand` version 1 is deployed and its rule dispatches at a role scope
* *WHEN* the update flow deploys version 2, repoints the rule to `PREPROC_RT.CAST_SHORTHAND_V2`, and retires version 1
* *THEN* `PREPROC.CONFIG` MUST name `PREPROC_RT.CAST_SHORTHAND_V2` for that rule
* *AND* `PREPROC.MODULES` MUST show version 2 `deployed` and version 1 `retired` with a `retired_at`
* *AND* an in-scope statement MUST be transformed by version 2
<!-- /DELTA:NEW -->

<!-- DELTA:NEW -->
### Scenario: A retired version is not dropped while still referenced by a live profile

* *GIVEN* version 1 has just been retired and a profile manifest that baked `PREPROC_RT.CAST_SHORTHAND_V1` is not yet orphaned-and-reclaimed
* *WHEN* the reclaim check runs for version 1
* *THEN* the reclaim MUST NOT drop `PREPROC_RT.CAST_SHORTHAND_V1`
* *AND* the version 1 provenance row MUST remain with `status = 'retired'`
<!-- /DELTA:NEW -->

<!-- DELTA:NEW -->
### Scenario: A retired version is reclaimed once no rule and no live profile reference it

* *GIVEN* version 1 is retired, no enabled `PREPROC.CONFIG` rule names it, and no non-orphaned `PREPROC.PROFILES` manifest contains it
* *WHEN* the reclaim runs for version 1
* *THEN* `PREPROC_RT.CAST_SHORTHAND_V1` MUST be dropped
* *AND* its `PREPROC.MODULES` row MUST be removed or its reclamation recorded
<!-- /DELTA:NEW -->

<!-- DELTA:NEW -->
### Scenario: Updating a version with no provenance row is rejected before any write

* *GIVEN* `PREPROC.MODULES` has no row for `cast-shorthand` version 1
* *WHEN* an update from version 1 to version 2 is attempted
* *THEN* the update MUST fail
* *AND* `PREPROC.CONFIG` and `PREPROC.MODULES` MUST be unchanged
<!-- /DELTA:NEW -->
