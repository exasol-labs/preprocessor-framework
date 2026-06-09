# Verification Report: add-zero-overhead-session-paths

**Generated:** 2026-06-08

## Verdict

| Result | Details |
|--------|---------|
| **PASS** | Convenience-mode sessions self-optimize after statement 1 (self-disable for no-rule users, profile swap for rule-bearing users); profile scripts are generated eagerly in the atomic rebuild and cleaned up via the generational retire-then-reclaim lifecycle. All 167 tests pass against a live Exasol 2025.2.1 instance; lint, format, and luacheck are clean; the deploy bundle is deterministic. |

| Check | Status |
|-------|--------|
| Build (`uv sync`) | ✓ |
| Tests (`uv run pytest`) | ✓ 167 passed, 7 skipped, 0 failed |
| Lint (`ruff check`) | ✓ |
| Format (`ruff format --check`) | ✓ |
| Lua lint (`luacheck sql/lua/`) | ✓ 0 warnings / 0 errors (15 files) |
| Scenario Coverage | ✓ all plan scenarios covered (see table) |
| Manual Tests | ✓ proven live (see Manual Tests) |

## Headline: a mid-implementation architecture change

The plan's **decision [13]** — MASTER as a thin `import("PREPROC_RT.MASTER_CORE")` + `run()` wrapper, sharing one importable runtime module with the retirement stub — was found **technically infeasible** during verification and was replaced (with the product owner's approval) by a **catalog-sourced** design.

**Root cause (proven live):** in the Exasol preprocessor context, dynamic `import()` of a rule module works **only at top-level script scope**, never from inside a function of an already-imported module. The import-wrapper therefore silently fail-opened *every* dispatch (a controlled experiment returned `SEED|DISPATCHFAIL`; the equivalent top-level inline script returned `SEED|R`). This broke all 35 dispatch/command-sugar/swap tests on first full run.

**Resolution (approved, implemented, verified):**
- `sql/lua/master.lua` is now the **single canonical inline dispatcher** (control guard → 8-column `MY_PIPELINE` lookup → fail-open → `TRANSLATE/EXPAND/REWRITE` dispatch → convenience-mode self-disable / profile swap → setsqltext-on-change). It is **not** a wrapper.
- `sql/lua/master_core.lua` and the `PREPROC_RT.MASTER_CORE` object were **removed** from the codebase, `install.py`, and `bundle.py`.
- The retirement stub is built by `REFRESH_CORE` from `PREPROC_RT.MASTER`'s **own catalog body** (`EXA_ALL_SCRIPTS.SCRIPT_TEXT`, header stripped). The stub is therefore byte-for-byte MASTER — one source of truth, **stub == MASTER by construction**, contract changes propagate to every stub created/refreshed afterward. This preserves the *intent* of decision [13] (no divergent dispatch copy, stub identical to MASTER, no untransformed gap) by a means Exasol permits.

A second environmental finding: **`EXA_ALL_SESSIONS` on Exasol 2025.2.1 has no `SQL_PREPROCESSOR` column.** All swap/self-disable/re-point assertions are verified **behaviorally** (rule-divergence using a keeper-user technique that keeps the orphaned hash referenced so a stale baked profile stays distinguishable from a fresh MASTER lookup), never by reading that column.

The spec delta `install/master-skeleton/spec.md` was updated to describe the inline + catalog-sourced design (MASTER_CORE scenarios rewritten/removed). The plan.md historical decision [13] text remains as the record of the original intent; reconcile during `/speq:record`.

## Test Evidence

### Test Results

| Type | Passed | Skipped | Failed |
|------|--------|---------|--------|
| Integration (live Exasol) | 161 | 7 | 0 |
| Unit (luacheck/source, no DB) | 6 | 0 | 0 |
| **Total** | **167** | **7** | **0** |

New/changed test files: `test_profile_codegen.py` (12), `test_profile_dispatch.py` (6), `test_session_optimizer.py` (8), `test_profile_cleanup.py` (15), `test_schema_layout.py` (16, +4 new), `test_master_skeleton.py` (8, reworked). Pre-existing suites confirmed non-regressed: `test_master_dispatch.py` (16), `test_command_sugar.py` (15).

**The 7 skips** are intentional and correct:
- 2 in `test_profile_cleanup.py` (`test_reclaimed_when_no_pinnable_session`, `test_zero_sessions_reclaims_all`) — the session-scoped suite connection logs in before any orphan, so its `LOGIN_TIME` legitimately prevents reclaim; skipping avoids asserting a false negative. The reclaim DROP path is covered unconditionally by `test_reclaim_drops_provably_unpinned_hash` (backdates `orphaned_at` to remove the session-age confound).
- 5 others are pre-existing environment/feature skips unchanged by this work.

### Manual Tests (proven live)

