# Feature: PREPROC Command Sugar

Provides a dogfooded convenience layer: the framework's own EXPAND phase recognises SQL-like `PREPROC …` commands and expands each into exactly one `EXECUTE SCRIPT` call against the admin control scripts. This lets an admin manage the framework with readable keyword commands (`PREPROC ADD RULE …`, `PREPROC DROP RULE 42`, `PREPROC LIST RULES`, `PREPROC REFRESH`) typed directly into any SQL client, while every command still funnels through the validating control-script surface — the framework eating its own dog food. The sugar is the first production (non-`DEMO_`) dispatched module and follows the versioning discipline (`CONTROL_SUGAR_V1`). This adds the `PREPROC SET SYS_GROUPING <bool>` verb to the live `CONTROL_SUGAR_V1` module (in place — the project is pre-first-release, so there is no active session that could bind a stale module body; the never-`CREATE OR REPLACE`-a-live-module discipline applies only to post-release grammar changes, which must use `CONTROL_SUGAR_V2`).

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

## Scenarios

### Scenario: ADD RULE command expands to a RULE_ADD execute-script call

* *GIVEN* the convenience module is deployed and the caller holds the convenience EXPAND rule under an active preprocessor
* *WHEN* the caller submits a `PREPROC ADD RULE FOR ROLE … PHASE … SCRIPT … FUNCTION …` command
* *THEN* the module MUST return exactly one `EXECUTE SCRIPT PREPROC.RULE_ADD(...)` statement carrying the parsed scope, phase, script, and function as bound arguments
* *AND* the role/user value, phase, and script name MUST follow Exasol identifier folding (unquoted → UPPER; `"..."` → verbatim; role/user always UPPER), while the `FUNCTION` name MUST be carried literally with its case preserved (a Lua symbol, not a SQL identifier)
* *AND* the returned text MUST be a single statement

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
