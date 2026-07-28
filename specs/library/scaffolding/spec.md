# Feature: Central Library Repo Scaffolding

Delivers the full initial scaffolding for the central module-library repo
`exasol-labs/preprocessor-library` (local checkout `/home/nc/Github/preprocessor-library`,
currently only `LICENSE` + `README.md`) — the community-facing home for modules
and the host of the federated registry index. These deliverables are authored
DIRECTLY in that separate repo, NOT in the framework repo. The scaffolding
includes the standard module layout, a module template, `CONTRIBUTING.md`, a CI
workflow that tests each module against a docker Exasol and regenerates the
registry index, the generated index, and the ergonomics modules migrated in as
the first two modules.

This records the composite contract's effect on the library: the two existing
ergonomics modules stay untouched as N=1 modules, the module template documents
`[[objects]]`, and the generated index moves to `schema_version` 2.

## Background

* This feature's deliverables live in a SEPARATE git repository,
  `exasol-labs/preprocessor-library` (local checkout
  `/home/nc/Github/preprocessor-library`). All paths in this feature are
  relative to that repo's root. Nothing in this feature is committed to the
  framework repo; the only framework-repo change for the migration is the
  REMOVAL of the ergonomics sources (see the `ergonomics/*` features).
* Ergonomics migrates as TWO independent modules, `cast_shorthand` and
  `trailing_comma`, not one combined module: each is a single Lua script with
  its own entry function, registration rule, and `module.toml`, and each is
  independently opt-in — matching the one-script/one-function/one-rule contract.
* The library repo MUST contain: `modules/<name>/{<name>_v<N>.sql, module.toml,
  README.md, tests/}` per module; a `modules/_template/` module skeleton;
  `registry/index.json` (generated) and `registry/external/` (curated external
  entries, initially with a `.gitkeep`); `CONTRIBUTING.md`; a CI workflow under
  `.github/workflows/`; and a top-level `README.md`.
* `CONTRIBUTING.md` MUST document both contribution paths: adding a
  library-deployed module (author the layout, fill `module.toml`, add tests, let
  CI regenerate the index) and PR-ing an external-repo registry entry (add one
  `registry/external/<name>.toml` block, repo@tag + sha + metadata, reviewed by
  a human as the trust gate). It also gains guidance for contributing a
  composite module: declare every object, keep the artifact runnable by hand in
  EXAplus, and expect CI to reject any drift between the declared and parsed
  inventories.
* The CI workflow MUST, for each module, run its `tests/` against a docker
  Exasol with the framework installed, and MUST regenerate `registry/index.json`
  and fail if the committed index is out of sync with the module manifests.
  Library CI additionally asserts, per module, that the declared inventory
  matches the artifact's parsed inventory — the same accept/reject discipline it
  already applies to `sha256`.
* The ergonomics modules `cast_shorthand` and `trailing_comma` MUST be present as
  module #1 and #2, each a complete conformant module: a single self-contained
  `_v1.sql` artifact, a `module.toml` with a matching `sha256`, a `README.md`,
  and `tests/`. Their Lua bodies are the framework's existing ergonomics module
  sources, carried over unchanged so the migration is a faithful move.
* The ergonomics `module.toml` files MUST declare `phase = "TRANSLATE"`,
  `deploy_mode = "library-deployed"`, a `suggested_scope`, `min_framework`, and a
  `sha256` that matches the artifact bytes.
* `cast_shorthand` and `trailing_comma` remain single-statement modules and
  their `module.toml` files are NOT edited by the composite delta. Because
  `[[objects]]` is optional for a single-statement artifact, both manifests stay
  conformant exactly as committed, and their `sha256` values stay valid because
  the digest is unaffected by the contract change. Preserving these two
  untouched is the regression proof that N=1 is a true degenerate case.
* `modules/_template/` documents the `[[objects]]` array: when it MUST be
  declared (any artifact carrying more than one statement), the `type` and
  fully-qualified `name` shape, the `_V<N>`-suffix requirement on every object,
  and the `--/` … `/` statement-separation convention.
* The regenerated `registry/index.json` declares `schema_version` 2 and carries
  an `objects` list on every library entry, single-statement entries included.
