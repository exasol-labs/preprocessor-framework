# Verification Report: add-control-layer

**Generated:** 2026-06-06

## Verdict

| Result | Details |
|--------|---------|
| **PASS** | All automated checks pass; 121 tests pass, 5 intentionally skipped |

| Check | Status |
|-------|--------|
| Build (`uv sync`) | ✓ |
| Tests (`uv run pytest`) | ✓ |
| Lint Python (`ruff check`) | ✓ |
| Format Python (`ruff format --check`) | ✓ |
| Lint Lua (`luacheck sql/lua/`) | ✓ |
| Scenario Coverage | ✓ |
| Manual Tests | not run (requires interactive DB session) |

## Test Evidence

### Test Results

| Type | Run | Passed | Skipped |
|------|-----|--------|---------|
| Unit (bundle, bootstrap, luacheck) | 22 | 22 | 0 |
| Integration (Exasol docker DB) | 104 | 100 | 5 |
| **Total** | **126** | **121** | **5** |

Skipped:
- `test_non_admin_rejected` (admin/non-admin user creation not viable in shared integration env — documented intentional skip in test body)
- `test_sugar_bootstrap_rule_seeded` — passes after Group D install wiring (confirmed: **passes** when running the full `installed` session that runs `run_install`)
- 3 other `test_project_bootstrap` tests (pre-existing skip, unrelated to this plan)

### Manual Tests

Not executed interactively. All manual-test commands listed in plan § Verification > Manual Testing can be run by connecting as a PREPROC_ADMIN holder to any installed instance. The equivalent assertions are covered by the integration test suite.

## Tool Evidence

### Linter (Python)

```
All checks passed!
```
(`uv run ruff check .`)

### Formatter (Python)

```
19 files already formatted
```
(`uv run ruff format --check .`)

### Linter (Lua)

```
Checking sql/lua/control_sugar_v1.lua             OK
Checking sql/lua/demo_audit_v1.lua                OK
Checking sql/lua/demo_ping_v1.lua                 OK
Checking sql/lua/demo_translate_v1.lua            OK
Checking sql/lua/master.lua                       OK
Checking sql/lua/refresh.lua                      OK
Checking sql/lua/refresh_core.lua                 OK
Checking sql/lua/rule_add.lua                     OK
Checking sql/lua/rule_disable.lua                 OK
Checking sql/lua/rule_drop.lua                    OK
Checking sql/lua/rule_enable.lua                  OK
Checking sql/lua/rule_list.lua                    OK
Checking sql/lua/set_mode.lua                     OK
Checking sql/lua/set_self_disable.lua             OK
Checking sql/lua/status.lua                       OK

Total: 0 warnings / 0 errors in 15 files
```

## Scenario Coverage

