# Feature: Federated Module Registry

Gives the operator's CLI a single federated index over every discoverable
module. The registry is the curation and trust boundary.

This delta adds the object inventory to the index entry and bumps the index
`schema_version` from 1 to 2. The inventory must travel in the index because the
in-DB resolver verifies the artifact against it, and because
`PREPROC.MODULE_CATALOG` — which never fetches an artifact — is the pre-install
review surface for what a composite module would create.

## Background

* An index entry for a `library-deployed` module carries an `objects` list
  alongside `sha256` and the artifact path: one element per object the artifact
  creates, each with a `type` and a fully-qualified `name`. The list is copied
  from the module's `module.toml` `[[objects]]` array, or derived as the single
  object named by `script_name` when the manifest declares none.
* The index top-level `schema_version` becomes 2. Readers MUST accept both 1 and
  2: a `schema_version = 1` index carries no `objects` list on any entry and
  every entry is interpreted as a single-statement module whose inventory is the
  object named by `script_name`. This is what lets an already-published
  version-1 index keep working unchanged.
* The generator continues to emit a deterministic index: the same set of
  `module.toml` and `registry/external/*.toml` inputs MUST produce a
  byte-identical `index.json`, with entries sorted by `name` then `version` and
  each entry's `objects` list emitted in the manifest's declared order.
* A `self-deployed` entry carries no artifact and therefore no `objects` list;
  its backing objects are its own installer's concern.

## Scenarios

<!-- DELTA:NEW -->
### Scenario: A composite module's index entry carries its object inventory

* *GIVEN* a `modules/<name>/module.toml` declaring an `[[objects]]` array of a Lua entry script and two Python UDFs
* *WHEN* the index generator runs
* *THEN* the resulting entry MUST carry an `objects` list of exactly those three objects with their types and fully-qualified names
* *AND* the list order MUST match the manifest's declared order
* *AND* re-running the generator on the same inputs MUST produce a byte-identical file
<!-- /DELTA:NEW -->

<!-- DELTA:NEW -->
### Scenario: The generated index declares schema_version 2

* *GIVEN* a `modules/` tree containing at least one module manifest
* *WHEN* the index generator runs
* *THEN* the emitted `index.json` MUST declare a top-level `schema_version` of 2
<!-- /DELTA:NEW -->

<!-- DELTA:NEW -->
### Scenario: A single-statement manifest with no objects array still yields an inventory

* *GIVEN* a `modules/cast_shorthand/module.toml` with `script_name = "PREPROC_RT.CAST_SHORTHAND_V1"` and no `[[objects]]` array
* *WHEN* the index generator runs
* *THEN* the resulting entry's `objects` list MUST contain exactly one object named `PREPROC_RT.CAST_SHORTHAND_V1`
* *AND* the manifest MUST NOT require editing for the generator to succeed
<!-- /DELTA:NEW -->

<!-- DELTA:NEW -->
### Scenario: A schema-version-1 index is read as single-statement entries

* *GIVEN* a published `index.json` declaring `schema_version` 1 whose entries carry no `objects` list
* *WHEN* the CLI reads that registry
* *THEN* the CLI MUST load every entry without error
* *AND* each entry's inventory MUST be interpreted as the single object named by its `script_name`
* *AND* the CLI MUST NOT reject the index for the absent `objects` field
<!-- /DELTA:NEW -->

<!-- DELTA:CHANGED -->
### Scenario: A central-library module is auto-indexed from its manifest

* *GIVEN* a `modules/cast_shorthand/module.toml` with `deploy_mode = "library-deployed"` and a `sha256`
* *WHEN* the index generator runs
* *THEN* the resulting entry MUST carry the manifest's `name`, `version`, `phase`, `script_name`, `function`, `sha256`, `objects`, and `source`
* *AND* the entry MUST record the artifact path relative to its source repo
<!-- /DELTA:CHANGED -->
