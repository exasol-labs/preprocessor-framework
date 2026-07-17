# Feature: Role and User Resolution

Expands the scoped rules in `PREPROC.CONFIG` (`ROLE`- and `USER`-scoped) into the flat, per-user `PREPROC.RESOLUTION` table that the runtime pipeline view reads. Resolution walks the transitive role-grant graph so a rule scoped to a role applies to every user who holds that role directly or through nested role grants. The transitive expansion is performed in SQL using Exasol's native `CONNECT BY` hierarchical query; the Lua script only orchestrates (executes the SQL, owns the transaction). The rebuild is a single atomic operation that also stamps the last-refresh time, so a partially-applied resolution is never observable. This delta adds profile hash computation and profile script codegen as a step in the atomic rebuild, ensuring profile scripts are always consistent with the current resolution, and adds the generational retire-then-reclaim cleanup of orphaned profile scripts as further steps inside the same atomic rebuild. It also adds a configurable SYS-grouping policy: a `PREPROC.SETTINGS.group_sys_with_dba` boolean governs whether the `SYS` account participates in the ROLE-grant closure. When TRUE (the default, preserving today's behavior) `SYS` resolves ROLE-derived rules exactly as any other role holder. When FALSE, `SYS` is excluded from the ENTIRE ROLE closure — DBA-derived, PUBLIC-derived, and nested-role-derived rows alike — giving `SYS` a guaranteed raw-SQL break-glass recovery path; only USER-scoped rules that name `SYS` still resolve for it. A missing or NULL value is treated as TRUE (grouped) so an upgraded install is unchanged.

## Background

* Resolution executes entirely inside the database. The only entry point is a Lua script owned by `PREPROC_ADMIN` (`PREPROC.REFRESH`), callable from any SQL client by a caller who holds `PREPROC_ADMIN` (or `EXECUTE ANY SCRIPT` / DBA). There is no external (Python) administration path: the framework is administered through DB-side scripts so a future framework-controlled management syntax can intercept and translate management statements onto these same scripts.
* The script orchestrates; it does not re-implement graph traversal in Lua. It reads the dictionary tables `EXA_DBA_ROLE_PRIVS` (role-to-role and role-to-user grants) and `EXA_DBA_USERS` (the user universe), and reads/writes the admin tables `PREPROC.CONFIG`, `PREPROC.RESOLUTION`, and `PREPROC.SETTINGS`.
* Exasol Lua scripts run with invoker rights; reading the dictionary tables requires the `SELECT ANY DICTIONARY` system privilege. The privilege is granted to the `PREPROC_ADMIN` role, and the refresh script is executable only by holders of that role (or `EXECUTE ANY SCRIPT` / DBA) — never by PUBLIC. Resolution is therefore an administrative operation, not a per-user one.
* Transitive role expansion is done in SQL with Exasol's native `CONNECT BY` hierarchical query over `EXA_DBA_ROLE_PRIVS` (`CONNECT BY PRIOR`/`START WITH`). `CONNECT BY` is native Exasol SQL and requires no separate license or add-on. The Lua script issues this query rather than iterating the grant graph itself.
* Only rules with `enabled = TRUE` are materialised. Disabled rules are excluded at resolution time and never reach `PREPROC.RESOLUTION`.
* Resolution is purely additive, modelled on SQL privileges: a user's resolved pipeline is the UNION (superset) of every enabled `ROLE`-derived rule (via the transitive role closure) and every enabled `USER`-scoped rule that applies to that user. There is no override, replacement, or exclusion mechanism of any kind — a `USER`-scoped rule adds to, and never displaces, `ROLE`-derived rules. To make a user execute less, the admin removes them from a role, disables the rule, or defines a narrower rule; the framework offers no per-user subtraction (exactly as SQL cannot revoke a single privilege from a user who inherits it through a role).
* Ordering within a phase is pure `rule_id` ascending, with scope type (`ROLE` vs `USER`) ignored — a `USER`-scoped rule does not sort before or after `ROLE`-derived rules by virtue of its scope; only its `rule_id` matters. The `ordinal` column on `PREPROC.RESOLUTION` records this `rule_id`-ascending order within each phase. The admin controls execution sequence through the `rule_id` values they assign.
* Deduplication: if the same `(phase, script, function)` reaches a user through multiple roles, or through both a role and a direct `USER` rule, it materialises as exactly one row in `PREPROC.RESOLUTION` (lowest contributing `rule_id` wins the `ordinal`). The same function MUST NOT execute twice in one phase for one user.
* The full rebuild (delete all rows, reinsert resolved rows, stamp `SETTINGS.last_refresh`) is committed as a single transaction. Either the new resolution and its timestamp are both visible, or neither is.
* The `group_sys_with_dba` toggle is read purely in SQL inside the single static `RESOLUTION_SQL` statement as `COALESCE((SELECT group_sys_with_dba FROM PREPROC.SETTINGS), TRUE)`; the Lua orchestrator adds no branching. The predicate gates ONLY the ROLE-closure UNION arm — it drops `closure.grantee = 'SYS'` from that arm when the toggle is FALSE — so the USER-scoped UNION arm is untouched and the downstream dedupe/ordinal CTEs are unaffected.
* The exclusion is `SYS`-only: no general excluded-users list. `SYS` is the one Exasol account that cannot be granted `PREPROC_ADMIN`, so it is the only account for which raw-SQL recovery must be guaranteed independent of the role graph.
* The default is grouped (`group_sys_with_dba = TRUE`): no behavior change on upgrade. Isolation is opt-in via `PREPROC.SET_SYS_GROUPING` (see Admin Control Scripts). Setting the toggle triggers a rebuild, so RESOLUTION reflects the new policy immediately.
* The toggle is deliberately NOT part of the resolution state fingerprint (`FINGERPRINT_SQL`), mirroring `mode`: setting it always rebuilds and restamps, so STATUS reports CURRENT afterward and the fingerprint continues to track only the user/role-grant inputs.

