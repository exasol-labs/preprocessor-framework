# Feature: Session-Level Zero-Overhead Paths

Enables convenience-mode sessions to pay the MASTER pipeline-lookup overhead (+7.3 ms) exactly once — on the first statement — then self-optimize for the rest of the session. Sessions with no rules self-disable (preprocessor cleared, subsequent statements pay ~0 ms). Sessions with rules swap to a generated, hash-named profile script `PREPROC_RT.P_<hash>` whose function set is baked in, reducing overhead to ~0.5 ms/stmt for the remainder of the session. Enforced-mode sessions are never modified; they pay the lookup floor by design.

Profile scripts whose hash is no longer referenced by any current resolved chain are cleaned up via a generational **retire-then-reclaim** lifecycle that runs inside the refresh path: an orphaned profile is first *retired* (its body replaced with a stub that bounces any still-pinned session back to MASTER, and stamped with `orphaned_at`) and only later, on a subsequent refresh, *reclaimed* (physically dropped) once no active session could still be pinned to it. Same-refresh dropping is provably unsafe; deferred dropping is race-free.

## Background

All scenarios assume `PREPROC_RT.MASTER` is the active session or system preprocessor for the session under test.
Profile scripts `PREPROC_RT.P_<hash>` are generated eagerly by the refresh path; MASTER never generates them on demand.
The schema-level PUBLIC EXECUTE grant on `PREPROC_RT` (ADR-002 invariant) covers profile scripts automatically.
Queries issued from inside a preprocessor script are not themselves preprocessed (no recursion risk).

## Scenarios

### Scenario: No-rule session self-disables in convenience mode

* *GIVEN* `PREPROC.SETTINGS.mode` is `convenience`
* *AND* `PREPROC.SETTINGS.self_disable_allowed` is `TRUE`
* *AND* the current user has no resolved rules in `PREPROC.RESOLUTION` (all-NULL phase sentinel)
* *AND* `PREPROC_RT.MY_PIPELINE` exposes a NULL `profile_hash` for the user
* *WHEN* the user submits any ordinary SQL statement (statement 1)
* *THEN* MASTER MUST dispatch statement 1 through the pipeline as normal (no-op, no rules)
* *AND* MASTER MUST issue `ALTER SESSION SET sql_preprocessor_script = ''` via pquery to clear the session preprocessor
* *AND* the `ALTER SESSION` MUST be issued after dispatching statement 1 but before writing the statement via setsqltext
* *AND* subsequent statements in that session MUST NOT invoke any preprocessor
* *AND* the self-disable `ALTER SESSION` MUST NOT be itself intercepted by MASTER

### Scenario: Self-disable is not attempted when self_disable_allowed is FALSE

* *GIVEN* `PREPROC.SETTINGS.mode` is `convenience`
* *AND* `PREPROC.SETTINGS.self_disable_allowed` is `FALSE`
* *AND* the current user has no resolved rules
* *WHEN* the user submits an ordinary SQL statement
* *THEN* MASTER MUST NOT issue any `ALTER SESSION` to modify the session preprocessor
* *AND* the statement MUST pass through unchanged (no-rules pass-through)
* *AND* subsequent statements MUST continue to invoke MASTER normally

### Scenario: Rule-bearing session swaps to a profile script in convenience mode

