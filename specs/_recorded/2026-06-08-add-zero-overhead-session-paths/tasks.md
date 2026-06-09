# Tasks: add-zero-overhead-session-paths

## Group 1 — DDL and view changes (prerequisite for everything)

- [x] 1.1 Add `profile_hash VARCHAR(64)` nullable column to `PREPROC.RESOLUTION` in `sql/install/20_tables.sql`
- [x] 1.2 Extend `PREPROC_RT.MY_PIPELINE` in `sql/install/40_views.sql` to expose `mode` (from SETTINGS) and `profile_hash` (from RESOLUTION)
- [x] 1.3 Write integration tests for the updated view: NULL `profile_hash` for no-rule user, correct columns present (`tests/test_schema_layout.py`)

## Group 6 DDL — orphaned_at column (parallel with Group 1)

- [x] 6.1 Add `orphaned_at TIMESTAMP` nullable column to `PREPROC.PROFILES` in `sql/install/20_tables.sql` (fresh installs) and `sql/install/25_column_upgrades.sql` (idempotent `--@tolerate-exists` `ALTER TABLE ... ADD COLUMN`)
- [x] 6.2 Write integration tests for the `orphaned_at` column: present and nullable on a fresh install, column upgrade is a no-op on re-apply (`tests/test_schema_layout.py`)

## Group 2 — Profile codegen in REFRESH_CORE (requires Group 1)

- [x] 2.1 Design the hash manifest format: ordered `phase|ordinal|script_name|function_name|client_pattern` concatenated with `||` separators, hashed with `HASH_MD5` in SQL; document it in `REFRESH_CORE` header [expert]
- [x] 2.2 Add per-user hash computation SQL to `REFRESH_CORE.rebuild()`: a single `UPDATE PREPROC.RESOLUTION SET profile_hash = <computed_hash>` step after the INSERT, covering all distinct user chains [expert]
- [x] 2.3 Add profile script generation Lua to `REFRESH_CORE.rebuild()`: for each distinct `profile_hash` in the freshly inserted RESOLUTION rows, generate the Lua body and `CREATE OR REPLACE LUA SCRIPT PREPROC_RT.P_<hash>` via pquery [expert]
- [x] 2.4 Add `PREPROC.PROFILES` upsert inside `REFRESH_CORE.rebuild()`: `MERGE` or `INSERT … WHERE NOT EXISTS` for each generated profile hash + manifest + created_at
- [x] 2.5 Ensure all codegen steps happen before `COMMIT` in `rebuild()` (verify ordering in code)
- [x] 2.6 Write integration tests for profile codegen: profile script created after refresh, idempotent on second refresh, hash changes when chain changes (`tests/test_profile_codegen.py`) [expert]

## Group 3 — Generated profile script content (requires Group 2 design)

- [x] 3.1 Write the Lua template for `P_<hash>` bodies: static baked rule table, full MASTER guard, EXA_ALL_SESSIONS client lookup, `client_matches` helper, phase dispatch loop (TRANSLATE→EXPAND→REWRITE, pcall per rule, EXPAND first-match-terminal, setsqltext-on-change) [expert]
- [x] 3.2 Write integration tests for profile script dispatch semantics: guard, per-rule fail-open, EXPAND first-match-terminal, client_pattern literal matching, no MY_PIPELINE call (`tests/test_profile_dispatch.py`) [expert]
- [x] 3.3 Write luacheck validation for generated profile scripts

## Group 4 — MASTER_CORE extraction + swap/self-disable logic (requires Group 1, Group 2)

- [x] 4.1 Extract `PREPROC_RT.MASTER_CORE` (new `sql/lua/master_core.lua` + install entry): move MASTER's resolve→dispatch→swap into an importable LUA SCRIPT exporting `run()`; extend MY_PIPELINE pquery to include `mode` and `profile_hash` (columns 7 and 8) [expert]
- [x] 4.2 Add the self-swap step inside `MASTER_CORE.run()`: after the dispatch loop, before setsqltext, check mode/no-rules/profile_hash and issue the ALTER SESSION pquery wrapped in pcall; fail-open on any error [expert]
- [x] 4.3 Convert `master.lua` to a thin wrapper: `import("PREPROC_RT.MASTER_CORE", "mc"); mc.run()` with no inline dispatch/swap logic; preserve the MASTER header safety-contract comment [expert]
- [x] 4.4 Update luacheck globals declarations in both `master.lua` and `master_core.lua` if needed
- [x] 4.5 Write integration tests for MASTER swap behavior: self-disable in convenience mode, profile swap, enforced-mode no-swap, self_disable_allowed=FALSE no-swap, swap-failure fail-open (`tests/test_session_optimizer.py`) [expert]
- [x] 4.6 Write integration test: first statement is correctly dispatched by MASTER (via MASTER_CORE) before the swap takes effect
- [x] 4.7 Write integration test asserting MASTER delegates to MASTER_CORE (master.lua is a thin import wrapper with no duplicated dispatch/swap logic) (`tests/test_master_skeleton.py`)

