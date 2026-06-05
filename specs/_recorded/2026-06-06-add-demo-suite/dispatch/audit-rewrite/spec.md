# Feature: Audit Rewrite (REWRITE phase demo)

Demonstrates the framework's REWRITE phase — behaviour-modifying transformation of already-valid Exasol SQL — with a deliberately simple, visible change: the canonical reference module `DEMO_AUDIT_V1` injects a marker comment carrying the current user and a timestamp into the statement text. It exists so the demo walkthrough can show, at a glance, that a REWRITE rule fired and which user it fired for, while staying valid Exasol SQL that runs unchanged in meaning.

## Background

* The REWRITE phase operates on input that IS already valid Exasol SQL and modifies its behaviour or text. On REWRITE the dispatcher runs every matching rule (run-all text threading), not first-match-terminal.
* `DEMO_AUDIT_V1` is a demonstration module, not a security or audit-logging feature. The mission explicitly places per-statement audit logging out of scope (transaction-conflict hazard); this module only annotates the statement text — it writes nothing to any table.
* The injected marker is an SQL comment of the form `/* PREPROC_AUDIT user=<CURRENT_USER> */` appended to the statement, so the rewritten text remains a single valid Exasol statement that compiles and runs identically in result to the original. The user identity is obtained inside the module (the `CURRENT_USER` the framework runs under), not parsed from the statement.
* The module `PREPROC_RT.DEMO_AUDIT_V1` exports a `tag(sqltext)` function following the module authoring contract (top-level global function, no defensive pcall, single-statement string output, `_V1` versioned name, `DEMO_` prefix). It always applies (returns the annotated text) when dispatched.
* Because the change is an appended comment, it is observable in the demo by reading back the rewritten statement text, and it never alters query results — making it the safest possible REWRITE demonstration.

## Scenarios

### Scenario: The audit module appends a marker comment to a statement

* *GIVEN* MASTER is active and the current user has a resolved REWRITE rule pointing at `PREPROC_RT.DEMO_AUDIT_V1` / `tag`
* *WHEN* the user submits an ordinary Exasol statement
* *THEN* MASTER MUST rewrite the statement to include an SQL comment containing the literal `PREPROC_AUDIT`
* *AND* the comment MUST carry the connecting user's identity
* *AND* the rewritten statement MUST remain a single valid Exasol statement that the engine compiles and that returns the same result as the original

### Scenario: The audit marker carries the current user identity

* *GIVEN* MASTER is active and the current user has the `DEMO_AUDIT_V1` REWRITE rule resolved
* *WHEN* the user submits a statement that the engine returns a result for
* *THEN* the injected `PREPROC_AUDIT` comment MUST contain a `user=` field equal to the current database user
* *AND* the module MUST derive that identity itself rather than from the submitted statement text

### Scenario: Audit rewrite runs after EXPAND in the pipeline

* *GIVEN* MASTER is active and the current user has both an EXPAND rule that expands a custom command and the `DEMO_AUDIT_V1` REWRITE rule resolved
* *WHEN* the user submits the custom command
* *THEN* MASTER MUST apply the EXPAND expansion first and then inject the audit comment into the expanded statement
* *AND* the final statement MUST be the expanded Exasol SQL carrying the `PREPROC_AUDIT` comment

### Scenario: DEMO_AUDIT_V1 is deployed by install

* *GIVEN* a database where `preproc install` has completed
* *WHEN* the runtime schema `PREPROC_RT` is inspected
* *THEN* the script `PREPROC_RT.DEMO_AUDIT_V1` MUST exist and be executable by PUBLIC via the schema-level grant

### Scenario: DEMO_AUDIT_V1 passes luacheck

* *GIVEN* the module source at `sql/lua/demo_audit_v1.lua`
* *WHEN* luacheck is run against `sql/lua/`
* *THEN* luacheck MUST report zero warnings and zero errors for the module
