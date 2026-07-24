# Feature: PREPROC Module Sugar

Provides the module-lifecycle sugar verbs — `PREPROC INSTALL MODULE … FROM … FOR
(ROLE|USER) …` and `PREPROC CATALOG MODULES FROM …` — that let a DBA drive the
in-database module installer (`PREPROC.MODULE_INSTALL` / `PREPROC.MODULE_CATALOG`)
with readable keyword commands instead of hand-writing an `EXECUTE SCRIPT` call.
The verbs were added to the live `CONTROL_SUGAR_V1` module IN PLACE (the same
pre-release exception as `PREPROC SET SYS_GROUPING`, ADR-038: the project is
pre-first-release, so there is no active session that could bind a stale module
body; any POST-release grammar change MUST still arrive via `CONTROL_SUGAR_V2`).
This feature is the module-lifecycle sibling of `control/command-sugar` (which
holds the original rule/refresh/settings/mode sugar verbs) — both are scenarios
of the one physical `CONTROL_SUGAR_V1` module, split here by subject matter
rather than by deployment unit.

## Background

* The convenience module is a production dispatched EXPAND module: it lives in
  the runtime schema `PREPROC_RT`, is deployed under a version-suffixed name
  (`CONTROL_SUGAR_V1`, never `CREATE OR REPLACE` on a live version), exports a
  top-level global function the dispatcher loads via `import(name, alias)`, and
  is covered by the schema-level PUBLIC grants on `PREPROC_RT` (ADR-002).
* Each `PREPROC …` command maps to exactly one
  `EXECUTE SCRIPT PREPROC.<SCRIPT>(...)` call (the EXPAND single-statement
  contract, ADR-012; the intercepted-statement mapping is ADR-053); the module
  verbs follow the same match-and-terminal shape and return `nil` for anything
  they do not recognise so the dispatcher keeps scanning — the same fail-open
  interception contract `control/command-sugar` documents for the original
  verbs.
* `INSTALL MODULE` carries three parsed parts into `PREPROC.MODULE_INSTALL`: the
  module name, the `FROM '<source>'` string (kept single-quoted verbatim so a
  `bucketfs:` or `https:` source survives intact), and the `FOR (ROLE|USER) <x>`
  clause folded into a single `TYPE:value` scope string. `CATALOG MODULES FROM
  '<source>'` maps to `PREPROC.MODULE_CATALOG('<source>')`.
* The `FROM '<source>'` clause is OPTIONAL on both verbs. When it is omitted the
  source defaults to a built-in constant — the public Exasol Labs library index
  pinned to the mutable `latest` tag
  (`https://raw.githubusercontent.com/exasol-labs/preprocessor-library/latest/registry/index.json`)
  — so a DBA can `PREPROC INSTALL MODULE <name> FOR (ROLE|USER) <x>` or `PREPROC
  CATALOG MODULES` with no source and get the newest published release. Supplying
  an explicit `FROM` overrides the default for a fork, a pinned `vX.Y.Z` tag, or a
  `bucketfs:` source. The default is a code constant, not a `PREPROC.SETTINGS`
  row, in keeping with ADR-051 (no settings toggle on the fetch path).
* Captured values follow Exasol identifier quoting semantics (the same rules
  `control/command-sugar` documents): the module name is an object-name
  identifier — an UNQUOTED token folds to UPPER CASE, a `"..."`-delimited token
  is taken verbatim (embedded `""` unescaped). Because module names are
  identifier-safe (letters/digits/underscore, no hyphens — enforced by the module
  contract) AND the resolver matches names CASE-INSENSITIVELY, a lower_snake_case
  key like `cast_shorthand` resolves whether written unquoted (`cast_shorthand` →
  emitted `'CAST_SHORTHAND'`), upper, or `"..."`-quoted — quoting is not required.
  The `FOR (ROLE|USER)` value is a role/user name, which Exasol always upper-cases
  regardless of quoting, so it is ALWAYS upper-cased (`FOR ROLE dba`, `DBA`, and
  `"dba"` all yield `DBA`).
* `gsub` results are captured to a single variable (never passed inline) to avoid
  the two-value return trap; keyword matching is case-insensitive with surrounding
  whitespace tolerated. The verbs add no privilege — the target admin script still
  enforces the `PREPROC_ADMIN` boundary at its own access.

## Scenarios

### Scenario: INSTALL MODULE command expands to a MODULE_INSTALL execute-script call

