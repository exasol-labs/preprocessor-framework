# Feature: Trailing Comma Tolerance

Delivers opt-in trailing-comma removal as a pure-Lua TRANSLATE-phase preprocessor
module (`ERGONOMICS_TRAILING_COMMA_V1`). A statement that contains a trailing
comma — a comma immediately before a closing `)` (after whitespace and comments),
or immediately before a small set of list-terminating SQL keywords — has those
trailing commas removed before the engine compiles the statement. A statement
with no trailing commas is returned byte-for-byte unchanged. The module is
delivered as `examples/ergonomics_trailing_comma.sql` — a self-registering
example add-on, not part of the core install.

## Background

* Exasol does not accept trailing commas in any SQL list context; they are syntax
  errors. The rewrite is purely additive: a statement that is already valid
  Exasol SQL never contains a trailing comma in a list context and is never
  altered by this module.
* The module MUST implement a single-pass scanner/tokenizer that identifies and
  skips: single-quoted string literals (`'...'`, including `''` escape
  sequences), double-quoted identifiers (`"..."`), line comments (`-- … EOL`),
  and block comments (`/* … */`). A comma that falls inside any of those regions
  MUST NOT be removed.
* The TRANSLATE phase uses run-all text threading: every matching TRANSLATE rule
  runs and threads its returned string forward. The module MUST return a string
  on every execution path — the rewritten text when it acted, the original text
  byte-for-byte when it did not.
* Fail-closed contract: on any internal error or non-match, the module MUST
  return the original input unchanged. Corrupted SQL must never reach the engine
  masquerading as a successful translation. The module MUST NOT add a defensive
  `pcall`; errors must propagate so MASTER's dispatch-level `pcall` can skip the
  rule visibly.
* Recognition rule (see ADR in plan.md): two safe, conservative patterns are
  combined.
  - **Structural**: a comma followed (ignoring whitespace and comments) by a
    closing `)` is always a trailing comma, regardless of surrounding context.
    This is structurally unambiguous and safe to remove.
  - **Keyword-terminated**: a comma followed (ignoring whitespace and comments)
    by one of the recognised list-terminating keywords:
    `FROM`, `WHERE`, `GROUP`, `ORDER`, `HAVING`, `LIMIT`, `UNION`, `INTERSECT`,
    `EXCEPT`, `INTO`, `SET`, `ON`, `RETURNING`.
    The keyword match is case-insensitive and requires a word-boundary after the
    keyword (space, newline, comment, or end-of-statement). This covers the
    common `SELECT a, b, FROM t` pattern.
  - Commas outside these two patterns MUST NOT be removed; the module errs on
    the side of leaving ambiguous commas intact rather than removing a comma that
    is syntactically required.
* The module Lua source lives inline in `examples/ergonomics_trailing_comma.sql`
  (same convention as the other example files). A canonical-source copy is also
  kept at `sql/lua/ergonomics_trailing_comma_v1.lua` for luacheck runs via the
  existing CI pattern.
* Registration: the example file ends with
  `PREPROC ADD RULE FOR ROLE PREPROC_ADMIN PHASE TRANSLATE SCRIPT PREPROC_RT.ERGONOMICS_TRAILING_COMMA_V1 FUNCTION trailing_comma;`

## Scenarios

### Scenario: A trailing comma before `)` in a parenthesised list is removed

* *GIVEN* MASTER is active and the current user has a resolved TRANSLATE rule pointing at `PREPROC_RT.ERGONOMICS_TRAILING_COMMA_V1` / `trailing_comma`
* *WHEN* the user submits `SELECT * FROM t WHERE col IN (1, 2, 3,)`
* *THEN* the module MUST return `SELECT * FROM t WHERE col IN (1, 2, 3)`
* *AND* the rewritten statement MUST be valid Exasol SQL that the engine compiles and executes

### Scenario: A trailing comma before `)` in a VALUES list is removed

* *GIVEN* MASTER is active and the current user has a resolved TRANSLATE rule pointing at `PREPROC_RT.ERGONOMICS_TRAILING_COMMA_V1` / `trailing_comma`
* *WHEN* the user submits `INSERT INTO t VALUES (1, 2,)`
* *THEN* the module MUST return `INSERT INTO t VALUES (1, 2)`

### Scenario: A trailing comma before `)` in a function argument list is removed

* *GIVEN* MASTER is active and the current user has a resolved TRANSLATE rule pointing at `PREPROC_RT.ERGONOMICS_TRAILING_COMMA_V1` / `trailing_comma`
* *WHEN* the user submits `SELECT coalesce(a, b,) FROM t`
* *THEN* the module MUST return `SELECT coalesce(a, b) FROM t`

### Scenario: A trailing comma before `)` with intervening whitespace is removed

* *GIVEN* MASTER is active and the current user has a resolved TRANSLATE rule pointing at `PREPROC_RT.ERGONOMICS_TRAILING_COMMA_V1` / `trailing_comma`
* *WHEN* the user submits `SELECT * FROM t WHERE col IN (1, 2, 3,  )`
* *THEN* the module MUST return `SELECT * FROM t WHERE col IN (1, 2, 3  )`
* *AND* whitespace between the removed comma and `)` MUST be preserved

### Scenario: A trailing comma before `)` with an intervening comment is removed

* *GIVEN* MASTER is active and the current user has a resolved TRANSLATE rule pointing at `PREPROC_RT.ERGONOMICS_TRAILING_COMMA_V1` / `trailing_comma`
* *WHEN* the user submits `SELECT * FROM t WHERE col IN (1, 2 /* last */ ,)`
* *THEN* the module MUST return `SELECT * FROM t WHERE col IN (1, 2 /* last */ )`

