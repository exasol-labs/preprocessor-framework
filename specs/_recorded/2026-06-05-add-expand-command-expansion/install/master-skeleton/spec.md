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
### Scenario: Dispatch honours client_pattern matching

* *GIVEN* MASTER is active and the current user has a resolved rule whose `client_pattern` is set
* *WHEN* the connecting client identity does not match that rule's `client_pattern` as a **literal substring**
* *THEN* MASTER MUST NOT dispatch that rule
* *AND* a rule whose `client_pattern` is NULL or is a literal substring of the client identity MUST be dispatched
* *AND* MASTER MUST NOT interpret `client_pattern` as a Lua regular expression pattern
<!-- /DELTA:CHANGED -->

<!-- DELTA:NEW -->
### Scenario: EXPAND phase uses first-match-terminal dispatch within the phase

* *GIVEN* MASTER is active as the system preprocessor
* *AND* the current user has multiple EXPAND rules in ordinal order
* *WHEN* a statement is submitted and an EXPAND module returns a non-nil expansion string
* *THEN* MASTER MUST stop trying further EXPAND rules for that statement
* *AND* MASTER MUST pass the expansion to the REWRITE phase
* *AND* MASTER MUST NOT apply first-match-terminal semantics to TRANSLATE or REWRITE phases
<!-- /DELTA:NEW -->

<!-- DELTA:NEW -->
### Scenario: An EXPAND module returning nil is not terminal

* *GIVEN* MASTER is active and the current user has EXPAND rules at ordinals 1 and 2
* *AND* the ordinal-1 EXPAND module returns `nil` for the submitted statement
* *WHEN* the user submits that statement
* *THEN* MASTER MUST continue and dispatch the ordinal-2 EXPAND rule
<!-- /DELTA:NEW -->
