# Verification Report: add-demo-suite

## Verdict: PASS — Full suite verified against live Exasol (docker-db 2025.2.1)

All checks pass, including the full integration suite against the local `exasol/docker-db:2025.2.1` instance. One real defect was found by the integration run and fixed (see Defect Found Live below).

---

## Automated Checks

| Step | Command | Result |
|------|---------|--------|
| Build | `uv sync` | Exit 0 ✓ |
| Test (live, full) | `uv run pytest` against docker-db 2025.2.1 | 89 passed, 4 skipped ✓ |
| Test (seed-gated pipeline) | seed applied → `pytest tests/test_audit_rewrite.py tests/test_dialect_translation.py` → teardown | 12 passed ✓ |
| Lint (Python) | `uv run ruff check . && uv run ruff format --check .` | 0 errors ✓ |
| Lint (Lua) | `luacheck sql/lua/` | 0 warnings / 0 errors (6 files) ✓ |

The 4 standing skips are the seed-gated pipeline tests, which skip by design when `sql/demo/seed.sql` is not applied; they were separately run seeded and all passed.

## Defect Found Live (fixed)

`demo_translate_v1.lua` originally called the transpile UDF with positional `?` placeholders. Exasol Lua `pquery` only supports **named `:param` placeholders** bound from a keyed table — the `?` form fails at runtime. Because the module is fail-closed, the failure was silently masked as "return original text", and the marked statement reached the engine untranslated (caught by `test_marked_statement_translated_through_pipeline`). Fixed to `pquery("SELECT … (:d, :t)", { d = dialect, t = stripped })`; verified live: `--!dialect:tsql SELECT TOP 1 42 AS X` → `SELECT 42 AS X LIMIT 1`, unmarked statements pass through byte-for-byte.

This is exactly the failure mode the fail-closed contract is designed to contain — and why the live pipeline test exists.

---

## Scenario Coverage

| Scenario | Test | Status |
|----------|------|--------|
| The transpile UDF converts a tagged foreign-dialect statement to Exasol SQL | `test_transpile_udf_converts_tsql_top_to_limit` | Integration ✓ (live) |
| The transpile UDF returns the ERR sentinel on unparseable input | `test_transpile_udf_returns_err_sentinel_on_garbage` | Integration ✓ (live) |
| A marked statement is translated to runnable Exasol SQL through the pipeline | `test_marked_statement_translated_through_pipeline` | Integration ✓ (live, seeded) |
| An unmarked statement passes through TRANSLATE unchanged | `test_unmarked_statement_passes_through_unchanged` | Integration ✓ (live) |
| A failed transpilation fails closed and does not corrupt the statement | `test_failed_transpile_fails_closed` | Integration ✓ (live) |
| DEMO_TRANSLATE_V1 and the transpile UDF are deployed by install | `test_translate_module_and_udf_deployed` | Integration ✓ (live) |
| DEMO_TRANSLATE_V1 passes luacheck | `test_translate_module_passes_luacheck` | Offline ✓ (subprocess luacheck) |
| The audit module appends a marker comment to a statement | `test_audit_appends_marker_comment` | Integration ✓ (live) |
| The audit marker carries the current user identity | `test_audit_marker_carries_current_user` | Integration ✓ (live) |
| Audit rewrite runs after EXPAND in the pipeline | `test_audit_runs_after_expand` | Integration ✓ (live) |
| DEMO_AUDIT_V1 is deployed by install | `test_audit_module_deployed` | Integration ✓ (live) |
| DEMO_AUDIT_V1 passes luacheck | `test_audit_module_passes_luacheck` | Offline ✓ (subprocess luacheck) |
| Bundle emits a single deployable SQL file | `test_bundle_emits_single_file` | Offline ✓ |
| Bundle contains the full install DDL in numbered order | `test_bundle_contains_install_ddl_in_order` | Offline ✓ |
| Bundle wraps every Lua script and the demo UDF as CREATE OR REPLACE | `test_bundle_wraps_scripts_and_udf` | Offline ✓ |
| Bundle uses slash delimiters so script bodies survive | `test_bundle_uses_slash_delimiters` | Offline ✓ |
| The generated bundle deploys an equivalent layout to CLI install | `test_bundle_deploys_equivalent_layout` | Integration ✓ (live) |
| Bundle is reproducible and excludes activation and seed data | `test_bundle_reproducible_no_activation_no_seed` | Offline ✓ |
| Seed creates demo roles and users with distinct pipelines | `test_seed_creates_roles_users_distinct_pipelines` | Integration ✓ (live) |
| Seed inserts rules across all three phases | `test_seed_inserts_rules_all_phases` | Integration ✓ (live) |
| Seed includes a client_pattern-scoped rule | `test_seed_includes_client_pattern_rule` | Integration ✓ (live) |
| Seed runs refresh so resolution reflects the demo config | `test_seed_runs_refresh` | Integration ✓ (live) |
| Different demo users see different resolved pipelines | `test_demo_users_see_different_pipelines` | Integration ✓ (live) |
| Teardown removes everything the seed created | `test_teardown_removes_demo_objects` | Integration ✓ (live) |
| Teardown is idempotent | `test_teardown_idempotent` | Integration ✓ (live) |
| Install bootstraps a clean database (now deploys demo modules + UDF) | `test_install_deploys_demo_modules_and_udf` | Integration ✓ (live) |
| Installed demo modules and UDF are executable by PUBLIC | `test_demo_modules_and_udf_public_executable` | Integration ✓ (live) |

