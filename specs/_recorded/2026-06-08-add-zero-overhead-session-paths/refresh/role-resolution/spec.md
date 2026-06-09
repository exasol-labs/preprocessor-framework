# Feature: Role and User Resolution

Expands scoped rules into the flat per-user `PREPROC.RESOLUTION` table. This delta adds profile hash computation and profile script codegen as a step in the atomic rebuild, ensuring profile scripts are always consistent with the current resolution, and adds the generational retire-then-reclaim cleanup of orphaned profile scripts as further steps inside the same atomic rebuild.

## Background

Resolution is rebuilt atomically (ADR-010). Profile codegen runs in the admin context of the refresh script. Generated profile scripts are never dropped (never-drop safety contract).

## Scenarios

<!-- DELTA:NEW -->

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

<!-- /DELTA:NEW -->
