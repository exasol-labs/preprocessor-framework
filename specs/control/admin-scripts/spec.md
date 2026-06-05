# Feature: Admin Control Scripts

Provides the complete SQL-only administrative surface for the preprocessor framework as `EXECUTE SCRIPT` entry points in the `PREPROC` admin schema, so every config and settings mutation an operator performs goes through a validating script rather than direct DML. The scripts own rule lifecycle (add, drop, enable, disable, list) and settings management (mode, self-disable flag), validate their inputs so the scripted path is the obviously-correct one, and keep `PREPROC.RESOLUTION` current by auto-refreshing after each mutation (with an opt-out for batch workflows). Direct `INSERT`/`UPDATE`/`DELETE` against `PREPROC.CONFIG` remains technically possible (invoker rights preclude a hard lock) but is declared unsupported.

## Background

* The control scripts live in the `PREPROC` admin schema alongside `REFRESH` and `STATUS`, are deployed by `preproc install` and emitted by `preproc bundle`, and are unversioned `CREATE OR REPLACE LUA SCRIPT` objects (they are not dispatched modules, so the `_V1` versioning discipline does not apply — same precedent as `REFRESH`/`STATUS`, ADR-008).
* The scripts run with INVOKER rights (Exasol Lua constraint): a caller must already hold the underlying DML privileges on `PREPROC.CONFIG`/`PREPROC.SETTINGS`, which are granted to `PREPROC_ADMIN`. A non-admin caller fails at the first table access with an insufficient-privileges error before any mutation. Hard lockdown of direct DML is therefore impossible; the scripts win on validation and convenience, not enforcement (validation + convention).
* All `pquery` calls bind parameters with named `:param` placeholders sourced from a keyed table; positional `?` placeholders silently fail under Exasol Lua and MUST NOT be used.
* Mutating scripts (`RULE_ADD`, `RULE_DROP`, `RULE_ENABLE`, `RULE_DISABLE`, `SET_MODE`, `SET_SELF_DISABLE`) auto-run the refresh logic inside the same script invocation after a successful mutation, because an EXPAND expansion is a single statement and a separate `REFRESH` call cannot be chained. A `NOREFRESH` parameter suppresses the auto-refresh for batch workflows where the caller will refresh once at the end.
* Read-only scripts (`RULE_LIST`) are `RETURNS TABLE` and never mutate `CONFIG`, `SETTINGS`, or `RESOLUTION`.
* `PREPROC.CONFIG` has no ordinal column; ordering within a phase is derived by refresh from `rule_id` ascending. The operator rule_id space is below the reserved demo range (9000–9999); `RULE_ADD` auto-allocates rule_ids from the operator space and never collides with the demo range.
* `PREPROC.SETTINGS` is a single-row table with columns `mode`, `self_disable_allowed`, `last_refresh`, `state_fingerprint`. Only `mode` and `self_disable_allowed` are operator-mutable through the control surface; `last_refresh` and `state_fingerprint` are owned by refresh.
* Script existence validation uses the dictionary view `EXA_ALL_SCRIPTS` (or `EXA_ALL_SCRIPTS`-equivalent), readable by the admin caller.

## Scenarios

### Scenario: RULE_ADD inserts a validated rule and auto-refreshes

* *GIVEN* a caller holding `PREPROC_ADMIN`
* *AND* a deployed module script the rule will reference
* *WHEN* the caller executes `RULE_ADD` with a valid scope type, scope value, phase, script name, and function name
* *THEN* the script MUST insert exactly one enabled row into `PREPROC.CONFIG` carrying those values, allocating a `rule_id` in the operator space below the reserved demo range (9000–9999)
* *AND* the script MUST run the refresh logic so `PREPROC.RESOLUTION` reflects the new rule before the call returns
* *AND* the script SHOULD return the allocated `rule_id` so the caller can observe the outcome

### Scenario: RULE_ADD auto-allocated rule_ids do not collide

* *GIVEN* a caller holding `PREPROC_ADMIN`
* *AND* `PREPROC.CONFIG` already contains operator rules
* *WHEN* the caller executes `RULE_ADD` twice in succession
* *THEN* each invocation MUST allocate a `rule_id` distinct from every existing `rule_id`
* *AND* neither allocated `rule_id` MUST fall in the reserved demo range (9000–9999)

### Scenario: RULE_ADD rejects an invalid phase

* *GIVEN* a caller holding `PREPROC_ADMIN`
* *WHEN* the caller executes `RULE_ADD` with a phase that is not one of `TRANSLATE`, `EXPAND`, `REWRITE`
* *THEN* the script MUST reject the call with a validation error naming the invalid phase
* *AND* the script MUST NOT insert any row into `PREPROC.CONFIG`
* *AND* `PREPROC.RESOLUTION` MUST remain unchanged

### Scenario: RULE_ADD rejects an invalid scope type

* *GIVEN* a caller holding `PREPROC_ADMIN`
* *WHEN* the caller executes `RULE_ADD` with a scope type that is not `ROLE` or `USER`
* *THEN* the script MUST reject the call with a validation error
* *AND* the script MUST NOT insert any row into `PREPROC.CONFIG`

