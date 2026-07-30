# Feature: PREPROC Command Sugar

Provides a dogfooded convenience layer: the framework's own EXPAND phase recognises SQL-like `PREPROC …` commands and expands each into exactly one `EXECUTE SCRIPT` call against the admin control scripts. This lets an admin manage the framework with readable keyword commands (`PREPROC ADD RULE …`, `PREPROC DROP RULE 42`, `PREPROC LIST RULES`, `PREPROC REFRESH`) typed directly into any SQL client, while every command still funnels through the validating control-script surface — the framework eating its own dog food. The sugar is the first production (non-`DEMO_`) dispatched module and follows the versioning discipline (`CONTROL_SUGAR_V1`). This adds the `PREPROC SET SYS_GROUPING <bool>` verb to the live `CONTROL_SUGAR_V1` module (in place — the project is pre-first-release, so there is no active session that could bind a stale module body; the never-`CREATE OR REPLACE`-a-live-module discipline applies only to post-release grammar changes, which must use `CONTROL_SUGAR_V2`). It also adds an OPTIONAL `CLIENT '<pattern>'` clause to the `PREPROC ADD RULE` verb, so a client-scoped rule can be created from the sugar instead of dropping out to `EXECUTE SCRIPT PREPROC.RULE_ENSURE(...)` — the last operator-visible gap in the `client_pattern` surface.

```
PREPROC ADD RULE FOR (ROLE|USER) <v> PHASE <p> SCRIPT <s> FUNCTION <f>
                 [CLIENT '<pattern>'] [NOREFRESH]
```

## Background