## Scenarios

### Scenario: A direct role grant resolves to the holder

* *GIVEN* `PREPROC.CONFIG` has an enabled rule scoped to `ROLE` `ROLE_A` for phase `EXPAND`
* *AND* user `ALICE` has been granted `ROLE_A` directly
* *WHEN* the refresh script runs
* *THEN* `PREPROC.RESOLUTION` MUST contain a row for `ALICE` carrying that rule's phase, script, and function
* *AND* the row MUST be readable by `ALICE` through `PREPROC_RT.MY_PIPELINE`

### Scenario: A nested role grant resolves transitively via CONNECT BY

* *GIVEN* an enabled `ROLE`-scoped rule for `ROLE_A`
* *AND* `ROLE_A` has been granted to `ROLE_B`
* *AND* user `BOB` holds `ROLE_B` but not `ROLE_A` directly
* *WHEN* the refresh script runs
* *THEN* `PREPROC.RESOLUTION` MUST contain a row for `BOB` derived from the `ROLE_A` rule
* *AND* the SQL `CONNECT BY` expansion MUST follow the transitive `EXA_DBA_ROLE_PRIVS` grant chain to arbitrary nesting depth

### Scenario: A user without the granting role gets no row

* *GIVEN* an enabled `ROLE`-scoped rule for `ROLE_A`
* *AND* user `CAROL` holds neither `ROLE_A` nor any role transitively granted `ROLE_A`
* *WHEN* the refresh script runs
* *THEN* `PREPROC.RESOLUTION` MUST contain no row for `CAROL` derived from that rule

### Scenario: A rule scoped to the PUBLIC role resolves to every user

