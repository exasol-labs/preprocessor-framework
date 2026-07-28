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

This delta makes that last sentence true. The artifact is generalised from
exactly one `CREATE OR REPLACE LUA SCRIPT` statement to a single `.sql` file
carrying 1..N statements, so a module that is the front door to a whole
subsystem (Lua entry script + Python UDFs + tables) can be `library-deployed`
and installed from the registry instead of being forced into `self-deployed`.
N=1 is the degenerate case and stays byte-compatible. No third `deploy_mode` is
introduced; `self-deployed` is unchanged.

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
  the module, carrying ONE OR MORE statements (ADR-054). Exactly one of those
  statements MUST be the `CREATE OR REPLACE LUA SCRIPT <script_name>` that
  defines the module's entry script; the remaining statements MAY create any
  other database objects the module needs — Python UDFs, further Lua scripts,
  tables, views. A single-statement artifact remains valid and is simply the
  N=1 case.
* Statements are separated by the EXAplus block-marker convention already
  emitted by the framework's own `release.sql` (ADR-058): a statement whose body
  contains semicolons is wrapped between a line consisting solely of `--/` and a
  line consisting solely of `/`; any other statement is terminated by `;`. The
  artifact therefore stays directly runnable by hand in EXAplus or DbVisualiser,
  which is what preserves the air-gap hand-carry story.
* `module.toml` MUST declare an `[[objects]]` array naming every object the
  artifact creates, each entry carrying a `type` (for example `LUA SCRIPT`,
  `PYTHON3 SCALAR SCRIPT`, `TABLE`) and a fully-qualified `name`. The
  declaration is the module's object inventory. `[[objects]]` MAY be omitted
  when the artifact carries exactly one statement, in which case the inventory
  is derived as the single entry named by `script_name` — this is what keeps
  existing single-statement manifests valid unchanged.
* The inventory is DECLARED and ALSO DERIVED: tooling parses the artifact to
  compute the object list independently and MUST reject the module when the
  parsed list and the declared list disagree, in either direction (ADR-055).
  This is the same accept/reject discipline already applied to `sha256`, and it
  is what makes drift impossible and lets `MODULE_RECLAIM` trust the declaration
  without re-parsing.
* Every object an artifact creates MUST carry the module's `_V<N>` version
  suffix, not only the entry script (ADR-056). The
  never-`CREATE OR REPLACE`-a-live-module discipline (ADR-019) therefore extends
  to companions, so a v1 and a v2 generation coexist with zero collisions. A
  companion whose body is identical between v1 and v2 is still redeployed under
  the new versioned name. The entry script MUST reference its companions by
  their versioned names.
* An artifact MAY create objects in any schema the installing admin can write;
  it is NOT restricted to `PREPROC_RT` or `PREPROC` (ADR-057). Installing a
  library-deployed module is therefore arbitrary DDL executed under the
  `PREPROC_ADMIN` boundary, which is why the inventory is reviewable before
  install: the registry index carries it and `PREPROC.MODULE_CATALOG` surfaces
  it, so an operator sees exactly which objects an install would create.
* Each module lives under the standard layout
  `modules/<name>/{<name>_v<N>.sql, README.md, module.toml, tests/}`. `<name>` is
  `snake_case` and MUST be identifier-safe — a letter followed by letters, digits
  or underscores, with NO hyphens — so it works unquoted in
  `PREPROC INSTALL MODULE <name>`. The deployed entry script name is
  `PREPROC_RT.<NAME>_V<N>` where `<NAME>` is the upper-case form of `<name>`.
* `module.toml` MUST declare at least: `name`, `description`, `phase`,
  `script_name`, `function`, `version`, `min_framework`, `suggested_scope`
  (a `type` of `ROLE` or `USER` and a `value`), and `deploy_mode`
  (`library-deployed` or `self-deployed`). `version` MUST equal the `_V<N>`
  suffix in `script_name` and in the artifact filename.
