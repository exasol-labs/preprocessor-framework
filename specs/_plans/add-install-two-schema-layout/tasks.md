# Tasks: add-install-two-schema-layout

## Group A: Project scaffold
- [x] 1.1 Create `pyproject.toml` (uv): deps pyexasol/pytest/ruff, `preproc` console entry point, ruff config
- [x] 1.2 Create directory skeleton: `src/preproc/`, `sql/install/`, `sql/lua/`, `sql/udf/`, `tests/`, `docs/`
- [x] 1.3 Add `.luacheckrc` configured for Exasol preprocessor globals

## Group B: SQL + Lua authoring
- [x] 2.1 `10_schemas.sql` — PREPROC_ADMIN role; PREPROC and PREPROC_RT schemas; ownership
- [x] 2.2 `20_tables.sql` — CONFIG, RESOLUTION, SETTINGS, PROFILES (IF NOT EXISTS)
- [x] 2.3 `30_settings_seed.sql` — guarded single-row SETTINGS seed
- [x] 2.4 `40_views.sql` — CREATE OR REPLACE VIEW PREPROC_RT.MY_PIPELINE (CURRENT_USER filter, phase ordering)
- [x] 2.5 `50_grants.sql` — schema-level EXECUTE/SELECT on PREPROC_RT to PUBLIC
- [x] 3.1 master.lua skeleton: pass-through, ALTER SESSION/SYSTEM guard, fail-open, correct setsqltext/gsub handling [expert]

## Group C: CLI + fixture
- [x] 1.4 pytest connection fixture in `tests/conftest.py`: env-var driven, yields/closes pyexasol connection, clean skip when unset [expert]
- [x] 4.1 Connection module: EXASOL_DSN/USER/PASSWORD validation, pyexasol connection, clear errors
- [x] 4.2 Install command: apply `sql/install/*.sql` in order, deploy MASTER, print (not run) activation statement; idempotent
- [x] 4.3 CLI entry point wiring (`preproc install`), non-zero exit on missing env / connection failure
- [x] 3.2 Wire MASTER deployment (CREATE OR REPLACE LUA SCRIPT) into the install applier

## Group D: Tests
- [x] 5.1 Integration tests: schema-layout scenarios (objects, ownership, grants, view scoping, idempotency)
- [x] 5.2 Integration tests: MASTER behaviour (pass-through, ALTER guard, fail-open) via session-scoped activation [expert]
- [x] 5.3 Integration tests: `preproc install` (clean bootstrap, prints-not-activates, re-run, missing env, unreachable)
- [x] 5.4 Unit tests: env-var validation (pure, no I/O)
- [x] 5.5 luacheck wired into lint step / test

## Group E: CI (last)
- [ ] 1.5 CI workflow skeleton: uv sync, ruff, luacheck, pytest against exasol/docker-db

## Verification
- [ ] V.1 `uv sync` exit 0
- [ ] V.2 `uv run pytest` 0 failures
- [ ] V.3 `uv run ruff check .` + `ruff format --check .` clean
- [ ] V.4 `luacheck sql/lua/` 0 warnings
- [ ] V.5 Scenario coverage audit vs plan
- [ ] V.6 Manual verification steps
- [ ] V.7 Verification report
