# Feature: Role and User Resolution

Expands the scoped rules in `PREPROC.CONFIG` (`ROLE`- and `USER`-scoped) into the flat, per-user `PREPROC.RESOLUTION` table that the runtime pipeline view reads. Resolution walks the transitive role-grant graph so a rule scoped to a role applies to every user who holds that role directly or through nested role grants. The transitive expansion is performed in SQL using Exasol's native `CONNECT BY` hierarchical query; the Lua script only orchestrates (executes the SQL, owns the transaction). The rebuild is a single atomic operation that also stamps the last-refresh time, so a partially-applied resolution is never observable.

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
