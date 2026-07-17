# Plan: add-module-ecosystem

## Summary

Turns the framework into the sole owner of Exasol's single preprocessor slot and
the multiplexer that dispatches to every registered syntax provider, by shipping
a stable module contract, a `preproc module` CLI, a federated registry, and a
DB-side module lifecycle with provenance — then proves the whole path end-to-end
by migrating the existing ergonomics modules out of the framework into the
separate `exasol-labs/preprocessor-library` repo as the first two modules. This
plan spans two git repos (see Repository Layout).

## Design

### Context

Exasol exposes exactly ONE `sql_preprocessor_script` slot (one system-wide, one
per session). Every preprocessor project today simply grabs it — this framework
sets `PREPROC_RT.MASTER`; others (e.g. exasol-semantic-views) clear and set it to
their own script — so two independent preprocessor projects cannot coexist: last
writer wins. The framework's core value is being the sole slot-owner that
MULTIPLEXES to every registered provider via MASTER dispatch. The module
ecosystem operationalizes that: it gives anyone a stable, scalable way to hook a
syntax provider into the one slot without fighting for it.

The registration mechanism already exists (`PREPROC.RULE_ADD` /
`PREPROC ADD RULE`, MASTER dispatch, the `_V<N>` versioning discipline, the
profile generate/retire/reclaim machinery). What is missing is everything
*around* a module: a contract authors target, a way to discover and fetch
modules, integrity/trust, an air-gap-safe deployment path, provenance, and a
disciplined update flow. This plan adds exactly that and nothing that duplicates
the existing dispatch engine.

- **Goals**
  - One stable integration contract: a module is `fn(text) -> nil|string` in a
    Lua script + a registration rule + a `module.toml` manifest. Deployment mode
    is orthogonal metadata, not a second contract.
  - A `preproc module` CLI (list/search/add/update/remove/bundle/validate/sync)
    that is the air-gap bridge: it talks to GitHub and to the DB; the DB never
    fetches code.
  - A federated registry index over the central library (auto-indexed) plus
    external repos (curated PR-an-entry), pinned by tag and sha256-verified.
  - DB-side provenance (`PREPROC.MODULES`) and a never-mutate-a-live-module
    update flow (deploy `_V<N+1>` → repoint rule → retire → guarded reclaim)
    mirroring the profile lifecycle.
  - Idempotent declarative operation (`sync` / `RULE_ENSURE`) that closes
    BUG-008.
  - The full initial scaffolding for the central library repo
    `exasol-labs/preprocessor-library` (a separate repo, local checkout
    `/home/nc/Github/preprocessor-library`), with ergonomics migrated in as the
    two proving modules.

- **Non-Goals**
  - The actual exasol-semantic-views integration — the contract accommodates a
    `self-deployed` + register-only module, but wiring their subsystem in needs
    their team and is a documented follow-on spike.
  - A fully extracted, pip-installable, independently-versioned test kit — this
    slice ships a lightweight CI bridge (the library depends on the framework
    package for connection/install helpers); the standalone published harness is
    a follow-on.
  - Any change to the framework's own `control_sugar_v1` sugar — it stays in the
    framework as engine infrastructure, it is not a module.
  - confd — untouched by this plan entirely.
  - Automated crawling of external repos — trust is a human PR review.

### Decision

The full ADR set (ADR-039 … ADR-048) is recorded in
`specs/_plans/add-module-ecosystem/decision-log.md`. The load-bearing choices:

#### Architecture

