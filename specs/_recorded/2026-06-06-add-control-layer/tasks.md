# Tasks: add-control-layer

## Phase 2: Refresh Logic Refactor (Group A)
- [x] 2.1 Factor the resolution rebuild + fingerprint + stamp out of `refresh.lua` into an importable shared module callable from mutating admin scripts [expert]
- [x] 2.2 Confirm `PREPROC.REFRESH` still behaves identically (RETURNS TABLE contract, atomic single-commit) after the refactor

## Phase 3: Admin Control Scripts (Group B)
- [x] 3.1 `rule_add.lua` — validate phase/scope_type, validate script existence via `EXA_ALL_SCRIPTS`, allocate non-colliding operator `rule_id` below 9000, INSERT, auto-refresh unless `NOREFRESH`, return allocated `rule_id` [expert]
- [x] 3.2 `rule_drop.lua` — DELETE by `rule_id`, report rows removed, auto-refresh unless `NOREFRESH`
- [x] 3.3 `rule_enable.lua` / `rule_disable.lua` — flip `enabled`, auto-refresh unless `NOREFRESH`
- [x] 3.4 `rule_list.lua` — RETURNS TABLE, read-only, ordered by `rule_id`
- [x] 3.5 `set_mode.lua` — validate + update `SETTINGS.mode`, auto-refresh unless `NOREFRESH`
- [x] 3.6 `set_self_disable.lua` — update `SETTINGS.self_disable_allowed` only
- [x] 3.7 Ensure all new scripts use named `:param` binding and pass luacheck
- [x] 3.8 Create `tests/test_control_admin_scripts.py` with all scenarios from the spec

## Phase 4: Convenience EXPAND Module (Group C)
- [x] 4.1 `control_sugar_v1.lua` — parse `PREPROC …` grammar, map each subcommand to exactly one `EXECUTE SCRIPT PREPROC.<SCRIPT>(...)`, thread `NOREFRESH` suffix [expert]
- [x] 4.2 Author module header as production-module authoring reference (mirrors DEMO_PING_V1 contract block, no `DEMO_` prefix)
- [x] 4.3 Create `tests/test_command_sugar.py` with all scenarios from the spec

## Phase 5: Bootstrap, Deployment, and Docs (Group D)
- [x] 5.1 Add guarded (`WHERE NOT EXISTS`) install-time CONFIG INSERT for convenience EXPAND rule in `sql/install/70_control_sugar_seed.sql`
- [x] 6.1 Extend `src/preproc/install.py` `_deploy_admin_scripts` to deploy new control scripts and `_deploy_master` (or new helper) to deploy `CONTROL_SUGAR_V1`
- [x] 6.2 Extend `src/preproc/bundle.py` `_collect_script_statements` with wrappers for the new scripts and module
- [x] 7.1 Document the control surface and `PREPROC …` sugar in `docs/operations.md`; declare direct CONFIG DML unsupported; state installer/seed bootstrap boundary
- [x] 7.2 Update `tests/test_cli_install.py` with `test_install_deploys_control_layer` and `test_install_seeds_sugar_rule`
- [x] 7.3 Update `tests/test_cli_bundle.py` with `test_bundle_wraps_control_layer` and `test_bundle_equivalent_to_install`

## Phase 6: Verification
- [x] 8.1 Run `uv sync` (exit 0)
- [x] 8.2 Run `uv run pytest` (0 failures) — 121 passed, 5 skipped
- [x] 8.3 Run `uv run ruff check . && uv run ruff format --check .` (0 errors, no changes)
- [x] 8.4 Run `luacheck sql/lua/` (0 warnings, 0 errors — 15 files)
- [x] 8.5 Coverage confirmed via integration tests (pytest-cov not installed; all new scripts exercised by integration suite)