| Feature | Result | Evidence |
|---------|--------|----------|
| Core dispatch (statement 1 via MASTER) | ✓ | `SELECT 'SEED'` → `SEED|R` with a REWRITE rule active |
| Profile codegen at refresh | ✓ | `EXECUTE SCRIPT PREPROC.REFRESH()` → `profile_hash` populated, `PREPROC_RT.P_<hash>` present in `EXA_ALL_SCRIPTS`, `PREPROC.PROFILES` row created |
| Retired-stub re-dispatch (no untransformed gap) | ✓ | Session pinned to a now-retired `P_<hash>`; after the user's rule changed to one appending `|NEWR`, the pinned session's next `SELECT 'SEED'` returned `SEED|NEWR` (transformed under the **now-current** pipeline), proving the stub re-runs full MASTER logic and re-points |
| Retire on orphan (not dropped) | ✓ | Orphaning refresh leaves `P_<hash>` present with non-NULL `orphaned_at` and a stub body |
| Bundle determinism | ✓ | `preproc bundle` produces byte-identical `deploy_all.sql` on re-run |

## Tool Evidence

### Tests
```
167 passed, 7 skipped in 94.23s
```

### Linter / Formatter
```
ruff check .            → All checks passed!
ruff format --check .   → 23 files already formatted
```

### Lua linter
```
luacheck sql/lua/       → Total: 0 warnings / 0 errors in 15 files
```

## Scenario Coverage

| Feature | Scenario | Test Location | Test Name | Passes |
|---------|----------|---------------|-----------|--------|
| runtime/session-optimizer | No-rule session self-disables | `tests/test_session_optimizer.py` | `test_no_rule_session_self_disables` | Pass |
| runtime/session-optimizer | Self-disable not attempted when disabled | `tests/test_session_optimizer.py` | `test_self_disable_not_attempted_when_disabled` | Pass |
| runtime/session-optimizer | Rule-bearing session swaps to profile | `tests/test_session_optimizer.py` | `test_rule_bearing_session_swaps_to_profile` | Pass |
| runtime/session-optimizer | Swap uses hash from view | `tests/test_session_optimizer.py` | `test_master_reads_mode_and_hash_from_view` | Pass |
| runtime/session-optimizer | Enforced/strict mode — no swap | `tests/test_session_optimizer.py` | `test_strict_mode_no_swap` | Pass |
| runtime/session-optimizer | First statement dispatched before swap | `tests/test_session_optimizer.py` | `test_first_statement_dispatched_before_swap` | Pass |
| runtime/session-optimizer | Swap failure fail-open | `tests/test_session_optimizer.py` | `test_swap_failure_fail_open` | Pass |
| runtime/session-optimizer | Self-disable failure fail-open | `tests/test_session_optimizer.py` | `test_self_disable_failure_fail_open` | Pass |
| runtime/session-optimizer | Profile reproduces full dispatch semantics | `tests/test_profile_dispatch.py` | `test_profile_full_dispatch_semantics` | Pass |
| runtime/session-optimizer | Profile client_pattern literal match | `tests/test_profile_dispatch.py` | `test_profile_client_pattern_literal_match` | Pass |
| runtime/session-optimizer | No profile for empty chain | `tests/test_profile_codegen.py` | `test_no_profile_for_empty_chain` | Pass |
| runtime/session-optimizer | Eager generation at refresh | `tests/test_profile_codegen.py` | `test_profile_generated_after_refresh` | Pass |
| runtime/session-optimizer | Content-addressed idempotent | `tests/test_profile_codegen.py` | `test_profile_idempotent_on_re_refresh` | Pass |
| runtime/session-optimizer | Hash manifest distinctness | `tests/test_profile_codegen.py` | `test_hash_manifest_distinctness` | Pass |
| runtime/session-optimizer | Hash in RESOLUTION + view | `tests/test_profile_codegen.py` | `test_profile_hash_in_view` | Pass |
| runtime/session-optimizer | Refresh never drops a profile | `tests/test_profile_codegen.py` | `test_refresh_never_drops_profile` | Pass |
| runtime/session-optimizer | Codegen atomicity | `tests/test_profile_codegen.py` | `test_profile_codegen_atomicity` | Pass |
| runtime/session-optimizer | Codegen is DB-side | `tests/test_profile_codegen.py` | `test_codegen_is_db_side` | Pass |
| runtime/session-optimizer | profile_hash populated per chain | `tests/test_profile_codegen.py` | `test_resolution_profile_hash_populated` | Pass |
| runtime/session-optimizer | Hash changes when chain changes | `tests/test_profile_codegen.py` | `test_hash_changes_when_chain_changes` | Pass |
| runtime/session-optimizer | Rebuild atomicity with profiles | `tests/test_profile_codegen.py` | `test_rebuild_atomicity_with_profiles` | Pass |
| runtime/session-optimizer | Regen on every mutation | `tests/test_profile_codegen.py` | `test_profile_regen_on_mutation` | Pass |
| install/schema-layout | RESOLUTION has profile_hash | `tests/test_schema_layout.py` | `test_resolution_has_profile_hash_column` | Pass |
| install/schema-layout | MY_PIPELINE exposes mode + hash | `tests/test_schema_layout.py` | `test_pipeline_view_exposes_mode_and_hash` | Pass |
| install/schema-layout | PROFILES gains orphaned_at | `tests/test_schema_layout.py` | `test_profiles_has_orphaned_at_column` | Pass |
| install/schema-layout | orphaned_at upgrade idempotent | `tests/test_schema_layout.py` | `test_orphaned_at_upgrade_idempotent` | Pass |
| install/master-skeleton | MASTER is single inline dispatcher | `tests/test_master_skeleton.py` | `test_master_is_inline_dispatcher` | Pass |
| install/master-skeleton | Stub == MASTER body (catalog-sourced) | `tests/test_master_skeleton.py` / `test_profile_cleanup.py` | `test_retirement_stub_is_master_body` / `test_stub_equals_master_body` | Pass |
| install/master-skeleton | MASTER passes luacheck | `tests/test_master_skeleton.py` | `test_master_passes_luacheck` | Pass |
| install/master-skeleton | Generated profile passes luacheck | `tests/test_master_skeleton.py` | `test_generated_profile_passes_luacheck` | Pass |
| refresh/role-resolution | Orphan retired, not dropped | `tests/test_profile_cleanup.py` | `test_orphan_retired_not_dropped` | Pass |
| refresh/role-resolution | Already-retired not re-stamped | `tests/test_profile_cleanup.py` | `test_retired_profile_not_restamped` | Pass |
| refresh/role-resolution | Retired session re-runs MASTER logic | `tests/test_profile_cleanup.py` | `test_retired_session_reruns_master_logic` | Pass |
| refresh/role-resolution | Stub transforms triggering statement | `tests/test_profile_cleanup.py` | `test_stub_transforms_triggering_statement` | Pass |
| refresh/role-resolution | Stub honors control guard | `tests/test_profile_cleanup.py` | `test_stub_honors_control_guard` | Pass |
| refresh/role-resolution | Stub fail-open on re-point failure | `tests/test_profile_cleanup.py` | `test_stub_fail_open` | Pass |
| refresh/role-resolution | Not reclaimed while pinnable session active | `tests/test_profile_cleanup.py` | `test_not_reclaimed_while_pinnable_session_active` | Pass |
| refresh/role-resolution | Reclaimed when provably unpinned | `tests/test_profile_cleanup.py` | `test_reclaim_drops_provably_unpinned_hash` | Pass |
| refresh/role-resolution | Re-adopted hash restored | `tests/test_profile_cleanup.py` | `test_readopted_hash_restored` | Pass |
| refresh/role-resolution | Current profile never cleaned | `tests/test_profile_cleanup.py` | `test_current_profile_never_cleaned` | Pass |
| refresh/role-resolution | Cleanup atomicity | `tests/test_profile_cleanup.py` | `test_cleanup_atomicity` | Pass |

