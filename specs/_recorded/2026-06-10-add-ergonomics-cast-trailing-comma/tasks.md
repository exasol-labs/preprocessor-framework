# Tasks: add-ergonomics-cast-trailing-comma

## Phase 2: Implementation (Group A — Lua modules, independent)
- [x] 1.2 Author `sql/lua/ergonomics_cast_v1.lua` — single-pass scanner with backwards operand scan + type-name capture [expert]
- [x] 1.3 Author `sql/lua/ergonomics_trailing_comma_v1.lua` — single-pass scanner with structural + keyword-terminated trailing-comma removal [expert]

## Phase 2: Implementation (Group B — example .sql wrappers, depends on A)
- [x] 1.4 Author `examples/ergonomics_cast.sql` — inline Lua body (synced from sql/lua) + self-registration sugar
- [x] 1.5 Author `examples/ergonomics_trailing_comma.sql` — inline Lua body (synced from sql/lua) + self-registration sugar
- [x] 1.6 Update `examples/README.md` — add two rows to the Files table

## Phase 2: Implementation (Group C — tests, depends on A)
- [x] 2.1 Write `tests/test_ergonomics_cast.py` — pipeline/harness fixture + all cast scenarios
- [x] 2.2 Write `tests/test_ergonomics_trailing_comma.py` — pipeline/harness fixture + all trailing-comma scenarios

## Phase 3: Verification (Group D)
- [x] 2.3 luacheck passes for both new sql/lua source files
- [x] 2.4 Run new pytest files green; then full suite + ruff
- [x] 3.1 Code review of changed files — found + fixed schema-qualified function-call cast bug (see decision-log Review Findings)
- [x] 3.2 Verification report
