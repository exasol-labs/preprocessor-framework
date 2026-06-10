# Feature: CAST Shorthand (`expr::type` → `CAST(expr AS type)`)

Delivers a PostgreSQL-style `expr::type` cast shorthand as an opt-in, pure-Lua
TRANSLATE-phase preprocessor module (`ERGONOMICS_CAST_V1`). A statement
containing `::` has every qualifying occurrence rewritten to
`CAST(expr AS type)` before the engine compiles it. A statement with no `::`
tokens, or where every `::` appears inside a string literal, quoted identifier,
or comment, is returned byte-for-byte unchanged. The module is delivered as
`examples/ergonomics_cast.sql` — a self-registering example add-on, not part of
the core install.

## Background

* Exasol does not natively recognise `::` as a cast operator; any statement
  containing `::` outside a string or comment is a hard syntax error. The
  rewrite is therefore purely additive: a statement that is already valid Exasol
  SQL never contains unquoted `::` and is never altered.
* The module MUST implement a single-pass scanner/tokenizer that identifies and
  skips: single-quoted string literals (`'...'`, including `''` escape
  sequences), double-quoted identifiers (`"..."`), line comments (`-- … EOL`),
  and block comments (`/* … */`). A `::` token that falls inside any of those
  regions MUST NOT be rewritten.
* The TRANSLATE phase uses run-all text threading: every matching TRANSLATE rule
  runs and threads its returned string forward. The module MUST return a string
  on every execution path — the rewritten text when it acted, the original text
  byte-for-byte when it did not.
* Fail-closed contract: on any internal error or non-match, the module MUST
  return the original input unchanged. Corrupted SQL must never reach the engine
  masquerading as a successful translation. The module MUST NOT add a defensive
  `pcall`; errors must propagate so MASTER's dispatch-level `pcall` can skip the
  rule visibly.
* The supported left-operand subset (see ADR in plan.md) covers the common
  cases: unqualified and schema-qualified identifiers (`a`, `schema.table.col`),
  parenthesised expressions `(...)`, function calls `f(...)`, string and numeric
  literals, and chained cast results. The module scans backwards from each `::`
  token to find the operand start, respecting balanced parentheses and skipping
  embedded quote/comment regions.
* The right-side type name is the token immediately following `::` plus an
  optional balanced `(...)` parameter list (for parameterised types such as
  `varchar(10)` or `decimal(10,2)`).
* Chained casts are rewritten inside-out in a single left-to-right pass:
  `a::int::text` → `CAST(CAST(a AS int) AS text)`.
* The module Lua source lives inline in `examples/ergonomics_cast.sql` (same
  convention as `demo_ping.sql` and `demo_translate.sql`). No companion `.lua`
  file is added to `sql/lua/`. The file also contains a canonical-source copy at
  `sql/lua/ergonomics_cast_v1.lua` for luacheck runs via the existing CI
  pattern.
* Registration: the example file ends with
  `PREPROC ADD RULE FOR ROLE PREPROC_ADMIN PHASE TRANSLATE SCRIPT PREPROC_RT.ERGONOMICS_CAST_V1 FUNCTION cast_shorthand;`

## Scenarios

### Scenario: A simple identifier cast is rewritten to CAST(expr AS type)

* *GIVEN* MASTER is active and the current user has a resolved TRANSLATE rule pointing at `PREPROC_RT.ERGONOMICS_CAST_V1` / `cast_shorthand`
* *WHEN* the user submits `SELECT col::integer FROM t`
* *THEN* the module MUST return `SELECT CAST(col AS integer) FROM t`
* *AND* the rewritten statement MUST be valid Exasol SQL that the engine compiles and executes

### Scenario: A schema-qualified identifier cast is rewritten correctly

* *GIVEN* MASTER is active and the current user has a resolved TRANSLATE rule pointing at `PREPROC_RT.ERGONOMICS_CAST_V1` / `cast_shorthand`
* *WHEN* the user submits `SELECT s.t.col::varchar(20) FROM s.t`
* *THEN* the module MUST return `SELECT CAST(s.t.col AS varchar(20)) FROM s.t`

### Scenario: A parenthesised expression cast is rewritten correctly

* *GIVEN* MASTER is active and the current user has a resolved TRANSLATE rule pointing at `PREPROC_RT.ERGONOMICS_CAST_V1` / `cast_shorthand`
* *WHEN* the user submits `SELECT (a + b)::decimal(10,2) FROM t`
* *THEN* the module MUST return `SELECT CAST((a + b) AS decimal(10,2)) FROM t`

### Scenario: A function-call operand is rewritten correctly

* *GIVEN* MASTER is active and the current user has a resolved TRANSLATE rule pointing at `PREPROC_RT.ERGONOMICS_CAST_V1` / `cast_shorthand`
* *WHEN* the user submits `SELECT trim(col)::varchar(50) FROM t`
* *THEN* the module MUST return `SELECT CAST(trim(col) AS varchar(50)) FROM t`

### Scenario: A chained cast is rewritten inside-out