```
 Author repo / central library repo (GitHub)           Operator CLI machine            Exasol DB (no egress)
 ┌───────────────────────────────────┐        ┌──────────────────────────────┐   ┌────────────────────────┐
 │ modules/<name>/                    │        │ preproc module ...           │   │ PREPROC_RT.MASTER (slot)│
 │   <name>_v<N>.sql  (the artifact)  │  HTTPS │  list/search  ─ registry read│   │ dispatches rules ──────▶│
 │   module.toml (contract manifest)  │◀──────▶│  add/update   ─ fetch+verify │   │ TRANSLATE/EXPAND/REWRITE│
 │   README.md  tests/                │        │  bundle       ─ offline .sql │   │                         │
 │ registry/index.json (generated)    │        │  validate     ─ scratch smoke│SQL│ PREPROC.CONFIG (rules)  │
 │ registry/external/<name>.toml (PR) │        │  sync         ─ converge     │──▶│ PREPROC.MODULES (prov.) │
 └───────────────────────────────────┘        └──────────────────────────────┘   │ REFRESH_CORE (lifecycle)│
        CI: test modules + regen index                 the air-gap bridge         └────────────────────────┘
```

Data flow: the CLI reads the federated index (online HTTPS or offline local
checkout), resolves a module, verifies its `sha256` for library-deployed
artifacts, then over SQL deploys the `_V<N>` script, registers the rule via the
idempotent `PREPROC.RULE_ENSURE`, writes a `PREPROC.MODULES` provenance row, and
refreshes. MASTER dispatches through the rule exactly as it does today. For
air-gapped databases, `preproc module bundle` emits a self-contained `.sql` to
hand-carry, using the same `--/`…`/` delimiter pattern as the engine's
`release.sql`.

#### Patterns

| Pattern | Where | Why |
|---------|-------|-----|
| One contract, many providers | `module/contract` | Rejecting a two-kinds split keeps the SDK surface minimal; deploy_mode is metadata |
| Federated index + curated entry | `module/registry` | Central library auto-indexes; external repos enter by human-reviewed PR (trust gate) |
| CLI is the air-gap bridge | `module/cli` | DB never fetches (ADR-006/007); offline `bundle` + `--registry <path>` cover no-egress DBs |
| Generational retire/reclaim | `module/lifecycle` | Reuses the profile never-same-refresh-drop invariant (ADR-022) for `_V<N>` versions |
| Idempotent ensure | `module/lifecycle`, `module/cli` `sync` | `RULE_ENSURE` closes BUG-008 so declarative re-apply never stacks duplicates |
| Move, don't fork | `library/scaffolding`, ergonomics REMOVED | Ergonomics migrates out cleanly (it was never in the engine bundle) — a copy left behind would reintroduce drift |

### Consequences

| Decision | Alternatives Considered | Rationale |
|----------|------------------------|-----------|
| Registry hosted IN the central library repo | Dedicated `preprocessor-registry` repo; file in framework repo | Library CI already has all manifests locally (trivial, always-in-sync auto-index); framework hosting would couple engine releases to registry churn and invert ownership; a dedicated repo adds a third repo + cross-repo sync for no MVP benefit. Index format is repo-agnostic, so a future split is lift-and-shift |
| Clean move of ergonomics + README pointer | Deprecation shim / keep a copy in framework | Ergonomics was never in `release.sql`/`deploy_all.sql` (examples-only), so removal changes no engine bundle; keeping a copy reintroduces the exact drift the ecosystem removes; ergonomics is the proving case for "the library is the home for modules" |
| Deploy/fetch in CLI, DB stores provenance only | DB-side fetch; profile-style DB codegen of modules | Air-gap is a hard constraint — the DB must never reach the network (ADR-006/007); the DB is the management + provenance surface, the CLI machine is the bridge |
| `RULE_ENSURE` added alongside `RULE_ADD` | Make `RULE_ADD` itself idempotent | Preserves `RULE_ADD`'s documented allocate-new behaviour and existing tests; `sync` gets a purpose-built idempotent surface |
| Author library deliverables directly in the existing `exasol-labs/preprocessor-library` repo | A `library-staging/` dir inside the framework repo; prose file manifest only | The repo now exists, so there is no need to stage-and-copy; authoring in place gives real luacheck-/pytest-able files with the correct git home. The plan therefore spans two repos (see Repository Layout) |
| Two modules (`cast-shorthand`, `trailing-comma`) | One combined `ergonomics` module | Each is a distinct Lua script + function + rule + `module.toml`; the contract is one-per-module and they are independently opt-in; two modules is the honest proving case for the registry |
| Test kit as follow-on with a CI bridge now | Extract pip-installable harness in this slice | Extracting + publishing + versioning a package would bloat the slice; the library CI can depend on the framework package for connection/install helpers today |

