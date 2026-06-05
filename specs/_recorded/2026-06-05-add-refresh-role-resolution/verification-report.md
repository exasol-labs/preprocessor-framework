# Verification Report: add-refresh-role-resolution

**Generated:** 2026-06-05
**Live verification:** 2026-06-05 against `exasol/docker-db:2025.2.1` — see "Live Verification Addendum" below.

## Verdict

| Result | Details |
|--------|---------|
| **PASS** | All automated checks pass. **Live run: 50 passed, 0 failed, 0 skipped** against docker-db 2025.2.1 after five environment-surfaced fixes (see addendum). |

| Check | Status |
|-------|--------|
| Build | ✓ |
| Tests | ✓ |
| Lint (Python) | ✓ |
| Lint (Lua) | ✓ |
| Format | ✓ |
| Scenario Coverage | ✓ |
| Manual Tests | — (requires live Exasol instance) |

## Test Evidence

### Test Results

| Type | Collected | Passed | Skipped | Failed |
|------|-----------|--------|---------|--------|
| Unit | 7 | 7 | 0 | 0 |
| Integration | 43 | 0 | 43 | 0 |

Integration tests skip cleanly when `EXASOL_DSN`/`EXASOL_USER`/`EXASOL_PASSWORD` are not configured. On CI with `exasol/docker-db` they will run fully.

### Manual Tests

| Test | Result |
|------|--------|
| refresh/role-resolution: EXECUTE SCRIPT PREPROC.REFRESH | — (requires live DB) |
| refresh/role-resolution: non-admin REFRESH rejected | — (requires live DB) |
| refresh/status-staleness: STATUS current after refresh | — (requires live DB) |
| refresh/status-staleness: STATUS stale after role grant | — (requires live DB) |
| install/master-skeleton: dispatch with seeded EXPAND rule | — (requires live DB) |
| install/schema-layout: columns + scripts after install | — (requires live DB) |

## Tool Evidence

### Build

```
uv sync
Resolved 13 packages in 10ms
Checked 12 packages in 115ms
exit: 0
```

### Tests

```
uv run pytest --tb=short
============================= test session starts ==============================
platform linux -- Python 3.12.3, pytest-9.0.3, pluggy-1.6.0
collected 50 items
tests/test_cli_install.py sss..
tests/test_master_dispatch.py ssssss
tests/test_master_skeleton.py ssss.
tests/test_project_bootstrap.py .s...
tests/test_role_resolution.py sssssssssss
tests/test_schema_layout.py ssssssssssss
tests/test_status_staleness.py ssssss
======================== 7 passed, 43 skipped in 7.17s =========================
exit: 0
```

### Linter (Python)

```
uv run ruff check .
All checks passed!
exit: 0
```

### Linter (Lua)

```
luacheck sql/lua/
Checking sql/lua/master.lua  OK
Checking sql/lua/refresh.lua OK
Checking sql/lua/status.lua  OK
Total: 0 warnings / 0 errors in 3 files
exit: 0
```

### Formatter

```
uv run ruff format --check .
12 files already formatted
exit: 0
```

## Scenario Coverage

