# Feature: MASTER Dispatcher Skeleton

Installs the MASTER preprocessor Lua script into `PREPROC_RT` as a safe pass-through skeleton. In this slice it reads the user's pipeline view and returns the statement text unchanged; real per-phase dispatch arrives in a later feature. The skeleton must already honour the non-negotiable safety rules — guard session/system control statements, fail open on any lookup failure, and never corrupt statement text — because once activated it intercepts every statement on the system.

## Background

* The MASTER script lives in `sql/lua/master.lua` and is deployed into `PREPROC_RT` via `CREATE OR REPLACE LUA SCRIPT`.
* Exasol Lua scripts run with invoker rights; MASTER reaches admin data only through the definer-rights view `PREPROC_RT.MY_PIPELINE`.
* The script passes luacheck with the project configuration and zero warnings.
* `sqltext` is obtained and returned via the preprocessor API; any `gsub`-style rewrite assigns the returned string to a variable before calling `setsqltext` (the second return value, the substitution count, is never passed onward).
* Installing this feature does not activate the preprocessor; activation is a separate DBA action.

## Scenarios

<!-- DELTA:NEW -->
### Scenario: MASTER script is installed into the runtime schema

* *GIVEN* schema `PREPROC_RT` exists
* *WHEN* the MASTER script is deployed
* *THEN* the system MUST create a Lua script named `MASTER` in `PREPROC_RT`
* *AND* the script SHALL be executable by PUBLIC via the schema-level grant
* *AND* re-deploying with `CREATE OR REPLACE` MUST succeed without error
<!-- /DELTA:NEW -->

<!-- DELTA:NEW -->
### Scenario: A normal statement passes through unchanged

* *GIVEN* MASTER is active as the system preprocessor
* *AND* the current user has a resolvable pipeline view row
* *WHEN* the user submits an ordinary SQL statement
* *THEN* MASTER MUST return the statement text byte-for-byte unchanged
* *AND* the statement SHALL compile and execute as if no preprocessor were present
<!-- /DELTA:NEW -->

<!-- DELTA:NEW -->
### Scenario: ALTER SESSION and ALTER SYSTEM statements are guarded

* *GIVEN* MASTER is active as the system preprocessor
* *WHEN* the user submits an `ALTER SESSION` or `ALTER SYSTEM` statement
* *THEN* MASTER MUST return early without rewriting the statement
* *AND* MASTER MUST NOT perform a pipeline lookup for that statement
* *AND* the statement text MUST be left unchanged
<!-- /DELTA:NEW -->

<!-- DELTA:NEW -->
### Scenario: Lookup failure fails open

* *GIVEN* MASTER is active as the system preprocessor
* *AND* the pipeline view query raises an error (thrown or pquery not-ok)
* *WHEN* the user submits an ordinary SQL statement
* *THEN* MASTER MUST return the original statement text unchanged (fail open)
* *AND* MASTER MUST NOT raise an error that blocks the statement
* *NOTE* an empty or all-NULL pipeline row is the normal no-rules case (MY_PIPELINE
  always returns at least one row via its LEFT JOIN); that row passes through
  and is not treated as a lookup failure
<!-- /DELTA:NEW -->

<!-- DELTA:NEW -->
### Scenario: MASTER passes luacheck

* *GIVEN* the MASTER script source at `sql/lua/master.lua`
* *WHEN* luacheck is run against `sql/lua/`
* *THEN* luacheck MUST report zero warnings and zero errors
<!-- /DELTA:NEW -->
