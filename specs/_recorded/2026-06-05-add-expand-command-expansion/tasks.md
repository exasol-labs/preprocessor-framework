# Tasks: add-expand-command-expansion

## Group A: client_pattern fix and test updates

- [x] 1.1 Fix `client_matches` in `sql/lua/master.lua`: add `1, true` to `string.find` call to use literal substring matching
- [x] 1.2 Update `test_dispatch_client_pattern_match` docstring to reflect literal substring semantics
- [x] 1.3 Update `test_dispatch_client_pattern_no_match` docstring similarly for literal substring language

## Group B: EXPAND dispatch logic

- [x] 2.1 Add `dispatch_expand_rule` function to `sql/lua/master.lua` [expert]
- [x] 2.2 Modify the EXPAND section of the dispatch loop in `sql/lua/master.lua` to use first-match-terminal [expert]
- [x] 2.3 Ensure TRANSLATE and REWRITE phases continue to use `dispatch_rule` (verify, no changes needed)

## Group C: demo module authoring and install wiring

- [x] 3.1 Create `sql/lua/demo_ping_v1.lua` with `ping(sqltext)` function and full header comment [expert]
- [x] 3.2 Add deploy of `DEMO_PING_V1` to `src/preproc/install.py`

## Group D: integration tests

- [x] 4.1 Write integration test `test_expand_terminal_first_match` in `tests/test_master_dispatch.py`
- [x] 4.2 Write integration test `test_expand_nil_continues_to_next` in `tests/test_master_dispatch.py`
- [x] 4.3 Write integration test `test_expand_failing_module_skipped_continues` in `tests/test_master_dispatch.py`
- [x] 4.4 Write integration test `test_expand_no_match_passes_through` in `tests/test_master_dispatch.py`
- [x] 4.5 Write integration test `test_expand_rewrite_still_runs_after_match` in `tests/test_master_dispatch.py`
- [x] 4.6 Write integration test `test_demo_ping_expands` in `tests/test_master_dispatch.py` [expert]
- [x] 4.7 Write integration test `test_demo_ping_nil_for_non_ping` in `tests/test_master_dispatch.py`
- [x] 4.8 Write unit test `test_client_pattern_literal_substring` in `tests/test_master_dispatch.py`

## Group E: luacheck + full suite

- [x] 2.4 Run luacheck against `sql/lua/` and fix any warnings introduced by the new function
- [x] 5.1 Run full test suite (`uv run pytest`) — all existing tests plus new tests MUST pass
- [x] 5.2 Run luacheck (`luacheck sql/lua/`) — zero warnings across all files including new module

## Code Review Fixes

- [x] CR.1 Fix `demo_ping_v1.lua` header: add import-loading contract, gsub two-value trap warning, single-statement output requirement, and engine-syntax-error consequence of non-expansion
- [x] CR.2 Update stale "no module ships in this slice" comment in `sql/lua/master.lua` header