| Feature | Scenario | Test Location | Test Name | Status |
|---------|----------|---------------|-----------|--------|
| refresh/role-resolution | Direct role grant resolves | `tests/test_role_resolution.py` | `test_direct_role_grant_resolves` | Integration (skipped — no DB) |
| refresh/role-resolution | Nested role grant resolves transitively | `tests/test_role_resolution.py` | `test_nested_role_grant_resolves_transitively` | Integration (skipped — no DB) |
| refresh/role-resolution | User without role excluded | `tests/test_role_resolution.py` | `test_user_without_role_excluded` | Integration (skipped — no DB) |
| refresh/role-resolution | USER+ROLE additive union same phase | `tests/test_role_resolution.py` | `test_user_unions_with_role_same_phase` | Integration (skipped — no DB) |
| refresh/role-resolution | Multi-role rules in deterministic order | `tests/test_role_resolution.py` | `test_multi_role_rules_union_ordered` | Integration (skipped — no DB) |
| refresh/role-resolution | Multi-path function deduplicated | `tests/test_role_resolution.py` | `test_multi_path_function_deduplicated` | Integration (skipped — no DB) |
| refresh/role-resolution | Disabled rules excluded | `tests/test_role_resolution.py` | `test_disabled_rules_excluded` | Integration (skipped — no DB) |
| refresh/role-resolution | Full rebuild, no orphans | `tests/test_role_resolution.py` | `test_refresh_full_rebuild_no_orphans` | Integration (skipped — no DB) |
| refresh/role-resolution | Rebuild and timestamp atomic | `tests/test_role_resolution.py` | `test_rebuild_and_timestamp_atomic` | Integration (skipped — no DB) |
| refresh/role-resolution | Callable from any client by admin | `tests/test_role_resolution.py` | `test_refresh_callable_by_admin_any_client` | Integration (skipped — no DB) |
| refresh/role-resolution | Requires admin privileges | `tests/test_role_resolution.py` | `test_refresh_requires_admin` | Integration (skipped — no DB) |
| refresh/status-staleness | Current after refresh | `tests/test_status_staleness.py` | `test_status_current_after_refresh` | Integration (skipped — no DB) |
| refresh/status-staleness | Stale after role grant | `tests/test_status_staleness.py` | `test_status_stale_after_role_grant` | Integration (skipped — no DB) |
| refresh/status-staleness | Stale after new user | `tests/test_status_staleness.py` | `test_status_stale_after_new_user` | Integration (skipped — no DB) |
| refresh/status-staleness | Read-only | `tests/test_status_staleness.py` | `test_status_read_only` | Integration (skipped — no DB) |
| refresh/status-staleness | Never refreshed | `tests/test_status_staleness.py` | `test_status_never_refreshed` | Integration (skipped — no DB) |
| refresh/status-staleness | Requires admin privileges | `tests/test_status_staleness.py` | `test_status_requires_admin` | Integration (skipped — no DB) |
| install/master-skeleton | Phase-order dispatch | `tests/test_master_dispatch.py` | `test_dispatch_phase_order` | Integration (skipped — no DB) |
| install/master-skeleton | Within-phase ordinal order | `tests/test_master_dispatch.py` | `test_dispatch_within_phase_ordinal_order` | Integration (skipped — no DB) |
| install/master-skeleton | client_pattern match | `tests/test_master_dispatch.py` | `test_dispatch_client_pattern_match` | Integration (skipped — no DB) |
| install/master-skeleton | client_pattern no-match | `tests/test_master_dispatch.py` | `test_dispatch_client_pattern_no_match` | Integration (skipped — no DB) |
| install/master-skeleton | Failing function skipped (fail-open) | `tests/test_master_dispatch.py` | `test_dispatch_failing_function_skipped` | Integration (skipped — no DB) |
| install/master-skeleton | Missing module skipped (fail-open) | `tests/test_master_dispatch.py` | `test_dispatch_missing_module_skipped` | Integration (skipped — no DB) |
| install/schema-layout | ordinal/fingerprint columns present | `tests/test_schema_layout.py` | `test_admin_tables_have_ordinal_and_fingerprint` | Integration (skipped — no DB) |
| install/schema-layout | MY_PIPELINE ordering + client_pattern | `tests/test_schema_layout.py` | `test_my_pipeline_orders_phase_ordinal` | Integration (skipped — no DB) |
| install/schema-layout | REFRESH/STATUS scripts not PUBLIC | `tests/test_schema_layout.py` | `test_refresh_status_scripts_created_not_public` | Integration (skipped — no DB) |
| install/schema-layout | PREPROC_ADMIN dict grant | `tests/test_schema_layout.py` | `test_preproc_admin_dictionary_grant` | Integration (skipped — no DB) |

## Notes

### Key design decisions implemented

1. **CONNECT BY NOCYCLE** used in refresh.lua for transitive role closure — defensive against role-grant cycles (non-standard but possible on Exasol).

