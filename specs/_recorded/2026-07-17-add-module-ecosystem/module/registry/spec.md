# Feature: Federated Module Registry

Gives the operator's CLI a single federated index over every discoverable
module — modules hosted in the central library repo (auto-indexed from their
`module.toml` by CI) and modules hosted in external repos (added by a curated
pull request). The registry is the curation and trust boundary: entries are
pinned by tag and library-deployed artifacts carry a `sha256` the CLI verifies
before deployment. The database never reads the registry; the operator's CLI
machine does, online against a pinned index URL or offline against a local
checkout.

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
* The generated index MUST be deterministic: the same set of `module.toml` and
  `registry/external/*.toml` inputs MUST produce a byte-identical `index.json`
  (entries sorted by `name` then `version`), so the index can be committed and
  diffed and CI can assert it is in sync with its sources.
* Registry reads are pure lookups: listing and searching MUST NOT require a
  database connection.

## Scenarios

<!-- DELTA:NEW -->
### Scenario: The index is generated deterministically from module manifests

* *GIVEN* a `modules/` tree containing two modules' `module.toml` files and one `registry/external/<name>.toml` block
* *WHEN* the index generator runs
* *THEN* it MUST emit an `index.json` containing exactly three entries
* *AND* the entries MUST be ordered by `name` then `version`
* *AND* re-running the generator on the same inputs MUST produce a byte-identical file
<!-- /DELTA:NEW -->

<!-- DELTA:NEW -->
### Scenario: A central-library module is auto-indexed from its manifest

* *GIVEN* a `modules/cast-shorthand/module.toml` with `deploy_mode = "library-deployed"` and a `sha256`
* *WHEN* the index generator runs
* *THEN* the resulting entry MUST carry the manifest's `name`, `version`, `phase`, `script_name`, `function`, `sha256`, and `source`
* *AND* the entry MUST record the artifact path relative to its source repo
<!-- /DELTA:NEW -->

<!-- DELTA:NEW -->
### Scenario: An external-repo entry is merged from a curated block

* *GIVEN* a `registry/external/semantic-views.toml` block declaring `deploy_mode = "self-deployed"`, a `source` repo and tag `ref`, and manifest metadata
* *WHEN* the index generator runs
* *THEN* the resulting index MUST contain the external entry alongside the library entries
* *AND* the external entry MUST retain its pinned `ref`
<!-- /DELTA:NEW -->

<!-- DELTA:NEW -->
### Scenario: The CLI reads the registry from a local checkout offline

* *GIVEN* a local checkout path containing a valid `index.json`
* *WHEN* the CLI reads the registry with `--registry <path>` and no network access
* *THEN* the CLI MUST load every entry from the local file
* *AND* the CLI MUST NOT attempt any network fetch
<!-- /DELTA:NEW -->

<!-- DELTA:NEW -->
### Scenario: A library-deployed artifact with a matching sha256 verifies

* *GIVEN* a registry entry with a `sha256` and the corresponding artifact bytes
* *WHEN* the CLI verifies the artifact against the entry
* *THEN* verification MUST succeed
<!-- /DELTA:NEW -->

<!-- DELTA:NEW -->
### Scenario: A library-deployed artifact with a mismatched sha256 aborts

* *GIVEN* a registry entry with a `sha256` and artifact bytes that hash to a different digest
* *WHEN* the CLI verifies the artifact against the entry
* *THEN* verification MUST fail
* *AND* the CLI MUST NOT proceed to deploy the artifact
<!-- /DELTA:NEW -->

<!-- DELTA:NEW -->
### Scenario: CI detects an index that is out of sync with its sources

* *GIVEN* a `modules/` tree whose `index.json` was not regenerated after a `module.toml` changed
* *WHEN* CI regenerates the index and compares it to the committed file
* *THEN* the comparison MUST report a difference
* *AND* CI MUST fail the check
<!-- /DELTA:NEW -->
