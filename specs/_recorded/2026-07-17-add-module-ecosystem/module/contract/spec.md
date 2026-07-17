# Feature: Module Integration Contract

Defines the stable, versioned contract every preprocessor module targets so that
independent authors can hook syntax providers into the framework's single
dispatch slot without coordinating with the framework team. A module is a
preprocessor function `fn(text) -> nil|string` living in a Lua script, plus a
registration rule (scope, phase, script, function) added through the existing
`PREPROC ADD RULE` / `PREPROC.RULE_ADD` surface, plus a `module.toml` manifest
that describes the artifact and how it is deployed. This is the SDK surface
external authors read; there is exactly ONE contract regardless of whether the
module is a tiny transform or the front door to a whole subsystem.

## Background

* There is exactly ONE contract. The earlier "bundled module vs subsystem
  module" split is explicitly rejected: a module is always `fn(text) -> nil|string`
  in a Lua script registered as a rule. What is behind the script (a pure
  transform or an entire catalog-backed subsystem) is the author's concern, not
  the contract's.
* The Lua entry function MUST accept the statement text as its single string
  argument and MUST return either `nil` or a string. For TRANSLATE and REWRITE
  phases it MUST return a string on every path (run-all text threading:
  unchanged input is returned byte-for-byte). For EXPAND it MUST return `nil`
  for any statement it does not recognise and a single expansion string when it
  matches (first-match-terminal, per ADR-012).
* A module MUST NOT add a defensive `pcall` around its own body; errors MUST
  propagate so MASTER's dispatch-level `pcall` skips the rule visibly (fail-open
  for EXPAND/REWRITE, fail-closed-by-return for TRANSLATE).
* The canonical artifact of a module is ONE self-contained `.sql` file that IS
  the module: a single `CREATE OR REPLACE LUA SCRIPT PREPROC_RT.<NAME>_V<N> AS
  <lua body>` statement. The version suffix `_V<N>` is mandatory and the
  never-`CREATE OR REPLACE`-a-live-module discipline (ADR-019) governs updates:
  a new behaviour ships as `_V<N+1>`, never by replacing a live `_V<N>`.
* Each module lives under the standard layout
  `modules/<name>/{<name>_v<N>.sql, README.md, module.toml, tests/}`. `<name>` is
  kebab-case; the deployed script name is `PREPROC_RT.<NAME>_V<N>` where `<NAME>`
  is the upper-snake-case form of `<name>`.
* `module.toml` MUST declare at least: `name`, `description`, `phase`,
  `script_name`, `function`, `version`, `min_framework`, `suggested_scope`
  (a `type` of `ROLE` or `USER` and a `value`), and `deploy_mode`
  (`library-deployed` or `self-deployed`). `version` MUST equal the `_V<N>`
  suffix in `script_name` and in the artifact filename.
* `deploy_mode` is orthogonal deployment convenience, NOT a second contract. A
  `library-deployed` module ships its whole body in the one `.sql` artifact and
  MUST additionally declare `sha256` (the hex digest of that artifact) so the
  operator's CLI can verify integrity. A `self-deployed` module is installed by
  its own project's installer and only needs its rule registered; it MUST
  declare `source` (`repo` and `ref`) and MAY omit `sha256`.
* `phase` MUST be one of `TRANSLATE`, `EXPAND`, `REWRITE`. `min_framework` is a
  semantic version string naming the lowest framework release the module
  supports.
* The contract is deployment-agnostic about air-gap: the manifest carries the
  data the operator's CLI machine needs to fetch, verify, and deploy or
  register the module; the database itself never fetches anything.

## Scenarios

<!-- DELTA:NEW -->
### Scenario: A minimal library-deployed manifest is accepted