* *GIVEN* MASTER is active and the current user has a resolved TRANSLATE rule pointing at `PREPROC_RT.ERGONOMICS_CAST_V1` / `cast_shorthand`
* *WHEN* the user submits `SELECT a::int::text FROM t`
* *THEN* the module MUST return `SELECT CAST(CAST(a AS int) AS text) FROM t`

### Scenario: A parameterised type is captured including its argument list

* *GIVEN* MASTER is active and the current user has a resolved TRANSLATE rule pointing at `PREPROC_RT.ERGONOMICS_CAST_V1` / `cast_shorthand`
* *WHEN* the user submits `SELECT x::decimal(10,2) FROM t`
* *THEN* the module MUST return `SELECT CAST(x AS decimal(10,2)) FROM t`
* *AND* the `(10,2)` parameter list MUST be preserved verbatim in the output

### Scenario: A statement with no `::` tokens is returned byte-for-byte unchanged

* *GIVEN* MASTER is active and the current user has a resolved TRANSLATE rule pointing at `PREPROC_RT.ERGONOMICS_CAST_V1` / `cast_shorthand`
* *WHEN* the user submits a valid Exasol statement that contains no `::` tokens
* *THEN* the module MUST return the statement text byte-for-byte unchanged
* *AND* the statement MUST compile and execute as if no TRANSLATE rule were present

### Scenario: A `::` inside a single-quoted string literal is NOT rewritten

* *GIVEN* MASTER is active and the current user has a resolved TRANSLATE rule pointing at `PREPROC_RT.ERGONOMICS_CAST_V1` / `cast_shorthand`
* *WHEN* the user submits `SELECT '::not_a_cast' AS x FROM dual`
* *THEN* the module MUST return the statement text byte-for-byte unchanged
* *AND* the `::` inside the string literal MUST NOT be altered

### Scenario: A `::` inside a double-quoted identifier is NOT rewritten

* *GIVEN* MASTER is active and the current user has a resolved TRANSLATE rule pointing at `PREPROC_RT.ERGONOMICS_CAST_V1` / `cast_shorthand`
* *WHEN* the user submits `SELECT "col::name" FROM t`
* *THEN* the module MUST return the statement text byte-for-byte unchanged
* *AND* the `::` inside the quoted identifier MUST NOT be altered

### Scenario: A `::` inside a line comment is NOT rewritten

* *GIVEN* MASTER is active and the current user has a resolved TRANSLATE rule pointing at `PREPROC_RT.ERGONOMICS_CAST_V1` / `cast_shorthand`
* *WHEN* the user submits `SELECT col FROM t -- col::integer is just a comment`
* *THEN* the module MUST return the statement text byte-for-byte unchanged
* *AND* the `::` inside the comment MUST NOT be altered

### Scenario: A `::` inside a block comment is NOT rewritten

* *GIVEN* MASTER is active and the current user has a resolved TRANSLATE rule pointing at `PREPROC_RT.ERGONOMICS_CAST_V1` / `cast_shorthand`
* *WHEN* the user submits `SELECT /* a::int placeholder */ col FROM t`
* *THEN* the module MUST return the statement text byte-for-byte unchanged
* *AND* the `::` inside the block comment MUST NOT be altered

### Scenario: A string literal containing `''` escape sequences is scanned correctly

* *GIVEN* MASTER is active and the current user has a resolved TRANSLATE rule pointing at `PREPROC_RT.ERGONOMICS_CAST_V1` / `cast_shorthand`
* *WHEN* the user submits `SELECT 'it''s::fine' AS x, col::integer FROM t`
* *THEN* the module MUST rewrite only the trailing `col::integer` outside the string
* *AND* the `::fine` inside the `''`-escaped string literal MUST NOT be altered
* *AND* the result MUST be `SELECT 'it''s::fine' AS x, CAST(col AS integer) FROM t`

### Scenario: Module-level fail-closed — unrecognised left operand returns original text

* *GIVEN* MASTER is active and the current user has a resolved TRANSLATE rule pointing at `PREPROC_RT.ERGONOMICS_CAST_V1` / `cast_shorthand`
* *WHEN* the user submits a statement whose `::` left operand cannot be parsed by the supported-operand scanner
* *THEN* the module MUST return the original statement text unchanged
* *AND* MASTER MUST NOT propagate corrupted SQL to the engine

### Scenario: ERGONOMICS_CAST_V1 is deployed by the example file (not by core install)

* *GIVEN* the framework is installed via `preproc install` and the example file `examples/ergonomics_cast.sql` has NOT been applied
* *WHEN* the runtime schema `PREPROC_RT` is inspected
* *THEN* the script `PREPROC_RT.ERGONOMICS_CAST_V1` MUST NOT be present
* *AND* it MUST NOT appear in `PREPROC.CONFIG`

### Scenario: ERGONOMICS_CAST_V1 passes luacheck

* *GIVEN* the module source at `sql/lua/ergonomics_cast_v1.lua`
* *WHEN* luacheck is run against `sql/lua/`
* *THEN* luacheck MUST report zero warnings and zero errors for the module
