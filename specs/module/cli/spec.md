# Feature: `preproc module` CLI

Adds a `preproc module` command group that is the operator's single entry point
for discovering, deploying, updating, removing, bundling, validating, and
declaratively syncing preprocessor modules. The CLI runs on the operator's
machine — the air-gap bridge — talking to GitHub for artifacts and to the
Exasol database over SQL; the database never fetches code. Deployment lives in
the CLI, never in the database (ADR-006/007).

## Background

* The command group is `preproc module <subcommand>` with subcommands `list`,
  `search`, `add`, `update`, `remove`, `bundle`, `validate`, and `sync`.
* `list` and `search` are pure registry reads and MUST NOT require a database
  connection. `add`, `update`, `remove`, and `sync` mutate the database and MUST
  resolve a connection from the environment (`EXASOL_DSN` / `EXASOL_USER` /
  `EXASOL_PASSWORD`), exiting with the missing-environment code when it is
  absent, consistent with the existing `install` command.
* `add <name>` resolves the module from the registry, and — for a
  `library-deployed` module — fetches its artifact, verifies its `sha256`,
  deploys the `PREPROC_RT.<NAME>_V<N>` script, registers the rule at the
  module's `suggested_scope` (or a `--scope` override) idempotently, writes a
  `PREPROC.MODULES` provenance row, and refreshes. For a `self-deployed` module
  `add` registers the rule and writes provenance only — it MUST NOT deploy an
  artifact (the module's own installer did).
* `bundle <names...>` is the air-gap path: it emits a single self-contained
  `.sql` file (the same slash/`--/`-delimited pattern as the engine's
  `release.sql`) containing the selected modules' artifacts and their rule
  registrations, to be hand-carried into a database with no internet egress.
  `bundle` MUST run fully offline and MUST NOT require a database connection.
* `validate <name>` performs a static check always (manifest conforms to the
  contract, artifact is a single `CREATE OR REPLACE` for the declared
  `script_name`, `sha256` matches for library-deployed, Lua passes lint) and,
  when a database connection is configured, additionally deploys the artifact to
  a scratch name, imports it, invokes the entry function with a no-op input, and
  asserts the return honours the phase contract (`nil` or string), tearing the
  scratch object down afterward.
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

* *GIVEN* a registry checkout with `cast_shorthand` and `trailing_comma`
* *WHEN* the operator runs `preproc module bundle cast_shorthand trailing_comma --output offline_modules.sql --registry <path>`
* *THEN* the command MUST write one `.sql` file containing both modules' `CREATE OR REPLACE` artifacts and their rule registrations
* *AND* the command MUST exit 0 without attempting a database connection
* *AND* the file MUST use the `--/` … `/` script-block delimiters so the Lua bodies' semicolons are not treated as terminators

### Scenario: `validate` statically rejects a nonconformant module offline

* *GIVEN* a module whose `module.toml` declares a `script_name` the artifact does not define
* *WHEN* the operator runs `preproc module validate <name> --registry <path>` with no database configured
* *THEN* the command MUST exit non-zero
* *AND* the command MUST report the artifact / `script_name` mismatch

### Scenario: `validate` smoke-tests a conformant module against a scratch deploy

* *GIVEN* an installed framework and a conformant `cast_shorthand` module
* *WHEN* the operator runs `preproc module validate cast_shorthand` with a database configured
* *THEN* the CLI MUST deploy the artifact to a scratch script, import it, and invoke the entry function
* *AND* the CLI MUST assert the return honours the phase contract
* *AND* the CLI MUST drop the scratch script afterward

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
