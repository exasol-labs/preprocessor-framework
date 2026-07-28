# Feature: Federated Module Registry

Gives the operator's CLI a single federated index over every discoverable
module — modules hosted in the central library repo (auto-indexed from their
`module.toml` by CI) and modules hosted in external repos (added by a curated
pull request). The registry is the curation and trust boundary: entries are
pinned by tag and library-deployed artifacts carry a `sha256` the CLI verifies
before deployment. The database never reads the registry; the operator's CLI
machine does, online against a pinned index URL or offline against a local
checkout.

The index entry gains the object inventory and the index `schema_version`
bumps from 1 to 2. The inventory must travel in the index because the in-DB
resolver verifies the artifact against it, and because
`PREPROC.MODULE_CATALOG` — which never fetches an artifact — is the
pre-install review surface for what a composite module would create.

## Background

* The registry is a generated index file (`index.json`) that lists one entry per
  known module version. Each entry carries at least: `name`, `version`, `phase`,
  `script_name`, `function`, `deploy_mode`, `min_framework`, `description`,
  `suggested_scope`, `source` (`repo` and `ref`, i.e. a pinned tag), and — for
  `library-deployed` entries — `sha256` and the artifact path relative to its
  source. The index file carries a top-level `schema_version`.
* The index is FEDERATED over two source classes: (1) central-library modules,
  auto-indexed by CI from every `modules/<name>/module.toml`; (2) external-repo
  modules, each represented by one curated block under `registry/external/<name>.toml`
  (a single repo@tag + sha + manifest metadata), merged into the same index.
  Human review of that PR is the trust gate — the registry is NEVER populated by
  automated crawling of arbitrary repositories.
* The CLI MUST read the registry in two interchangeable ways: ONLINE, by
  fetching the pinned `index.json` over HTTPS from the operator's CLI machine;
  and OFFLINE, by reading `index.json` (or the `modules/` tree) from a local
  checkout given via `--registry <path>`. The database is never a party to
  either path.
* External entries are pinned by `ref` (a tag), never a floating branch, so a
  given registry state resolves deterministically to the same artifacts.
* For a `library-deployed` entry the CLI MUST verify the fetched artifact's
  `sha256` against the entry before the artifact is deployed; a mismatch MUST
  abort without deploying.
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
* A `self-deployed` entry carries no artifact and therefore no `objects` list;
  its backing objects are its own installer's concern.
* The generated index MUST be deterministic: the same set of `module.toml` and
  `registry/external/*.toml` inputs MUST produce a byte-identical `index.json`
  (entries sorted by `name` then `version`, and each entry's `objects` list
  emitted in the manifest's declared order), so the index can be committed and
  diffed and CI can assert it is in sync with its sources.
* Registry reads are pure lookups: listing and searching MUST NOT require a
  database connection.

## Scenarios

### Scenario: The index is generated deterministically from module manifests

* *GIVEN* a `modules/` tree containing two modules' `module.toml` files and one `registry/external/<name>.toml` block
* *WHEN* the index generator runs
* *THEN* it MUST emit an `index.json` containing exactly three entries
* *AND* the entries MUST be ordered by `name` then `version`
* *AND* re-running the generator on the same inputs MUST produce a byte-identical file

### Scenario: A central-library module is auto-indexed from its manifest

* *GIVEN* a `modules/cast_shorthand/module.toml` with `deploy_mode = "library-deployed"` and a `sha256`
* *WHEN* the index generator runs
* *THEN* the resulting entry MUST carry the manifest's `name`, `version`, `phase`, `script_name`, `function`, `sha256`, `objects`, and `source`
* *AND* the entry MUST record the artifact path relative to its source repo

### Scenario: An external-repo entry is merged from a curated block

* *GIVEN* a `registry/external/semantic_views.toml` block declaring `deploy_mode = "self-deployed"`, a `source` repo and tag `ref`, and manifest metadata
* *WHEN* the index generator runs
* *THEN* the resulting index MUST contain the external entry alongside the library entries
* *AND* the external entry MUST retain its pinned `ref`

### Scenario: The CLI reads the registry from a local checkout offline

* *GIVEN* a local checkout path containing a valid `index.json`
* *WHEN* the CLI reads the registry with `--registry <path>` and no network access
* *THEN* the CLI MUST load every entry from the local file
* *AND* the CLI MUST NOT attempt any network fetch

### Scenario: A library-deployed artifact with a matching sha256 verifies

* *GIVEN* a registry entry with a `sha256` and the corresponding artifact bytes
* *WHEN* the CLI verifies the artifact against the entry
* *THEN* verification MUST succeed

### Scenario: A library-deployed artifact with a mismatched sha256 aborts

* *GIVEN* a registry entry with a `sha256` and artifact bytes that hash to a different digest
* *WHEN* the CLI verifies the artifact against the entry
* *THEN* verification MUST fail
* *AND* the CLI MUST NOT proceed to deploy the artifact

### Scenario: CI detects an index that is out of sync with its sources

* *GIVEN* a `modules/` tree whose `index.json` was not regenerated after a `module.toml` changed
* *WHEN* CI regenerates the index and compares it to the committed file
* *THEN* the comparison MUST report a difference
* *AND* CI MUST fail the check

### Scenario: A composite module's index entry carries its object inventory

* *GIVEN* a `modules/<name>/module.toml` declaring an `[[objects]]` array of a Lua entry script and two Python UDFs
* *WHEN* the index generator runs
* *THEN* the resulting entry MUST carry an `objects` list of exactly those three objects with their types and fully-qualified names
* *AND* the list order MUST match the manifest's declared order
* *AND* re-running the generator on the same inputs MUST produce a byte-identical file

### Scenario: The generated index declares schema_version 2

* *GIVEN* a `modules/` tree containing at least one module manifest
* *WHEN* the index generator runs
* *THEN* the emitted `index.json` MUST declare a top-level `schema_version` of 2

### Scenario: A single-statement manifest with no objects array still yields an inventory

* *GIVEN* a `modules/cast_shorthand/module.toml` with `script_name = "PREPROC_RT.CAST_SHORTHAND_V1"` and no `[[objects]]` array
* *WHEN* the index generator runs
* *THEN* the resulting entry's `objects` list MUST contain exactly one object named `PREPROC_RT.CAST_SHORTHAND_V1`
* *AND* the manifest MUST NOT require editing for the generator to succeed

### Scenario: A schema-version-1 index is read as single-statement entries

* *GIVEN* a published `index.json` declaring `schema_version` 1 whose entries carry no `objects` list
* *WHEN* the CLI reads that registry
* *THEN* the CLI MUST load every entry without error
* *AND* each entry's inventory MUST be interpreted as the single object named by its `script_name`
* *AND* the CLI MUST NOT reject the index for the absent `objects` field
