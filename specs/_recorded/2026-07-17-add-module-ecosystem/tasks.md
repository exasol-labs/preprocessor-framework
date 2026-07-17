# Tasks: add-module-ecosystem

Two repos: no prefix = framework-repo (this repo); `[library-repo]` = /home/nc/Github/preprocessor-library.

## Group A — contract foundation (pure Python)
- [x] 1.1 docs/module-authoring.md + module.toml schema reference
- [x] 1.2 src/preproc/module/manifest.py (parse/validate module.toml, sha256, single-CREATE check)
- [x] 1.3 tests/test_module_contract.py

## Group B — registry (needs A)
- [x] 2.1 src/preproc/module/registry.py (read local/online index, deterministic gen, merge, sha256, drift)
- [x] 2.2 tests/test_module_registry.py

## Group C — DB surface (independent of A/B)
- [x] 3.1 sql/install/22_modules_table.sql (PREPROC.MODULES) + PREPROC_RT.INSTALLED_MODULES view (40_views.sql)
- [x] 3.2 sql/lua/rule_ensure.lua (PREPROC.RULE_ENSURE, idempotent) [expert]
- [x] 3.3 sql/lua/module_register.lua (PREPROC.MODULE_REGISTER, idempotent upsert)
- [x] 3.4 sql/lua/module_retire.lua + module_reclaim.lua (guarded generational reclaim) [expert]
- [x] 3.7 tests/test_module_lifecycle.py [expert]

## Group D — wire DB + counts (needs C)
- [x] 3.5 wire new admin scripts into install.py + bundle.py (orders)
- [x] 3.6 update tests/test_cli_bundle.py counts + regenerate release.sql

## Group E — CLI (needs A+B+C)
- [x] 4.1 src/preproc/module/deploy.py (deploy artifact + RULE_ENSURE + MODULE_REGISTER + refresh)
- [x] 4.2 src/preproc/module/offline_bundle.py (self-contained .sql)
- [x] 4.3 src/preproc/module/commands.py + wire `module` subparser into cli.py
- [x] 4.4 sync convergence (declarative, idempotent) [expert]
- [x] 4.5 validate (static + live scratch smoke)
- [x] 4.6 tests/test_module_cli.py

## Group F — library scaffolding + framework removal (needs A+B)
- [x] 5.1 [library-repo] skeleton: modules/_template/, registry/external/.gitkeep, CONTRIBUTING.md, README.md, .github/workflows/ci.yml
- [x] 5.2 [library-repo] modules/cast-shorthand/ (sql, module.toml+sha, README, tests)
- [x] 5.3 [library-repo] modules/trailing-comma/ (sql, module.toml+sha, README, tests)
- [x] 5.4 [library-repo] generate registry/index.json
- [x] 5.5 [framework-repo] remove ergonomics sources/examples/tests; update examples/README.md pointer
- [x] 5.6a [library-repo] tests/test_scaffolding.py
- [x] 5.6b [framework-repo] tests/test_ergonomics_migrated.py

## Group H — docs + final verification (needs all)
- [x] 6.1 update docs/operations.md + README.md (preproc module workflow, single-slot positioning)
- [x] 6.2 (docs part) document semantic-views & test-kit follow-ons in docs/operations.md; luacheck/ruff/pytest/speq-validate portion tracked separately in Verification

## Verification
- [x] V.1 pytest — 235 passed, 7 skipped, 0 failed (live Docker Exasol 2026.1.0)
- [x] V.2 ruff check + format — clean
- [x] V.3 luacheck sql/lua/ — 0/0 (20 files)
- [x] V.4 code review (no correctness bugs) + verification report

## Post-review fixes (orchestrator)
- [x] Reserved-word bug: PREPROC.MODULES source→source_repo, ref→source_ref (table, view, module_register.lua, lifecycle test)
- [x] Test-harness reserved word: contract expand harness column matched→did_match
- [x] validate.py luacheck: lint with generated .luacheckrc (preprocessor globals + entry fn) so conformant modules pass
- [x] Review nit: exclude `_`-prefixed dirs (modules/_template) from the registry index; regenerated library index.json
- [x] Review nit: deploy.py module_register annotation tuple[bool]→bool
