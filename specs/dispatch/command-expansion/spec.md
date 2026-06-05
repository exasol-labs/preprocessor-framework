# Feature: EXPAND Command Expansion

Enables whole-statement custom commands in the EXPAND phase: a non-standard SQL statement is matched against registered command modules in ordinal order; the first module that recognises the statement expands it to runnable Exasol SQL (first-match-terminal within EXPAND only), while REWRITE still runs afterward. Provides the module-authoring contract, the EXPAND dispatch return protocol, and a permanent demo module (`DEMO_PING_V1`) that proves the mechanism end-to-end.

## Background

* The EXPAND phase runs AFTER TRANSLATE and BEFORE REWRITE. Its input is assumed to be NOT valid Exasol SQL; its output must be valid Exasol SQL so that REWRITE operates on a sound foundation (ADR-011).
* Fail-open is inherently safe for EXPAND: a statement that is not expanded dies loudly in the Exasol engine with a syntax error — no silent data access.
* EXPAND uses first-match-terminal semantics: the dispatch loop tries EXPAND-phase rules in ascending `ordinal` order and stops as soon as one module signals a match by returning a non-nil string. Remaining EXPAND rules are skipped. REWRITE rules always run after EXPAND completes.
* The module-authoring contract for EXPAND functions: return `nil` to signal "not my command, try the next rule"; return a non-nil string to signal "I recognised this command — here is the expansion" (match + terminal). A pcall failure on a module is fail-open: the failing rule is skipped and the next EXPAND rule is tried.
* The EXPAND return contract differs from TRANSLATE and REWRITE: those phases use plain text threading (returning the same or modified text, no nil); EXPAND modules MUST return `nil` for non-match precisely to enable first-match-terminal.
* Command modules are Lua scripts in `PREPROC_RT`, loaded via `import(name, alias)`. Schema-level PUBLIC grants on `PREPROC_RT` cover all deployed modules automatically (ADR-002).
* `client_pattern` matching uses plain literal substring search (`string.find(client, pattern, 1, true)`) — not Lua pattern matching.
* Modules are versioned objects (`NAME_V1`, `NAME_V2`, …); `CREATE OR REPLACE` is never used on a live module — a new version is deployed alongside and rules are migrated.

## Scenarios

### Scenario: EXPAND dispatch stops at the first matching module (first-match-terminal)

* *GIVEN* MASTER is active as the system preprocessor
* *AND* the current user has two EXPAND rules: `rule_A` at ordinal 1 and `rule_B` at ordinal 2
* *AND* the submitted statement is recognised by `rule_A` (its function returns a non-nil expansion string)
* *WHEN* the user submits that statement
* *THEN* MASTER MUST dispatch only `rule_A` for the EXPAND phase
* *AND* MASTER MUST NOT dispatch `rule_B` for the EXPAND phase
* *AND* the expansion returned by `rule_A` MUST be the text passed to the REWRITE phase

### Scenario: EXPAND dispatch continues to the next rule when a module returns nil

* *GIVEN* MASTER is active as the system preprocessor
* *AND* the current user has two EXPAND rules: `rule_A` at ordinal 1 and `rule_B` at ordinal 2
* *AND* the submitted statement is NOT recognised by `rule_A` (its function returns `nil`)
* *AND* the submitted statement IS recognised by `rule_B` (its function returns a non-nil expansion string)
* *WHEN* the user submits that statement
* *THEN* MASTER MUST dispatch `rule_A` and, after receiving `nil`, proceed to dispatch `rule_B`
* *AND* the expansion returned by `rule_B` MUST be the text passed to the REWRITE phase

### Scenario: A failing EXPAND module is skipped fail-open and the next rule is tried

* *GIVEN* MASTER is active as the system preprocessor
* *AND* the current user has two EXPAND rules: `rule_A` at ordinal 1 (raises an error) and `rule_B` at ordinal 2 (returns a non-nil expansion)
* *WHEN* the user submits a statement
* *THEN* MASTER MUST skip `rule_A` (fail-open) and proceed to try `rule_B`
* *AND* the expansion returned by `rule_B` MUST be used
* *AND* MASTER MUST NOT raise an error that blocks the statement

### Scenario: An unrecognised statement passes through all EXPAND rules unchanged

* *GIVEN* MASTER is active as the system preprocessor
* *AND* the current user has one or more EXPAND rules, none of which recognise the submitted statement (all return `nil`)
* *WHEN* the user submits that statement
* *THEN* MASTER MUST pass the original statement text unchanged to the REWRITE phase
* *AND* MASTER MUST NOT raise an error

### Scenario: REWRITE rules run after a matched EXPAND expansion

* *GIVEN* MASTER is active as the system preprocessor
* *AND* the current user has an EXPAND rule that recognises the statement and expands it
* *AND* the current user also has a REWRITE rule
* *WHEN* the user submits the custom command statement
* *THEN* MASTER MUST run the EXPAND rule first
* *AND* MASTER MUST run the REWRITE rule on the text produced by EXPAND
* *AND* `setsqltext` MUST receive the final text after all phases

### Scenario: DEMO_PING_V1 expands PING to SELECT 'PONG'

* *GIVEN* `PREPROC_RT.DEMO_PING_V1` is deployed
* *AND* MASTER is active as the system preprocessor
* *AND* the current user has an EXPAND rule pointing at `PREPROC_RT.DEMO_PING_V1 / ping`
* *WHEN* the user submits the statement `PING`
* *THEN* MASTER MUST expand it to `SELECT 'PONG'`
* *AND* the statement MUST execute and return the value `PONG`

### Scenario: DEMO_PING_V1 returns nil for any non-PING statement

* *GIVEN* `PREPROC_RT.DEMO_PING_V1` is deployed
* *AND* a MASTER EXPAND rule points at its `ping` function
* *WHEN* any statement other than `PING` (case-insensitive, trimmed) is submitted
* *THEN* `ping` MUST return `nil`
* *AND* MASTER MUST NOT treat the statement as matched

### Scenario: DEMO_PING_V1 passes luacheck

* *GIVEN* the module source at `sql/lua/demo_ping_v1.lua`
* *WHEN* luacheck is run against `sql/lua/`
* *THEN* luacheck MUST report zero warnings and zero errors for that file

### Scenario: Module authoring contract is documented in the demo module source

* *GIVEN* the source file `sql/lua/demo_ping_v1.lua`
* *WHEN* a module author reads its header comment block
* *THEN* the comments MUST explain the `import(name, alias)` loading contract
* *AND* the comments MUST explain that `nil` return signals no-match and a string return signals match-and-terminal
* *AND* the comments MUST warn about the `gsub` two-value return trap
* *AND* the comments MUST state that preprocessing output MUST be a single statement
* *AND* the comments MUST state that fail-open implications mean an unrecognised command will produce a loud syntax error from the engine

### Scenario: client_pattern matching uses literal substring search

* *GIVEN* MASTER is active and a rule has a `client_pattern` value that contains Lua pattern special characters (e.g. a dot or parenthesis)
* *WHEN* the connecting client identity contains that exact character sequence as a literal substring
* *THEN* MASTER MUST match the rule (literal substring match)
* *AND* MASTER MUST NOT treat the `client_pattern` as a Lua regular expression pattern