* `deploy_mode` is orthogonal deployment convenience, NOT a second contract. A
  `library-deployed` module ships its whole body in the one `.sql` artifact and
  MUST additionally declare `sha256` (the hex digest of that artifact) so the
  operator's CLI can verify integrity. `sha256` is computed over the whole file
  and is unaffected by the statement count. A `self-deployed` module is
  installed by its own project's installer and only needs its rule registered;
  it MUST declare `source` (`repo` and `ref`) and MAY omit `sha256`.
* `phase` MUST be one of `TRANSLATE`, `EXPAND`, `REWRITE`. `min_framework` is a
  semantic version string naming the lowest framework release the module
  supports.
* The contract is deployment-agnostic about air-gap: the manifest carries the
  data the operator's CLI machine needs to fetch, verify, and deploy or
  register the module; the database itself never fetches anything.
* The statement splitter exists in two implementations — Python for the CLI path
  and Lua for the in-database path — which MUST agree. A shared conformance
  corpus at `tests/fixtures/artifact_split_corpus.json` pins the expected split
  for a set of artifact texts, and both implementations are tested against it.

## Scenarios

<!-- DELTA:REMOVED -->
### Scenario: The artifact is a single self-contained CREATE OR REPLACE statement

* *GIVEN* the pre-delta contract required a module's canonical `.sql` artifact to contain exactly one `CREATE OR REPLACE LUA SCRIPT` statement, contradicting the same feature's promise of one contract for a tiny transform or the front door to a whole subsystem
* *WHEN* this plan is applied and the artifact is generalised to a single file carrying 1..N statements
* *THEN* the contract MUST NOT require an artifact to contain exactly one `CREATE OR REPLACE LUA SCRIPT` statement
* *AND* this scenario MUST be replaced by the one-or-more-statements scenario below
<!-- /DELTA:REMOVED -->

<!-- DELTA:NEW -->
### Scenario: The artifact is a single self-contained file carrying one or more statements

* *GIVEN* a module's canonical `.sql` artifact
* *WHEN* the artifact is parsed
* *THEN* it MUST be accepted when it carries one or more statements
* *AND* exactly one statement MUST be a `CREATE OR REPLACE LUA SCRIPT` whose target name equals the manifest `script_name`
* *AND* an artifact carrying no `CREATE OR REPLACE LUA SCRIPT` for the manifest `script_name` MUST be rejected
<!-- /DELTA:NEW -->

<!-- DELTA:NEW -->
### Scenario: A composite artifact declaring a Lua entry script and companion objects is accepted

* *GIVEN* a `module.toml` with `deploy_mode = "library-deployed"`, `script_name = "PREPROC_RT.CONTROL_DATABASE_V1"`, a matching `sha256`, and an `[[objects]]` array declaring a `LUA SCRIPT PREPROC_RT.CONTROL_DATABASE_V1` and a `PYTHON3 SCALAR SCRIPT PREPROC_RT.CONFD_JOB_V1`
* *AND* an artifact creating exactly those two objects
* *WHEN* the module is validated against the contract
* *THEN* the validator MUST report the module as valid
* *AND* the validator MUST NOT require a `deploy_mode` other than `library-deployed`
<!-- /DELTA:NEW -->

<!-- DELTA:NEW -->
### Scenario: Statements are separated by block markers and semicolon terminators

* *GIVEN* an artifact in which a `CREATE OR REPLACE LUA SCRIPT` whose body contains semicolons is wrapped between a lone `--/` line and a lone `/` line, followed by a `CREATE TABLE` statement terminated by `;`
* *WHEN* the artifact is split into statements
* *THEN* the splitter MUST yield exactly two statements
* *AND* the block-wrapped statement MUST retain its body's semicolons unsplit
* *AND* the `--/` and `/` marker lines MUST NOT appear in the yielded statement text
<!-- /DELTA:NEW -->

<!-- DELTA:NEW -->
### Scenario: A declared object inventory that disagrees with the parsed artifact is rejected