* This feature does not itself create the GitHub repo (it already exists with
  `LICENSE` + `README.md`); it authors the initial tree into the existing
  `exasol-labs/preprocessor-library` checkout.

## Scenarios

### Scenario: The library repo has the required top-level structure

* *GIVEN* the `exasol-labs/preprocessor-library` repo after this plan authors its scaffolding
* *WHEN* its structure is inspected
* *THEN* it MUST contain `modules/`, `registry/index.json`, `registry/external/`, `CONTRIBUTING.md`, `README.md`, and a workflow under `.github/workflows/`
* *AND* it MUST contain a `modules/_template/` module skeleton with a `module.toml` and a placeholder artifact

### Scenario: Ergonomics cast_shorthand is present as a conformant module

* *GIVEN* the library repo's `modules/cast_shorthand/` directory
* *WHEN* its contents are inspected
* *THEN* it MUST contain `cast_shorthand_v1.sql`, `module.toml`, `README.md`, and a `tests/` directory
* *AND* the `module.toml` `script_name` MUST be `PREPROC_RT.CAST_SHORTHAND_V1` with `phase = "TRANSLATE"` and `deploy_mode = "library-deployed"`

### Scenario: Ergonomics trailing_comma is present as a conformant module

* *GIVEN* the library repo's `modules/trailing_comma/` directory
* *WHEN* its contents are inspected
* *THEN* it MUST contain `trailing_comma_v1.sql`, `module.toml`, `README.md`, and a `tests/` directory
* *AND* the `module.toml` `script_name` MUST be `PREPROC_RT.ERGONOMICS_TRAILING_COMMA_V1` with `phase = "TRANSLATE"`

### Scenario: Each module.toml declares a sha256 matching its artifact

* *GIVEN* a library-deployed module directory in the library repo
* *WHEN* the artifact bytes are hashed and compared to the manifest `sha256`
* *THEN* the digests MUST match for every library-deployed module

### Scenario: The generated index lists every library module

* *GIVEN* the library repo's `registry/index.json`
* *WHEN* it is read
* *THEN* it MUST contain an entry for `cast_shorthand` and `trailing_comma`
* *AND* it MUST declare a top-level `schema_version` of 2
* *AND* every entry MUST carry an `objects` list, single-statement entries included
* *AND* regenerating the index from the library repo's `modules/` MUST reproduce it byte-for-byte

### Scenario: The CI workflow tests modules and checks index sync

* *GIVEN* the library repo's `.github/workflows/` CI workflow file
* *WHEN* its contents are inspected
* *THEN* it MUST run each module's `tests/` against a docker Exasol with the framework installed
* *AND* it MUST regenerate the registry index and fail when the committed index is out of sync
* *AND* it MUST fail when a module's declared object inventory disagrees with its artifact's parsed inventory

### Scenario: CONTRIBUTING documents both the module and external-entry paths

* *GIVEN* the library repo's `CONTRIBUTING.md`
* *WHEN* it is read
* *THEN* it MUST describe adding a library-deployed module via the standard layout
* *AND* it MUST describe PR-ing an external-repo registry entry as a curated `registry/external/<name>.toml` block reviewed by a human

### Scenario: The ergonomics modules remain conformant with no manifest edit

* *GIVEN* the library repo's `modules/cast_shorthand/` and `modules/trailing_comma/` directories exactly as committed, each with a single-statement artifact and no `[[objects]]` array
* *WHEN* each module is validated against the composite contract
* *THEN* both modules MUST validate as conformant
* *AND* neither `module.toml` MUST require an `[[objects]]` array
* *AND* each declared `sha256` MUST still match its artifact bytes

### Scenario: The module template documents the object inventory and statement separation

* *GIVEN* the library repo's `modules/_template/` skeleton
* *WHEN* its contents are inspected
* *THEN* its `module.toml` MUST document the `[[objects]]` array with a `type` and a fully-qualified `name` per object
* *AND* it MUST state that `[[objects]]` is REQUIRED once the artifact carries more than one statement
* *AND* it MUST document the `--/` … `/` statement-separation convention and the `_V<N>` suffix requirement on every object
