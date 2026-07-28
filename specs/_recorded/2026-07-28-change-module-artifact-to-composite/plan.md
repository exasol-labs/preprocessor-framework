# Plan: change-module-artifact-to-composite

## Summary

Generalise the module artifact from exactly one `CREATE OR REPLACE LUA SCRIPT`
statement to a single `.sql` file carrying 1..N statements, so a module that is
the front door to a whole subsystem can be `library-deployed` and installed from
the registry instead of being forced into `self-deployed`. N=1 stays valid
unchanged and no third `deploy_mode` is introduced.

## Design

### Context

`specs/module/contract/spec.md:10` promises "exactly ONE contract regardless of
whether the module is a tiny transform **or the front door to a whole
subsystem**", but `specs/module/contract/spec.md:100-105` requires the artifact
to be exactly one `CREATE OR REPLACE LUA SCRIPT`. Both cannot hold. Any module
needing a Python UDF or a table is silently pushed to `self-deployed`, where the
library can only carry a pointer and never deliver the code — making
`preprocessor-library` a phone book rather than a distribution channel for
precisely the modules most worth distributing.

The stated justification for the one-statement rule
(`docs/module-authoring.md:70` — "trivially hand-carryable for air-gapped
deployment and trivially hashed for integrity verification") does not survive
scrutiny: a multi-statement file is equally hand-carryable and hashes
identically. The real cause is mechanical — `sql/lua/module_install.lua:230`
executes a single `pquery(artifact_text)`, and `pquery` takes one statement.

There is also a latent bug. `src/preproc/module/manifest.py:58`
`_CREATE_SCRIPT_RE` matches only `CREATE OR REPLACE LUA SCRIPT`, so an artifact
with 1 Lua script + N Python UDFs **passes** CLI validation today and fails
later at DB install when `pquery` receives multiple statements. The validator
and the executor already disagree about what an artifact is.

- **Goals** — one contract that genuinely covers subsystem modules; N=1 stays
  byte-compatible; the air-gap hand-carry invariant survives; `sha256` unchanged;
  reclaim stays exact; the CLI and in-DB paths stay end-state byte-identical.
- **Non-Goals** — no third `deploy_mode`; `self-deployed` untouched; no
  packaged-archive format; no cross-statement transactional rollback (Exasol DDL
  cannot provide it); migrating `confd-via-sql` onto the contract.

### Decision

#### Architecture

```
module.toml  [[objects]]  ──┐
                            ├──▶ declared inventory ─┐
artifact .sql (1..N stmts) ─┘                        ├──▶ AGREE? ──▶ accept
        │                                            │              else reject
        └──▶ splitter ──▶ statements ──▶ derived ────┘
                              inventory

        CLI path                          in-DB path
   ┌──────────────────┐            ┌───────────────────────┐
   │ artifact.py      │  verbatim  │ module_resolver_v1.sql│
   │ (canonical       │═══════════▶│ (embedded copy,       │
   │  Python splitter)│  CI-checked│  CI byte-identical)   │
   └────────┬─────────┘            └───────────┬───────────┘
            │                                  │ emits chunks + inventory
            │                                  ▼
            │                      ┌───────────────────────┐
            │                      │ module_install.lua    │
            │                      │ (Lua splitter)        │
            │                      └───────────┬───────────┘
            │                                  │
            └──────────┬───────────────────────┘
                       ▼
        tests/fixtures/artifact_split_corpus.json
        (both implementations pinned to the same expected split)
```

Three deployment sites, but only **two** real implementations: the canonical
Python splitter is embedded verbatim into the resolver UDF and CI asserts the
copy is byte-identical, so Python-vs-Python divergence is structurally
impossible. Python-vs-Lua divergence is caught by the shared conformance corpus.

#### Patterns

| Pattern | Where | Why |
|---------|-------|-----|
| Declare + derive + compare | manifest, resolver, CI | Same accept/reject discipline as `sha256`; makes inventory drift impossible |
| EXAplus `--/` … `/` block markers | artifact grammar | Already emitted by `release.sql` and `build.py`; keeps the file hand-runnable, preserving the air-gap story |
| Abort-before-write pre-flight | `module_install.lua`, `deploy.py` | Mirrors the existing "ANY resolver row with status = ERROR aborts HERE" pattern; primary atomicity mechanism |
| Compensating cleanup | `module_install.lua`, `deploy.py` | DDL cannot roll back; the inventory is the undo list |
| Rule registration last | install order | Rule is DML and rollback-able; a partial install is inert, not half-active |
| Verbatim embed + CI equality check | `artifact.py` → resolver UDF | Collapses two Python copies to one source of truth |
| Idempotent additive column | `25_column_upgrades.sql` | Established pattern for `orphaned_at`, `source_url`, `library_version` |

### Consequences

| Decision | Alternatives Considered | Rationale |
|----------|------------------------|-----------|
| Inventory declared in `module.toml` AND parse-verified (D1) | Derive-only (no declaration); declare-only (no parse) | Derive-only leaves nothing human-reviewable before install and forces reclaim to re-parse an artifact the DB never retains; declare-only permits silent drift. Declare+verify gets both, and matches the `sha256` discipline already in place |
| `[[objects]]` optional when N=1, required when N>1 | Required always | Required-always would force edits to `cast_shorthand` and `trailing_comma`, breaking the stated "keep working untouched" invariant. Optional-at-N=1 derives the inventory from `script_name`, which is exactly today's check |
| Every object carries `_V<N>` (D2) | Version only the entry script | Unversioned companions collide across generations and make reclaim ambiguous; ADR-019's discipline is meaningless if a v2 install overwrites a live v1 companion |
| Any writable schema (D3) | Restrict to `PREPROC_RT` / `PREPROC` | A subsystem module legitimately owns its own schema. Safe because the inventory is declared and parse-verified, so reclaim knows exactly what it owns; `MODULE_CATALOG` surfaces the inventory pre-install as the review gate |
| `--/` … `/` separation (D4) | Bare `;` splitting; JSON/TOML statement array; a packaged archive | Bare `;` cannot survive semicolons inside Lua/Python bodies. A structured container stops the file being hand-runnable, killing the air-gap story. `--/` is already emitted by `release.sql` and `build.py`, and `module_install.lua` already strips a lone `/` |
| Atomicity constructed from pre-flight + cleanup + idempotent re-run + rule-last | Wrap the install in a transaction | Exasol DDL implicitly commits and cannot be rolled back — the framework's own `refresh_core.lua:111,117,742` and `module_reclaim.lua:68` document this and work around it. A transaction would be a false guarantee |
| Canonical Python splitter embedded verbatim into the UDF, CI-checked | Three independent implementations; vendor the `preproc` package into the UDF | Three implementations triples the divergence surface; vendoring is explicitly rejected by `module/db-resolver`. Verbatim embed + equality check gives one source of truth with no packaging change |
| Index `schema_version` 1 → 2, readers accept both | Additive field with no bump; hard break at 2 | `MODULE_CATALOG` depends on `objects` for its pre-install review story, so the field changes entry meaning and warrants the bump the field exists for. Accepting 1 keeps published indexes working |

## Features

| Feature | Status | Spec |
|---------|--------|------|
| module/contract | CHANGED | `module/contract/spec.md` |
| module/db-install | CHANGED | `module/db-install/spec.md` |
| module/db-resolver | CHANGED | `module/db-resolver/spec.md` |
| module/db-catalog | CHANGED | `module/db-catalog/spec.md` |
| module/lifecycle | CHANGED | `module/lifecycle/spec.md` |
| module/cli | CHANGED | `module/cli/spec.md` |
| module/registry | CHANGED | `module/registry/spec.md` |
| library/scaffolding | CHANGED | `library/scaffolding/spec.md` |
| install/schema-layout | CHANGED | `install/schema-layout/spec.md` |

## Requirements

| Requirement | Details |
|-------------|---------|
| Back-compat, manifests | `cast_shorthand` and `trailing_comma` `module.toml` files MUST NOT be edited; their `sha256` values stay valid |
| Back-compat, indexes | A published `schema_version = 1` index MUST keep resolving |
| Back-compat, provenance | A `PREPROC.MODULES` row with NULL `objects` MUST still reclaim its entry script |
| Air-gap | The artifact MUST remain one hand-carryable file, directly runnable in EXAplus; the DB never fetches |
| Integrity | `sha256` MUST stay a digest over the whole file, unaffected by statement count |
| Parity | CLI `add` and `MODULE_INSTALL` MUST reach an end-state byte-identical for the same module |

## Dependencies

None external. All work is in `preprocessor-framework`, except the
`library/scaffolding` deliverables which land in the separate
`exasol-labs/preprocessor-library` checkout at `/home/nc/Github/preprocessor-library`.

## Migration

| Current | New |
|---------|-----|
| Artifact = exactly 1 `CREATE OR REPLACE LUA SCRIPT` | Artifact = 1..N statements, exactly one of which is the entry Lua script |
| `module.toml` has no object inventory | `[[objects]]` array; optional at N=1, required at N>1 |
| `index.json` `schema_version: 1`, no `objects` | `schema_version: 2` with `objects`; readers accept 1 and 2 |
| `PREPROC.MODULES` has no inventory column | Nullable `objects` column via idempotent additive upgrade |
| Reclaim drops one `PREPROC_RT`-owned script | Reclaim drops every object in the recorded inventory, any schema |
| `module_install.lua` single `pquery(artifact_text)` | Split, pre-flight, execute in order, compensating cleanup on failure |

## Implementation Tasks

### Group A — Contract core

- [ ] 1.1 Canonical Python statement splitter + object-derivation in new `src/preproc/module/artifact.py` (`--/` blocks, `;` terminators, lone-`/` strip, comment and string handling) [expert]
- [ ] 1.2 Author the shared conformance corpus `tests/fixtures/artifact_split_corpus.json` covering block-wrapped bodies, semicolons in strings and comments, lone `/`, CRLF, trailing whitespace, and a statement straddling a 2,000,000-char chunk boundary [expert]
- [ ] 1.3 `[[objects]]` parsing and validation in `manifest.py`: type + fully-qualified name, `_V<N>` suffix rule on every object, optional at N=1 and required at N>1
- [ ] 1.4 Replace `verify_artifact_script_name` with inventory verification and retire `_CREATE_SCRIPT_RE`; the new parser MUST recognise every DDL form the contract admits, closing the 1-Lua + N-UDF latent bug [expert]
- [ ] 1.5 Extend `tests/test_module_contract.py` for the contract scenarios

### Group B — Registry and storage

- [ ] 2.1 `registry.py`: emit `objects` per entry, bump `SCHEMA_VERSION` to 2, accept 1 and 2 on read, derive a single-object inventory for v1 entries
- [ ] 2.2 `sql/install/25_column_upgrades.sql`: nullable `PREPROC.MODULES.objects` column; expose it through `PREPROC_RT.INSTALLED_MODULES` in `40_views.sql`
- [ ] 2.3 `PREPROC.MODULE_REGISTER` gains an `objects` argument and persists it
- [ ] 2.4 Extend `tests/test_module_registry.py` and `tests/test_schema_layout.py`

### Group C — Lua splitter and cross-language agreement

- [ ] 3.1 Lua statement splitter in `sql/lua/module_install.lua`, matching the canonical grammar [expert]
- [ ] 3.2 Cross-language conformance test driving both the Python and the Lua splitter over `artifact_split_corpus.json` and asserting statement-for-statement agreement [expert]

### Group D — In-database path

- [ ] 4.1 `module_install.lua`: split, pre-flight all statements and the inventory before the first DDL, execute in file order, compensating reverse-order cleanup on failure, rule registration last [expert]
- [ ] 4.2 `sql/udf/module_resolver_v1.sql`: embed the canonical Python splitter verbatim, replace `_verify_script_name` with inventory verification, emit the derived inventory; CI test asserting the embedded copy is byte-identical to `artifact.py`
- [ ] 4.3 `module_catalog.lua`: report each entry's declared object count, types, and fully-qualified names
- [ ] 4.4 `module_reclaim.lua`: inventory-driven reverse-order drop; widen the `PREPROC_RT`-only guard to the recorded inventory; NULL-inventory rows fall back to `script_name` [expert]
- [ ] 4.5 Extend `tests/test_db_module_install.py`, `tests/test_db_resolver.py`, `tests/test_db_module_catalog.py`, `tests/test_module_lifecycle.py`

### Group E — CLI path

- [ ] 5.1 `deploy.py`: composite execution with the same pre-flight, compensating cleanup, and rule-last ordering as the in-DB path [expert]
- [ ] 5.2 `validate.py`: inventory checks statically; the live check compiles every `SCRIPT` statement under a disposable name inside one throwaway schema, smoke-tests the entry function, and tears that schema down — it never executes a statement of the artifact as written
- [ ] 5.3 `offline_bundle.py`: emit every statement of a composite artifact with its `--/` … `/` delimiters preserved
- [ ] 5.4 Extend `tests/test_module_cli.py` and `tests/test_cli_bundle.py`

### Group F — Documentation and library repo

- [ ] 6.1 Rewrite `docs/module-authoring.md` § The canonical artifact: composite artifact, `[[objects]]`, `_V<N>` on every object, `--/` separation; correct the obsolete one-statement justification
- [ ] 6.2 Library repo: `modules/_template/` documents `[[objects]]`; `CONTRIBUTING.md` gains the composite path; regenerate `registry/index.json` at `schema_version` 2; CI asserts declared-vs-parsed inventory
- [ ] 6.3 Verify `cast_shorthand` and `trailing_comma` validate unchanged against the new contract

## Parallelization

| Parallel Group | Tasks |
|----------------|-------|
| Group A | 1.1, 1.2 (then 1.3, 1.4, 1.5) |
| Group B | 2.1, 2.2, 2.3, 2.4 |
| Group C | 3.1, 3.2 |
| Group D | 4.1, 4.2, 4.3, 4.4, 4.5 |
| Group E | 5.1, 5.2, 5.3, 5.4 |
| Group F | 6.1, 6.2, 6.3 |

Sequential dependencies:
- A → C (the Lua splitter targets the grammar and corpus A defines)
- A → E (the CLI path uses the canonical splitter)
- A, B → D (the in-DB path needs the grammar, the index `objects`, and the `MODULES.objects` column)
- A and B may run concurrently; C and E may run concurrently once A lands
- D → F 6.3 (the back-compat check exercises the finished contract)

## Dead Code Removal

| Type | Location | Reason |
|------|----------|--------|
| Constant | `src/preproc/module/manifest.py:58` `_CREATE_SCRIPT_RE` | Lua-only regex replaced by the full-DDL parser; source of the latent 1-Lua + N-UDF bug |
| Function | `src/preproc/module/manifest.py:267` `_extract_script_names` | Superseded by object derivation over the parsed statement list |
| Function | `sql/udf/module_resolver_v1.sql:364` `_verify_script_name` | Superseded by inventory verification |
| Guard | `sql/lua/module_reclaim.lua:141-145` `PREPROC_RT`-only schema guard | Replaced by the recorded-inventory ownership boundary |
| Comment block | `sql/lua/module_reclaim.lua:55-57` "PREPROC_RT-owned artifacts only" | Documents the removed guard |

## Verification

### Scenario Coverage

| Scenario | Test Type | Test Location | Test Name |
|----------|-----------|---------------|-----------|
| contract: artifact is a single file carrying one or more statements | Unit | `tests/test_module_contract.py` | `test_artifact_accepts_one_or_more_statements` |
| contract: composite artifact with entry script and companions accepted | Unit | `tests/test_module_contract.py` | `test_composite_artifact_with_companions_is_valid` |
| contract: statements separated by block markers and `;` | Unit | `tests/test_module_contract.py` | `test_splitter_separates_block_and_semicolon_statements` |
| contract: declared inventory disagreeing with parsed artifact rejected | Unit | `tests/test_module_contract.py` | `test_inventory_disagreement_rejected_both_directions` |
| contract: undeclared non-Lua statements rejected statically | Unit | `tests/test_module_contract.py` | `test_undeclared_python_udfs_rejected_statically` |
| contract: object missing the version suffix rejected | Unit | `tests/test_module_contract.py` | `test_object_without_version_suffix_rejected` |
| contract: single-statement artifact without objects array valid | Unit | `tests/test_module_contract.py` | `test_single_statement_without_objects_array_valid` |
| contract: multi-statement artifact without objects array rejected | Unit | `tests/test_module_contract.py` | `test_multi_statement_without_objects_array_rejected` |
| contract: composite artifact may create objects outside PREPROC_RT | Unit | `tests/test_module_contract.py` | `test_objects_outside_preproc_rt_accepted` |
| contract: Python and Lua splitters agree on the corpus | Integration | `tests/test_db_module_install.py` | `test_python_and_lua_splitters_agree_on_corpus` |
| contract: sha256 mismatch rejected on a multi-statement artifact | Unit | `tests/test_module_contract.py` | `test_sha256_mismatch_rejected_multi_statement` |
| db-install: BucketFS install byte-identical to CLI install | Integration | `tests/test_db_module_install.py` | `test_bucketfs_install_matches_cli_end_state` |
| db-install: composite installs every declared object in file order | Integration | `tests/test_db_module_install.py` | `test_composite_installs_all_objects_in_order` |
| db-install: pre-flight validation precedes the first DDL | Integration | `tests/test_db_module_install.py` | `test_preflight_aborts_before_any_ddl` |
| db-install: failure at statement k drops objects 1..k-1 | Integration | `tests/test_db_module_install.py` | `test_midartifact_failure_compensating_cleanup` |
| db-install: partially installed module is inert | Integration | `tests/test_db_module_install.py` | `test_partial_install_is_inert_no_rule` |
| db-install: re-running a part-way failed install converges | Integration | `tests/test_db_module_install.py` | `test_failed_install_rerun_converges` |
| db-install: objects outside PREPROC_RT created and cleaned up | Integration | `tests/test_db_module_install.py` | `test_out_of_schema_objects_created_and_cleaned` |
| db-install: sha256 mismatch aborts before any write | Integration | `tests/test_db_module_install.py` | `test_sha256_mismatch_aborts_before_write` |
| db-resolver: CREATE target differing from script_name is a hard failure | Integration | `tests/test_db_resolver.py` | `test_script_name_mismatch_hard_failure` |
| db-resolver: composite inventory verified against index entry | Integration | `tests/test_db_resolver.py` | `test_composite_inventory_verified_against_entry` |
| db-resolver: derived objects disagreeing with entry is a hard failure | Integration | `tests/test_db_resolver.py` | `test_inventory_disagreement_hard_failure` |
| db-resolver: schema-version-1 entry verified as single-statement | Integration | `tests/test_db_resolver.py` | `test_schema_version_1_entry_single_statement` |
| db-resolver: oversized artifact chunked, split only after reassembly | Integration | `tests/test_db_resolver.py` | `test_chunked_artifact_split_after_reassembly` |
| db-catalog: catalog reports declared object inventory | Integration | `tests/test_db_module_catalog.py` | `test_catalog_reports_object_inventory` |
| db-catalog: catalog reveals objects outside PREPROC_RT | Integration | `tests/test_db_module_catalog.py` | `test_catalog_reveals_out_of_schema_objects` |
| db-catalog: schema-version-1 entry reports single-object inventory | Integration | `tests/test_db_module_catalog.py` | `test_catalog_v1_entry_single_object` |
| lifecycle: registering a module writes a provenance row | Integration | `tests/test_module_lifecycle.py` | `test_module_register_writes_provenance_with_objects` |
| lifecycle: provenance records full inventory of a composite module | Integration | `tests/test_module_lifecycle.py` | `test_provenance_records_full_inventory` |
| lifecycle: retired version reclaimed once unreferenced | Integration | `tests/test_module_lifecycle.py` | `test_reclaim_drops_all_inventory_objects_reverse_order` |
| lifecycle: reclaim drops objects outside PREPROC_RT | Integration | `tests/test_module_lifecycle.py` | `test_reclaim_drops_out_of_schema_objects` |
| lifecycle: reclaim never drops an object absent from the inventory | Integration | `tests/test_module_lifecycle.py` | `test_reclaim_spares_objects_absent_from_inventory` |
| lifecycle: NULL-inventory row reclaims its entry script | Integration | `tests/test_module_lifecycle.py` | `test_null_inventory_row_reclaims_entry_script` |
| lifecycle: reclaiming v1 leaves live v2 companions untouched | Integration | `tests/test_module_lifecycle.py` | `test_reclaim_v1_leaves_v2_companions` |
| cli: `add` deploys every object of a composite module | Integration | `tests/test_module_cli.py` | `test_add_deploys_all_composite_objects` |
| cli: failed `add` leaves no partial objects and no rule | Integration | `tests/test_module_cli.py` | `test_failed_add_cleans_up_and_registers_no_rule` |
| cli: `validate` statically rejects a nonconformant module | Unit | `tests/test_module_cli.py` | `test_validate_rejects_script_name_and_inventory_mismatch` |
| cli: `validate` rejects undeclared Python UDFs | Unit | `tests/test_module_cli.py` | `test_validate_rejects_undeclared_python_udfs` |
| cli: `validate` compiles scripts in a disposable schema and smoke-tests the entry | Integration | `tests/test_module_cli.py` | `test_validate_live_passes_a_composite_without_creating_its_objects` |
| cli: live validation never creates a real object of the module | Integration | `tests/test_module_cli.py` | `test_validate_live_disposable_objects_exist_during_the_deploy` |
| cli: `bundle` emits a self-contained offline artifact | Unit | `tests/test_cli_bundle.py` | `test_bundle_emits_composite_statements_with_delimiters` |
| registry: composite entry carries its object inventory | Unit | `tests/test_module_registry.py` | `test_index_entry_carries_object_inventory` |
| registry: generated index declares schema_version 2 | Unit | `tests/test_module_registry.py` | `test_generated_index_declares_schema_version_2` |
| registry: single-statement manifest still yields an inventory | Unit | `tests/test_module_registry.py` | `test_single_statement_manifest_yields_one_object` |
| registry: schema-version-1 index read as single-statement entries | Unit | `tests/test_module_registry.py` | `test_schema_version_1_index_still_reads` |
| registry: central-library module auto-indexed from its manifest | Unit | `tests/test_module_registry.py` | `test_library_module_autoindexed_with_objects` |
| schema-layout: MODULES gains a nullable objects column idempotently | Integration | `tests/test_schema_layout.py` | `test_modules_objects_column_idempotent_upgrade` |
| schema-layout: installed-modules view exposes the inventory | Integration | `tests/test_schema_layout.py` | `test_installed_modules_view_exposes_objects` |
| scaffolding: ergonomics modules conformant with no manifest edit | Integration | library repo `tests/test_library_modules.py` | `test_ergonomics_modules_valid_without_objects_array` |
| scaffolding: template documents inventory and separation | Integration | library repo `tests/test_library_modules.py` | `test_template_documents_objects_and_separation` |
| scaffolding: generated index lists every library module | Integration | library repo `tests/test_library_modules.py` | `test_index_schema_version_2_lists_all_modules` |
| scaffolding: CI tests modules and checks index sync | Integration | library repo `tests/test_library_modules.py` | `test_ci_workflow_checks_index_and_inventory_sync` |

### Manual Testing

| Feature | Command | Expected Output |
|---------|---------|-----------------|
| module/contract | `uv run preproc module validate <composite> --registry ./fixtures` | Exit 0; reports the module valid and lists its declared objects |
| module/contract | `uv run preproc module validate <undeclared-udf-module> --registry ./fixtures` | Exit non-zero; names the undeclared Python UDFs |
| module/cli | `uv run preproc module add <composite>` | Every declared object exists; `SELECT * FROM PREPROC_RT.INSTALLED_MODULES` shows the inventory |
| module/cli | `uv run preproc module bundle <composite> --output /tmp/off.sql --registry ./fixtures` | Exit 0 offline; `/tmp/off.sql` runs unmodified in EXAplus and creates every object |
| module/db-install | `EXECUTE SCRIPT PREPROC.MODULE_INSTALL('bucketfs:PREPROC_BFS/preproc-lib-<v>.tar.gz', '<composite>', 'ROLE:ANALYSTS')` | Returns `installed`; every declared object present; rule registered |
| module/db-install | Re-run `MODULE_INSTALL` after a deliberately broken statement 4 | First run fails naming statement 4, no objects and no rule remain; second run with the fixed artifact returns `installed` |
| module/db-catalog | `EXECUTE SCRIPT PREPROC.MODULE_CATALOG('bucketfs:PREPROC_BFS/preproc-lib-<v>.tar.gz')` | Lists each module with its object count and fully-qualified object names before any install |
| module/lifecycle | Retire and reclaim a composite v1 while v2 is live | Every v1 object dropped, every v2 object intact, in-scope statements still transformed by v2 |
| module/registry | `uv run python -m preproc.module.registry --generate` then `git diff registry/index.json` | `schema_version: 2`, every entry carries `objects`, regeneration byte-identical |
| install/schema-layout | `uv run preproc install` twice against an existing install | Both runs exit 0; `PREPROC.MODULES` has one `objects` column; existing rows preserved with NULL |

### Checklist

| Step | Command | Expected |
|------|---------|----------|
| Environment | `uv sync` | Exit 0 |
| Test | `uv run pytest` | 0 failures |
| Lint | `uv run ruff check . && uv run ruff format --check .` | 0 errors/warnings |
| Lua lint | `luacheck sql/lua/` | 0 warnings, 0 errors |
| Coverage | `uv run pytest --cov=preproc --cov-report=term-missing` | No regression in `preproc.module` coverage |
