# Feature: MASTER Dispatcher Skeleton

Installs the MASTER preprocessor Lua script into `PREPROC_RT`. This slice upgrades it from a pass-through skeleton to a real dispatcher: it reads the current user's resolved pipeline from `PREPROC_RT.MY_PIPELINE` and applies each rule's module function in fixed phase order, threading the statement text through the pipeline. It continues to honour the non-negotiable safety rules — guard session/system control statements, fail open on any lookup failure, skip individual failing rules rather than block, and never corrupt statement text — because once activated it intercepts every statement on the system.

## Background

* The MASTER script lives in `sql/lua/master.lua` and is deployed into `PREPROC_RT` via `CREATE OR REPLACE LUA SCRIPT`.
* Exasol Lua scripts run with invoker rights; MASTER reaches admin data only through the definer-rights view `PREPROC_RT.MY_PIPELINE`, which is filtered to `CURRENT_USER` and ordered by `phase, ordinal`.
* Phases dispatch in the fixed order `TRANSLATE` then `EXPAND` then `REWRITE`; within a phase, rules dispatch in `ordinal` order (pure `rule_id` ascending, scope type ignored). `EXPAND` precedes `REWRITE` so behaviour-modifying rules always operate on valid, fully-expanded SQL. The text output of one rule is the input to the next.
* `MY_PIPELINE` always returns at least one row via its LEFT JOIN; an all-NULL phase/script/function row is the no-rules sentinel and is never dispatched.
* The script passes luacheck with the project configuration and zero warnings. Any `gsub`-style rewrite assigns the returned string to a variable before calling `setsqltext`.

## Scenarios

<!-- DELTA:CHANGED -->
### Scenario: A normal statement passes through unchanged

* *GIVEN* MASTER is active as the system preprocessor
* *AND* the current user has no rules in `PREPROC.RESOLUTION` (the all-NULL `MY_PIPELINE` sentinel row)
* *WHEN* the user submits an ordinary SQL statement
* *THEN* MASTER MUST return the statement text byte-for-byte unchanged
* *AND* MASTER MUST NOT dispatch to any module function
* *AND* the statement SHALL compile and execute as if no preprocessor were present
<!-- /DELTA:CHANGED -->

<!-- DELTA:NEW -->
### Scenario: MASTER dispatches the resolved pipeline in phase order

* *GIVEN* MASTER is active as the system preprocessor
* *AND* the current user has resolved rules in `PREPROC.RESOLUTION` spanning phases `TRANSLATE`, `EXPAND`, and `REWRITE`
* *WHEN* the user submits a statement that those rules transform
* *THEN* MASTER MUST read the rows from `PREPROC_RT.MY_PIPELINE` and dispatch to each rule's `script_name.function_name`
* *AND* MASTER MUST apply the phases in fixed order `TRANSLATE` then `EXPAND` then `REWRITE`, and within a phase in the deterministic `ordinal` order carried by the rows
* *AND* MASTER MUST pass the transformed text of one rule as the input to the next
* *AND* MASTER MUST write the final transformed text via `setsqltext`
<!-- /DELTA:NEW -->

<!-- DELTA:NEW -->
### Scenario: Dispatch honours client_pattern matching

* *GIVEN* MASTER is active and the current user has a resolved rule whose `client_pattern` is set
* *WHEN* the connecting client identity does not match that rule's `client_pattern`
* *THEN* MASTER MUST NOT dispatch that rule
* *AND* a rule whose `client_pattern` is NULL or matches the client MUST be dispatched
<!-- /DELTA:NEW -->

<!-- DELTA:NEW -->
### Scenario: A failing module function is skipped, fail-open

* *GIVEN* MASTER is active with a resolved multi-rule pipeline for the current user
* *AND* one non-`TRANSLATE` module function raises an error when dispatched
* *WHEN* the user submits a statement
* *THEN* MASTER MUST skip the failing rule and continue with the remaining rules (fail-open)
* *AND* MASTER MUST NOT raise an error that blocks the statement
* *AND* the text produced by the successful rules MUST still be applied
<!-- /DELTA:NEW -->

<!-- DELTA:CHANGED -->
### Scenario: ALTER SESSION and ALTER SYSTEM statements are guarded

* *GIVEN* MASTER is active as the system preprocessor
* *WHEN* the user submits an `ALTER SESSION` or `ALTER SYSTEM` statement
* *THEN* MASTER MUST return early without rewriting the statement
* *AND* MASTER MUST NOT perform a pipeline lookup or dispatch for that statement
* *AND* the statement text MUST be left unchanged
<!-- /DELTA:CHANGED -->

<!-- DELTA:CHANGED -->
### Scenario: Lookup failure fails open

* *GIVEN* MASTER is active as the system preprocessor
* *AND* the pipeline view query raises an error (thrown or pquery not-ok)
* *WHEN* the user submits an ordinary SQL statement
* *THEN* MASTER MUST return the original statement text unchanged (fail open)
* *AND* MASTER MUST NOT dispatch any rule
* *AND* MASTER MUST NOT raise an error that blocks the statement
* *NOTE* an empty or all-NULL pipeline row is the normal no-rules case (MY_PIPELINE
  always returns at least one row via its LEFT JOIN); that row passes through
  and is not treated as a lookup failure
<!-- /DELTA:CHANGED -->