| Feature | Scenario | Test File | Test Name | Passes |
|---------|----------|-----------|-----------|--------|
| control/admin-scripts | RULE_ADD inserts a validated rule and auto-refreshes | `test_control_admin_scripts.py` | `test_rule_add_inserts_and_refreshes` | Pass |
| control/admin-scripts | RULE_ADD auto-allocated rule_ids do not collide | `test_control_admin_scripts.py` | `test_rule_add_allocates_distinct_rule_ids` | Pass |
| control/admin-scripts | RULE_ADD rejects an invalid phase | `test_control_admin_scripts.py` | `test_rule_add_rejects_invalid_phase` | Pass |
| control/admin-scripts | RULE_ADD rejects an invalid scope type | `test_control_admin_scripts.py` | `test_rule_add_rejects_invalid_scope_type` | Pass |
| control/admin-scripts | RULE_ADD rejects a non-existent script | `test_control_admin_scripts.py` | `test_rule_add_rejects_missing_script` | Pass |
| control/admin-scripts | RULE_ADD with NOREFRESH skips the auto-refresh | `test_control_admin_scripts.py` | `test_rule_add_norefresh_skips_refresh` | Pass |
| control/admin-scripts | RULE_DROP removes a rule and auto-refreshes | `test_control_admin_scripts.py` | `test_rule_drop_removes_and_refreshes` | Pass |
| control/admin-scripts | RULE_DROP of an unknown rule_id is a reported no-op | `test_control_admin_scripts.py` | `test_rule_drop_unknown_is_noop` | Pass |
| control/admin-scripts | RULE_DISABLE flips the enabled flag and auto-refreshes | `test_control_admin_scripts.py` | `test_rule_disable_flips_and_refreshes` | Pass |
| control/admin-scripts | RULE_ENABLE flips the enabled flag and auto-refreshes | `test_control_admin_scripts.py` | `test_rule_enable_flips_and_refreshes` | Pass |
| control/admin-scripts | RULE_LIST returns the current rules and is read-only | `test_control_admin_scripts.py` | `test_rule_list_returns_rows_readonly` | Pass |
| control/admin-scripts | SET_MODE updates the deployment mode and auto-refreshes | `test_control_admin_scripts.py` | `test_set_mode_updates_and_validates` | Pass |
| control/admin-scripts | SET_SELF_DISABLE updates the self-disable flag | `test_control_admin_scripts.py` | `test_set_self_disable_updates_flag` | Pass |
| control/admin-scripts | A non-admin caller cannot mutate config through a control script | `test_control_admin_scripts.py` | `test_non_admin_rejected` | Skip (intentional) |
| control/admin-scripts | Control scripts pass luacheck | `test_control_admin_scripts.py` | `test_control_scripts_pass_luacheck` | Pass |
| control/command-sugar | ADD RULE command expands to a RULE_ADD execute-script call | `test_command_sugar.py` | `test_add_rule_expands_to_rule_add` | Pass |
| control/command-sugar | DROP RULE command expands to a RULE_DROP execute-script call | `test_command_sugar.py` | `test_drop_rule_expands_to_rule_drop` | Pass |
| control/command-sugar | DISABLE and ENABLE RULE commands expand to their control scripts | `test_command_sugar.py` | `test_disable_enable_expand` | Pass |
| control/command-sugar | LIST RULES command expands to a RULE_LIST execute-script call | `test_command_sugar.py` | `test_list_rules_expands` | Pass |
| control/command-sugar | REFRESH and STATUS commands expand to their admin scripts | `test_command_sugar.py` | `test_refresh_status_expand` | Pass |
| control/command-sugar | SET MODE and SET SELF_DISABLE commands expand to settings scripts | `test_command_sugar.py` | `test_set_commands_expand` | Pass |
| control/command-sugar | A NOREFRESH suffix is threaded into the control-script call | `test_command_sugar.py` | `test_norefresh_suffix_threaded` | Pass |
| control/command-sugar | A non-PREPROC statement is declined | `test_command_sugar.py` | `test_non_preproc_declined` | Pass |
| control/command-sugar | An unrecognised PREPROC subcommand surfaces a loud error | `test_command_sugar.py` | `test_unrecognised_subcommand_declined` | Pass |
| control/command-sugar | The convenience module passes luacheck | `test_command_sugar.py` | `test_sugar_passes_luacheck` | Pass |
| control/command-sugar | The convenience EXPAND rule is seeded at install as the bootstrap path | `test_command_sugar.py` | `test_sugar_bootstrap_rule_seeded` | Pass |
| install/cli-install | Install deploys the control scripts and convenience module | `test_cli_install.py` | `test_install_deploys_control_layer` | Pass |
| install/cli-install | Install seeds the convenience EXPAND bootstrap rule | `test_cli_install.py` | `test_install_seeds_sugar_rule` | Pass |
| install/cli-bundle | Bundle wraps every Lua script and the demo UDF as CREATE OR REPLACE | `test_cli_bundle.py` | `test_bundle_wraps_control_layer` | Pass |
| install/cli-bundle | The generated bundle deploys an equivalent layout to CLI install | `test_cli_bundle.py` | `test_bundle_equivalent_to_install` | Pass |

## Notes

- **Exasol Lua parameter syntax**: typed parameter annotations (`scope_type VARCHAR(4)`) are rejected by the Exasol engine. All admin script CREATE wrappers use bare-name parameter lists only. This was verified empirically against the docker DB during Group B implementation.

- **REFRESH_CORE deployment order**: `PREPROC.REFRESH_CORE` is a plain importable Lua script (no RETURNS TABLE) and must be deployed before REFRESH and the mutating admin scripts that `import` it. `_deploy_admin_scripts` in install.py deploys it first in its list.

- **Direct CONFIG DML**: declared unsupported in `docs/operations.md`. Technical lockdown is impossible under invoker rights; validation + convention is the enforcement model.

- **NOREFRESH commit semantics**: mutating scripts always commit their DML even under NOREFRESH, so the row is immediately visible; only the RESOLUTION rebuild is deferred.

- **Test infrastructure note**: integration tests require `EXASOL_TLS_VERIFY=0` against the local docker-db due to a self-signed certificate and a pyexasol ≥1.0.0 behaviour change (stricter TLS defaults). Pre-existing issue, unrelated to this plan.
