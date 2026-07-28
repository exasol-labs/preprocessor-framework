# Feature: `preproc module` CLI

Adds a `preproc module` command group that is the operator's single entry point
for discovering, deploying, updating, removing, bundling, validating, and
declaratively syncing preprocessor modules.

This delta teaches the CLI-side deploy and validate paths the composite artifact:
`add` executes 1..N statements with the same constructed atomicity as the in-DB
installer, and `validate` checks the object inventory instead of asserting a
single `CREATE`.

## Background

* `add <name>` for a `library-deployed` module resolves the entry, fetches the
  artifact, verifies its `sha256`, splits it into statements, validates the
  derived object inventory against the declared one BEFORE executing anything,
  then executes the statements in file order, registers the rule idempotently,
  writes a `PREPROC.MODULES` provenance row carrying the object inventory, and
  refreshes.
* The CLI path uses the SAME statement grammar and the SAME split result as the
  in-database path. Both implementations are pinned by the shared conformance
  corpus, because a divergence would break the byte-identical end-state
  guarantee between `preproc module add` and `PREPROC.MODULE_INSTALL`.
* `add` inherits the constructed atomicity of ADR-059: pre-flight validation
  before the first DDL, compensating cleanup of already-created objects on a
  mid-artifact failure, idempotent re-run via `CREATE OR REPLACE`, and rule
  registration last so a failed `add` leaves the module inert. The existing
  "a failed add MUST NOT leave a registered rule pointing at a script that was
  not deployed" invariant is strengthened to cover every object of the artifact.
* `validate <name>` static checks become: the manifest conforms to the contract;
  the artifact splits cleanly into statements; the derived object inventory
  equals the declared `[[objects]]` inventory (or, for a single-statement
  artifact with no `[[objects]]`, equals the single object named by
  `script_name`); every object carries the module's `_V<N>` suffix; `sha256`
  matches for library-deployed; Lua passes lint. The live check executes
  NOTHING the artifact wrote: it compiles every `SCRIPT` statement under a
  disposable name inside one throwaway `PREPROC_VALIDATE_<entropy>` schema it
  creates for the run, invokes the entry function through that disposable copy
  via the same `import` binding the runtime uses, asserts the returned type
  honours the phase contract, and drops the disposable schema afterward.
  Non-`SCRIPT` statements and statements that create nothing are left
  undeployed and reported as such, because a disposable copy of one of those
  would either resolve a live reference or write to a live object. For a
  composite module, an invocation the probe cannot complete is reported
  inconclusive rather than as a failure; only a single-statement artifact,
  which the disposable deploy reproduces whole, makes an invocation error
  conclusive.
* `bundle <names...>` already emits `--/` … `/` delimited blocks; a composite
  artifact's statements are emitted with their delimiters preserved so the
  bundle stays directly runnable by hand. `bundle` remains fully offline and
  MUST NOT require a database connection.

## Scenarios

<!-- DELTA:NEW -->
### Scenario: `add` deploys every object of a composite module

* *GIVEN* an installed framework and a registry entry for a composite module whose artifact creates a Lua entry script, two Python UDFs, and one table
* *WHEN* the operator runs `preproc module add <name>`
* *THEN* the CLI MUST execute the artifact's statements in file order
* *AND* every object in the declared inventory MUST exist in the database
* *AND* the CLI MUST write a provenance row recording the full object inventory
* *AND* a subsequent statement in scope MUST be transformed by the module
<!-- /DELTA:NEW -->

<!-- DELTA:NEW -->
### Scenario: A failed `add` leaves no partially created objects and no rule

* *GIVEN* a registry entry for a composite module whose fourth statement fails at execution
* *WHEN* the operator runs `preproc module add <name>`
* *THEN* the CLI MUST exit non-zero reporting the failing statement's error
* *AND* the CLI MUST drop the objects created by the preceding statements
* *AND* `PREPROC.CONFIG` MUST contain no rule for the module and `PREPROC.MODULES` no provenance row
<!-- /DELTA:NEW -->

<!-- DELTA:CHANGED -->
### Scenario: `validate` statically rejects a nonconformant module offline

* *GIVEN* a module whose `module.toml` declares a `script_name` the artifact does not define
* *WHEN* the operator runs `preproc module validate <name> --registry <path>` with no database configured
* *THEN* the command MUST exit non-zero
* *AND* the command MUST report the artifact / `script_name` mismatch
* *AND* the command MUST equally reject an artifact whose derived object inventory disagrees with the declared `[[objects]]` array
<!-- /DELTA:CHANGED -->

<!-- DELTA:NEW -->
### Scenario: `validate` rejects a composite artifact whose Python UDFs are undeclared

* *GIVEN* a module whose artifact creates one Lua entry script and two Python UDFs, and whose `module.toml` declares only the Lua script
* *WHEN* the operator runs `preproc module validate <name> --registry <path>` with no database configured
* *THEN* the command MUST exit non-zero naming the two undeclared Python UDFs
* *AND* the command MUST NOT report the module as valid on the strength of its single Lua `CREATE` alone
<!-- /DELTA:NEW -->

<!-- DELTA:CHANGED -->
### Scenario: `validate` compiles a conformant module's scripts in a disposable schema and smoke-tests the entry function

* *GIVEN* an installed framework and a conformant composite module
* *WHEN* the operator runs `preproc module validate <name>` with a database configured
* *THEN* the CLI MUST NOT execute any statement of the artifact as written
* *AND* the CLI MUST compile every `SCRIPT` statement of the artifact under a disposable name inside one throwaway `PREPROC_VALIDATE_<entropy>` schema it creates for the run
* *AND* the CLI MUST invoke the entry function through the disposable entry copy, using the same `import` binding the runtime uses, and assert the returned type honours the phase contract
* *AND* the CLI MUST report how many statements it compiled and how many non-script or non-creating statements it left undeployed
* *AND* the CLI MUST drop the disposable schema and everything created inside it afterward, even when a statement failed partway through
<!-- /DELTA:CHANGED -->

<!-- DELTA:NEW -->
### Scenario: Live validation never creates, replaces, or drops a real object of the module

* *GIVEN* an installed framework and a library-deployed composite module already installed under its real names
* *WHEN* the operator runs `preproc module validate <name>` with a database configured
* *THEN* the CLI MUST NOT create, replace, or drop any object under the module's declared names, or write anything into any schema the artifact names
* *AND* every object the CLI creates during the run MUST live inside the one disposable schema it created for that run
* *AND* if the entry function's invocation errors because it reaches for a companion object that exists only under its real name, the CLI MUST report the result inconclusive rather than as a failure
<!-- /DELTA:NEW -->

<!-- DELTA:CHANGED -->
### Scenario: `bundle` emits a self-contained offline artifact with no database connection

* *GIVEN* a registry checkout with `cast_shorthand` and a composite module
* *WHEN* the operator runs `preproc module bundle cast_shorthand <composite> --output offline_modules.sql --registry <path>`
* *THEN* the command MUST write one `.sql` file containing every statement of both modules' artifacts and their rule registrations
* *AND* the command MUST exit 0 without attempting a database connection
* *AND* the file MUST use the `--/` … `/` script-block delimiters so the Lua and Python bodies' semicolons are not treated as terminators
* *AND* the emitted file MUST be directly runnable by an interactive EXAplus client
<!-- /DELTA:CHANGED -->
