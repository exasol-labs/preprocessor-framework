# Tasks: add-demo-suite

## Group A: TRANSLATE + REWRITE modules (no deps)
- [x] 1.1 Add `sql/udf/demo_sqlglot_transpile_v1.sql` — Python3 scalar UDF [expert]
- [x] 1.2 Add `sql/lua/demo_translate_v1.lua` — translate(sqltext) module [expert]
- [x] 1.3 Verify luacheck passes for demo_translate_v1.lua
- [x] 2.1 Add `sql/lua/demo_audit_v1.lua` — tag(sqltext) module
- [x] 2.2 Verify luacheck passes for demo_audit_v1.lua

## Group B: Bundle generator (no deps, parallel with A)
- [x] 4.1 Add `src/preproc/bundle.py` — assemble deploy_all.sql [expert]
- [x] 4.2 Add `bundle` subcommand to `src/preproc/cli.py`

## Group C: Install deployment (depends on A)
- [x] 3.1 Extend `src/preproc/install.py` to deploy DEMO_TRANSLATE_V1, DEMO_AUDIT_V1, and the UDF
- [x] 3.2 Confirm schema-level PUBLIC grants cover new objects (no grant change expected)

## Group D: Demo seed/teardown (depends on A + C)
- [x] 5.1 Add `sql/demo/seed.sql` — demo config with roles, users, rules, REFRESH [expert]
- [x] 5.2 Add `sql/demo/teardown.sql` — idempotent removal of demo objects

## Group E: Docs + Tests (depends on features they cover)
- [x] 6.1 Update `README.md` quickstart (deploy_all.sql + demo walkthrough)
- [x] 6.2 Update `docs/operations.md` (bundle, seed/teardown, TRANSLATE cost note)
- [x] 6.3 Update `docs/how-it-works.md` (TRANSLATE/UDF architecture)
- [x] 7.1 Add `tests/test_dialect_translation.py` [expert]
- [x] 7.2 Add `tests/test_audit_rewrite.py`
- [x] 7.3 Add `tests/test_cli_bundle.py`
- [x] 7.4 Add `tests/test_demo_seed.py`
- [x] 7.5 Extend `tests/test_cli_install.py` for new install deltas

## Verification
- [x] V.1 Run `uv sync` — exit 0
- [x] V.2 Run `uv run pytest` — 18 passed (75 integration deselected, no Exasol instance)
- [x] V.3 Run `uv run ruff check . && uv run ruff format --check .` — 0 errors
- [x] V.4 Run `luacheck sql/lua/` — 0 warnings / 0 errors (6 files)
