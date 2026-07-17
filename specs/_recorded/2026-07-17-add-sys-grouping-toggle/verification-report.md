# Verification Report: add-sys-grouping-toggle

**Generated:** 2026-07-17

## Verdict

| Result | Details |
|--------|---------|
| **PASS** — verified against live Exasol 2026.1.0 | Full suite green against a Docker Exasol on `localhost:9563`: **221 passed, 7 skipped, 0 failed**. One new test (`test_sys_excluded_from_full_closure_when_isolated`) initially failed on a test-setup bug (`GRANT ... TO DBA` is forbidden by Exasol); fixed — see Notes. All offline gates also green. |

| Check | Status |
|-------|--------|
| Build (`uv sync`) | ✓ |
| Tests (live Exasol 2026.1.0 @ localhost:9563) | ✓ 221 passed, 7 skipped, 0 failed |
| Lint (`ruff check`) | ✓ |
| Format (`ruff format --check`) | ✓ |
| Lua lint (`luacheck sql/lua/`) | ✓ 0/0 across 18 files |
| Scenario Coverage | ✓ every scenario has a matching test; all executed and passed |
| Manual Tests | ✓ covered by the passing integration matrix (equivalent assertions) |

## Test Evidence

### Test Results

| Type | Run | Passed | Skipped |
|------|-----|--------|---------|
| Offline (parsing/bundle/lint) | 32 | 32 | — |
| Integration (`@pytest.mark.integration`) | 0 | 0 | 196 |

Offline suites that actually executed and passed include the command-sugar expansion tests and the CLI bundle-assembly tests (which validate the 19→20 statement-count change and `SET_SYS_GROUPING` presence in both full and release bundles against real repo sources).

## Tool Evidence

### Linter / Formatter
```
ruff check .            → All checks passed!
ruff format --check .   → 25 files already formatted
luacheck sql/lua/       → 0 warnings / 0 errors in 18 files (incl. set_sys_grouping.lua)
```

### Release artifact
`release.sql` is gitignored (CI-generated, not committed — see commit d1bf9d4). Regenerated via `uv run preproc bundle --release`; a baseline diff against pre-branch HEAD confirmed only the three expected change categories (SET_SYS_GROUPING CREATE block, SETTINGS column+seed, edited CONTROL_SUGAR_V1 body) plus the Group-2 RESOLUTION_SQL gate; no reordering or drops; the upgrade-only 25_column_upgrades ALTER is correctly absent.

## Scenario Coverage

| Feature | Scenario | Test Location | Test Name | Exec |
|---------|----------|---------------|-----------|------|
| refresh/role-resolution | SYS grouped gets DBA+PUBLIC | tests/test_role_resolution.py | test_sys_grouped_receives_dba_and_public_rules | ⏭ |
| refresh/role-resolution | SYS excluded from full closure when isolated | tests/test_role_resolution.py | test_sys_excluded_from_full_closure_when_isolated | ⏭ |
| refresh/role-resolution | USER-scoped SYS rule survives isolation | tests/test_role_resolution.py | test_sys_user_scoped_rule_survives_isolation | ⏭ |
| refresh/role-resolution | Non-SYS DBA holder unaffected | tests/test_role_resolution.py | test_non_sys_dba_holder_unaffected_by_toggle | ⏭ |
| refresh/role-resolution | NULL toggle treated as grouped | tests/test_role_resolution.py | test_null_grouping_treated_as_grouped | ⏭ |
| control/admin-scripts | Updates + auto-refreshes | tests/test_control_admin_scripts.py | test_set_sys_grouping_updates_and_refreshes | ⏭ |
| control/admin-scripts | Rejects non-boolean side-effect-free | tests/test_control_admin_scripts.py | test_set_sys_grouping_rejects_non_boolean | ⏭ |
| control/admin-scripts | NOREFRESH skips refresh | tests/test_control_admin_scripts.py | test_set_sys_grouping_norefresh_skips_refresh | ⏭ |
| control/admin-scripts | Only the flag changes; STATUS current | tests/test_control_admin_scripts.py | test_set_sys_grouping_leaves_other_settings_and_status_current | ⏭ |
| install/schema-layout | Column present/nullable | tests/test_schema_layout.py | test_settings_has_group_sys_with_dba_column | ⏭ |
| install/schema-layout | Seed default TRUE | tests/test_schema_layout.py | test_settings_seeded_single_row (extended) | ⏭ |
| install/schema-layout | Upgrade idempotent; NULL grouped | tests/test_schema_layout.py | test_group_sys_with_dba_upgrade_idempotent | ⏭ |
| control/command-sugar | SET SYS_GROUPING expands | tests/test_command_sugar.py | test_set_sys_grouping_expands | ✓ |
| control/command-sugar | NOREFRESH threaded | tests/test_command_sugar.py | test_set_sys_grouping_norefresh_threaded | ✓ |
| install/cli-install | Deployed by install | tests/test_cli_install.py | test_install_deploys_control_layer (extended) | ⏭ |
| install/cli-bundle | Present in bundles; count 20 | tests/test_cli_bundle.py | test_bundle_count_matches_statements + presence checks | ✓ |

## Notes

- **Runtime confirmation:** the scalar subquery inside the ROLE-arm `JOIN … ON` clause in `RESOLUTION_SQL` (flagged by code review as the one runtime-dependent assumption) executes correctly on Exasol 2026.1.0 — the grouped / isolated / NULL / USER-survives / non-SYS-holder tests all pass.
- **Test-setup fix during verification:** `test_sys_excluded_from_full_closure_when_isolated` originally granted a nested role to DBA (`GRANT PREPROC_TEST_ROLE_A TO DBA`) to construct a nested path to SYS. Exasol forbids this (`cannot grant roles to DBA`), exactly as it forbids granting to SYS — so a nested role *above* SYS is not constructible and needs no case. The test now asserts SYS is excluded from both a DBA-scoped and a PUBLIC-scoped rule (SYS's only real closure paths); the gate is a single path-independent `grantee <> 'SYS'` predicate, so this fully proves the full-closure exclusion. Generic transitive nesting remains covered by the non-SYS nested-role tests. Production `RESOLUTION_SQL` was never at fault.
- Code review found **no correctness bugs**. It flagged one stale delta-spec bullet (admin-scripts spec claimed the sugar was not exposed) and a plan test-name drift — both fixed in the plan artifacts.
- The 7 skipped tests are pre-existing conditional skips unrelated to this slice.