* *GIVEN* the convenience module is deployed and the caller holds the convenience EXPAND rule under an active preprocessor
* *WHEN* the caller submits `PREPROC INSTALL MODULE "cast_shorthand" FROM 'bucketfs:PREPROC_BFS/preproc-lib-0.1.0.tar.gz' FOR ROLE PREPROC_ADMIN`
* *THEN* the module MUST return exactly one `EXECUTE SCRIPT PREPROC.MODULE_INSTALL('bucketfs:PREPROC_BFS/preproc-lib-0.1.0.tar.gz', 'cast_shorthand', 'ROLE:PREPROC_ADMIN')` statement
* *AND* the `FROM` source MUST be carried single-quoted exactly as written
* *AND* the `"..."`-delimited module name MUST be carried verbatim
* *AND* the returned text MUST be a single statement

### Scenario: A FOR USER scope threads a USER-typed scope argument

* *GIVEN* the convenience module is deployed and active for the caller
* *WHEN* the caller submits `PREPROC INSTALL MODULE "cast_shorthand" FROM 'bucketfs:PREPROC_BFS/preproc-lib-0.1.0.tar.gz' FOR USER BOB`
* *THEN* the module MUST return exactly one `EXECUTE SCRIPT PREPROC.MODULE_INSTALL(...)` statement whose scope argument is `'USER:BOB'`
* *AND* the scope `TYPE` MUST be matched case-insensitively against `ROLE` and `USER`

### Scenario: Captured values follow Exasol identifier quoting semantics

* *GIVEN* the convenience module is deployed and active for the caller
* *WHEN* the caller submits an `INSTALL MODULE` command with an UNQUOTED module name and a `FOR ROLE`/`FOR USER` value in any case (for example `PREPROC INSTALL MODULE cast_mod FROM 'https://h/registry/index.json' FOR ROLE dba`)
* *THEN* the unquoted module name MUST be folded to UPPER CASE (`'CAST_MOD'`) — the regular-identifier rule (the resolver then matches it case-insensitively against the registry key)
* *AND* the role/user value MUST be folded to UPPER CASE (`'ROLE:DBA'`) whether it was written unquoted or `"..."`-delimited, matching how Exasol upper-cases users and roles

### Scenario: CATALOG MODULES command expands to a MODULE_CATALOG execute-script call

* *GIVEN* the convenience module is deployed and active for the caller
* *WHEN* the caller submits `PREPROC CATALOG MODULES FROM 'bucketfs:PREPROC_BFS/preproc-lib-0.1.0.tar.gz'`
* *THEN* the module MUST return exactly one `EXECUTE SCRIPT PREPROC.MODULE_CATALOG('bucketfs:PREPROC_BFS/preproc-lib-0.1.0.tar.gz')` statement

### Scenario: INSTALL MODULE without a FROM clause defaults to the Exasol Labs library at latest

* *GIVEN* the convenience module is deployed and active for the caller
* *WHEN* the caller submits `PREPROC INSTALL MODULE cast_shorthand FOR ROLE PREPROC_ADMIN` with no `FROM` clause
* *THEN* the module MUST return exactly one `EXECUTE SCRIPT PREPROC.MODULE_INSTALL('https://raw.githubusercontent.com/exasol-labs/preprocessor-library/latest/registry/index.json', 'CAST_SHORTHAND', 'ROLE:PREPROC_ADMIN')` statement
* *AND* the source MUST default to the built-in Exasol Labs library index pinned to the `latest` tag
* *AND* the `FOR (ROLE|USER)` clause MUST be parsed at its shifted position (no `FROM '<source>'` tokens precede it)
* *AND* supplying an explicit `FROM '<source>'` MUST override the default (a fork or a pinned `vX.Y.Z`)

### Scenario: CATALOG MODULES without a FROM clause defaults to the Exasol Labs library at latest

* *GIVEN* the convenience module is deployed and active for the caller
* *WHEN* the caller submits `PREPROC CATALOG MODULES` with no `FROM` clause
* *THEN* the module MUST return exactly one `EXECUTE SCRIPT PREPROC.MODULE_CATALOG('https://raw.githubusercontent.com/exasol-labs/preprocessor-library/latest/registry/index.json')` statement
* *AND* the source MUST default to the built-in Exasol Labs library index pinned to the `latest` tag

### Scenario: A malformed INSTALL MODULE command is declined fail-open

* *GIVEN* the convenience module is deployed and active for the caller
* *WHEN* the caller submits a `PREPROC INSTALL …` statement whose shape the module does not recognise (for example a missing `FOR` clause, or a `FOR` clause with a scope type that is neither `ROLE` nor `USER`)
* *THEN* the module MUST NOT return a malformed `EXECUTE SCRIPT` string that silently misbehaves
* *AND* the module SHOULD return `nil` so the original text reaches the engine and fails with a loud syntax error (fail-open EXPAND contract)
