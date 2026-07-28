# Feature: `preproc module` CLI

Adds a `preproc module` command group that is the operator's single entry point
for discovering, deploying, updating, removing, bundling, validating, and
declaratively syncing preprocessor modules. The CLI runs on the operator's
machine — the air-gap bridge — talking to GitHub for artifacts and to the
Exasol database over SQL; the database never fetches code. Deployment lives in
the CLI, never in the database (ADR-006/007).

The CLI-side deploy and validate paths now understand the composite artifact:
`add` executes 1..N statements with the same constructed atomicity as the
in-DB installer, and `validate` checks the object inventory instead of
asserting a single `CREATE`.

## Background

* The command group is `preproc module <subcommand>` with subcommands `list`,
  `search`, `add`, `update`, `remove`, `bundle`, `validate`, and `sync`.
* `list` and `search` are pure registry reads and MUST NOT require a database
  connection. `add`, `update`, `remove`, and `sync` mutate the database and MUST
  resolve a connection from the environment (`EXASOL_DSN` / `EXASOL_USER` /
  `EXASOL_PASSWORD`), exiting with the missing-environment code when it is
  absent, consistent with the existing `install` command.
* `add <name>` for a `library-deployed` module resolves the entry, fetches the
  artifact, verifies its `sha256`, splits it into statements, validates the
  derived object inventory against the declared one BEFORE executing anything,
  then executes the statements in file order, registers the rule idempotently
  at the module's `suggested_scope` (or a `--scope` override), writes a
  `PREPROC.MODULES` provenance row carrying the object inventory, and
  refreshes. For a `self-deployed` module `add` registers the rule and writes
  provenance only — it MUST NOT deploy an artifact (the module's own installer
  did).
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
* `bundle <names...>` is the air-gap path: it emits a single self-contained
  `.sql` file (the same slash/`--/`-delimited pattern as the engine's
  `release.sql`) containing the selected modules' artifacts and their rule
  registrations, to be hand-carried into a database with no internet egress.
  `bundle` MUST run fully offline and MUST NOT require a database connection.
  A composite artifact's statements are emitted with their delimiters preserved
  so the bundle stays directly runnable by hand.
* `validate <name>` static checks: the manifest conforms to the contract; the
  artifact splits cleanly into statements; the derived object inventory equals
  the declared `[[objects]]` inventory (or, for a single-statement artifact
  with no `[[objects]]`, equals the single object named by `script_name`);
  every object carries the module's `_V<N>` suffix; `sha256` matches for
  library-deployed; Lua passes lint. The live check, run when a database
  connection is configured, executes NOTHING the artifact wrote: it compiles
  every `SCRIPT` statement under a disposable name inside one throwaway
  `PREPROC_VALIDATE_<entropy>` schema it creates for the run, invokes the entry
  function through that disposable copy via the same `import` binding the
  runtime uses, asserts the returned type honours the phase contract, and
  drops the disposable schema afterward. Non-`SCRIPT` statements and statements
  that create nothing are left undeployed and reported as such, because a
  disposable copy of one of those would either resolve a live reference or
  write to a live object. For a composite module, an invocation the probe
  cannot complete is reported inconclusive rather than as a failure; only a
  single-statement artifact, which the disposable deploy reproduces whole,
  makes an invocation error conclusive.
* `sync <desired.toml>` is declarative and idempotent: it converges the database
  to the desired set of `module@version` + scope entries. Re-running `sync` with
  an unchanged desired file MUST make no changes and MUST NOT create duplicate
  rules (BUG-008): rule registration goes through the idempotent
  `PREPROC.RULE_ENSURE` surface, not a blind add.
* Every mutating subcommand MUST leave the database consistent on failure: a
  failed `add`/`update` MUST NOT leave a registered rule pointing at a script
  that was not deployed, nor a provenance row for a version that is not present.
* Registry location is selectable with `--registry <path-or-url>`; offline runs
  point it at a local checkout.

## Scenarios

### Scenario: `list` prints registry entries without a database connection

* *GIVEN* a registry checkout with two indexed modules and no configured database environment
* *WHEN* the operator runs `preproc module list --registry <path>`
* *THEN* the command MUST print both modules with their name, version, phase, and deploy mode
* *AND* the command MUST exit 0 without attempting a database connection

### Scenario: `search` filters registry entries by term

* *GIVEN* a registry checkout containing `cast_shorthand` and `trailing_comma`
* *WHEN* the operator runs `preproc module search cast --registry <path>`
* *THEN* the command MUST print `cast_shorthand`
* *AND* the command MUST NOT print `trailing_comma`

### Scenario: `add` deploys a library-deployed module and registers its rule

* *GIVEN* an installed framework and a registry entry `cast_shorthand` with `deploy_mode = "library-deployed"` and a valid `sha256`
* *WHEN* the operator runs `preproc module add cast_shorthand`
* *THEN* the CLI MUST deploy `PREPROC_RT.CAST_SHORTHAND_V1`
* *AND* the CLI MUST register the TRANSLATE rule at the module's suggested scope
* *AND* the CLI MUST write a `PREPROC.MODULES` provenance row for the deployed version
* *AND* a subsequent statement in scope MUST be transformed by the module

### Scenario: `add` for a self-deployed module registers the rule without deploying an artifact

* *GIVEN* an installed framework and a registry entry with `deploy_mode = "self-deployed"` whose backing script is already present
* *WHEN* the operator runs `preproc module add <name>`
* *THEN* the CLI MUST register the rule and write a provenance row
* *AND* the CLI MUST NOT create or replace the backing script

