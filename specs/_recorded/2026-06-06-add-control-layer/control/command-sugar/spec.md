# Feature: PREPROC Command Sugar

Provides a dogfooded convenience layer: the framework's own EXPAND phase recognises SQL-like `PREPROC …` commands and expands each into exactly one `EXECUTE SCRIPT` call against the admin control scripts. This lets an admin manage the framework with readable keyword commands (`PREPROC ADD RULE …`, `PREPROC DROP RULE 42`, `PREPROC LIST RULES`, `PREPROC REFRESH`) typed directly into any SQL client, while every command still funnels through the validating control-script surface — the framework eating its own dog food. The sugar is the first production (non-`DEMO_`) dispatched module and follows the versioning discipline (`CONTROL_SUGAR_V1`).

## Background

* The convenience module is a production dispatched EXPAND module: it lives in the runtime schema `PREPROC_RT`, is deployed under a version-suffixed name (`CONTROL_SUGAR_V1`, never `CREATE OR REPLACE` on a live version), exports a top-level global function the dispatcher loads via `import(name, alias)`, and is covered by the schema-level PUBLIC grants on `PREPROC_RT` (ADR-002).
* It carries NO `DEMO_` prefix: it is a real, supported command module, not a demonstration. It is the first such module in the framework.
* The module obeys the EXPAND return contract (ADR-012): return `nil` for any statement it does not recognise (so the dispatcher keeps scanning), and return a single expansion string for a recognised `PREPROC …` command (match + terminal).
* An EXPAND expansion MUST be exactly one statement. Each `PREPROC …` command therefore maps to exactly one `EXECUTE SCRIPT PREPROC.<SCRIPT>(...)` call; auto-refresh is performed inside the target control script (see Admin Control Scripts), never as a second emitted statement.
* The sugar is reachable only for a user who both holds the convenience EXPAND rule (a `CONFIG` row scoped to `PREPROC_ADMIN`, seeded at install as the sanctioned bootstrap) AND is running under an active preprocessor. The underlying `EXECUTE SCRIPT` admin surface remains directly callable without the preprocessor active — the sugar is additive, never a prerequisite.
* Because the expansion is just an `EXECUTE SCRIPT` call running with the caller's privileges, a non-admin who somehow holds the sugar rule still cannot mutate config: the control script rejects them at its own privilege check. The sugar adds no privilege.
* String handling follows the module-authoring contract: `gsub` results are captured to a single variable (never passed inline) to avoid the two-value return trap; matching is case-insensitive with surrounding whitespace tolerated.

## Scenarios

### Scenario: ADD RULE command expands to a RULE_ADD execute-script call

* *GIVEN* the convenience module is deployed and the caller holds the convenience EXPAND rule under an active preprocessor
* *WHEN* the caller submits a `PREPROC ADD RULE FOR ROLE … PHASE … SCRIPT … FUNCTION …` command
* *THEN* the module MUST return exactly one `EXECUTE SCRIPT PREPROC.RULE_ADD(...)` statement carrying the parsed scope, phase, script, and function as bound arguments
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
