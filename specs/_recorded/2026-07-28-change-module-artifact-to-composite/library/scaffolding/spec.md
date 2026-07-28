# Feature: Central Library Repo Scaffolding

Delivers the full initial scaffolding for the central module-library repo
`exasol-labs/preprocessor-library`. These deliverables are authored DIRECTLY in
that separate repo, NOT in the framework repo.

This delta records the composite contract's effect on the library: the two
existing ergonomics modules stay untouched as N=1 modules, the module template
documents `[[objects]]`, and the generated index moves to `schema_version` 2.

## Background

* `cast_shorthand` and `trailing_comma` remain single-statement modules and
  their `module.toml` files are NOT edited by this delta. Because `[[objects]]`
  is optional for a single-statement artifact, both manifests stay conformant
  exactly as committed, and their `sha256` values stay valid because the digest
  is unaffected by the contract change. Preserving these two untouched is the
  regression proof that N=1 is a true degenerate case.
* `modules/_template/` documents the `[[objects]]` array: when it MUST be
  declared (any artifact carrying more than one statement), the `type` and
  fully-qualified `name` shape, the `_V<N>`-suffix requirement on every object,
  and the `--/` … `/` statement-separation convention.
* `CONTRIBUTING.md` gains guidance for contributing a composite module: declare
  every object, keep the artifact runnable by hand in EXAplus, and expect CI to
  reject any drift between the declared and parsed inventories.
* The regenerated `registry/index.json` declares `schema_version` 2 and carries
  an `objects` list on every library entry, single-statement entries included.
* Library CI additionally asserts, per module, that the declared inventory
  matches the artifact's parsed inventory — the same accept/reject discipline
  it already applies to `sha256`.

## Scenarios

<!-- DELTA:NEW -->
### Scenario: The ergonomics modules remain conformant with no manifest edit

* *GIVEN* the library repo's `modules/cast_shorthand/` and `modules/trailing_comma/` directories exactly as committed, each with a single-statement artifact and no `[[objects]]` array
* *WHEN* each module is validated against the composite contract
* *THEN* both modules MUST validate as conformant
* *AND* neither `module.toml` MUST require an `[[objects]]` array
* *AND* each declared `sha256` MUST still match its artifact bytes
<!-- /DELTA:NEW -->

<!-- DELTA:NEW -->
### Scenario: The module template documents the object inventory and statement separation

* *GIVEN* the library repo's `modules/_template/` skeleton
* *WHEN* its contents are inspected
* *THEN* its `module.toml` MUST document the `[[objects]]` array with a `type` and a fully-qualified `name` per object
* *AND* it MUST state that `[[objects]]` is REQUIRED once the artifact carries more than one statement
* *AND* it MUST document the `--/` … `/` statement-separation convention and the `_V<N>` suffix requirement on every object
<!-- /DELTA:NEW -->

<!-- DELTA:CHANGED -->
### Scenario: The generated index lists every library module

* *GIVEN* the library repo's `registry/index.json`
* *WHEN* it is read
* *THEN* it MUST contain an entry for `cast_shorthand` and `trailing_comma`
* *AND* it MUST declare a top-level `schema_version` of 2
* *AND* every entry MUST carry an `objects` list, single-statement entries included
* *AND* regenerating the index from the library repo's `modules/` MUST reproduce it byte-for-byte
<!-- /DELTA:CHANGED -->

<!-- DELTA:CHANGED -->
### Scenario: The CI workflow tests modules and checks index sync

* *GIVEN* the library repo's `.github/workflows/` CI workflow file
* *WHEN* its contents are inspected
* *THEN* it MUST run each module's `tests/` against a docker Exasol with the framework installed
* *AND* it MUST regenerate the registry index and fail when the committed index is out of sync
* *AND* it MUST fail when a module's declared object inventory disagrees with its artifact's parsed inventory
<!-- /DELTA:CHANGED -->