* *GIVEN* an enabled rule scoped to `ROLE` `PUBLIC`
* *AND* every database user implicitly holds `PUBLIC`, a membership that is automatic and never recorded as a row in `EXA_DBA_ROLE_PRIVS`
* *WHEN* the refresh script runs
* *THEN* `PREPROC.RESOLUTION` MUST contain a row carrying that rule for **every** user in `EXA_DBA_USERS`, including users that hold no explicit role
* *AND* the resolution MUST NOT depend on any `PUBLIC` grant appearing in `EXA_DBA_ROLE_PRIVS` (the closure MUST treat `PUBLIC` as held by all users)
* *AND* a real role that has been granted to `PUBLIC` MUST likewise resolve to every user (its grant chain reaches `PUBLIC` and fans out to all users)

### Scenario: A USER-scoped rule unions with ROLE-derived rules for the same phase

* *GIVEN* an enabled `ROLE`-scoped rule for `ROLE_A` at phase `EXPAND` carrying functions `a`, `b`, `c` (`rule_id`s 10, 20, 30)
* *AND* an enabled `USER`-scoped rule for user `DAVE` at phase `EXPAND` carrying function `d` (`rule_id` 40)
* *AND* `DAVE` holds `ROLE_A`
* *WHEN* the refresh script runs
* *THEN* `DAVE`'s `EXPAND` resolution MUST contain the superset of the `ROLE_A`-derived rules and the `USER`-scoped rule — functions `a`, `b`, `c`, and `d`
* *AND* the `USER`-scoped rule MUST NOT displace, override, or exclude any `ROLE_A`-derived rule
* *AND* the rules MUST be ordered by `rule_id` ascending with scope type ignored, so `DAVE`'s `EXPAND` pipeline runs `a`, `b`, `c`, `d` in that order

### Scenario: Multiple role-derived rules for one phase coexist in deterministic order

* *GIVEN* two enabled `ROLE`-scoped rules at phase `EXPAND`, with `rule_id` 10 and 20, applying to user `ERIN` through her roles
* *AND* no `USER`-scoped `EXPAND` rule exists for `ERIN`
* *WHEN* the refresh script runs
* *THEN* `PREPROC.RESOLUTION` MUST contain both rules for `ERIN` at phase `EXPAND`
* *AND* the rule with `rule_id` 10 MUST carry a lower `ordinal` than the rule with `rule_id` 20

### Scenario: The same function reaching a user by multiple paths is deduplicated

* *GIVEN* an enabled rule carrying phase `EXPAND`, script `S`, function `f` reaches user `FRANK` through two distinct roles he holds (`ROLE_A` and `ROLE_B`)
* *AND* a second copy of the same `(EXPAND, S, f)` also reaches `FRANK` through a direct `USER`-scoped rule
* *WHEN* the refresh script runs
* *THEN* `PREPROC.RESOLUTION` MUST contain exactly one row for `FRANK` carrying `(EXPAND, S, f)`
* *AND* that function MUST NOT be dispatched more than once in the `EXPAND` phase for `FRANK`
* *AND* the surviving row's `ordinal` MUST derive from the lowest contributing `rule_id`

### Scenario: Disabled rules are excluded

* *GIVEN* a `CONFIG` rule whose `enabled` flag is `FALSE`
* *WHEN* the refresh script runs
* *THEN* `PREPROC.RESOLUTION` MUST contain no row derived from that rule
* *AND* a later refresh after the rule is re-enabled MUST then materialise it

### Scenario: Refresh fully rebuilds, never appends

* *GIVEN* `PREPROC.RESOLUTION` already holds rows from a previous refresh
* *AND* a rule that produced some of those rows has since been deleted from `PREPROC.CONFIG`
* *WHEN* the refresh script runs again
* *THEN* `PREPROC.RESOLUTION` MUST reflect only the current enabled `CONFIG`, with no orphaned rows from the deleted rule
* *AND* the table MUST NOT contain duplicate rows from the previous run

### Scenario: Rebuild and timestamp are atomic