### Scenario: `add` aborts when the artifact sha256 does not match

* *GIVEN* a registry entry whose fetched artifact hashes to a digest other than the entry's `sha256`
* *WHEN* the operator runs `preproc module add <name>`
* *THEN* the CLI MUST exit non-zero
* *AND* the CLI MUST NOT deploy the artifact nor register a rule

### Scenario: `bundle` emits a self-contained offline artifact with no database connection

* *GIVEN* a registry checkout with `cast_shorthand` and a composite module
* *WHEN* the operator runs `preproc module bundle cast_shorthand <composite> --output offline_modules.sql --registry <path>`
* *THEN* the command MUST write one `.sql` file containing every statement of both modules' artifacts and their rule registrations
* *AND* the command MUST exit 0 without attempting a database connection
* *AND* the file MUST use the `--/` … `/` script-block delimiters so the Lua and Python bodies' semicolons are not treated as terminators
* *AND* the emitted file MUST be directly runnable by an interactive EXAplus client

### Scenario: `validate` statically rejects a nonconformant module offline

* *GIVEN* a module whose `module.toml` declares a `script_name` the artifact does not define
* *WHEN* the operator runs `preproc module validate <name> --registry <path>` with no database configured
* *THEN* the command MUST exit non-zero
* *AND* the command MUST report the artifact / `script_name` mismatch
* *AND* the command MUST equally reject an artifact whose derived object inventory disagrees with the declared `[[objects]]` array

### Scenario: `validate` compiles a conformant module's scripts in a disposable schema and smoke-tests the entry function

* *GIVEN* an installed framework and a conformant composite module
* *WHEN* the operator runs `preproc module validate <name>` with a database configured
* *THEN* the CLI MUST NOT execute any statement of the artifact as written
* *AND* the CLI MUST compile every `SCRIPT` statement of the artifact under a disposable name inside one throwaway `PREPROC_VALIDATE_<entropy>` schema it creates for the run
* *AND* the CLI MUST invoke the entry function through the disposable entry copy, using the same `import` binding the runtime uses, and assert the returned type honours the phase contract
* *AND* the CLI MUST report how many statements it compiled and how many non-script or non-creating statements it left undeployed
* *AND* the CLI MUST drop the disposable schema and everything created inside it afterward, even when a statement failed partway through

### Scenario: `sync` is idempotent — re-running an unchanged desired file changes nothing

* *GIVEN* a desired-state file listing `cast_shorthand@1` at a ROLE scope, already synced once
* *WHEN* the operator runs `preproc module sync <file>` a second time
* *THEN* the command MUST report no changes
* *AND* `PREPROC.CONFIG` MUST contain exactly one rule for that module and scope

### Scenario: `sync` retires a module dropped from the desired file

* *GIVEN* a database synced with `cast_shorthand@1` and a new desired file that omits it
* *WHEN* the operator runs `preproc module sync <file>`
* *THEN* the CLI MUST remove the module's rule and mark its provenance row retired
* *AND* the CLI MUST perform a single refresh for the whole convergence

### Scenario: A mutating subcommand exits with the missing-environment code when unconfigured

* *GIVEN* no Exasol connection environment variables are set
* *WHEN* the operator runs `preproc module add cast_shorthand`
* *THEN* the command MUST exit with the missing-environment exit code
* *AND* the command MUST name the missing variables

### Scenario: `add` deploys every object of a composite module

* *GIVEN* an installed framework and a registry entry for a composite module whose artifact creates a Lua entry script, two Python UDFs, and one table
* *WHEN* the operator runs `preproc module add <name>`
* *THEN* the CLI MUST execute the artifact's statements in file order
* *AND* every object in the declared inventory MUST exist in the database
* *AND* the CLI MUST write a provenance row recording the full object inventory
* *AND* a subsequent statement in scope MUST be transformed by the module

### Scenario: A failed `add` leaves no partially created objects and no rule

* *GIVEN* a registry entry for a composite module whose fourth statement fails at execution
* *WHEN* the operator runs `preproc module add <name>`
* *THEN* the CLI MUST exit non-zero reporting the failing statement's error
* *AND* the CLI MUST drop the objects created by the preceding statements
* *AND* `PREPROC.CONFIG` MUST contain no rule for the module and `PREPROC.MODULES` no provenance row

### Scenario: `validate` rejects a composite artifact whose Python UDFs are undeclared

* *GIVEN* a module whose artifact creates one Lua entry script and two Python UDFs, and whose `module.toml` declares only the Lua script
* *WHEN* the operator runs `preproc module validate <name> --registry <path>` with no database configured
* *THEN* the command MUST exit non-zero naming the two undeclared Python UDFs
* *AND* the command MUST NOT report the module as valid on the strength of its single Lua `CREATE` alone

### Scenario: Live validation never creates, replaces, or drops a real object of the module

* *GIVEN* an installed framework and a library-deployed composite module already installed under its real names
* *WHEN* the operator runs `preproc module validate <name>` with a database configured
* *THEN* the CLI MUST NOT create, replace, or drop any object under the module's declared names, or write anything into any schema the artifact names
* *AND* every object the CLI creates during the run MUST live inside the one disposable schema it created for that run
* *AND* if the entry function's invocation errors because it reaches for a companion object that exists only under its real name, the CLI MUST report the result inconclusive rather than as a failure