* *GIVEN* `PREPROC.SETTINGS.mode` is `convenience`
* *AND* the current user has resolved rules in `PREPROC.RESOLUTION`
* *AND* `PREPROC_RT.MY_PIPELINE` exposes a non-NULL `profile_hash` for the user
* *AND* `PREPROC_RT.P_<hash>` exists in the runtime schema
* *WHEN* the user submits any ordinary SQL statement (statement 1)
* *THEN* MASTER MUST dispatch statement 1 through the full pipeline (the user's rules apply)
* *AND* MASTER MUST issue `ALTER SESSION SET sql_preprocessor_script = PREPROC_RT.P_<hash>` via pquery after dispatching statement 1
* *AND* subsequent statements in that session MUST be dispatched by `PREPROC_RT.P_<hash>`, not MASTER
* *AND* the swap `ALTER SESSION` MUST NOT be intercepted by MASTER

### Scenario: Profile swap uses profile_hash from MY_PIPELINE, not re-derived in Lua

* *GIVEN* the current user has resolved rules with a `profile_hash` recorded in `PREPROC_RT.MY_PIPELINE`
* *WHEN* MASTER reads MY_PIPELINE on statement 1
* *THEN* MASTER MUST use the `profile_hash` value read from the view to name the target script
* *AND* MASTER MUST NOT compute or re-hash the rule chain in Lua
* *AND* the column MUST be readable by invoker-rights MASTER via the definer-rights view without direct access to admin tables

### Scenario: Enforced mode — swap is never attempted, lookup runs on every statement

* *GIVEN* `PREPROC.SETTINGS.mode` is `enforced`
* *AND* the session is running with `PREPROC_RT.MASTER` as the system preprocessor
* *WHEN* the user submits any statement
* *THEN* MASTER MUST NOT issue any `ALTER SESSION` to modify the session preprocessor
* *AND* MASTER MUST perform the full MY_PIPELINE lookup on every statement
* *AND* the user's pipeline MUST be dispatched normally on every statement

### Scenario: First statement is correctly dispatched before the swap

* *GIVEN* `PREPROC.SETTINGS.mode` is `convenience`
* *AND* the current user has resolved rules including a REWRITE rule that appends a tag
* *WHEN* the user submits a statement (statement 1)
* *THEN* MASTER MUST dispatch statement 1 through the user's full pipeline (the REWRITE tag MUST appear in the output)
* *AND* the swap `ALTER SESSION` MUST be issued after dispatch and before setsqltext
* *AND* the statement text written by setsqltext MUST reflect the pipeline's transformation of statement 1

### Scenario: Swap failure leaves the session on MASTER, fail open

* *GIVEN* `PREPROC.SETTINGS.mode` is `convenience`
* *AND* the current user has resolved rules with a valid `profile_hash`
* *AND* the `ALTER SESSION` pquery fails (e.g. the target profile script is unexpectedly missing, or the call is rejected)
* *WHEN* the user submits a statement
* *THEN* MASTER MUST catch the failure and continue normally
* *AND* MASTER MUST NOT raise an error that blocks or corrupts the statement
* *AND* subsequent statements MUST continue to invoke MASTER (fail-open: session stays on the full-lookup path)

### Scenario: Self-disable failure leaves the session on MASTER, fail open

* *GIVEN* `PREPROC.SETTINGS.mode` is `convenience`
* *AND* `PREPROC.SETTINGS.self_disable_allowed` is `TRUE`
* *AND* the current user has no rules
* *AND* the `ALTER SESSION SET sql_preprocessor_script = ''` pquery fails unexpectedly
* *WHEN* the user submits a statement
* *THEN* MASTER MUST catch the failure and continue normally
* *AND* the statement MUST pass through unchanged (no-rules pass-through)
* *AND* MASTER MUST NOT raise an error that blocks the statement

### Scenario: Profile script reproduces full dispatch semantics without a MY_PIPELINE lookup

* *GIVEN* `PREPROC_RT.P_<hash>` is the active session preprocessor
* *AND* the script contains a baked, static rule table derived from the user's resolved chain at refresh time
* *WHEN* the user submits a statement
* *THEN* the profile script MUST NOT issue any pquery to `PREPROC_RT.MY_PIPELINE` or any other view or table
* *AND* the profile script MUST apply the ALTER SESSION / ALTER SYSTEM guard (return early, unchanged)
* *AND* the profile script MUST apply per-rule pcall fail-open (a failing baked rule MUST be skipped)
* *AND* the profile script MUST enforce EXPAND first-match-terminal semantics
* *AND* the profile script MUST apply client_pattern literal-substring matching (not Lua pattern matching)
* *AND* the profile script MUST apply phases in fixed order TRANSLATE then EXPAND then REWRITE
* *AND* the profile script MUST write the result via setsqltext only when text changed

### Scenario: Profile script reproduces client_pattern literal-substring matching

* *GIVEN* `PREPROC_RT.P_<hash>` is the active session preprocessor
* *AND* the baked rule table contains a rule with a non-NULL `client_pattern`
* *WHEN* the connecting client identity contains the pattern as a literal substring
* *THEN* the rule MUST be dispatched
* *AND* when the client identity does not contain the pattern as a literal substring, the rule MUST NOT be dispatched
* *AND* the profile script MUST NOT interpret `client_pattern` as a Lua regular expression pattern

### Scenario: Profile script for no-rule user is never generated

* *GIVEN* a user has no resolved rules (empty RESOLUTION for that user)
* *WHEN* the refresh path runs profile codegen
* *THEN* no `PREPROC_RT.P_<hash>` script MUST be generated for that user's empty chain
* *AND* no entry MUST be inserted into `PREPROC.PROFILES` for an empty rule chain

## Scenarios (Profile Codegen — Refresh Integration)

### Scenario: Eager profile generation at refresh time under admin context

* *GIVEN* a refresh (via `PREPROC.REFRESH` or any mutating admin script) has completed the RESOLUTION rebuild
* *AND* the caller holds `PREPROC_ADMIN` privileges
* *WHEN* the profile codegen step runs as part of the same refresh invocation
* *THEN* `PREPROC.REFRESH_CORE` MUST generate `PREPROC_RT.P_<hash>` scripts for every distinct non-empty resolved chain
* *AND* generated scripts MUST be created via `CREATE OR REPLACE LUA SCRIPT` in the `PREPROC_RT` schema
* *AND* each script MUST be recorded in `PREPROC.PROFILES` with its `profile_hash`, `manifest`, and `created_at`
* *AND* the codegen MUST run under the invoker-rights of the caller (i.e. in the admin context of the refresh script — not in the context of any end user)

### Scenario: Content-addressed profiles are idempotent across refreshes

* *GIVEN* a user's resolved chain has not changed between two consecutive refreshes
* *WHEN* the second refresh runs
* *THEN* `PREPROC.REFRESH_CORE` MUST issue `CREATE OR REPLACE LUA SCRIPT` for the same hash, replacing it with an identical body
* *AND* the operation MUST succeed without error (idempotent)
* *AND* sessions currently pinned to that profile MUST continue to function correctly

### Scenario: Hash manifest distinguishes chains with different rules, order, or client patterns

* *GIVEN* user A has rules `[module1@v1.fn, module2@v1.fn]` in that ordinal order
* *AND* user B has rules `[module2@v1.fn, module1@v1.fn]` in the reversed order
* *WHEN* the refresh path computes profile hashes
* *THEN* user A and user B MUST receive different `profile_hash` values
* *AND* a user with the same function list but a different `client_pattern` on a rule MUST also receive a different hash

### Scenario: Profile hash is carried in RESOLUTION and surfaced via MY_PIPELINE

* *GIVEN* a refresh has completed
* *AND* a user has at least one resolved rule
* *WHEN* MY_PIPELINE is queried for that user
* *THEN* the result MUST include a non-NULL `profile_hash` column
* *AND* the hash MUST identify the single `PREPROC_RT.P_<hash>` script that encodes that user's complete rule chain
* *AND* users with no resolved rules MUST see a NULL `profile_hash` from MY_PIPELINE

### Scenario: Refresh never drops an existing profile script

* *GIVEN* `PREPROC_RT.P_<old-hash>` exists and may be pinned as the session preprocessor for one or more active sessions
* *AND* the rule configuration has changed such that no current user maps to `<old-hash>` after the refresh
* *WHEN* the refresh path runs
* *THEN* `PREPROC_RT.P_<old-hash>` MUST NOT be dropped
* *AND* `PREPROC.PROFILES` MUST NOT have the `<old-hash>` row deleted
* *AND* sessions pinned to `P_<old-hash>` MUST continue to execute their statements successfully

### Scenario: Profile generation preserves atomicity — RESOLUTION never references a not-yet-created profile

* *GIVEN* a refresh is in progress
* *WHEN* `PREPROC.REFRESH_CORE` executes the RESOLUTION rebuild and profile codegen
* *THEN* profile scripts MUST be created (via CREATE OR REPLACE) before the transaction that writes `profile_hash` into RESOLUTION is committed
* *AND* at no point MUST a committed RESOLUTION row reference a `profile_hash` whose corresponding `PREPROC_RT.P_<hash>` script does not yet exist

### Scenario: Profile codegen is DB-side Lua, not Python

* *GIVEN* the framework's ADR-007 constraint that Python CLI is deployment-only
* *WHEN* profile scripts are generated during a refresh or admin mutation
* *THEN* the generation MUST be performed by a Lua script running in the database (i.e. in `PREPROC.REFRESH_CORE` or a helper it imports)
* *AND* the Python CLI MUST NOT contain profile generation logic

## Scenarios (Profile Cleanup — Retire then Reclaim)

### Scenario: An orphaned profile is retired (not dropped) on the refresh that orphans it

* *GIVEN* `PREPROC_RT.P_<hash>` exists and its row in `PREPROC.PROFILES` has `orphaned_at` NULL
* *AND* after the resolution rebuild no current RESOLUTION row references `<hash>`
* *WHEN* the retire phase of `PREPROC.REFRESH_CORE.rebuild()` runs
* *THEN* the system MUST NOT physically drop `PREPROC_RT.P_<hash>` on this refresh
* *AND* the system MUST issue `CREATE OR REPLACE LUA SCRIPT PREPROC_RT.P_<hash>` replacing its body with a retirement stub
* *AND* the system MUST stamp `PREPROC.PROFILES.orphaned_at = CURRENT_TIMESTAMP` for `<hash>`
* *AND* the retire and stamp MUST occur inside the same single-commit atomic rebuild envelope as the RESOLUTION rebuild (ADR-010)
* *AND* a profile whose `orphaned_at` is already non-NULL MUST NOT be re-stamped (retirement is stamped exactly once)

### Scenario: A session pinned to a retired profile re-runs full MASTER logic for the current statement and re-points to the correct current target

* *GIVEN* a convenience-mode session is pinned to `PREPROC_RT.P_<hash>` as its session preprocessor
* *AND* `<hash>` has been retired (its body is now the retirement stub, which is functionally identical to MASTER)
* *AND* the now-current resolution for the user includes a REWRITE rule that transforms the statement (e.g. appends a tag)
* *WHEN* the session submits its next (ordinary, non-control) statement
* *THEN* the retirement stub MUST re-run the full resolve→dispatch→swap logic for the CURRENT statement, looking up the now-current pipeline via `PREPROC_RT.MY_PIPELINE`
* *AND* the stub MUST transform the CURRENT triggering statement through the now-current pipeline (the now-current REWRITE rule MUST appear in the output of that same triggering statement)
* *AND* the stub MUST NOT return the triggering statement unchanged when the now-current pipeline would transform it (there MUST be no untransformed-statement gap)
* *AND* the stub MUST re-point the session via `ALTER SESSION` to the correct current target — the correct current `PREPROC_RT.P_<new-hash>` when the user has rules, or self-disable (`sql_preprocessor_script = ''`) / `PREPROC_RT.MASTER` as the current resolution dictates — so subsequent statements run on the right script
* *AND* the stub MUST apply the ALTER SESSION / ALTER SYSTEM control-statement guard (a control statement MUST return early, unchanged, without dispatching or re-pointing)
* *AND* the stub MUST apply per-rule pcall fail-open and setsqltext-only-on-change exactly as MASTER does
* *AND* the stub MUST be fail-open: if the re-point `ALTER SESSION` pquery fails, the stub MUST catch the error and MUST NOT raise or block the statement (the statement's transformation MUST still be applied; the session remains safely dispatchable)

### Scenario: A retired profile is NOT reclaimed while an active session that logged in before its orphaned_at exists

* *GIVEN* `PREPROC.PROFILES.orphaned_at` for `<hash>` is non-NULL
* *AND* at least one active session in `EXA_ALL_SESSIONS` has `LOGIN_TIME < orphaned_at`
* *WHEN* the reclaim phase of `PREPROC.REFRESH_CORE.rebuild()` runs
* *THEN* the system MUST NOT issue `DROP SCRIPT PREPROC_RT.P_<hash>`
* *AND* the system MUST NOT delete the `<hash>` row from `PREPROC.PROFILES`
* *AND* the retired stub script MUST remain in place so that the potentially-pinned session is still bounced safely on its next statement

### Scenario: A retired profile IS reclaimed once no active session logged in before its orphaned_at

* *GIVEN* `PREPROC.PROFILES.orphaned_at` for `<hash>` is non-NULL
* *AND* no active session in `EXA_ALL_SESSIONS` has `LOGIN_TIME < orphaned_at` (equivalently `orphaned_at <= MIN(LOGIN_TIME)` over all active sessions, OR there are zero active sessions)
* *WHEN* the reclaim phase of `PREPROC.REFRESH_CORE.rebuild()` runs
* *THEN* the system MUST issue `DROP SCRIPT PREPROC_RT.P_<hash>`
* *AND* the system MUST delete the `<hash>` row from `PREPROC.PROFILES`
* *AND* the drop and the RESOLUTION rebuild MUST commit together in the single atomic envelope (ADR-010)
* *AND* when there are zero active sessions every retired profile MUST be reclaimable

### Scenario: A re-adopted hash has orphaned_at cleared and its real baked body restored

* *GIVEN* `PREPROC_RT.P_<hash>` was previously retired (its body is the stub and `PREPROC.PROFILES.orphaned_at` is non-NULL)
* *AND* a configuration change re-adds rules such that a current RESOLUTION chain now resolves to `<hash>` again
* *WHEN* `PREPROC.REFRESH_CORE.rebuild()` runs (generate phase before retire phase)
* *THEN* the generate phase MUST `CREATE OR REPLACE LUA SCRIPT PREPROC_RT.P_<hash>` with the real baked dispatch body (not the stub)
* *AND* the system MUST clear `PREPROC.PROFILES.orphaned_at` back to NULL for `<hash>`
* *AND* the retire phase MUST NOT re-retire `<hash>` (it is again current/referenced)
* *AND* the reclaim phase MUST NOT drop `<hash>`

### Scenario: Cleanup never retires or drops a currently-referenced profile

* *GIVEN* `PREPROC_RT.P_<hash>` is referenced by at least one current RESOLUTION row after the rebuild
* *WHEN* the retire and reclaim phases run
* *THEN* the system MUST NOT install a retirement stub for `<hash>`
* *AND* the system MUST NOT stamp `orphaned_at` for `<hash>`
* *AND* the system MUST NOT drop `PREPROC_RT.P_<hash>`
* *AND* `<hash>`'s `PREPROC.PROFILES.orphaned_at` MUST remain NULL

### Scenario: The retirement stub is fail-open and honors the control-statement guard

* *GIVEN* `PREPROC_RT.P_<hash>` has been retired to a stub body and is the active session preprocessor
* *WHEN* the session submits an `ALTER SESSION` or `ALTER SYSTEM` control statement
* *THEN* the stub MUST return that control statement unchanged and MUST NOT dispatch or issue any re-point ALTER SESSION (the guard prevents intercepting control statements)
* *AND* when the re-point `ALTER SESSION` pquery fails for any reason, the stub MUST catch the failure and MUST NOT raise an error that blocks or corrupts the user's statement
* *AND* on such a re-point failure the stub MUST still return the correctly transformed text for the current statement (fail-open applies to the re-point, never to the transformation)
* *AND* a per-rule dispatch failure MUST be caught (per-rule pcall fail-open) and MUST NOT block the statement, exactly as in MASTER

### Scenario: Atomicity — a reader never observes RESOLUTION referencing a dropped or missing profile hash

* *GIVEN* a refresh that both rebuilds RESOLUTION and reclaims (drops) one or more retired profiles
* *WHEN* the rebuild executes
* *THEN* every `DROP SCRIPT` and `PREPROC.PROFILES` delete from the reclaim phase MUST commit in the same single transaction as the RESOLUTION DELETE/INSERT and the profile generate/retire steps
* *AND* at no committed point MUST a RESOLUTION row reference a `profile_hash` whose `PREPROC_RT.P_<hash>` script has been dropped
* *AND* the fingerprint-first / single-commit atomicity invariant from ADR-010 MUST be preserved across the full generate → retire → reclaim → stamp sequence