* *GIVEN* a populated `PREPROC.CONFIG`
* *WHEN* the refresh script completes successfully
* *THEN* `PREPROC.SETTINGS.last_refresh` MUST be updated to the refresh time within the same transaction as the `RESOLUTION` rebuild
* *AND* no concurrent reader MUST observe an emptied `RESOLUTION` paired with an old `last_refresh`, nor a populated `RESOLUTION` paired with a stale `last_refresh`

### Scenario: Refresh is callable from any SQL client by an admin

* *GIVEN* a database user who holds `PREPROC_ADMIN` connected through any SQL client
* *WHEN* that user issues `EXECUTE SCRIPT PREPROC.REFRESH`
* *THEN* the resolution MUST be rebuilt and `last_refresh` stamped, with no external (Python) tool required
* *AND* the script SHOULD return the number of resolution rows produced and the new `last_refresh` time so the caller can observe the outcome

### Scenario: Refresh requires admin privileges

* *GIVEN* a database user who does not hold `PREPROC_ADMIN`, `EXECUTE ANY SCRIPT`, or DBA
* *WHEN* that user attempts to execute `PREPROC.REFRESH`
* *THEN* the database MUST reject the execution with an insufficient-privileges error
* *AND* `PREPROC.RESOLUTION` MUST remain unchanged

### Scenario: Refresh computes and stores a profile_hash for every non-empty resolved user chain

* *GIVEN* the resolution rebuild has produced at least one row for a given user
* *WHEN* `PREPROC.REFRESH_CORE.rebuild()` completes
* *THEN* `PREPROC.RESOLUTION` MUST contain a `profile_hash` value for that user's rows (all rows for the same user MUST carry the same hash)
* *AND* the hash MUST be a deterministic function of the ordered `(phase, ordinal, script_name, function_name, client_pattern)` chain for that user
* *AND* users with no resolved rows MUST have no rows in RESOLUTION and therefore no hash

### Scenario: Profile_hash changes when a user's chain changes

* *GIVEN* a user's resolved chain at refresh N is `[module_A.fn @ ordinal 1]`
* *AND* the chain at refresh N+1 is `[module_A.fn @ ordinal 1, module_B.fn @ ordinal 2]` (a rule was added)
* *WHEN* refresh N+1 runs
* *THEN* the `profile_hash` for that user MUST differ from the hash produced by refresh N

### Scenario: Rebuild atomicity is preserved with profile codegen

* *GIVEN* a refresh is in progress
* *WHEN* `PREPROC.REFRESH_CORE.rebuild()` executes
* *THEN* the profile codegen step MUST run after RESOLUTION rows are computed but before the single COMMIT
* *AND* the `PREPROC_RT.P_<hash>` script MUST be created (via CREATE OR REPLACE) before the COMMIT that makes the RESOLUTION rows (including their profile_hash) visible to readers
* *AND* the fingerprint-first / single-commit atomicity invariant from ADR-010 MUST be preserved

### Scenario: Profile codegen runs after every CONFIG mutation that triggers rebuild

* *GIVEN* a mutating admin script (e.g. RULE_ADD, SET_MODE) triggers `PREPROC.REFRESH_CORE.rebuild()` via the ADR-018 auto-refresh mechanism
* *WHEN* the rebuild completes
* *THEN* profile scripts MUST be generated or refreshed for all current distinct non-empty user chains
* *AND* the `profile_hash` columns in RESOLUTION MUST reflect the newly generated scripts

### Scenario: Rebuild runs generate, retire, and reclaim phases in a fixed order within the atomic envelope