### Scenario: A trailing comma before FROM in a SELECT list is removed

* *GIVEN* MASTER is active and the current user has a resolved TRANSLATE rule pointing at `PREPROC_RT.ERGONOMICS_TRAILING_COMMA_V1` / `trailing_comma`
* *WHEN* the user submits `SELECT a, b, FROM t`
* *THEN* the module MUST return `SELECT a, b FROM t`
* *AND* the rewritten statement MUST be valid Exasol SQL that the engine compiles and executes

### Scenario: A trailing comma before GROUP BY is removed

* *GIVEN* MASTER is active and the current user has a resolved TRANSLATE rule pointing at `PREPROC_RT.ERGONOMICS_TRAILING_COMMA_V1` / `trailing_comma`
* *WHEN* the user submits `SELECT a, b, FROM t GROUP BY a, b,`
* *THEN* the module MUST return `SELECT a, b FROM t GROUP BY a, b`

### Scenario: A statement with no trailing commas is returned byte-for-byte unchanged

* *GIVEN* MASTER is active and the current user has a resolved TRANSLATE rule pointing at `PREPROC_RT.ERGONOMICS_TRAILING_COMMA_V1` / `trailing_comma`
* *WHEN* the user submits a valid Exasol statement that contains no trailing commas
* *THEN* the module MUST return the statement text byte-for-byte unchanged
* *AND* the statement MUST compile and execute as if no TRANSLATE rule were present

### Scenario: A comma inside a single-quoted string literal is NOT removed

* *GIVEN* MASTER is active and the current user has a resolved TRANSLATE rule pointing at `PREPROC_RT.ERGONOMICS_TRAILING_COMMA_V1` / `trailing_comma`
* *WHEN* the user submits `SELECT 'a,b,c,' AS x FROM t`
* *THEN* the module MUST return the statement text byte-for-byte unchanged
* *AND* the commas inside the string literal MUST NOT be altered

### Scenario: A comma inside a double-quoted identifier is NOT removed

* *GIVEN* MASTER is active and the current user has a resolved TRANSLATE rule pointing at `PREPROC_RT.ERGONOMICS_TRAILING_COMMA_V1` / `trailing_comma`
* *WHEN* the user submits `SELECT "col,name" FROM t`
* *THEN* the module MUST return the statement text byte-for-byte unchanged

### Scenario: A comma inside a line comment is NOT removed

* *GIVEN* MASTER is active and the current user has a resolved TRANSLATE rule pointing at `PREPROC_RT.ERGONOMICS_TRAILING_COMMA_V1` / `trailing_comma`
* *WHEN* the user submits `SELECT a, b FROM t -- trailing comma not here,`
* *THEN* the module MUST return the statement text byte-for-byte unchanged
* *AND* the comma inside the line comment MUST NOT be altered

### Scenario: A comma inside a block comment is NOT removed

* *GIVEN* MASTER is active and the current user has a resolved TRANSLATE rule pointing at `PREPROC_RT.ERGONOMICS_TRAILING_COMMA_V1` / `trailing_comma`
* *WHEN* the user submits `SELECT a FROM t /* col, b, */ WHERE x = 1`
* *THEN* the module MUST return the statement text byte-for-byte unchanged

### Scenario: A string literal with `''` escapes is scanned correctly before a real trailing comma

* *GIVEN* MASTER is active and the current user has a resolved TRANSLATE rule pointing at `PREPROC_RT.ERGONOMICS_TRAILING_COMMA_V1` / `trailing_comma`
* *WHEN* the user submits `SELECT 'it''s,ok' AS x, col FROM t WHERE col IN (1, 2,)`
* *THEN* the commas inside the `''`-escaped string literal MUST NOT be altered
* *AND* the trailing comma before `)` at the end of the IN list MUST be removed
* *AND* the result MUST be `SELECT 'it''s,ok' AS x, col FROM t WHERE col IN (1, 2)`

### Scenario: Multiple trailing commas in one statement are all removed

* *GIVEN* MASTER is active and the current user has a resolved TRANSLATE rule pointing at `PREPROC_RT.ERGONOMICS_TRAILING_COMMA_V1` / `trailing_comma`
* *WHEN* the user submits `SELECT a, b, FROM t WHERE col IN (1, 2,) AND y IN (3,)`
* *THEN* ALL trailing commas in the statement MUST be removed in a single pass
* *AND* the result MUST be `SELECT a, b FROM t WHERE col IN (1, 2) AND y IN (3)`

### Scenario: Module-level fail-closed — internal error returns original text

* *GIVEN* MASTER is active and the current user has a resolved TRANSLATE rule pointing at `PREPROC_RT.ERGONOMICS_TRAILING_COMMA_V1` / `trailing_comma`
* *WHEN* an internal scanner error occurs during processing
* *THEN* the module MUST return the original statement text unchanged
* *AND* MASTER MUST NOT propagate corrupted SQL to the engine

### Scenario: ERGONOMICS_TRAILING_COMMA_V1 is deployed by the example file (not by core install)

* *GIVEN* the framework is installed via `preproc install` and the example file `examples/ergonomics_trailing_comma.sql` has NOT been applied
* *WHEN* the runtime schema `PREPROC_RT` is inspected
* *THEN* the script `PREPROC_RT.ERGONOMICS_TRAILING_COMMA_V1` MUST NOT be present
* *AND* it MUST NOT appear in `PREPROC.CONFIG`

### Scenario: ERGONOMICS_TRAILING_COMMA_V1 passes luacheck

* *GIVEN* the module source at `sql/lua/ergonomics_trailing_comma_v1.lua`
* *WHEN* luacheck is run against `sql/lua/`
* *THEN* luacheck MUST report zero warnings and zero errors for the module