* *GIVEN* a `module.toml` whose `[[objects]]` array declares two objects and an artifact that creates three
* *WHEN* the module is validated against the contract
* *THEN* validation MUST fail
* *AND* the error MUST name the object present in the artifact but absent from the declaration
* *AND* validation MUST also fail in the reverse direction when the declaration names an object the artifact does not create
<!-- /DELTA:NEW -->

<!-- DELTA:NEW -->
### Scenario: An artifact whose non-Lua statements are undeclared is rejected by static validation

* *GIVEN* an artifact containing one `CREATE OR REPLACE LUA SCRIPT` for the declared `script_name` and two `CREATE OR REPLACE PYTHON3 SCALAR SCRIPT` statements, and a `module.toml` declaring only the Lua script
* *WHEN* the module is validated against the contract on the CLI, with no database connection
* *THEN* validation MUST fail naming the two undeclared Python UDFs
* *AND* validation MUST NOT defer the failure to database install time
<!-- /DELTA:NEW -->

<!-- DELTA:NEW -->
### Scenario: An artifact object missing the module version suffix is rejected

* *GIVEN* a `module.toml` with `version = 1` whose `[[objects]]` array declares a companion `PYTHON3 SCALAR SCRIPT PREPROC_RT.CONFD_JOB` with no `_V1` suffix
* *WHEN* the module is validated against the contract
* *THEN* validation MUST fail
* *AND* the error MUST state that every object in the artifact MUST carry the module's `_V<N>` version suffix
<!-- /DELTA:NEW -->

<!-- DELTA:NEW -->
### Scenario: A single-statement artifact without an objects array remains valid

* *GIVEN* an existing `module.toml` for `cast_shorthand` declaring `script_name = "PREPROC_RT.CAST_SHORTHAND_V1"`, a matching `sha256`, and no `[[objects]]` array
* *AND* a single-statement artifact creating exactly that script
* *WHEN* the module is validated against the contract
* *THEN* the validator MUST report the module as valid
* *AND* the validator MUST derive the object inventory as the single entry named by `script_name`
* *AND* the manifest MUST NOT require editing to remain conformant
<!-- /DELTA:NEW -->

<!-- DELTA:NEW -->
### Scenario: A multi-statement artifact without an objects array is rejected

* *GIVEN* a `module.toml` with no `[[objects]]` array and an artifact carrying two statements
* *WHEN* the module is validated against the contract
* *THEN* validation MUST fail
* *AND* the error MUST state that `[[objects]]` is REQUIRED once the artifact carries more than one statement
<!-- /DELTA:NEW -->

<!-- DELTA:NEW -->
### Scenario: A composite artifact may create objects outside PREPROC_RT

* *GIVEN* a `module.toml` whose `[[objects]]` array declares an object in a schema other than `PREPROC_RT` or `PREPROC`, and an artifact creating exactly the declared objects
* *WHEN* the module is validated against the contract
* *THEN* the validator MUST report the module as valid
* *AND* the validator MUST NOT restrict declared objects to the framework's own schemas
<!-- /DELTA:NEW -->

<!-- DELTA:NEW -->
### Scenario: The Python and Lua statement splitters agree on the shared conformance corpus

* *GIVEN* the conformance corpus at `tests/fixtures/artifact_split_corpus.json`, pinning an expected statement split for each artifact text it carries
* *WHEN* the Python splitter and the in-database Lua splitter are each run over every corpus case
* *THEN* both implementations MUST produce the corpus's expected statement list for every case
* *AND* the two implementations MUST agree statement-for-statement on every case
<!-- /DELTA:NEW -->

<!-- DELTA:CHANGED -->
### Scenario: A library-deployed artifact whose sha256 does not match is rejected

* *GIVEN* a `module.toml` declaring a `sha256` and a companion multi-statement `.sql` artifact whose bytes hash to a different digest
* *WHEN* the artifact integrity is verified against the manifest
* *THEN* verification MUST fail
* *AND* the failure MUST NOT deploy any statement of the artifact
* *AND* the digest MUST be computed over the whole file regardless of how many statements it carries
<!-- /DELTA:CHANGED -->