* *GIVEN* a refresh is in progress
* *WHEN* `PREPROC.REFRESH_CORE.rebuild()` executes
* *THEN* the rebuild MUST perform the steps in the fixed order: capture fingerprint, DELETE/INSERT RESOLUTION, generate (`CREATE OR REPLACE` real bodies for all current chains, clearing `orphaned_at` for any re-adopted hash), retire (stub + stamp `orphaned_at` for newly-orphaned hashes), reclaim (`DROP SCRIPT` + delete `PREPROC.PROFILES` row for retired hashes that are now provably unpinned), stamp SETTINGS, then a single COMMIT
* *AND* the generate phase MUST run before the retire phase so a re-adopted hash is restored to its real body and never left as a stub
* *AND* all of generate, retire, and reclaim MUST occur before the single COMMIT so the entire sequence is atomic (ADR-010)
* *AND* no committed RESOLUTION row MUST ever reference a `profile_hash` whose script has been dropped by the reclaim phase

### Scenario: SYS resolves DBA-derived rules when grouping is enabled (default)

* *GIVEN* `PREPROC.SETTINGS.group_sys_with_dba` is `TRUE`
* *AND* an enabled rule scoped to `ROLE` `DBA` for phase `EXPAND`
* *AND* the `SYS` account holds `DBA` (as every Exasol superuser does)
* *WHEN* the refresh script runs
* *THEN* `PREPROC.RESOLUTION` MUST contain a row for `SYS` derived from the `DBA` rule, identical to the row any other `DBA` holder receives
* *AND* a rule scoped to `ROLE` `PUBLIC` MUST likewise resolve to `SYS`

### Scenario: SYS is excluded from the entire role closure when grouping is disabled

* *GIVEN* `PREPROC.SETTINGS.group_sys_with_dba` is `FALSE`
* *AND* an enabled rule scoped to `ROLE` `DBA`, an enabled rule scoped to `ROLE` `PUBLIC`, and an enabled rule scoped to a role `ROLE_X` that is transitively granted to `DBA`
* *WHEN* the refresh script runs
* *THEN* `PREPROC.RESOLUTION` MUST contain no ROLE-derived row for `SYS` — neither from the `DBA` rule, the `PUBLIC` rule, nor the nested `ROLE_X` rule (the full closure is excluded)
* *AND* the exclusion MUST be achieved purely within the static resolution SQL, gating only the ROLE-closure arm

### Scenario: A USER-scoped rule for SYS still resolves when grouping is disabled

* *GIVEN* `PREPROC.SETTINGS.group_sys_with_dba` is `FALSE`
* *AND* an enabled `USER`-scoped rule whose `scope_value` is `SYS` for phase `EXPAND`
* *WHEN* the refresh script runs
* *THEN* `PREPROC.RESOLUTION` MUST contain the `USER`-scoped row for `SYS`
* *AND* the SYS-exclusion predicate MUST NOT remove any `USER`-scoped row, because it gates only the ROLE-closure arm

### Scenario: Non-SYS DBA holders are unaffected by the grouping toggle

* *GIVEN* `PREPROC.SETTINGS.group_sys_with_dba` is `FALSE`
* *AND* an enabled rule scoped to `ROLE` `DBA`
* *AND* a non-`SYS` user `PP_DBA` who holds `DBA`
* *WHEN* the refresh script runs
* *THEN* `PREPROC.RESOLUTION` MUST contain the `DBA`-derived row for `PP_DBA`
* *AND* the toggle MUST exclude only the `SYS` account, never any other `DBA` holder

### Scenario: A missing or NULL grouping value is treated as grouped

* *GIVEN* an upgraded install where `PREPROC.SETTINGS.group_sys_with_dba` is `NULL` (the column was added by the additive upgrade and never set)
* *AND* an enabled rule scoped to `ROLE` `DBA`
* *WHEN* the refresh script runs
* *THEN* `PREPROC.RESOLUTION` MUST contain the `DBA`-derived row for `SYS`, because a `NULL` toggle `COALESCE`s to `TRUE` (grouped)
* *AND* the resolution MUST NOT depend on any backfill `UPDATE` having run — the `COALESCE`-to-`TRUE` at the read site is the sole guarantee