* The convenience module is a production dispatched EXPAND module: it lives in the runtime schema `PREPROC_RT`, is deployed under a version-suffixed name (`CONTROL_SUGAR_V1`, never `CREATE OR REPLACE` on a live version), exports a top-level global function the dispatcher loads via `import(name, alias)`, and is covered by the schema-level PUBLIC grants on `PREPROC_RT` (ADR-002).
* It carries NO `DEMO_` prefix: it is a real, supported command module, not a demonstration. It is the first such module in the framework.
* The module obeys the EXPAND return contract (ADR-012): return `nil` for any statement it does not recognise (so the dispatcher keeps scanning), and return a single expansion string for a recognised `PREPROC …` command (match + terminal).
* An EXPAND expansion MUST be exactly one statement. Each `PREPROC …` command therefore maps to exactly one `EXECUTE SCRIPT PREPROC.<SCRIPT>(...)` call; auto-refresh is performed inside the target control script (see Admin Control Scripts), never as a second emitted statement.
* The sugar is reachable only for a user who both holds the convenience EXPAND rule (a `CONFIG` row scoped to `PREPROC_ADMIN`, seeded at install as the sanctioned bootstrap) AND is running under an active preprocessor. The underlying `EXECUTE SCRIPT` admin surface remains directly callable without the preprocessor active — the sugar is additive, never a prerequisite.
* Because the expansion is just an `EXECUTE SCRIPT` call running with the caller's privileges, a non-admin who somehow holds the sugar rule still cannot mutate config: the control script rejects them at its own privilege check. The sugar adds no privilege.
* String handling follows the module-authoring contract: `gsub` results are captured to a single variable (never passed inline) to avoid the two-value return trap; matching is case-insensitive with surrounding whitespace tolerated. The tokenizer keeps a `"..."` or `'...'` span (including doubled-quote escapes) as a single token so a delimited identifier or source survives embedded whitespace.
* Captured VALUES follow Exasol identifier quoting semantics rather than being copied through verbatim: an object-name identifier (a rule's `SCRIPT` name, the `PHASE`) folds to UPPER CASE when written unquoted and is taken verbatim when `"..."`-delimited (embedded `""` unescaped); a role/user name (`FOR ROLE|USER` value) is ALWAYS upper-cased, matching how Exasol upper-cases users and roles regardless of quoting (so `FOR ROLE dba` targets `DBA`). Two positions are NOT SQL identifiers and stay literal: the `FUNCTION` name is a case-sensitive Lua symbol (quotes stripped if present, case never changed), and the `SET MODE` value plus boolean/`rule_id` operands are passed through as written.
* The `SET SYS_GROUPING` verb maps to the `PREPROC.SET_SYS_GROUPING` admin script (see Admin Control Scripts). It is MUTATING (it changes resolution), so it accepts a trailing `NOREFRESH` suffix exactly like `PREPROC SET MODE` — unlike `PREPROC SET SELF_DISABLE`, which is non-refreshing and rejects `NOREFRESH`.
* The parsed value for `SET SYS_GROUPING` is passed through UNQUOTED as a boolean literal (mirroring how `SET SELF_DISABLE` passes `word(3)` unquoted), NOT single-quoted like `SET MODE`'s string value. The trailing `norefresh` slot is always emitted (`true` when `NOREFRESH` is present, else `NULL`) so the emitted `EXECUTE SCRIPT` meets Exasol's exact-argument-count requirement, mirroring the always-pass-the-slot pattern of `SET MODE` / `ADD RULE`.
* The `CONTROL` map gains `set_sys_grouping = "PREPROC.SET_SYS_GROUPING"`; parsing is `verb == "SET" and upper_word(2) == "SYS_GROUPING"`, with the subcommand-count check consistent with `SET MODE` (three tokens after `PREPROC` before any `NOREFRESH` strip) and `value = word(3)`.
* The `CLIENT` clause is added to the live `CONTROL_SUGAR_V1` module IN PLACE, under the same pre-first-release exception (ADR-038) already granted to `PREPROC SET SYS_GROUPING` and the module-lifecycle verbs: there is no active session that could bind a stale module body. Any POST-release grammar change MUST still arrive via `CONTROL_SUGAR_V2`, and the module header must keep saying so, so the exception is not mistaken for the rule.
* An OPTIONAL clause in this grammar is already established by `INSTALL MODULE`, whose arm accepts 8 words with `FROM '<source>'` and 6 without, branching on the count and on the keyword at the expected position. `ADD RULE` follows that shape: 13 words with `CLIENT '<pattern>'`, 11 without.
* `NOREFRESH` is detected and STRIPPED from the token list BEFORE arm parsing, so the `ADD RULE` arm sees either 11 or 13 words and the two optional features compose without a combinatorial explosion of accepted counts. `CLIENT` is therefore never parsed at a shifted index.
* `NOREFRESH` stripping compares the last token to the bare keyword, and the tokenizer keeps a `'...'` span WITH its quotes. A quoted pattern is therefore never mistaken for the suffix: the token `'NOREFRESH'` does not equal `NOREFRESH`.
* The captured pattern is NOT an Exasol identifier and MUST NOT be identifier-folded. It is matched as a literal substring against a client application name the connecting tool reports in its own casing (`DbVisualizer`, `DBeaver`, `PyExasol 2.2.1`), so upper-casing it would break every realistic pattern — silently, since the rule would still resolve and dispatch would still evaluate it. This makes `CLIENT` the second capture position after `FUNCTION` that is deliberately exempt from folding, and for the same class of reason: the value denotes something outside Exasol's identifier namespace. The quoting-semantics section of the module header records it alongside `FUNCTION`.
* The operand MUST be a single-quoted literal. Unlike the `"..."`-delimited identifier positions, the `CLIENT` operand's quotes are SQL string-literal quotes: the span is unwrapped, any doubled `''` unescaped, and the value re-quoted for the emitted call by the existing `quote()` helper. An unquoted operand, an unterminated span, or a missing operand is a malformed clause.
* The emitted call MUST fill every argument slot of the target script (the always-pass-the-slot rule): an absent `CLIENT` emits `NULL` for `client_pattern`, and an absent `NOREFRESH` emits `NULL` for `norefresh`, because Exasol enforces an exact argument count.
* The verb adds no privilege and no validation: `PREPROC.RULE_ADD` still enforces the `PREPROC_ADMIN` boundary and still performs every check, including rejecting a whitespace-only or over-length pattern.
* An EMPTY operand (`CLIENT ''`) is the one exception to carrying a well-formed literal through: it is DECLINED. Exasol does not distinguish the empty string from NULL (`SELECT CASE WHEN '' IS NULL THEN 'YES' END` answers `YES`), so an emitted `''` argument is ALREADY NULL at the `EXECUTE SCRIPT` boundary — it reaches `PREPROC.RULE_ADD` as the `null` sentinel, indistinguishable from an omitted clause. Carrying it through would therefore skip the shared validator entirely and silently create an ALL-CLIENTS rule from a command that READS as a restriction, re-opening the very footgun this delta's validation exists to close. Declining keeps `''` from being the single silent outcome in a grammar whose every other malformed input is loud. A WHITESPACE-ONLY operand (`CLIENT ' '`) is NOT collapsed by Exasol, survives transit as a real value, and IS still carried through so the validator raises its explanatory rejection.
* The empty-operand refusal is a property of the `CLIENT` operand POSITION, not of string-literal syntax: `''` is a well-formed literal, and the shared literal-parsing helper reports it faithfully as the empty string. The position-specific policy therefore lives at the `ADD RULE` arm's capture site, alongside the folding exemption, so the helper's `nil` keeps its single meaning — "not a well-formed string literal" — rather than being overloaded to also mean "well formed but empty".

## Scenarios

### Scenario: ADD RULE command expands to a RULE_ADD execute-script call

* *GIVEN* the convenience module is deployed and the caller holds the convenience EXPAND rule under an active preprocessor
* *WHEN* the caller submits a `PREPROC ADD RULE FOR ROLE … PHASE … SCRIPT … FUNCTION …` command with no `CLIENT` clause
* *THEN* the module MUST return exactly one `EXECUTE SCRIPT PREPROC.RULE_ADD(...)` statement carrying the parsed scope, phase, script, and function as bound arguments, followed by `NULL` for `client_pattern`
* *AND* the role/user value, phase, and script name MUST follow Exasol identifier folding (unquoted → UPPER; `"..."` → verbatim; role/user always UPPER), while the `FUNCTION` name MUST be carried literally with its case preserved (a Lua symbol, not a SQL identifier)
* *AND* the returned text MUST be a single statement, and the command MUST behave exactly as it did before this delta

### Scenario: ADD RULE with a CLIENT clause threads a client_pattern argument

* *GIVEN* the convenience module is deployed and the caller holds the convenience EXPAND rule under an active preprocessor
* *WHEN* the caller submits `PREPROC ADD RULE FOR ROLE DBA PHASE EXPAND SCRIPT PREPROC_RT.DEMO_HELP_V1 FUNCTION help_menu CLIENT 'DbVisualizer'`
* *THEN* the module MUST return exactly one `EXECUTE SCRIPT PREPROC.RULE_ADD(...)` statement whose `client_pattern` argument is the single-quoted literal `'DbVisualizer'`
* *AND* the `client_pattern` argument MUST occupy the slot between `function_name` and `norefresh`, matching the deployed parameter order
* *AND* the trailing `norefresh` slot MUST be emitted as `NULL`

### Scenario: The CLIENT value is carried verbatim and never identifier-folded

* *GIVEN* the convenience module is deployed and active for the caller
* *WHEN* the caller submits an `ADD RULE` command with `CLIENT 'DbVisualizer'`
* *THEN* the emitted `client_pattern` argument MUST preserve the operand's original casing exactly
* *AND* it MUST NOT be upper-cased, lower-cased, or trimmed, because it is matched against a self-reported client application name rather than an Exasol identifier
* *AND* a pattern containing whitespace (`CLIENT 'DbVisualizer 24'`) MUST be carried as ONE argument with the space intact, and an embedded doubled quote MUST survive as a single quote in the emitted literal

### Scenario: CLIENT composes with NOREFRESH in either combination

* *GIVEN* the convenience module is deployed and active for the caller
* *WHEN* the caller submits an `ADD RULE` command with both a `CLIENT '<pattern>'` clause and a trailing `NOREFRESH`
* *THEN* the module MUST emit the pattern as `client_pattern` and `true` as `norefresh`
* *AND* the same command with only `NOREFRESH` MUST emit `NULL` as `client_pattern` and `true` as `norefresh`, and the same command with only `CLIENT` MUST emit the pattern and `NULL`
* *AND* `NOREFRESH` MUST remain the trailing keyword, stripped before the arm parses positions, so the arm only ever sees 11 or 13 words

### Scenario: A quoted pattern is never mistaken for the NOREFRESH suffix

* *GIVEN* the convenience module is deployed and active for the caller
* *WHEN* the caller submits an `ADD RULE` command ending in `CLIENT 'NOREFRESH'`
* *THEN* the module MUST emit `'NOREFRESH'` as the `client_pattern` argument
* *AND* it MUST emit `NULL` as `norefresh`, because the quoted span is a single token that does not equal the bare `NOREFRESH` keyword

### Scenario: A malformed CLIENT clause is declined fail-open

* *GIVEN* the convenience module is deployed and active for the caller
* *WHEN* the caller submits an `ADD RULE` command whose `CLIENT` clause is malformed — the keyword with no operand, an operand that is not a single-quoted literal (for example `CLIENT DbVisualizer`), an unterminated quoted span, or an unrecognised word in the `CLIENT` keyword position
* *THEN* the module MUST NOT emit an `EXECUTE SCRIPT` string that would create a rule with a wrong or missing pattern
* *AND* the module MUST return `nil` so the original text reaches the engine and fails with a loud syntax error, matching the fail-open contract the other verbs follow
* *AND* a word count that is neither 11 nor 13 after the `NOREFRESH` strip MUST also decline

### Scenario: An empty CLIENT operand is declined because Exasol collapses it to NULL

* *GIVEN* the convenience module is deployed and active for the caller
* *WHEN* the caller submits an `ADD RULE` command whose `CLIENT` operand is the empty literal `''`, with or without a trailing `NOREFRESH`
* *THEN* the module MUST return `nil` and MUST NOT emit an `EXECUTE SCRIPT` string, so the original text reaches the engine and fails with a loud syntax error
* *AND* the cause MUST be understood as transit semantics rather than syntax — `''` is a well-formed string literal, but Exasol does not distinguish the empty string from NULL, so an emitted `''` would arrive at `PREPROC.RULE_ADD` as the `null` sentinel, bypass the shared `client_pattern` validator, and silently create an ALL-CLIENTS rule from a command that reads as a restriction
* *AND* a WHITESPACE-ONLY operand (`CLIENT ' '`) MUST still be carried through, because Exasol does not collapse it: it survives transit as a real value and the validator raises its explanatory rejection naming NULL as the way to express "all clients"
* *AND* the emptiness test MUST apply to the UNESCAPED value and not to the token text, so `CLIENT ''''` — one apostrophe, a four-character token — MUST still be carried through

### Scenario: The CLIENT keyword is matched case-insensitively like every other keyword

* *GIVEN* the convenience module is deployed and active for the caller
* *WHEN* the caller submits the clause as `client '<pattern>'` or `Client '<pattern>'`
* *THEN* the module MUST recognise it, because `PREPROC` keywords are always matched case-insensitively
* *AND* the OPERAND's casing MUST still be preserved

### Scenario: The CLIENT grammar change is an in-place V1 edit under the pre-release exception

* *GIVEN* the project is pre-first-release and no session can be bound to a stale `CONTROL_SUGAR_V1` body
* *WHEN* the `CLIENT` clause is added to the grammar
* *THEN* it MUST be added to `PREPROC_RT.CONTROL_SUGAR_V1` in place, with no new module version and no repointing of the seeded `PREPROC_ADMIN`- and `DBA`-scoped EXPAND rules
* *AND* the module header MUST continue to record that a POST-release grammar change requires `CONTROL_SUGAR_V2`, so the exception is not mistaken for the rule
* *AND* the seeded rules MUST keep resolving to `CONTROL_SUGAR_V1` unchanged after the edit

### Scenario: DROP RULE command expands to a RULE_DROP execute-script call

* *GIVEN* the convenience module is deployed and active for the caller
* *WHEN* the caller submits `PREPROC DROP RULE 42`
* *THEN* the module MUST return exactly one `EXECUTE SCRIPT PREPROC.RULE_DROP(42)` statement
* *AND* the parsed `rule_id` MUST be carried as a numeric argument

### Scenario: DISABLE and ENABLE RULE commands expand to their control scripts

* *GIVEN* the convenience module is deployed and active for the caller
* *WHEN* the caller submits `PREPROC DISABLE RULE 42`
* *THEN* the module MUST return exactly one `EXECUTE SCRIPT PREPROC.RULE_DISABLE(42)` statement
* *AND* when the caller submits `PREPROC ENABLE RULE 42` the module MUST return exactly one `EXECUTE SCRIPT PREPROC.RULE_ENABLE(42)` statement

### Scenario: LIST RULES command expands to a RULE_LIST execute-script call

* *GIVEN* the convenience module is deployed and active for the caller
* *WHEN* the caller submits `PREPROC LIST RULES`
* *THEN* the module MUST return exactly one `EXECUTE SCRIPT PREPROC.RULE_LIST` statement

### Scenario: REFRESH and STATUS commands expand to their admin scripts

* *GIVEN* the convenience module is deployed and active for the caller
* *WHEN* the caller submits `PREPROC REFRESH`
* *THEN* the module MUST return exactly one `EXECUTE SCRIPT PREPROC.REFRESH` statement
* *AND* when the caller submits `PREPROC STATUS` the module MUST return exactly one `EXECUTE SCRIPT PREPROC.STATUS` statement

### Scenario: SET MODE and SET SELF_DISABLE commands expand to settings scripts

* *GIVEN* the convenience module is deployed and active for the caller
* *WHEN* the caller submits a `PREPROC SET MODE …` command
* *THEN* the module MUST return exactly one `EXECUTE SCRIPT PREPROC.SET_MODE(...)` statement carrying the parsed mode value
* *AND* when the caller submits a `PREPROC SET SELF_DISABLE …` command the module MUST return exactly one `EXECUTE SCRIPT PREPROC.SET_SELF_DISABLE(...)` statement

### Scenario: A NOREFRESH suffix is threaded into the control-script call

* *GIVEN* the convenience module is deployed and active for the caller
* *WHEN* the caller submits a mutating `PREPROC …` command with a `NOREFRESH` suffix
* *THEN* the module MUST set the control script's `NOREFRESH` argument so the auto-refresh is suppressed
* *AND* the returned text MUST remain a single `EXECUTE SCRIPT` statement

### Scenario: A non-PREPROC statement is declined

* *GIVEN* the convenience module is deployed
* *WHEN* any statement that does not begin with the `PREPROC` keyword (case-insensitive, leading whitespace tolerated) is submitted
* *THEN* the module MUST return `nil`
* *AND* the dispatcher MUST NOT treat the statement as matched

### Scenario: An unrecognised PREPROC subcommand surfaces a loud error

* *GIVEN* the convenience module is deployed and active for the caller
* *WHEN* the caller submits a `PREPROC` statement whose subcommand the module does not recognise
* *THEN* the module MUST NOT return a malformed `EXECUTE SCRIPT` string that silently misbehaves
* *AND* the module SHOULD return `nil` so the original `PREPROC …` text reaches the engine and fails with a loud syntax error (fail-open EXPAND contract)

### Scenario: The convenience module passes luacheck

* *GIVEN* the convenience module source
* *WHEN* luacheck is run against it
* *THEN* luacheck MUST report zero warnings and zero errors for that file

### Scenario: The convenience EXPAND rule is seeded at install as the bootstrap path

* *GIVEN* a freshly installed framework
* *WHEN* the install completes
* *THEN* `PREPROC.CONFIG` MUST contain an enabled EXPAND rule scoped to `PREPROC_ADMIN` pointing at the convenience module
* *AND* that rule MUST be seeded by the installer's own DML, the sanctioned bootstrap that resolves the chicken-and-egg of registering the sugar before the sugar exists

### Scenario: SET SYS_GROUPING command expands to a SET_SYS_GROUPING execute-script call

* *GIVEN* the convenience module is deployed and active for the caller
* *WHEN* the caller submits `PREPROC SET SYS_GROUPING FALSE`
* *THEN* the module MUST return exactly one `EXECUTE SCRIPT PREPROC.SET_SYS_GROUPING(FALSE, NULL)` statement
* *AND* the parsed value MUST be carried UNQUOTED as a boolean literal (not single-quoted)
* *AND* when the caller submits `PREPROC SET SYS_GROUPING TRUE` the module MUST return exactly one `EXECUTE SCRIPT PREPROC.SET_SYS_GROUPING(TRUE, NULL)` statement

### Scenario: A NOREFRESH suffix on SET SYS_GROUPING threads true

* *GIVEN* the convenience module is deployed and active for the caller
* *WHEN* the caller submits `PREPROC SET SYS_GROUPING FALSE NOREFRESH`
* *THEN* the module MUST return exactly one `EXECUTE SCRIPT PREPROC.SET_SYS_GROUPING(FALSE, true)` statement
* *AND* the `NOREFRESH` keyword MUST be matched case-insensitively and MUST NOT appear in the emitted statement
