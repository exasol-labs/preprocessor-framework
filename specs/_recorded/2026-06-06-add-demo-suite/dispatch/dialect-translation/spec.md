# Feature: Dialect Translation (TRANSLATE phase demo)

Demonstrates the framework's TRANSLATE phase: a foreign-dialect SQL statement, tagged with an explicit dialect marker, is transpiled to valid Exasol SQL by an AST-based `sqlglot` Python UDF before the engine compiles it. The capability is delivered as the canonical TRANSLATE-phase reference module `DEMO_TRANSLATE_V1` plus its companion scalar UDF `DEMO_SQLGLOT_TRANSPILE_V1`. It exists to make the most compelling part of the framework visible: a statement that is a hard syntax error in Exasol (`SELECT TOP 5 …`) is silently rewritten into the equivalent Exasol statement (`SELECT … LIMIT 5`) for users whose resolved pipeline carries the rule.

## Background

* The TRANSLATE phase is fail-closed by contract: an un-applied translation must never let corrupted or wrong-dialect text reach the engine masquerading as a successful translation. The framework's MASTER dispatch is fail-open at the rule level (a throwing or missing module skips the rule, leaving text unchanged); the module itself MUST additionally guarantee it never returns corrupted statement text.
* The trigger is an explicit dialect marker comment at the very start of the statement: `--!dialect:<name>` (for example `--!dialect:tsql`). A statement without the marker is not this module's concern and the module returns the text byte-for-byte unchanged so the dispatcher threads it on untouched. Always-transpile was rejected: it is slow (~133 ms/statement, no warm UDF VM reuse) and dangerous (cross-dialect quote-escaping hazards on already-valid Exasol SQL).
* Translation is performed by a Python 3 scalar UDF `PREPROC_RT.DEMO_SQLGLOT_TRANSPILE_V1(dialect VARCHAR, text VARCHAR) RETURNS VARCHAR` that calls `sqlglot.transpile(text, read=dialect, write='exasol')` using the `sqlglot` (≥27.x) shipped in Exasol's default Python3 UDF container — no BucketFS vendoring. On any parse/transpile failure the UDF returns the sentinel `ERR:<message>` rather than raising, so the calling module can distinguish a real translation from a failure.
* The module `PREPROC_RT.DEMO_TRANSLATE_V1` exports a `translate(sqltext)` function following the module authoring contract (top-level global function, no defensive pcall, single-statement string output, `_V1` versioned name, `DEMO_` prefix). On the TRANSLATE phase the dispatcher runs every matching rule (run-all text threading), not first-match-terminal.
* Verified empirically against Exasol 2025.2.x with `sqlglot` 27.17.0: T-SQL `SELECT TOP 5 col FROM t` → `SELECT col FROM t LIMIT 5`; T-SQL `GETDATE()` → `CURRENT_TIMESTAMP()`; MySQL backtick quoting → double-quoted identifiers. `SELECT TOP 5 …` is a hard syntax error in Exasol, which is what makes it a compelling demo.
* The cost (~133–151 ms/statement, dominated by UDF VM startup with no reuse across statements) is acceptable for a demo and MUST be documented; production rules SHOULD be scoped narrowly via `client_pattern`.

## Scenarios

### Scenario: The transpile UDF converts a tagged foreign-dialect statement to Exasol SQL

* *GIVEN* the UDF `PREPROC_RT.DEMO_SQLGLOT_TRANSPILE_V1` is deployed
* *WHEN* it is called with dialect `tsql` and text `SELECT TOP 5 col FROM t`
* *THEN* it MUST return a string equal to the `sqlglot` `tsql`→`exasol` transpilation `SELECT col FROM t LIMIT 5`
* *AND* the returned string MUST NOT carry the `ERR:` sentinel prefix

### Scenario: The transpile UDF returns the ERR sentinel on unparseable input

* *GIVEN* the UDF `PREPROC_RT.DEMO_SQLGLOT_TRANSPILE_V1` is deployed
* *WHEN* it is called with dialect `tsql` and text that `sqlglot` cannot parse
* *THEN* it MUST return a string beginning with the `ERR:` sentinel prefix
* *AND* it MUST NOT raise an error that fails the calling statement

### Scenario: A marked statement is translated to runnable Exasol SQL through the pipeline

* *GIVEN* MASTER is active and the current user has a resolved TRANSLATE rule pointing at `PREPROC_RT.DEMO_TRANSLATE_V1` / `translate`
* *AND* the UDF `PREPROC_RT.DEMO_SQLGLOT_TRANSPILE_V1` is deployed
* *WHEN* the user submits a statement beginning with `--!dialect:tsql` followed by `SELECT TOP 5 col FROM t`
* *THEN* MASTER MUST rewrite the statement to the Exasol form using `LIMIT` rather than `TOP`
* *AND* the dialect marker comment MUST NOT remain in the rewritten statement
* *AND* the rewritten statement MUST be valid Exasol SQL that the engine compiles

### Scenario: An unmarked statement passes through TRANSLATE unchanged

* *GIVEN* MASTER is active and the current user has the `DEMO_TRANSLATE_V1` TRANSLATE rule resolved
* *WHEN* the user submits an ordinary Exasol statement with no `--!dialect:` marker
* *THEN* the `translate` function MUST return the statement text byte-for-byte unchanged
* *AND* MASTER MUST NOT invoke the transpile UDF for that statement
* *AND* the statement SHALL compile and execute as if no TRANSLATE rule were present

### Scenario: A failed transpilation fails closed and does not corrupt the statement

* *GIVEN* MASTER is active and the current user has the `DEMO_TRANSLATE_V1` TRANSLATE rule resolved
* *AND* a statement is marked `--!dialect:tsql` but its body cannot be transpiled (UDF returns the `ERR:` sentinel)
* *WHEN* the user submits that statement
* *THEN* the `translate` function MUST NOT thread the `ERR:` sentinel string forward as statement text
* *AND* the module MUST return the original statement text unchanged (fail closed)
* *AND* MASTER MUST NOT corrupt or partially rewrite the statement text

### Scenario: DEMO_TRANSLATE_V1 and the transpile UDF are deployed by install

* *GIVEN* a database where `preproc install` has completed
* *WHEN* the runtime schema `PREPROC_RT` is inspected
* *THEN* the script `PREPROC_RT.DEMO_TRANSLATE_V1` MUST exist and be executable by PUBLIC via the schema-level grant
* *AND* the scalar UDF `PREPROC_RT.DEMO_SQLGLOT_TRANSPILE_V1` MUST exist and be executable by PUBLIC via the schema-level grant

### Scenario: DEMO_TRANSLATE_V1 passes luacheck

* *GIVEN* the module source at `sql/lua/demo_translate_v1.lua`
* *WHEN* luacheck is run against `sql/lua/`
* *THEN* luacheck MUST report zero warnings and zero errors for the module
