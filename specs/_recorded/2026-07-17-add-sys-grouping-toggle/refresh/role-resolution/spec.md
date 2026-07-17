# Feature: Role and User Resolution

Expands the scoped rules in `PREPROC.CONFIG` into the flat, per-user `PREPROC.RESOLUTION` table. This delta adds a configurable SYS-grouping policy: a `PREPROC.SETTINGS.group_sys_with_dba` boolean governs whether the `SYS` account participates in the ROLE-grant closure. When TRUE (the default, preserving today's behavior) `SYS` resolves ROLE-derived rules exactly as any other role holder. When FALSE, `SYS` is excluded from the ENTIRE ROLE closure — DBA-derived, PUBLIC-derived, and nested-role-derived rows alike — giving `SYS` a guaranteed raw-SQL break-glass recovery path; only USER-scoped rules that name `SYS` still resolve for it. A missing or NULL value is treated as TRUE (grouped) so an upgraded install is unchanged.

## Background

* The toggle is read purely in SQL inside the single static `RESOLUTION_SQL` statement as `COALESCE((SELECT group_sys_with_dba FROM PREPROC.SETTINGS), TRUE)`; the Lua orchestrator adds no branching. The predicate gates ONLY the ROLE-closure UNION arm — it drops `closure.grantee = 'SYS'` from that arm when the toggle is FALSE — so the USER-scoped UNION arm is untouched and the downstream dedupe/ordinal CTEs are unaffected.
* The exclusion is `SYS`-only: no general excluded-users list. `SYS` is the one Exasol account that cannot be granted `PREPROC_ADMIN`, so it is the only account for which raw-SQL recovery must be guaranteed independent of the role graph.
* The default is grouped (`group_sys_with_dba = TRUE`): no behavior change on upgrade. Isolation is opt-in via `PREPROC.SET_SYS_GROUPING` (see Admin Control Scripts). Setting the toggle triggers a rebuild, so RESOLUTION reflects the new policy immediately.
* The toggle is deliberately NOT part of the resolution state fingerprint (`FINGERPRINT_SQL`), mirroring `mode`: setting it always rebuilds and restamps, so STATUS reports CURRENT afterward and the fingerprint continues to track only the user/role-grant inputs.

## Scenarios

<!-- DELTA:NEW -->

### Scenario: SYS resolves DBA-derived rules when grouping is enabled (default)

* *GIVEN* `PREPROC.SETTINGS.group_sys_with_dba` is `TRUE`
* *AND* an enabled rule scoped to `ROLE` `DBA` for phase `EXPAND`
* *AND* the `SYS` account holds `DBA` (as every Exasol superuser does)
* *WHEN* the refresh script runs
* *THEN* `PREPROC.RESOLUTION` MUST contain a row for `SYS` derived from the `DBA` rule, identical to the row any other `DBA` holder receives
* *AND* a rule scoped to `ROLE` `PUBLIC` MUST likewise resolve to `SYS`

<!-- /DELTA:NEW -->

<!-- DELTA:NEW -->

### Scenario: SYS is excluded from the entire role closure when grouping is disabled

* *GIVEN* `PREPROC.SETTINGS.group_sys_with_dba` is `FALSE`
* *AND* an enabled rule scoped to `ROLE` `DBA`, an enabled rule scoped to `ROLE` `PUBLIC`, and an enabled rule scoped to a role `ROLE_X` that is transitively granted to `DBA`
* *WHEN* the refresh script runs
* *THEN* `PREPROC.RESOLUTION` MUST contain no ROLE-derived row for `SYS` — neither from the `DBA` rule, the `PUBLIC` rule, nor the nested `ROLE_X` rule (the full closure is excluded)
* *AND* the exclusion MUST be achieved purely within the static resolution SQL, gating only the ROLE-closure arm

<!-- /DELTA:NEW -->

<!-- DELTA:NEW -->

### Scenario: A USER-scoped rule for SYS still resolves when grouping is disabled

* *GIVEN* `PREPROC.SETTINGS.group_sys_with_dba` is `FALSE`
* *AND* an enabled `USER`-scoped rule whose `scope_value` is `SYS` for phase `EXPAND`
* *WHEN* the refresh script runs
* *THEN* `PREPROC.RESOLUTION` MUST contain the `USER`-scoped row for `SYS`
* *AND* the SYS-exclusion predicate MUST NOT remove any `USER`-scoped row, because it gates only the ROLE-closure arm

<!-- /DELTA:NEW -->

<!-- DELTA:NEW -->

### Scenario: Non-SYS DBA holders are unaffected by the grouping toggle

* *GIVEN* `PREPROC.SETTINGS.group_sys_with_dba` is `FALSE`
* *AND* an enabled rule scoped to `ROLE` `DBA`
* *AND* a non-`SYS` user `PP_DBA` who holds `DBA`
* *WHEN* the refresh script runs
* *THEN* `PREPROC.RESOLUTION` MUST contain the `DBA`-derived row for `PP_DBA`
* *AND* the toggle MUST exclude only the `SYS` account, never any other `DBA` holder

<!-- /DELTA:NEW -->

<!-- DELTA:NEW -->

### Scenario: A missing or NULL grouping value is treated as grouped

* *GIVEN* an upgraded install where `PREPROC.SETTINGS.group_sys_with_dba` is `NULL` (the column was added by the additive upgrade and never set)
* *AND* an enabled rule scoped to `ROLE` `DBA`
* *WHEN* the refresh script runs
* *THEN* `PREPROC.RESOLUTION` MUST contain the `DBA`-derived row for `SYS`, because a `NULL` toggle `COALESCE`s to `TRUE` (grouped)
* *AND* the resolution MUST NOT depend on any backfill `UPDATE` having run — the `COALESCE`-to-`TRUE` at the read site is the sole guarantee

<!-- /DELTA:NEW -->