## Features

| Feature | Status | Spec |
|---------|--------|------|
| `module/contract` | NEW | `specs/_plans/add-module-ecosystem/module/contract/spec.md` |
| `module/registry` | NEW | `specs/_plans/add-module-ecosystem/module/registry/spec.md` |
| `module/cli` | NEW | `specs/_plans/add-module-ecosystem/module/cli/spec.md` |
| `module/lifecycle` | NEW | `specs/_plans/add-module-ecosystem/module/lifecycle/spec.md` |
| `library/scaffolding` | NEW | `specs/_plans/add-module-ecosystem/library/scaffolding/spec.md` |
| `ergonomics/cast-shorthand` | REMOVED | `specs/_plans/add-module-ecosystem/ergonomics/cast-shorthand/spec.md` |
| `ergonomics/trailing-comma` | REMOVED | `specs/_plans/add-module-ecosystem/ergonomics/trailing-comma/spec.md` |

## Requirements

| Requirement | Details |
|-------------|---------|
| Contract SDK doc | `docs/module-authoring.md` documents the `fn(text)->nil\|string` contract, `module.toml` schema, `_V<N>` discipline, and deploy modes |
| Air-gap invariant | No code path added by this plan makes the database perform a network fetch |
| Backward compatibility | `preproc install` and `preproc bundle` continue to work; `RULE_ADD` behaviour is unchanged |
| Provenance visibility | Non-admin users can read `PREPROC_RT.INSTALLED_MODULES` (schema-level PUBLIC grants, ADR-002) |

## Dependencies

- Existing framework internals: `REFRESH_CORE.rebuild()`, `PREPROC.RULE_ADD`,
  `PREPROC.CONFIG`/`RESOLUTION`/`PROFILES`, `src/preproc/{install,bundle,connection}.py`.
- `tomllib` (stdlib, Python 3.11+) for reading `module.toml`; a TOML writer is
  not required (external entries are hand-authored). JSON (stdlib) for the index.
- A docker Exasol for the library repo's CI (same image the framework tests use).

## Repository Layout (this plan spans TWO repos)

This plan is implemented across two git repositories. The implement phase MUST
commit each file to the repo indicated by its path prefix:

- **Framework repo** (this repo, `preprocessor-framework`): the module contract
  spec/docs, the `preproc module` CLI, registry consumption, the DB
  lifecycle/provenance SQL + Lua, the count-test updates, and the REMOVAL of the
  ergonomics sources. Tasks below with no repo prefix are framework-repo tasks.
- **Library repo** (`exasol-labs/preprocessor-library`, local checkout
  `/home/nc/Github/preprocessor-library`, currently `LICENSE` + `README.md`
  only): all `library/scaffolding` deliverables — `modules/`, `module.toml`s,
  module `tests/`, `registry/index.json`, `registry/external/`, `CONTRIBUTING.md`,
  the CI workflow, and the migrated ergonomics modules. Tasks below prefixed
  `[library-repo]` are authored and committed in that checkout, NOT here.

The registry index lives at `registry/index.json` in the library repo (the
hosting recommendation, now with a concrete home). The framework CLI consumes it
via `--registry /home/nc/Github/preprocessor-library` (offline local checkout)
or a pinned raw URL (online).

## Migration

Files LEAVE the framework repo (removed here) and are authored fresh in the
library repo checkout:

