# Tasks: add-sys-grouping-toggle

## Group 1 — SETTINGS column, seed, upgrade
- [x] 1.1 Add `group_sys_with_dba BOOLEAN` (nullable) to PREPROC.SETTINGS in sql/install/20_tables.sql
- [x] 1.2 Extend seed in sql/install/30_settings_seed.sql to insert group_sys_with_dba = TRUE
- [x] 1.3 Add `--@tolerate-exists` ALTER TABLE ADD COLUMN in sql/install/25_column_upgrades.sql
- [x] 1.4 Integration tests in tests/test_schema_layout.py (column present/nullable; seed default TRUE; upgrade idempotent; NULL treated as grouped)

## Group 2 — Resolution predicate
- [x] 2.1 Add SYS-grouping gate to RESOLUTION_SQL in sql/lua/refresh_core.lua [expert]
- [x] 2.2 Update RESOLUTION_SQL header comment documenting the gate
- [x] 2.3 Role-resolution integration tests in tests/test_role_resolution.py (full matrix) [expert]

## Group 3 — SET_SYS_GROUPING admin script + sugar
- [x] 3.1 Author sql/lua/set_sys_grouping.lua (model set_mode.lua)
- [x] 3.2 Register in src/preproc/install.py
- [x] 3.3 Register in src/preproc/bundle.py (_FULL_SCRIPT_ORDER + _RELEASE_SCRIPT_ORDER)
- [x] 3.4 Update comment in sql/install/65_admin_script_grant.sql
- [x] 3.5 Control-admin-scripts tests in tests/test_control_admin_scripts.py
- [x] 3.6 Update deploy tests: tests/test_cli_install.py + tests/test_cli_bundle.py (19→20)
- [x] 3.7 Add PREPROC SET SYS_GROUPING verb to sql/lua/control_sugar_v1.lua in place
- [x] 3.8 Command-sugar tests in tests/test_command_sugar.py

## Group 4 — Regenerate release.sql + docs
- [x] 4.1 Regenerate release.sql via `preproc bundle --release`; verify diff
- [x] 4.2 Update docs/operations.md
- [x] 4.3 Update docs/how-it-works.md
- [x] 4.4 Update README if it enumerates the admin surface / SETTINGS columns (skipped — README does not enumerate either; it defers to docs/operations.md)

## Verification
- [x] V.1 uv run pytest (32 passed, 196 skipped — integration needs live Exasol, 0 failures)
- [x] V.2 uv run ruff check . && uv run ruff format --check . (clean)
- [x] V.3 luacheck sql/lua/ (0/0)
- [x] V.4 Scenario coverage audit + verification report