---

## Manual Testing

Covered by the live integration run (UDF transpile, pipeline translation, audit comment, seed/teardown round-trips, install deployment all asserted against docker-db). Commands per plan for reference:

| Feature | Command | Expected |
|---------|---------|----------|
| dispatch/dialect-translation | `SELECT PREPROC_RT.DEMO_SQLGLOT_TRANSPILE_V1('tsql','SELECT TOP 5 col FROM t')` | `SELECT col FROM t LIMIT 5` |
| dispatch/dialect-translation | Submit `--!dialect:tsql` + `SELECT TOP 5 1 AS X` through active MASTER | Returns 1 AS X with no syntax error |
| dispatch/audit-rewrite | Submit `SELECT 1`, inspect rewritten text | Contains `/* PREPROC_AUDIT user=<USER> */` |
| install/cli-bundle | `uv run preproc bundle --output /tmp/deploy_all.sql` | Reports path + statement count; file exists |
| demo/config-seed | Run `sql/demo/seed.sql` as admin, `SELECT * FROM PREPROC.RESOLUTION` | Demo users show different resolved rules |
| demo/config-seed | Run `sql/demo/teardown.sql` | Demo roles/users/rules gone |
| install/cli-install | `uv run preproc install`, check `PREPROC_RT` scripts | DEMO_TRANSLATE_V1, DEMO_AUDIT_V1, DEMO_SQLGLOT_TRANSPILE_V1 present |

---

## Key Implementation Decisions

**1. teardown.sql uses a transient Lua drop script for idempotency**
Exasol has no `DROP USER IF EXISTS` / `DROP ROLE IF EXISTS` syntax. The teardown script creates a transient `PREPROC_RT.DEMO_TEARDOWN_DROP` Lua script that checks the dictionary before each DROP and then drops itself. This is the only correct way to make teardown idempotent without requiring Python tooling.

**Consequence for test_demo_seed.py**: The `/`-delimited transient Lua block in teardown.sql cannot be applied via `install._apply_sql_file` (its `;`-splitter would corrupt the Lua body). A local `_split_demo_sql` helper in `test_demo_seed.py` honours the standalone `/` terminator (EXAplus convention) and applies teardown.sql correctly. Operators running teardown.sql must use EXAplus, DbVisualiser, or another client that understands `/` — feeding it through `install.py`'s splitter is not safe.

**2. Bundle's install DDL strips comments**
`bundle.py` reuses `install._split_statements` to split install DDL, which strips `--` comments. The bundle's DDL is therefore clean (comment-free) rather than comment-preserving. The bundle and CLI install produce identical executed SQL; the difference is only presentational. Comment-preserving output would require a separate splitter and is a deliberate follow-up if needed for auditability.

**3. CONFIG has no `ordinal` column**
The plan task 5.1 mentioned an `ordinal` column on CONFIG. The actual schema does not have one — ordinal is computed by REFRESH from `rule_id` ascending and stored on RESOLUTION. The seed assigns rule_ids in pipeline order (9001→TRANSLATE, 9002→EXPAND, 9003→REWRITE) so REFRESH derives the intended ordinal.

**4. CONFIG requires `enabled` and `version` NOT NULL columns**
The seed explicitly sets `enabled = TRUE` and `version = 1` on all inserted rules. The plan did not mention these columns; omitting them would have caused INSERT failures.

---

## Files Added / Modified

### New files
- `sql/udf/demo_sqlglot_transpile_v1.sql` — Python3 scalar UDF
- `sql/lua/demo_translate_v1.lua` — TRANSLATE-phase module
- `sql/lua/demo_audit_v1.lua` — REWRITE-phase module
- `sql/demo/seed.sql` — demo config seed (heavily commented)
- `sql/demo/teardown.sql` — idempotent demo teardown
- `src/preproc/bundle.py` — offline bundle generator
- `tests/test_dialect_translation.py` — 7 tests
- `tests/test_audit_rewrite.py` — 5 tests
- `tests/test_cli_bundle.py` — 12 tests (offline)
- `tests/test_demo_seed.py` — 7 tests

### Modified files
- `src/preproc/install.py` — deploy DEMO_TRANSLATE_V1, DEMO_AUDIT_V1, DEMO_SQLGLOT_TRANSPILE_V1
- `src/preproc/cli.py` — add `bundle` subcommand
- `tests/test_cli_install.py` — +2 tests for demo module deployment
- `README.md` — bundle deploy path + demo walkthrough
- `docs/operations.md` — bundle section, demo seed/teardown, TRANSLATE cost note
- `docs/how-it-works.md` — TRANSLATE/UDF architecture, dispatch-semantics correction, PREPROC_RT objects table, DEMO_TRANSLATE_V1 walkthrough

---

## Ready for: /speq:record add-demo-suite
