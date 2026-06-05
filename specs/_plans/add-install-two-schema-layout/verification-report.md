# Verification Report: add-install-two-schema-layout

**Generated:** 2026-06-05

## Verdict

| Result | Details |
|--------|---------|
| **PASS** | All 23 tests green against live Exasol 2025.2.0; lint/format/luacheck clean; review findings fixed; all 23 plan scenarios covered by passing tests |

| Check | Status |
|-------|--------|
| Build (`uv sync`) | ✓ exit 0, 12 packages |
| Tests (`uv run pytest`) | ✓ 23 passed, 0 failed |
| Lint Python (`ruff check .`) | ✓ 0 errors |
| Lint Lua (`luacheck sql/lua/ --config .luacheckrc`) | ✓ 0 warnings / 0 errors |
| Format (`ruff format --check .`) | ✓ no changes |
| Scenario Coverage | ✓ 23/23 |
| Manual Tests | ✓ all covered (see below) |

## Test Evidence

### Test Results

| Type | Run | Passed | Failed | Notes |
|------|-----|--------|--------|-------|
| Unit (`-m "not integration"`) | 7 | 7 | 0 | env validation, skip logic, luacheck, ruff, uv sync, CLI missing-env/unreachable |
| Integration (full suite) | 23 | 23 | 0 | against live Exasol 2025.2.0 (deployment2, eu-central-1) |
| Skip behaviour (no env) | 23 collected | 7 passed / 16 skipped | 0 | verified during implementation; skip reason names missing variables |

### Scenario Coverage

All 23 scenarios from plan.md `## Verification > Scenario Coverage` map to implemented, passing tests with the exact planned names. Notable:

- `test_my_pipeline_filters_current_user` — privacy invariant (sentinel row for foreign user invisible) ✓
- `test_public_schema_grants` + ownership assertions — both ADR-bound invariants encoded ✓
- `test_lookup_failure_fails_open` — fail-open on thrown lookup error (view renamed away) ✓
- `test_normal_statement_passthrough` — additionally asserts the no-rules precondition, covering the all-NULL LEFT JOIN row shape ✓
- `test_install_prints_not_activates` — guards against vacuous pass (`before_row is not None`) ✓

### Manual Tests

| Plan manual step | How verified | Result |
|---|---|---|
| `uv sync` exit 0 | direct + `test_uv_sync_environment` | ✓ |
| `preproc install` → schemas present, SETTINGS 1 row | live run via `installed` fixture + layout tests | ✓ |
| CLI prints activation statement, setting unchanged | `test_install_prints_not_activates` against live instance (preexisting system preprocessor `TEST.LUA_PREPROCESSOR1` unchanged) | ✓ |
| Missing env → exit 2, variable named, no connection | `test_missing_env_fails_fast` | ✓ |
| `luacheck sql/lua/` 0 warnings | direct (luacheck 1.2.0) + `test_master_passes_luacheck` | ✓ |
| MASTER active session-scoped, `SELECT 1` → 1 unchanged | `test_normal_statement_passthrough` live | ✓ |

## Code Review

speq code-reviewer pass: 14 findings (1 blocker-class design/spec mismatch, 6 should-fix, 7 nits). Orchestrator triage:

- **Fail-open/no-row blocker** — ruled a spec-wording defect, not a view defect: the LEFT JOIN intentionally always returns ≥1 row so MASTER receives `SELF_DISABLE_ALLOWED` even for no-rule users. Spec reworded; master.lua extension-point docs corrected; explicit no-rules precondition added to the pass-through test. (Fix F1–F3)
- All 6 should-fix findings addressed (tolerance docstring, task-ID leak in error string, vacuous-pass guard, table-ownership assertions, CLI fallthrough, CI luacheck config). (F4–F9)
- Accepted nits: CI integration job `continue-on-error` (documented bootstrap choice — integration enforcement is a manual/live gate for now), stdout interleaving of verbose SQL with activation hint, test overlap between function-level and subprocess-level idempotency.

## Implementation Bugs Caught by Live Verification

Three contract violations surfaced only when run against a real instance (commit `008ceb0`):

1. Statement splitter split on `;` inside comments → rewritten comment-aware/line-based.
2. Exasol's duplicate-role error says "conflicts with another user or role name", not "already exists" → tolerance matcher broadened; re-install idempotency restored.
3. Schema-level grants surface as `OBJECT_NAME`/`OBJECT_TYPE='SCHEMA'` with `OBJECT_SCHEMA` NULL in `EXA_DBA_OBJ_PRIVS`; pyexasol uses `{named}` params and `options["dsn"]` → three test assertions corrected.

## Known Limitations (carried forward)

- `sql/` path resolution assumes repo-tree install; PyPI packaging requires bundling `sql/` as package data (documented in `install.py`).
- CI integration tier is `continue-on-error: true` pending docker-db boot-stability data; `lint` + `unit` jobs are the blocking gates.
- Enforced-mode behaviours (lock parameter) are not exercised in this slice — by design, activation is out of scope.

**Ready for:** `/speq:record add-install-two-schema-layout`