* *GIVEN* a `module.toml` declaring `name`, `description`, `phase = "TRANSLATE"`, `script_name = "PREPROC_RT.CAST_SHORTHAND_V1"`, `function`, `version = 1`, `min_framework`, `suggested_scope`, `deploy_mode = "library-deployed"`, and a valid `sha256`
* *WHEN* the manifest is validated against the contract
* *THEN* the validator MUST report the manifest as valid
* *AND* the validator MUST confirm `version` equals the `_V1` suffix in `script_name`
<!-- /DELTA:NEW -->

<!-- DELTA:NEW -->
### Scenario: A self-deployed manifest without sha256 but with source is accepted

* *GIVEN* a `module.toml` with `deploy_mode = "self-deployed"`, a `source` table declaring `repo` and `ref`, and no `sha256`
* *WHEN* the manifest is validated against the contract
* *THEN* the validator MUST report the manifest as valid
* *AND* the validator MUST NOT require an artifact `sha256` for a self-deployed module
<!-- /DELTA:NEW -->

<!-- DELTA:NEW -->
### Scenario: A library-deployed manifest missing sha256 is rejected

* *GIVEN* a `module.toml` with `deploy_mode = "library-deployed"` and no `sha256`
* *WHEN* the manifest is validated against the contract
* *THEN* the validator MUST reject the manifest
* *AND* the error MUST name the missing `sha256` field
<!-- /DELTA:NEW -->

<!-- DELTA:NEW -->
### Scenario: A manifest whose version disagrees with its script_name suffix is rejected

* *GIVEN* a `module.toml` with `version = 2` and `script_name = "PREPROC_RT.CAST_SHORTHAND_V1"`
* *WHEN* the manifest is validated against the contract
* *THEN* the validator MUST reject the manifest
* *AND* the error MUST name the version / `_V<N>` mismatch
<!-- /DELTA:NEW -->

<!-- DELTA:NEW -->
### Scenario: A manifest with an unknown phase is rejected

* *GIVEN* a `module.toml` with `phase = "OPTIMIZE"`
* *WHEN* the manifest is validated against the contract
* *THEN* the validator MUST reject the manifest
* *AND* the error MUST state that the allowed phases are `TRANSLATE`, `EXPAND`, `REWRITE`
<!-- /DELTA:NEW -->

<!-- DELTA:NEW -->
### Scenario: A library-deployed artifact whose sha256 does not match is rejected

* *GIVEN* a `module.toml` declaring a `sha256` and a companion `.sql` artifact whose bytes hash to a different digest
* *WHEN* the artifact integrity is verified against the manifest
* *THEN* verification MUST fail
* *AND* the failure MUST NOT deploy the artifact
<!-- /DELTA:NEW -->

<!-- DELTA:NEW -->
### Scenario: The artifact is a single self-contained CREATE OR REPLACE statement

* *GIVEN* a module's canonical `.sql` artifact
* *WHEN* the artifact is parsed
* *THEN* it MUST contain exactly one `CREATE OR REPLACE LUA SCRIPT PREPROC_RT.<NAME>_V<N>` statement
* *AND* the script name in that statement MUST equal the manifest `script_name`
<!-- /DELTA:NEW -->

<!-- DELTA:NEW -->
### Scenario: A conformant EXPAND module returns nil for an unrecognised statement

* *GIVEN* a deployed EXPAND module and a statement the module does not recognise
* *WHEN* the module's entry function is invoked with that statement text
* *THEN* the function MUST return `nil`
* *AND* returning `nil` MUST NOT be treated by the contract as an error
<!-- /DELTA:NEW -->

<!-- DELTA:NEW -->
### Scenario: A conformant TRANSLATE module returns the input unchanged when it does not act

* *GIVEN* a deployed TRANSLATE module and a statement the module chooses not to transform
* *WHEN* the module's entry function is invoked with that statement text
* *THEN* the function MUST return the input text byte-for-byte
* *AND* the function MUST NOT return `nil` for a TRANSLATE or REWRITE phase
<!-- /DELTA:NEW -->