| Removed from framework repo | Authored in library repo (`/home/nc/Github/preprocessor-library`) |
|-----------------------------|-------------------------------------------------------------------|
| `examples/ergonomics_cast.sql` | `modules/cast-shorthand/cast_shorthand_v1.sql` |
| `sql/lua/ergonomics_cast_v1.lua` | folded into the module artifact + `modules/cast-shorthand/tests/` |
| `tests/test_ergonomics_cast.py` | `modules/cast-shorthand/tests/` |
| `examples/ergonomics_trailing_comma.sql` | `modules/trailing-comma/trailing_comma_v1.sql` |
| `sql/lua/ergonomics_trailing_comma_v1.lua` | folded into the module artifact + `modules/trailing-comma/tests/` |
| `tests/test_ergonomics_trailing_comma.py` | `modules/trailing-comma/tests/` |
| `specs/ergonomics/*` (recorded) | removed from framework specs on record; carried as module READMEs |

Bundle/DDL consequence: adding `PREPROC.MODULES` (one table DDL + one view) and
the new admin scripts (`RULE_ENSURE`, `MODULE_REGISTER`, `MODULE_RETIRE`,
`MODULE_RECLAIM`) changes the statement counts asserted by
`tests/test_cli_bundle.py` (`test_bundle_count_matches_statements`) and the
release-bundle assertions. These must be updated, and `release.sql` regenerated
by CI. Ergonomics removal does NOT affect these counts (it was examples-only).

## Implementation Tasks