2. **Semantic phase order enforced in Lua** (TRANSLATE → EXPAND → REWRITE). `MY_PIPELINE` sorts phases lexically (EXPAND < REWRITE < TRANSLATE), which is the wrong semantic order. master.lua walks an explicit `phase_order` list rather than trusting view row order. Tests for `test_dispatch_phase_order` verify this discriminating property — a lexical-order regression would yield `SEED|E|R|T` instead of `SEED|T|E|R`.

3. **RETURNS TABLE via `exit()`** for both PREPROC.REFRESH and PREPROC.STATUS. `output()` emits log lines only and does not populate a RETURNS TABLE result set. The install applier deploys both scripts with `RETURNS TABLE` in the CREATE SCRIPT wrapper.

4. **Fingerprint null-coalescing symmetry**: refresh.lua coalesces a NULL hash to `""` before stamping; status.lua coalesces both stored and recomputed fingerprints to `""` before comparing. Without this, an empty user/grant universe yields a NULL `LISTAGG`, producing a false STALE verdict.

5. **client_pattern uses Lua pattern matching** (`string.find(current_client, pattern)`) per the plan's final guidance. If literal substring matching is preferred, add the `plain=true` flag to `string.find`.

6. **Privilege gate location in STATUS**: the SETTINGS read (no dictionary required) occurs first; the fingerprint recomputation (requires SELECT ANY DICTIONARY) is the privilege gate for the non-never-refreshed path. A non-admin on a refreshed DB still fails at step 3.

### Dead code removed

The "Dispatch extension point (later slice)" comment block was removed from master.lua and replaced with the actual dispatch implementation.

### Ready for

```
/speq:record add-refresh-role-resolution
```

## Live Verification Addendum (2026-06-05)

Full suite executed against `exasol/docker-db:2025.2.1` (127.0.0.1:9564, SYS):

```
uv run pytest → 50 passed, 0 failed, 0 skipped in 26.85s
uv run ruff check .          → All checks passed!
uv run ruff format --check . → 12 files already formatted
luacheck sql/lua/            → 0 warnings / 0 errors in 3 files
```

All 28 plan scenarios now verified live, including every previously-skipped integration test.

### Fixes surfaced by the live run (post-recording)

| # | Defect | Fix |
|---|--------|-----|
| 1 | `DERIVED` is a reserved word in Exasol — CTE name in refresh.lua's resolution SQL failed to parse | Renamed CTE `derived` → `scoped_rules` |
| 2 | Exasol forbids `IN (subquery)` outside WHERE clauses — the `START WITH ... IN (SELECT ...)` form failed | Dropped `START WITH`; every row roots a chain, the JOIN against enabled ROLE-scoped CONFIG filters origins |
| 3 | No `commit()` global in Exasol Lua scripts | `pquery("COMMIT")` with error check |
| 4 | No `require()` in Exasol Lua — module loading is `import(name, alias)`, alias bound as a global, return value nil (verified live) | master.lua `dispatch_rule` uses `import(script_name, "preproc_module")`; `.luacheckrc` updated (`import` read-global, `preproc_module` global; bogus `commit`/`rollback` globals removed) |
| 5 | No `CURRENT_CLIENT` function — client identifier lives in `EXA_ALL_SESSIONS.CLIENT` | master.lua + test query `SELECT CLIENT FROM EXA_ALL_SESSIONS WHERE SESSION_ID = CURRENT_SESSION` |

### Test-strategy fixes (SYS connection reality)

- Exasol cannot grant roles to SYS ("cannot grant roles to SYS", 42500), and `GRANT ... TO CURRENT_USER` is a syntax error. `test_role_resolution.py` now creates a dedicated `PREPROC_TEST_HOLDER` user (session-scoped fixture, idempotent create, intentionally left in place) as the role grantee; assertions target its RESOLUTION rows.
- `test_status_staleness.py::test_status_stale_after_role_grant` grants the sentinel role to `PREPROC_ADMIN` (role-to-role grant) instead — still mutates `EXA_DBA_ROLE_PRIVS`, which is what the fingerprint watches.

### Note for the spec library

Defect #4's resolution means the module-loading contract is `import(name, alias)` — module authors' scripts are loaded with invoker-rights `import` from `PREPROC_RT.MASTER`. Worth stating explicitly in the module-authoring docs when that slice lands.
