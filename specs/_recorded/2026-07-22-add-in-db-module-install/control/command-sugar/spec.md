# Feature: PREPROC Command Sugar

Provides a dogfooded convenience layer: the framework's own EXPAND phase
recognises SQL-like `PREPROC …` commands and expands each into exactly one
`EXECUTE SCRIPT` call against the admin control scripts. This delta adds the
in-database module-management verbs — `PREPROC INSTALL MODULE … FROM … FOR
(ROLE|USER) …` and `PREPROC CATALOG MODULES FROM …` — to the live
`CONTROL_SUGAR_V1` module IN PLACE. The in-place edit is the
same pre-release exception as ADR-038 (`SET SYS_GROUPING`): the project is
pre-first-release, so there is no active session that could bind a stale module
body; any POST-release grammar change MUST still arrive via `CONTROL_SUGAR_V2`.

## Background

* Each `PREPROC …` command maps to exactly one `EXECUTE SCRIPT PREPROC.<SCRIPT>(...)`
  call (the EXPAND single-statement contract, ADR-012); the new verbs follow the
  same match-and-terminal shape and return `nil` for anything they do not
  recognise so the dispatcher keeps scanning.
* `INSTALL MODULE` carries three parsed parts into `PREPROC.MODULE_INSTALL`: the
  module name, the `FROM '<source>'` string (kept single-quoted verbatim so a
  `bucketfs:` or `https:` source survives intact), and the `FOR (ROLE|USER) <x>`
  clause folded into a single `TYPE:value` scope string. `CATALOG MODULES FROM
  '<source>'` maps to `PREPROC.MODULE_CATALOG('<source>')`.
* `gsub` results are captured to a single variable (never passed inline) to avoid
  the two-value return trap; keyword matching is case-insensitive with surrounding
  whitespace tolerated. The verbs add no privilege — the target admin script still
  enforces the `PREPROC_ADMIN` boundary at its own access.

## Scenarios

<!-- DELTA:NEW -->
### Scenario: INSTALL MODULE command expands to a MODULE_INSTALL execute-script call

* *GIVEN* the convenience module is deployed and the caller holds the convenience EXPAND rule under an active preprocessor
* *WHEN* the caller submits `PREPROC INSTALL MODULE cast-shorthand FROM 'bucketfs:PREPROC_BFS/preproc-lib-0.1.0.tar.gz' FOR ROLE PREPROC_ADMIN`
* *THEN* the module MUST return exactly one `EXECUTE SCRIPT PREPROC.MODULE_INSTALL('bucketfs:PREPROC_BFS/preproc-lib-0.1.0.tar.gz', 'cast-shorthand', 'ROLE:PREPROC_ADMIN')` statement
* *AND* the `FROM` source MUST be carried single-quoted exactly as written
* *AND* the returned text MUST be a single statement
<!-- /DELTA:NEW -->

<!-- DELTA:NEW -->
### Scenario: A FOR USER scope threads a USER-typed scope argument

* *GIVEN* the convenience module is deployed and active for the caller
* *WHEN* the caller submits `PREPROC INSTALL MODULE cast-shorthand FROM 'bucketfs:PREPROC_BFS/preproc-lib-0.1.0.tar.gz' FOR USER BOB`
* *THEN* the module MUST return exactly one `EXECUTE SCRIPT PREPROC.MODULE_INSTALL(...)` statement whose scope argument is `'USER:BOB'`
* *AND* the scope `TYPE` MUST be matched case-insensitively against `ROLE` and `USER`
<!-- /DELTA:NEW -->

<!-- DELTA:NEW -->
### Scenario: CATALOG MODULES command expands to a MODULE_CATALOG execute-script call

* *GIVEN* the convenience module is deployed and active for the caller
* *WHEN* the caller submits `PREPROC CATALOG MODULES FROM 'bucketfs:PREPROC_BFS/preproc-lib-0.1.0.tar.gz'`
* *THEN* the module MUST return exactly one `EXECUTE SCRIPT PREPROC.MODULE_CATALOG('bucketfs:PREPROC_BFS/preproc-lib-0.1.0.tar.gz')` statement
<!-- /DELTA:NEW -->

<!-- DELTA:NEW -->
### Scenario: A malformed INSTALL MODULE command is declined fail-open

* *GIVEN* the convenience module is deployed and active for the caller
* *WHEN* the caller submits a `PREPROC INSTALL …` statement whose shape the module does not recognise (for example a missing `FROM` or `FOR` clause)
* *THEN* the module MUST NOT return a malformed `EXECUTE SCRIPT` string that silently misbehaves
* *AND* the module SHOULD return `nil` so the original text reaches the engine and fails with a loud syntax error (fail-open EXPAND contract)
<!-- /DELTA:NEW -->