### Scenario: RULE_ADD rejects a non-existent script

* *GIVEN* a caller holding `PREPROC_ADMIN`
* *WHEN* the caller executes `RULE_ADD` referencing a script name that does not exist in `EXA_ALL_SCRIPTS`
* *THEN* the script MUST reject the call with a validation error naming the missing script
* *AND* the script MUST NOT insert any row into `PREPROC.CONFIG`

### Scenario: RULE_ADD with NOREFRESH skips the auto-refresh

* *GIVEN* a caller holding `PREPROC_ADMIN`
* *AND* a valid rule to add
* *WHEN* the caller executes `RULE_ADD` with the `NOREFRESH` flag set
* *THEN* the script MUST insert the rule into `PREPROC.CONFIG`
* *AND* the script MUST NOT rebuild `PREPROC.RESOLUTION`
* *AND* `PREPROC.SETTINGS.last_refresh` MUST be unchanged by the call

### Scenario: RULE_DROP removes a rule and auto-refreshes

* *GIVEN* a caller holding `PREPROC_ADMIN`
* *AND* an existing rule with a known `rule_id`
* *WHEN* the caller executes `RULE_DROP` for that `rule_id`
* *THEN* the script MUST delete that row from `PREPROC.CONFIG`
* *AND* the script MUST run the refresh logic so `PREPROC.RESOLUTION` no longer reflects the dropped rule
* *AND* the script SHOULD report how many rows were removed

### Scenario: RULE_DROP of an unknown rule_id is a reported no-op

* *GIVEN* a caller holding `PREPROC_ADMIN`
* *WHEN* the caller executes `RULE_DROP` for a `rule_id` that does not exist in `PREPROC.CONFIG`
* *THEN* the script MUST NOT modify any other row in `PREPROC.CONFIG`
* *AND* the script SHOULD report that zero rows were removed
* *AND* the script MUST NOT raise an error

### Scenario: RULE_DISABLE flips the enabled flag and auto-refreshes

* *GIVEN* a caller holding `PREPROC_ADMIN`
* *AND* an enabled rule with a known `rule_id`
* *WHEN* the caller executes `RULE_DISABLE` for that `rule_id`
* *THEN* the script MUST set that rule's `enabled` flag to `FALSE`
* *AND* the script MUST run the refresh logic so `PREPROC.RESOLUTION` no longer reflects the disabled rule

### Scenario: RULE_ENABLE flips the enabled flag and auto-refreshes

* *GIVEN* a caller holding `PREPROC_ADMIN`
* *AND* a disabled rule with a known `rule_id`
* *WHEN* the caller executes `RULE_ENABLE` for that `rule_id`
* *THEN* the script MUST set that rule's `enabled` flag to `TRUE`
* *AND* the script MUST run the refresh logic so `PREPROC.RESOLUTION` reflects the re-enabled rule

### Scenario: RULE_LIST returns the current rules and is read-only

* *GIVEN* a caller holding `PREPROC_ADMIN`
* *AND* `PREPROC.CONFIG` contains one or more rules
* *WHEN* the caller executes `RULE_LIST`
* *THEN* the script MUST return one row per rule carrying at least `rule_id`, `scope_type`, `scope_value`, `phase`, `script_name`, `function_name`, and `enabled`
* *AND* the script MUST NOT modify `PREPROC.CONFIG`, `PREPROC.SETTINGS`, or `PREPROC.RESOLUTION`
* *AND* the rows SHOULD be ordered by `rule_id` ascending

### Scenario: SET_MODE updates the deployment mode and auto-refreshes

* *GIVEN* a caller holding `PREPROC_ADMIN`
* *WHEN* the caller executes `SET_MODE` with a valid mode value
* *THEN* the script MUST update `PREPROC.SETTINGS.mode` to that value
* *AND* the script MUST run the refresh logic so any mode-dependent resolution is rebuilt
* *AND* the script MUST reject an unrecognised mode value without changing `PREPROC.SETTINGS`

### Scenario: SET_SELF_DISABLE updates the self-disable flag

* *GIVEN* a caller holding `PREPROC_ADMIN`
* *WHEN* the caller executes `SET_SELF_DISABLE` with a boolean value
* *THEN* the script MUST update `PREPROC.SETTINGS.self_disable_allowed` to that value
* *AND* the script MUST NOT alter `PREPROC.SETTINGS.mode`, `last_refresh`, or `state_fingerprint`

### Scenario: A non-admin caller cannot mutate config through a control script

* *GIVEN* a database user who does not hold `PREPROC_ADMIN`, `EXECUTE ANY SCRIPT`, or DBA
* *WHEN* that user attempts to execute any mutating control script
* *THEN* the database MUST reject the execution with an insufficient-privileges error
* *AND* `PREPROC.CONFIG`, `PREPROC.SETTINGS`, and `PREPROC.RESOLUTION` MUST remain unchanged

### Scenario: Control scripts pass luacheck

* *GIVEN* the control-script Lua sources
* *WHEN* luacheck is run against them
* *THEN* luacheck MUST report zero warnings and zero errors for those files
