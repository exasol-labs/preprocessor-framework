# Feature: MASTER Dispatcher Skeleton

Installs the MASTER preprocessor Lua script into `PREPROC_RT`. This delta extends the dispatcher to perform a one-time session self-optimization swap in convenience mode after dispatching the first statement.

## Background

MASTER is active as the SQL preprocessor for the session. All safety invariants from ADR-004 apply and MUST NOT be weakened by the swap addition.

## Scenarios

<!-- DELTA:CHANGED -->

### Scenario: A normal statement passes through unchanged

* *GIVEN* MASTER is active as the system preprocessor
* *AND* the current user has no rules in `PREPROC_RT.MY_PIPELINE` (the all-NULL phase sentinel row)
* *WHEN* the user submits an ordinary SQL statement
* *THEN* MASTER MUST return the statement text byte-for-byte unchanged
* *AND* MASTER MUST NOT dispatch to any module function
* *AND* the statement SHALL compile and execute as if no preprocessor were present
* *AND* if `mode` is `convenience` and `self_disable_allowed` is `TRUE`, MASTER MUST attempt to self-disable via ALTER SESSION before returning

<!-- /DELTA:CHANGED -->

<!-- DELTA:NEW -->

### Scenario: MASTER reads mode and profile_hash from MY_PIPELINE

* *GIVEN* MASTER is active as the system preprocessor
* *WHEN* MASTER performs its single MY_PIPELINE lookup on any statement
* *THEN* the lookup MUST retrieve `mode`, `self_disable_allowed`, and `profile_hash` columns in addition to the pipeline rule columns
* *AND* MASTER MUST NOT issue any additional pquery to read mode or profile_hash from admin tables
* *AND* MASTER MUST determine swap or self-disable eligibility entirely from the data returned by that single lookup

### Scenario: MASTER swap logic passes luacheck

* *GIVEN* the MASTER script source at `sql/lua/master.lua` with the swap and self-disable additions
* *WHEN* luacheck is run against the file
* *THEN* luacheck MUST report zero warnings and zero errors

### Scenario: MASTER is the single canonical inline dispatcher

* *GIVEN* the resolve→dispatch→swap logic lives INLINE at top-level script scope in `sql/lua/master.lua` (deployed verbatim as `PREPROC_RT.MASTER`)
* *AND* an `import("PREPROC_RT.MASTER_CORE")` wrapper is technically infeasible here — in the Exasol preprocessor context the per-rule dynamic `import()` works ONLY at top-level script scope, never from inside a function of an imported module, so a thin import-wrapper silently fail-opens every dispatch
* *WHEN* `PREPROC_RT.MASTER` runs for a statement
* *THEN* the control-statement guard, the single MY_PIPELINE lookup, the per-rule pcall fail-open dispatch loop, the convenience-mode swap/self-disable step, and setsqltext-on-change MUST all be implemented inline in `master.lua` so there is exactly one source of truth for the dispatch+swap contract
* *AND* `master.lua` MUST NOT import a `PREPROC_RT.MASTER_CORE` module (no such module exists)

### Scenario: The retirement stub is sourced from MASTER's own catalog body, so stub == MASTER by construction

* *GIVEN* `sql/lua/master.lua` (deployed as `PREPROC_RT.MASTER`) is the single source of truth for resolve→dispatch→swap
* *WHEN* the refresh path generates a retirement-stub body for an orphaned `PREPROC_RT.P_<hash>`
* *THEN* the refresh path MUST read `PREPROC_RT.MASTER`'s own `SCRIPT_TEXT` from `EXA_ALL_SCRIPTS`, strip the leading `CREATE ... AS` header line, and deploy the remaining body verbatim as the stub, so the stub is byte-for-byte MASTER's body
* *AND* the stub MUST NOT contain an independently-baked copy of the dispatch or swap logic (no `RETIREMENT_STUB_BODY` constant, no `MASTER_CORE` import wrapper)
* *AND* a change to the dispatch+swap contract in `master.lua` MUST take effect for every retirement stub created or refreshed afterward without separate edits

<!-- /DELTA:NEW -->