## Group 6 — Profile cleanup: retire then reclaim (requires Group 1 DDL, Group 2 codegen, Group 4 MASTER_CORE)

- [x] 6.3 Write the retirement-stub Lua template as the SAME thin wrapper MASTER uses: `import("PREPROC_RT.MASTER_CORE", "mc"); mc.run()` (requires Group 4 MASTER_CORE) [expert]
- [x] 6.4 Add the GENERATE re-adoption handling to `REFRESH_CORE.rebuild()`: when a current chain resolves to a hash whose `PREPROC.PROFILES.orphaned_at` is non-NULL, generate phase emits the real baked body and clears `orphaned_at` back to NULL
- [x] 6.5 Add the RETIRE phase to `REFRESH_CORE.rebuild()`: for each `PREPROC.PROFILES` hash not in any current RESOLUTION row AND `orphaned_at IS NULL`, `CREATE OR REPLACE` the stub body and stamp `orphaned_at = CURRENT_TIMESTAMP`; skip hashes already retired
- [x] 6.6 Add the RECLAIM phase to `REFRESH_CORE.rebuild()`: compute the unpinned criterion (`no active EXA_ALL_SESSIONS.LOGIN_TIME < orphaned_at`) and for each qualifying retired hash issue `DROP SCRIPT PREPROC_RT.P_<hash>` and delete its `PREPROC.PROFILES` row [expert]
- [x] 6.7 Enforce the fixed step order in `rebuild()`: generate → retire → reclaim → stamp → single COMMIT; verify no committed RESOLUTION row can reference a dropped hash [expert]
- [x] 6.8 Write integration tests for retire: orphaned hash gets stub + `orphaned_at` stamp and is not dropped on orphaning refresh; already-retired hash not re-stamped; currently-referenced hash never retired (`tests/test_profile_cleanup.py`)
- [x] 6.9 Write integration tests for the retired-profile correctness path: session pinned to retired stub re-runs full MASTER logic for the current statement, correctly transforms the triggering statement, re-points to correct current target; stub honors control-statement guard; stub fail-open on re-point failure (`tests/test_profile_cleanup.py`) [expert]
- [x] 6.10 Write integration tests for reclaim: not dropped while active session logged in before `orphaned_at`; dropped once none did; zero-active-sessions drops all retired; re-adopted hash has `orphaned_at` cleared + real body restored and is not dropped (`tests/test_profile_cleanup.py`) [expert]
- [x] 6.11 Write integration test for cleanup atomicity: reader never sees RESOLUTION referencing a dropped/missing profile hash; DROP + RESOLUTION rebuild commit together (`tests/test_profile_cleanup.py`)
- [x] 6.12 Write luacheck validation for the generated stub script body

## Group 7 — Verification and documentation (requires Groups 2-4 and Group 6 complete)

- [x] 7.1 Update `REFRESH_CORE` header comment to document the generate/retire/reclaim cleanup steps, never-same-refresh-drop rule, login-time reclaim criterion, and catalog-sourced stub
- [x] 7.2 Update `MASTER` header comment to document the single canonical inline dispatcher (MASTER_CORE import design abandoned — Exasol top-level-only dynamic import), the self-swap step, and mode gating
- [x] 7.3 Regenerate `deploy_all.sql` via `preproc bundle` (MASTER_CORE removed; MASTER is the single dispatcher; stub sourced from MASTER's catalog body)

## Verification

- [x] V.1 Run `uv run pytest` — 0 failures
- [x] V.2 Run `uv run ruff check . && uv run ruff format --check .` — 0 errors
- [x] V.3 Run `luacheck sql/lua/` — 0 errors/warnings
