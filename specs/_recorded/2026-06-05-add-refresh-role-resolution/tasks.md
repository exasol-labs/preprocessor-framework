# Tasks: add-refresh-role-resolution

## Group A: DDL Changes (sql/install/)
- [x] A.1 Add `ordinal` column to PREPROC.RESOLUTION and fingerprint to PREPROC.SETTINGS (fresh + guarded ALTER)
- [x] A.2 Update 40_views.sql: MY_PIPELINE exposes client_pattern, orders by phase, ordinal
- [x] A.3 Add 60_dictionary_grant.sql: GRANT SELECT ANY DICTIONARY TO PREPROC_ADMIN (tolerate-exists)
- [x] A.4 Verify PREPROC.REFRESH and PREPROC.STATUS are not PUBLIC-accessible (no inherited grant)

## Group B: Refresh Database Script (sql/lua/refresh.lua)
- [x] B.1 Transitive role-to-user closure via SQL CONNECT BY over EXA_DBA_ROLE_PRIVS [expert]
- [x] B.2 Additive UNION of ROLE+USER rules, deduplicate, ordinal assignment [expert]
- [x] B.3 Atomic rebuild: DELETE+INSERT RESOLUTION, stamp fingerprint+last_refresh, single commit [expert]
- [x] B.4 Factor fingerprint into shared helper definition (refresh writer, status reader)
- [x] B.5 Name/shape as stable syntax-mappable admin entry point

## Group C: Status Database Script (sql/lua/status.lua)
- [x] C.1 Recompute fingerprint from EXA_DBA_USERS/EXA_DBA_ROLE_PRIVS, compare vs SETTINGS, return verdict [expert]
- [x] C.2 Share fingerprint definition from B.4; name/shape PREPROC.STATUS as stable entry point

## Task 4: Deploy via Install Applier
- [x] D.1 Wire PREPROC.REFRESH and PREPROC.STATUS deployment into install.py (deploy alongside MASTER)

## Group D: MASTER Dispatch Upgrade (sql/lua/master.lua)
- [x] E.1 Dispatch loop: TRANSLATE→EXPAND→REWRITE in phase order, ordinal within phase, threading text [expert]
- [x] E.2 Per-rule client_pattern matching: dispatch only matching rules [expert]
- [x] E.3 Per-rule fail-open: pcall each module dispatch, skip on error, keep guard+lookup fail-open [expert]
- [x] E.4 Keep luacheck clean

## Group E: Tests
- [x] F.1 test_role_resolution.py — integration tests for all refresh/role-resolution scenarios [expert]
- [x] F.2 test_status_staleness.py — integration tests for all status scenarios [expert]
- [x] F.3 test_master_dispatch.py — integration tests for dispatch scenarios [expert]
- [x] F.4 Extend test_schema_layout.py — ordinal/fingerprint columns, MY_PIPELINE, scripts, dict grant

## Verification
- [x] V.1 uv sync (build)
- [x] V.2 uv run pytest (7 passed, 43 skipped — integration tests skip cleanly without DB)
- [x] V.3 uv run ruff check . (0 errors)
- [x] V.4 luacheck sql/lua/ (0 warnings)
- [x] V.5 uv run ruff format --check . (no changes)