## Code Review

A `code-reviewer` pass found **no merge-blocking defect**. Two robustness findings were **fixed** in this slice:
1. **Stub header-strip fragility** — `master_stub_body()` now anchors the strip on the header's terminating ` AS\n` (tolerating a reflowed multi-line header) and raises if the stripped body still carries a `CREATE`/`RETURNS` remnant, rather than splitting on the first newline.
2. **RECLAIM drop-failure leak** — RECLAIM now `DROP`s first and deletes the `PREPROC.PROFILES` row **only after confirming the script is gone**, so a transient DROP failure leaves the hash retired (retried next refresh) instead of stranding an untracked script.
A stale "thin-wrapper stub" comment was corrected.

Remaining low-severity review notes (test-clarity/comment hygiene, e.g. `test_codegen_is_db_side` near-tautology, plan-vs-code `enforced`/`MASTER_CORE` naming drift in plan.md) are non-blocking and recorded for `/speq:record` reconciliation.

## Known Deviations from Plan

| Plan element | As-built | Reason |
|--------------|----------|--------|
| Decision [13]: `PREPROC_RT.MASTER_CORE` importable module; MASTER + stub are thin `import` wrappers | MASTER is the single canonical **inline** dispatcher; stub is sourced from MASTER's catalog body | Exasol dynamic `import()` works only at top-level preprocessor scope; the wrapper silently fail-opened all dispatch. Catalog-sourcing preserves the "one source of truth / stub == MASTER / no untransformed gap" intent. Approved by product owner. |
| Mode value `enforced` | `strict` | `PREPROC.SET_MODE` validates only `convenience`/`strict`; code is internally consistent. Plan/spec wording to be reconciled at record time. |
| Swap observability via `EXA_ALL_SESSIONS.SQL_PREPROCESSOR` | Behavioral verification (rule divergence) | That column does not exist on Exasol 2025.2.1. |

## Next Step

Ready for: `/speq:record add-zero-overhead-session-paths` — and at record time, reconcile the `MASTER_CORE`/`enforced` language in the permanent specs/mission to match the shipped inline + catalog-sourced design.