- [ ] 1.1 Author `docs/module-authoring.md` and the `module.toml` schema reference (contract SDK surface)
- [ ] 1.2 Add `src/preproc/module/manifest.py` — parse `module.toml`, validate against the contract (phase, version↔`_V<N>`, deploy-mode field rules), verify artifact is a single `CREATE OR REPLACE` for `script_name`, sha256 verify
- [ ] 1.3 Write `tests/test_module_contract.py`
- [ ] 2.1 Add `src/preproc/module/registry.py` — read local checkout / online index, deterministic index generation (sort by name,version), merge `modules/*/module.toml` + `registry/external/*.toml`, sha256 verify, sync-drift check
- [ ] 2.2 Write `tests/test_module_registry.py`
- [ ] 3.1 Add `sql/install/22_modules_table.sql` (`PREPROC.MODULES`) and the `PREPROC_RT.INSTALLED_MODULES` view (extend `sql/install/40_views.sql`)
- [ ] 3.2 Add `sql/lua/rule_ensure.lua` (`PREPROC.RULE_ENSURE`) — idempotent add on the natural rule key; return existing `rule_id` when present [expert]
- [ ] 3.3 Add `sql/lua/module_register.lua` (`PREPROC.MODULE_REGISTER`) — idempotent provenance upsert
- [ ] 3.4 Add the retire/reclaim lifecycle (`sql/lua/module_retire.lua`, `sql/lua/module_reclaim.lua`) — mark-retire, and guarded `DROP SCRIPT` only when no enabled CONFIG rule and no non-orphaned PROFILES manifest reference the version [expert]
- [ ] 3.5 Wire the new admin scripts into `src/preproc/install.py` (`_deploy_admin_scripts`) and `src/preproc/bundle.py` (`_script_statements`, `_FULL_SCRIPT_ORDER`, `_RELEASE_SCRIPT_ORDER`)
- [ ] 3.6 Update `tests/test_cli_bundle.py` statement counts and release-bundle assertions for the new table + scripts; regenerate `release.sql`
- [ ] 3.7 Write `tests/test_module_lifecycle.py` [expert]
- [ ] 4.1 Add `src/preproc/module/deploy.py` — deploy a library-deployed artifact + `RULE_ENSURE` + `MODULE_REGISTER` + refresh atomically; self-deployed → register-only (no artifact deploy)
- [ ] 4.2 Add `src/preproc/module/offline_bundle.py` — emit a self-contained `.sql` (reuse `bundle.py` emit/delimiter helpers) with selected artifacts + rule registrations
- [ ] 4.3 Add `src/preproc/module/commands.py` and wire the `module` subparser (`list/search/add/update/remove/bundle/validate/sync`) into `src/preproc/cli.py`
- [ ] 4.4 Implement `sync` convergence — declarative, idempotent (deploy missing, `RULE_ENSURE`, repoint changed, retire dropped, single refresh) [expert]
- [ ] 4.5 Implement `validate` — static checks always; live scratch-deploy smoke test when a DB is configured; tear down scratch
- [ ] 4.6 Write `tests/test_module_cli.py`
- [ ] 5.1 `[library-repo]` Create the library repo skeleton: `modules/_template/`, `registry/external/.gitkeep`, `CONTRIBUTING.md`, `README.md`, `.github/workflows/ci.yml`
- [ ] 5.2 `[library-repo]` Author cast-shorthand → `modules/cast-shorthand/` (`cast_shorthand_v1.sql` from the framework source, `module.toml` + sha256, `README.md`, `tests/`)
- [ ] 5.3 `[library-repo]` Author trailing-comma → `modules/trailing-comma/` (`trailing_comma_v1.sql` from the framework source, `module.toml` + sha256, `README.md`, `tests/`)
- [ ] 5.4 `[library-repo]` Generate `registry/index.json` from the library `modules/`
- [ ] 5.5 `[framework-repo]` Remove ergonomics from the framework: delete `examples/ergonomics_cast.sql`, `examples/ergonomics_trailing_comma.sql`, `sql/lua/ergonomics_cast_v1.lua`, `sql/lua/ergonomics_trailing_comma_v1.lua`, `tests/test_ergonomics_cast.py`, `tests/test_ergonomics_trailing_comma.py`; update `examples/README.md` with the moved-to-library pointer
- [ ] 5.6a `[library-repo]` Write `tests/test_scaffolding.py` verifying the library tree, modules, sha256, index, CI workflow, and CONTRIBUTING (runs in the library repo's CI)
- [ ] 5.6b `[framework-repo]` Write `tests/test_ergonomics_migrated.py` verifying the framework-side removal
- [ ] 6.1 Update `docs/operations.md` and `README.md` with the `preproc module` workflow and the single-slot-multiplexer positioning
- [ ] 6.2 Run luacheck, ruff, full pytest, and `speq plan validate add-module-ecosystem`; document the semantic-views and test-kit follow-ons in `docs/`

## Parallelization

| Parallel Group | Tasks |
|----------------|-------|
| Group A (pure-Python foundation) | 1.1, 1.2 |
| Group B (registry, needs manifest) | 2.1 |
| Group C (DB surface, independent of A/B) | 3.1, 3.2, 3.3, 3.4 |
| Group D (wire DB + count tests, needs C) | 3.5, 3.6 |
| Group E (CLI, needs A+B+C) | 4.1, 4.2, 4.3, 4.4, 4.5 |
| Group F (library-repo scaffolding + framework-repo removal, needs A+B) | 5.1, 5.2, 5.3, 5.4, 5.5 |
| Group G (tests, each needs its subject) | 1.3, 2.2, 3.7, 4.6, 5.6a, 5.6b |
| Group H (docs + final verification, needs all) | 6.1, 6.2 |

Sequential dependencies:
- Group A → Group B (registry parses manifests)
- Group A + B + Group C → Group E (CLI orchestrates manifest, registry, and DB surfaces)
- Group C → Group D (wiring + count tests need the scripts/table)
- Group A + B → Group F (the library `module.toml`/index use the manifest + registry code)
- Group E + F → Group H

## Dead Code Removal

| Type | Location | Reason |
|------|----------|--------|
| Example | `examples/ergonomics_cast.sql` | Migrated to the library as module #1 |
| Example | `examples/ergonomics_trailing_comma.sql` | Migrated to the library as module #2 |
| Lua source | `sql/lua/ergonomics_cast_v1.lua` | Body folded into the library module artifact |
| Lua source | `sql/lua/ergonomics_trailing_comma_v1.lua` | Body folded into the library module artifact |
| Test | `tests/test_ergonomics_cast.py` | Moved to the library module's `tests/` |
| Test | `tests/test_ergonomics_trailing_comma.py` | Moved to the library module's `tests/` |
| Docs rows | `examples/README.md` ergonomics rows | Replaced by a moved-to-library pointer |

## Verification

### Scenario Coverage

| Scenario | Test Type | Test Location | Test Name |
|----------|-----------|---------------|-----------|
| Minimal library-deployed manifest accepted | Unit | `tests/test_module_contract.py` | `test_manifest_minimal_library_valid` |
| Self-deployed manifest without sha256 accepted | Unit | `tests/test_module_contract.py` | `test_manifest_self_deployed_no_sha_valid` |
| Library-deployed missing sha256 rejected | Unit | `tests/test_module_contract.py` | `test_manifest_library_missing_sha_rejected` |
| Version disagrees with script_name suffix rejected | Unit | `tests/test_module_contract.py` | `test_manifest_version_suffix_mismatch_rejected` |
| Unknown phase rejected | Unit | `tests/test_module_contract.py` | `test_manifest_unknown_phase_rejected` |
| Library artifact sha256 mismatch rejected | Unit | `tests/test_module_contract.py` | `test_artifact_sha_mismatch_rejected` |
| Artifact is a single CREATE OR REPLACE | Unit | `tests/test_module_contract.py` | `test_artifact_single_create_statement` |
| Conformant EXPAND returns nil for unrecognised | Integration | `tests/test_module_contract.py` | `test_contract_expand_returns_nil` |
| Conformant TRANSLATE returns input unchanged | Integration | `tests/test_module_contract.py` | `test_contract_translate_returns_input` |
| Index generated deterministically | Unit | `tests/test_module_registry.py` | `test_index_deterministic` |
| Central-library module auto-indexed | Unit | `tests/test_module_registry.py` | `test_index_autoindex_library_module` |
| External entry merged from curated block | Unit | `tests/test_module_registry.py` | `test_index_merge_external_entry` |
| CLI reads local checkout offline | Unit | `tests/test_module_registry.py` | `test_registry_read_local_offline` |
| Library artifact matching sha256 verifies | Unit | `tests/test_module_registry.py` | `test_registry_sha_match` |
| Library artifact mismatched sha256 aborts | Unit | `tests/test_module_registry.py` | `test_registry_sha_mismatch_aborts` |
| CI detects out-of-sync index | Unit | `tests/test_module_registry.py` | `test_index_sync_check_detects_drift` |
| `list` prints entries without DB | Integration | `tests/test_module_cli.py` | `test_cli_module_list` |
| `search` filters by term | Integration | `tests/test_module_cli.py` | `test_cli_module_search` |
| `add` deploys library-deployed + registers rule | Integration | `tests/test_module_cli.py` | `test_cli_module_add_library` |
| `add` self-deployed register-only | Integration | `tests/test_module_cli.py` | `test_cli_module_add_self_deployed` |
| `add` aborts on sha256 mismatch | Integration | `tests/test_module_cli.py` | `test_cli_module_add_sha_mismatch_aborts` |
| `bundle` emits offline artifact, no DB | Integration | `tests/test_module_cli.py` | `test_cli_module_bundle_offline` |
| `validate` statically rejects nonconformant offline | Integration | `tests/test_module_cli.py` | `test_cli_module_validate_static_reject` |
| `validate` smoke-tests against scratch deploy | Integration | `tests/test_module_cli.py` | `test_cli_module_validate_live_smoke` |
| `sync` idempotent — unchanged file changes nothing | Integration | `tests/test_module_cli.py` | `test_cli_module_sync_idempotent` |
| `sync` retires a module dropped from desired file | Integration | `tests/test_module_cli.py` | `test_cli_module_sync_retire` |
| Mutating subcommand exits missing-env code | Integration | `tests/test_module_cli.py` | `test_cli_module_add_missing_env` |
| Registering a module writes a provenance row | Integration | `tests/test_module_lifecycle.py` | `test_module_register_writes_row` |
| Re-registering identical version idempotent | Integration | `tests/test_module_lifecycle.py` | `test_module_register_idempotent` |
| RULE_ENSURE does not duplicate an identical rule | Integration | `tests/test_module_lifecycle.py` | `test_rule_ensure_no_duplicate` |
| RULE_ENSURE creates when none matches | Integration | `tests/test_module_lifecycle.py` | `test_rule_ensure_creates` |
| Update deploys V+1, repoints, marks V retired | Integration | `tests/test_module_lifecycle.py` | `test_module_update_flow` |
| Retired version not dropped while profile live | Integration | `tests/test_module_lifecycle.py` | `test_module_reclaim_blocked_by_profile` |
| Retired version reclaimed once unreferenced | Integration | `tests/test_module_lifecycle.py` | `test_module_reclaim_when_unreferenced` |
| Update with no provenance rejected pre-write | Integration | `tests/test_module_lifecycle.py` | `test_module_update_missing_provenance_rejected` |
| Library repo has required top-level structure | Integration | `[library-repo] tests/test_scaffolding.py` | `test_library_top_level_structure` |
| cast-shorthand present as conformant module | Integration | `[library-repo] tests/test_scaffolding.py` | `test_cast_module_present` |
| trailing-comma present as conformant module | Integration | `[library-repo] tests/test_scaffolding.py` | `test_trailing_comma_module_present` |
| module.toml sha256 matches artifact | Integration | `[library-repo] tests/test_scaffolding.py` | `test_module_sha_matches` |
| Generated index lists every library module | Integration | `[library-repo] tests/test_scaffolding.py` | `test_index_lists_modules` |
| CI workflow tests + checks index sync | Integration | `[library-repo] tests/test_scaffolding.py` | `test_ci_workflow_present` |
| CONTRIBUTING documents both paths | Integration | `[library-repo] tests/test_scaffolding.py` | `test_contributing_documents_paths` |
| ergonomics/cast-shorthand REMOVED from framework | Integration | `[framework-repo] tests/test_ergonomics_migrated.py` | `test_ergonomics_cast_removed_from_framework` |
| ergonomics/trailing-comma REMOVED from framework | Integration | `[framework-repo] tests/test_ergonomics_migrated.py` | `test_ergonomics_trailing_comma_removed_from_framework` |

### Manual Testing

| Feature | Command | Expected Output |
|---------|---------|-----------------|
| `module/registry` | `preproc module list --registry /home/nc/Github/preprocessor-library` | Lists `cast-shorthand` and `trailing-comma` with phase + deploy mode; exit 0, no DB |
| `module/contract` | `preproc module validate cast-shorthand --registry /home/nc/Github/preprocessor-library` | Static conformance passes (manifest + artifact + sha + lint); exit 0 |
| `module/cli` | `preproc module add cast-shorthand --registry /home/nc/Github/preprocessor-library` (DB configured) then `ALTER SESSION SET sql_preprocessor_script = PREPROC_RT.MASTER; SELECT 42::integer AS x` | Deploys + registers; `42::integer` returns `42` with no syntax error |
| `module/lifecycle` | `SELECT name, version, status FROM PREPROC_RT.INSTALLED_MODULES` | One `deployed` row for `cast-shorthand` v1 |
| `module/cli` (air-gap) | `preproc module bundle cast-shorthand trailing-comma --output offline_modules.sql --registry /home/nc/Github/preprocessor-library` | Writes one self-contained `.sql`; exit 0, no DB connection |
| `library/scaffolding` | (in the library repo) regenerate `registry/index.json`, then `preproc module list --registry /home/nc/Github/preprocessor-library` | Regenerated `index.json` is byte-identical to the committed one |
| ergonomics removal | `ls examples/ \| grep ergonomics; echo done` | No ergonomics files; capability restored via `preproc module add` |

### Checklist

| Step | Command | Expected |
|------|---------|----------|
| Sync + deps | `uv sync` | Exit 0 |
| Lint (Lua) | `luacheck sql/lua/` | 0 errors/warnings |
| Lint (Python) | `uv run ruff check .` | 0 errors |
| Format check | `uv run ruff format --check .` | No changes |
| Test suite | `uv run pytest` | 0 failures |
| Plan validation | `speq plan validate add-module-ecosystem` | pass |
