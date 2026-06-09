# Plan: add-zero-overhead-session-paths

## Summary

Implements mission Core Capability #3: in convenience mode, the MASTER dispatcher self-optimizes on a session's first statement by issuing an ALTER SESSION that either clears the preprocessor entirely (no-rule sessions, ~0 ms/stmt) or repoints it to a generated, hash-named profile script `PREPROC_RT.P_<hash>` with the user's function set baked in (~0.5 ms/stmt vs. the +7.3 ms/stmt MASTER lookup floor). Profile scripts are generated eagerly during the refresh path under admin context; MASTER never generates them on demand.

## Design

### Context

The current MASTER dispatch path pays ~7.3 ms per statement for the `MY_PIPELINE` pquery. In enforced mode this is unavoidable (ALTER SESSION is prohibited). In convenience mode, MASTER can repoint the session to a faster preprocessor after the first statement — removing the lookup for all subsequent statements. Two paths exist: users with no rules clear the session preprocessor entirely (self-disable); users with rules swap to a profile script whose rule chain is hardcoded.

- **Goals** — Eliminate the per-statement MY_PIPELINE overhead for convenience-mode sessions after statement 1; keep the full MASTER safety contract intact; never block or corrupt user SQL.
- **Non-Goals** — Enforced-mode overhead reduction (not possible without engine changes); on-demand profile creation by MASTER (blocked by invoker rights — ADR-001). Cleanup of orphaned profile scripts is now IN SCOPE via the generational retire-then-reclaim lifecycle (decisions [11]-[13]); the never-same-refresh-drop rule remains the hard safety constraint.

### Decision

#### Architecture

```
Refresh path (admin context, PREPROC_ADMIN invoker rights):
  REFRESH_CORE.rebuild()
      ├─ [existing] capture fingerprint
      ├─ [existing] DELETE/INSERT RESOLUTION
      ├─ [new] compute profile_hash per distinct user chain
      ├─ [new] UPDATE RESOLUTION.profile_hash for all rows per user
      ├─ [new] GENERATE: CREATE OR REPLACE PREPROC_RT.P_<hash> (real body) for each current chain;
      │         clear PROFILES.orphaned_at = NULL for any re-adopted hash
      ├─ [new] INSERT OR UPDATE PREPROC.PROFILES record per current hash
      ├─ [new] RETIRE: for each PROFILES hash not in any current chain AND orphaned_at IS NULL:
      │         CREATE OR REPLACE PREPROC_RT.P_<hash> (bounce-stub body) + stamp orphaned_at
      ├─ [new] RECLAIM: for each retired hash (orphaned_at IS NOT NULL) that is provably unpinned
      │         (no active EXA_ALL_SESSIONS.LOGIN_TIME < orphaned_at): DROP SCRIPT + delete PROFILES row
      ├─ [existing] stamp SETTINGS
      └─ COMMIT  (generate→retire→reclaim all inside the single commit — atomicity preserved)

MY_PIPELINE view (extended):
      + mode column (from PREPROC.SETTINGS)
      + profile_hash column (from PREPROC.RESOLUTION, NULL for no-rule users)

MASTER_CORE.run() (shared importable module — single source of truth for resolve→dispatch→swap;
                   mirrors the PREPROC.REFRESH_CORE import() precedent):
      [existing] guard ALTER SESSION/SYSTEM → return early
      [existing] pquery MY_PIPELINE (now reads mode + profile_hash too)
      [existing] no-rules sentinel check
      [existing] client resolution
      [existing] phase dispatch loop (per-rule pcall fail-open)
      [new]      attempt self_swap:
                   if mode != 'convenience' → skip
                   else if phase_sentinel (no rules) and self_disable_allowed → pquery ALTER SESSION SET ... = ''
                   else if profile_hash != null → pquery ALTER SESSION SET ... = PREPROC_RT.P_<hash>
                   (wrap in pcall; any failure → leave session on current script, continue)
      [existing] setsqltext-on-change

MASTER (thin wrapper):  import("PREPROC_RT.MASTER_CORE", "mc"); mc.run()
      (no inline dispatch/swap logic — delegates entirely to the shared core)

PREPROC_RT.P_<hash> (generated Lua, per distinct chain):
      static baked rule table (no MY_PIPELINE pquery)
      full MASTER guard (ALTER SESSION/SYSTEM → return early)
      EXA_ALL_SESSIONS client lookup (same as MASTER)
      phase dispatch loop (identical semantics)
      setsqltext-on-change

PREPROC_RT.P_<hash> (RETIREMENT STUB body, replaces the baked body on retire):
      import("PREPROC_RT.MASTER_CORE", "mc"); mc.run()   -- SAME thin wrapper as MASTER
      → functionally identical to MASTER: re-runs full resolve→dispatch→swap for the
        CURRENT statement (looks up the now-current pipeline, transforms THIS statement
        correctly, re-points the session via ALTER SESSION to the correct current target —
        P_<new-hash>, self-disable, or MASTER). NO untransformed-statement gap.
      control-stmt guard, per-rule pcall fail-open, swap fail-open, setsqltext-on-change
        all come from MASTER_CORE (single source of truth — no third copy of dispatch logic).
```

#### Patterns

| Pattern | Where | Why |
|---------|-------|-----|
| Content-addressed naming (`P_<hash>`) | Profile scripts | Idempotent CREATE OR REPLACE; sessions pinned to a hash always find a valid script |
| Never same-refresh drop | `REFRESH_CORE` retire/reclaim | A profile is never dropped on the refresh that orphans it — a just-pinned session is still active and the drop cannot be proven safe (catastrophic lockout — same hazard as ADR-002 enforced-mode grant omission) |
| Generational retire-then-reclaim | `REFRESH_CORE` cleanup phases | Orphaned hashes are first retired to a bounce stub + `orphaned_at` stamp, then reclaimed on a later refresh once provably unpinned; the pinned-session set is fixed at orphan-time and only shrinks (decisions [11]-[13]) |
| Login-time reclaim criterion | `REFRESH_CORE` reclaim phase | Drop only when no active session has `LOGIN_TIME < orphaned_at`; any pinnable session logged in before orphan-time, so this is provably safe and needs no new bookkeeping (decision [12]) |
| Shared dispatch core (`MASTER_CORE.run()`) | `master.lua`, retired `P_<hash>` body | MASTER's resolve→dispatch→swap is factored into the importable `PREPROC_RT.MASTER_CORE` (mirrors the `PREPROC.REFRESH_CORE` precedent). MASTER and the retirement stub are the SAME thin wrapper, so a retired profile is functionally identical to MASTER and there is one tested source of truth — no third copy of dispatch semantics (decision [13]) |
| Retirement stub == MASTER (no transform gap) | retired `P_<hash>` body | The stub re-runs full resolve→dispatch→swap for the CURRENT statement: it transforms that statement under the now-current pipeline and re-points the session to the correct current target. The owner rejected the earlier pass-through-and-bounce design because it ran one statement untransformed (decision [13]) |
| Eager codegen under admin context | `REFRESH_CORE.rebuild()` | MASTER runs with invoker rights (ADR-001); an ordinary user cannot CREATE SCRIPT in PREPROC_RT — so on-demand creation by MASTER is impossible |
| Fail-open ALTER SESSION | `master.lua` swap step | The swap is a performance optimization layered on the existing safety contract (ADR-004); a swap failure MUST NOT weaken it |
| Hash manifest over ordered chain | `REFRESH_CORE` | Different chains, client_patterns, or ordinal orders produce different hashes; two users with identical but differently-ordered rules get distinct profiles |
| Single MY_PIPELINE read surfaces profile_hash | `MY_PIPELINE` view | Avoids a Lua re-hash (Lua/SQL hash-mismatch hazard); preserves the single-lookup design; no direct admin-table access from invoker-rights MASTER |

### Consequences

| Decision | Alternatives Considered | Rationale |
|----------|------------------------|-----------|
| Eager profile generation in REFRESH_CORE (DB-side Lua) | On-demand by MASTER; Python CLI codegen | ADR-001 blocks on-demand (invoker rights); ADR-007 blocks Python codegen at runtime |
| Never same-refresh drop; generational retire-then-reclaim | Same-refresh drop; tombstone-forever; out-of-band cleanup job | Same-refresh drop fails catastrophically (ADR-002 analogue); tombstone-forever leaks scripts + PROFILES rows; an out-of-band job adds a second place to get the race wrong. Folding retire+reclaim into the atomic rebuild keeps one source of truth (decision [11]) |
| Login-time reclaim criterion (`no active LOGIN_TIME < orphaned_at`) | Per-session pinned-hash tracking; reference counting; time-based TTL | Login-time needs no new bookkeeping and is provably safe: any pinnable session logged in before orphan-time. Conservative only in delaying cleanup, never in dropping unsafely (decision [12]) |
| Retirement stub re-runs full MASTER logic for the current statement; MASTER and stub share an extracted `MASTER_CORE` | Pass-through + bounce (one-statement gap); stub bakes a third copy of dispatch logic; stub raises; stub no-ops | Pass-through ran one statement untransformed (skipping e.g. a fail-open-sensitive REWRITE rule) — REJECTED by the product owner. Baking a third copy re-introduces the decision-[7] divergence hazard; raising violates fail-open ADR-004; no-op never converges. Extracting `MASTER_CORE` makes "stub == MASTER" true by construction with one source of truth — CHOSEN (decision [13]) |
| ALTER SESSION issued via pquery inside MASTER | Issued before setsqltext; issued separately | pquery from inside a preprocessor is not itself preprocessed (no recursion); must be issued before setsqltext so the call order is: dispatch → swap → commit text |
| profile_hash column on RESOLUTION + MY_PIPELINE column | Separate per-user mapping table; MASTER re-hashes in Lua | Single source of truth; no cross-language hash mismatch; existing view is the only read path for invoker-rights MASTER |
| self_disable: convenience + self_disable_allowed both required | self_disable_allowed alone | mode is the system posture gate; self_disable_allowed is the per-deployment safety toggle — two independent axes |
| Profile codegen in same COMMIT as RESOLUTION rebuild | Separate post-commit step | Atomicity invariant (ADR-010): RESOLUTION.profile_hash must never reference a not-yet-existing script |

## Features

| Feature | Status | Spec |
|---------|--------|------|
| `runtime/session-optimizer` | NEW | `specs/_plans/add-zero-overhead-session-paths/runtime/session-optimizer/spec.md` |
| `install/schema-layout` | CHANGED | `specs/_plans/add-zero-overhead-session-paths/install/schema-layout/spec.md` |
| `install/master-skeleton` | CHANGED | `specs/_plans/add-zero-overhead-session-paths/install/master-skeleton/spec.md` |
| `refresh/role-resolution` | CHANGED | `specs/_plans/add-zero-overhead-session-paths/refresh/role-resolution/spec.md` |

## Migration

| Current | New |
|---------|-----|
| `PREPROC.RESOLUTION` — no `profile_hash` column | Add `profile_hash VARCHAR(64)` nullable column |
| `PREPROC.PROFILES` — `(profile_hash, manifest, created_at)` | Add `orphaned_at TIMESTAMP` nullable column via the `--@tolerate-exists` `ALTER TABLE ADD COLUMN` pattern in `sql/install/25_column_upgrades.sql` (and in `20_tables.sql` for fresh installs) |
| `PREPROC_RT.MY_PIPELINE` — exposes `self_disable_allowed, phase, client_pattern, script_name, function_name, ordinal` | Also exposes `mode` (from SETTINGS) and `profile_hash` (from RESOLUTION, NULL for no-rule users) |
| `PREPROC.REFRESH_CORE.rebuild()` — fingerprint + DELETE/INSERT RESOLUTION + stamp + COMMIT | Add generate (profile codegen) + retire + reclaim steps between INSERT and the single COMMIT, in that fixed order |
| `PREPROC_RT.MASTER` — reads 6 columns from MY_PIPELINE; inline dispatch | Becomes a thin wrapper: `import("PREPROC_RT.MASTER_CORE", "mc"); mc.run()` — all resolve→dispatch→swap logic moves into the new shared module |
| _(new)_ `PREPROC_RT.MASTER_CORE` — does not exist | New importable LUA SCRIPT exporting `run()`; the single source of truth for resolve (8 columns) → dispatch → swap/self-disable; imported by both MASTER and the retirement stub (mirrors the `PREPROC.REFRESH_CORE` pattern) |

## Implementation Tasks

### Group 1 — DDL and view changes (prerequisite for everything)

- [ ] 1.1 Add `profile_hash VARCHAR(64)` nullable column to `PREPROC.RESOLUTION` in `sql/install/20_tables.sql`
- [ ] 1.2 Extend `PREPROC_RT.MY_PIPELINE` in `sql/install/40_views.sql` to expose `mode` (from SETTINGS) and `profile_hash` (from RESOLUTION)
- [ ] 1.3 Write integration tests for the updated view: NULL `profile_hash` for no-rule user, correct columns present (`test_schema_layout.py`)

### Group 2 — Profile codegen in REFRESH_CORE (requires Group 1)

- [ ] 2.1 Design the hash manifest format: ordered `phase|ordinal|script_name|function_name|client_pattern` concatenated with `||` separators, hashed with `HASH_MD5` in SQL; document it in `REFRESH_CORE` header [expert]
- [ ] 2.2 Add per-user hash computation SQL to `REFRESH_CORE.rebuild()`: a single `UPDATE PREPROC.RESOLUTION SET profile_hash = <computed_hash> WHERE user_name = ...` step after the INSERT, covering all distinct user chains [expert]
- [ ] 2.3 Add profile script generation Lua to `REFRESH_CORE.rebuild()`: for each distinct `profile_hash` in the freshly inserted RESOLUTION rows, generate the Lua body and `CREATE OR REPLACE LUA SCRIPT PREPROC_RT.P_<hash>` via pquery [expert]
- [ ] 2.4 Add `PREPROC.PROFILES` upsert inside `REFRESH_CORE.rebuild()`: `MERGE` or `INSERT … WHERE NOT EXISTS` for each generated profile hash + manifest + created_at
- [ ] 2.5 Ensure all codegen steps happen before `COMMIT` in `rebuild()` (verify ordering in code)
- [ ] 2.6 Write integration tests for profile codegen: profile script created after refresh, idempotent on second refresh, hash changes when chain changes (`tests/test_profile_codegen.py`) [expert]

### Group 3 — Generated profile script content (requires Group 2 design)

- [ ] 3.1 Write the Lua template for `P_<hash>` bodies: static baked rule table, full MASTER guard (ALTER SESSION/SYSTEM early-return), EXA_ALL_SESSIONS client lookup, `client_matches` helper (literal substring, plain find), phase dispatch loop (TRANSLATE → EXPAND → REWRITE, pcall per rule, EXPAND first-match-terminal, setsqltext-on-change) [expert]
- [ ] 3.2 Write integration tests for profile script dispatch semantics: guard, per-rule fail-open, EXPAND first-match-terminal, client_pattern literal matching, no MY_PIPELINE call (`tests/test_profile_dispatch.py`) [expert]
- [ ] 3.3 Write luacheck validation for generated profile scripts (either via a test that generates a sample and runs luacheck, or via a static generated-script fixture)

### Group 4 — MASTER_CORE extraction + swap/self-disable logic (requires Group 1, Group 2)

- [ ] 4.1 Extract `PREPROC_RT.MASTER_CORE` (new `sql/lua/master_core.lua` + install entry): move MASTER's resolve→dispatch→swap into an importable LUA SCRIPT exporting a top-level `run()` (mirroring `PREPROC.REFRESH_CORE`); inside `run()`, extend the `MY_PIPELINE` pquery column list to include `mode` and `profile_hash` (columns 7 and 8) [expert]
- [ ] 4.2 Add the self-swap step inside `MASTER_CORE.run()`: after the dispatch loop, before setsqltext, check mode/no-rules/profile_hash and issue the ALTER SESSION pquery wrapped in pcall; fail-open on any error so the session stays on its current script [expert]
- [ ] 4.3 Convert `master.lua` to a thin wrapper that `import("PREPROC_RT.MASTER_CORE", "mc"); mc.run()` and contains no inline dispatch/swap logic; preserve the MASTER header safety-contract comment [expert]
- [ ] 4.4 Update luacheck globals declarations in both `master.lua` and `master_core.lua` if needed
- [ ] 4.5 Write integration tests for MASTER swap behavior: self-disable in convenience mode, profile swap, enforced-mode no-swap, self_disable_allowed=FALSE no-swap, swap-failure fail-open (`tests/test_session_optimizer.py`) [expert]
- [ ] 4.6 Write integration test: first statement is correctly dispatched by MASTER (via MASTER_CORE) before the swap takes effect
- [ ] 4.7 Write integration test asserting MASTER delegates to MASTER_CORE (master.lua is a thin import wrapper with no duplicated dispatch/swap logic) (`tests/test_master_skeleton.py`)

### Group 6 — Profile cleanup: retire then reclaim (requires Group 1 DDL, Group 2 codegen)

- [ ] 6.1 Add the `orphaned_at TIMESTAMP` nullable column to `PREPROC.PROFILES` in both `sql/install/20_tables.sql` (fresh installs) and `sql/install/25_column_upgrades.sql` (idempotent `--@tolerate-exists` `ALTER TABLE ... ADD COLUMN` for existing installs)
- [ ] 6.2 Write integration tests for the `orphaned_at` column: present and nullable on a fresh install, and the column upgrade is a no-op on re-apply (`tests/test_schema_layout.py`)
- [ ] 6.3 Write the retirement-stub Lua template as the SAME thin wrapper MASTER uses: `import("PREPROC_RT.MASTER_CORE", "mc"); mc.run()`. The retired profile is thereby functionally identical to MASTER — it re-runs the full resolve→dispatch→swap for the CURRENT statement (transforms the triggering statement under the now-current pipeline; re-points the session via ALTER SESSION to the correct current target: `PREPROC_RT.P_<new-hash>`, self-disable, or MASTER). NO pass-through, NO untransformed gap; control-guard / per-rule fail-open / swap fail-open / setsqltext-on-change all inherited from MASTER_CORE (requires Group 4 MASTER_CORE extraction) [expert]
- [ ] 6.4 Add the GENERATE re-adoption handling to `REFRESH_CORE.rebuild()`: when a current chain resolves to a hash whose `PREPROC.PROFILES.orphaned_at` is non-NULL, the generate phase emits the real baked body (not the stub) and clears `orphaned_at` back to NULL
- [ ] 6.5 Add the RETIRE phase to `REFRESH_CORE.rebuild()`: for each `PREPROC.PROFILES` hash not referenced by any current RESOLUTION row AND with `orphaned_at IS NULL`, `CREATE OR REPLACE` the stub body and stamp `orphaned_at = CURRENT_TIMESTAMP`; skip hashes already retired
- [ ] 6.6 Add the RECLAIM phase to `REFRESH_CORE.rebuild()`: compute the unpinned criterion (`no active EXA_ALL_SESSIONS.LOGIN_TIME < orphaned_at`, incl. zero-active-sessions ⇒ all reclaimable) and for each qualifying retired hash issue `DROP SCRIPT PREPROC_RT.P_<hash>` and delete its `PREPROC.PROFILES` row [expert]
- [ ] 6.7 Enforce the fixed step order in `rebuild()`: generate → retire → reclaim → stamp → single COMMIT (all DDL before the COMMIT); verify no committed RESOLUTION row can reference a dropped hash [expert]
- [ ] 6.8 Write integration tests for retire: orphaned hash gets a stub + `orphaned_at` stamp and is not dropped on the orphaning refresh; already-retired hash is not re-stamped; currently-referenced hash is never retired (`tests/test_profile_cleanup.py`)
- [ ] 6.9 Write integration tests for the retired-profile correctness path: a session pinned to a retired stub re-runs full MASTER logic for the CURRENT statement — the triggering statement is correctly transformed under the now-current pipeline (assert the now-current REWRITE tag appears in the triggering statement's output; explicitly assert it is NOT returned unchanged when the pipeline would transform it) AND the session is re-pointed to the correct current target (`P_<new-hash>` / self-disable / MASTER); stub honors the control-statement guard; stub is fail-open on re-point failure while still transforming the statement (`tests/test_profile_cleanup.py`) [expert]
- [ ] 6.10 Write integration tests for reclaim: not dropped while an active session logged in before `orphaned_at`; dropped once none did; zero-active-sessions drops all retired; re-adopted hash has `orphaned_at` cleared + real body restored and is not dropped (`tests/test_profile_cleanup.py`) [expert]
- [ ] 6.11 Write integration test for cleanup atomicity: a reader never observes RESOLUTION referencing a dropped/missing profile hash; DROP + RESOLUTION rebuild commit together (`tests/test_profile_cleanup.py`)
- [ ] 6.12 Write luacheck validation for the generated stub script body

### Group 7 — Verification and documentation

- [ ] 7.1 Update `REFRESH_CORE` header comment to document the generate/retire/reclaim cleanup steps, the never-same-refresh-drop rule, and the login-time reclaim criterion
- [ ] 7.2 Update the `MASTER` / `MASTER_CORE` header comments to document the MASTER_CORE extraction (single source of truth for resolve→dispatch→swap, imported by both MASTER and the retirement stub), the self-swap step, and the mode gating
- [ ] 7.3 Update `deploy_all.sql` (regenerate via `preproc bundle`) after all code changes, ensuring the new `PREPROC_RT.MASTER_CORE` script is installed before `PREPROC_RT.MASTER` (import dependency)

## Parallelization

| Parallel Group | Tasks |
|----------------|-------|
| Group 1 (DDL) | 1.1, 1.2, 1.3 — all can run concurrently |
| Group 2 start | 2.1 (hash design) unblocks 2.2, 2.3 sequentially; 2.4, 2.5 follow 2.3 |
| Group 3 + 4 in parallel | Once 2.1 is complete: Group 3 (profile Lua template) and Group 4 (MASTER_CORE extraction + MASTER changes) can be authored in parallel; they share the profile_hash column contract from Group 1 |
| Group 6 DDL | 6.1, 6.2 (orphaned_at column) can run concurrently with Group 1 |
| Group 6 stub template | 6.3 (stub = MASTER_CORE thin wrapper) and 6.12 (stub luacheck) require Group 4's MASTER_CORE extraction (4.1–4.3) to exist first; author them after Group 4 |
| Group 6 phases | 6.4, 6.5, 6.6, 6.7 are sequential within rebuild() and require Group 2's codegen step to exist; 6.8-6.11 follow their respective phase tasks |
| Group 7 | After Groups 2-4 and Group 6 are complete |

Sequential dependencies:
- Group 1 → Groups 2, 3, 4, 6 (DDL columns must exist before code reads them)
- Group 2.1 → Groups 2.2, 2.3, 3.1 (hash manifest format drives both codegen and template)
- Group 2 (generate step) → Group 6.4-6.7 (retire/reclaim extend the same rebuild() codegen step)
- Group 4 (MASTER_CORE extraction, 4.1-4.3) → Group 6.3 (stub IS the MASTER_CORE thin wrapper — the core must exist first)
- Group 6.3 (stub template) → Group 6.5 (retire installs the stub body)
- Groups 2, 3, 4, 6 → Group 7

## Dead Code Removal

| Type | Location | Reason |
|------|----------|--------|
| None | — | This slice adds new behavior; no existing code is removed. The `PREPROC.PROFILES` table already exists (20_tables.sql) but is currently unused — this slice populates it. |

## Verification

### Scenario Coverage

| Scenario | Test Type | Test Location | Test Name |
|----------|-----------|---------------|-----------|
| No-rule session self-disables in convenience mode | Integration | `tests/test_session_optimizer.py` | `test_no_rule_session_self_disables` |
| Self-disable not attempted when self_disable_allowed is FALSE | Integration | `tests/test_session_optimizer.py` | `test_self_disable_not_attempted_when_disabled` |
| Rule-bearing session swaps to profile script | Integration | `tests/test_session_optimizer.py` | `test_rule_bearing_session_swaps_to_profile` |
| Profile swap uses profile_hash from MY_PIPELINE | Integration | `tests/test_session_optimizer.py` | `test_swap_uses_hash_from_view` |
| Enforced mode — swap never attempted | Integration | `tests/test_session_optimizer.py` | `test_enforced_mode_no_swap` |
| First statement dispatched before swap | Integration | `tests/test_session_optimizer.py` | `test_first_statement_dispatched_before_swap` |
| Swap failure leaves session on MASTER, fail open | Integration | `tests/test_session_optimizer.py` | `test_swap_failure_fail_open` |
| Self-disable failure leaves session on MASTER, fail open | Integration | `tests/test_session_optimizer.py` | `test_self_disable_failure_fail_open` |
| Profile script reproduces full dispatch semantics | Integration | `tests/test_profile_dispatch.py` | `test_profile_full_dispatch_semantics` |
| Profile script reproduces client_pattern literal-substring matching | Integration | `tests/test_profile_dispatch.py` | `test_profile_client_pattern_literal_match` |
| Profile script for no-rule user is never generated | Integration | `tests/test_profile_codegen.py` | `test_no_profile_for_empty_chain` |
| Eager profile generation at refresh time | Integration | `tests/test_profile_codegen.py` | `test_profile_generated_after_refresh` |
| Content-addressed profiles are idempotent | Integration | `tests/test_profile_codegen.py` | `test_profile_idempotent_on_re_refresh` |
| Hash manifest distinguishes chains by rules, order, client_pattern | Unit | `tests/test_profile_codegen.py` | `test_hash_manifest_distinctness` |
| Profile hash carried in RESOLUTION and surfaced via MY_PIPELINE | Integration | `tests/test_profile_codegen.py` | `test_profile_hash_in_view` |
| Refresh never drops an existing profile script | Integration | `tests/test_profile_codegen.py` | `test_refresh_never_drops_profile` |
| Profile generation preserves atomicity | Integration | `tests/test_profile_codegen.py` | `test_profile_codegen_atomicity` |
| Profile codegen is DB-side Lua | Integration | `tests/test_profile_codegen.py` | `test_codegen_is_db_side` |
| Refresh computes profile_hash for every non-empty chain | Integration | `tests/test_profile_codegen.py` | `test_resolution_profile_hash_populated` |
| Profile_hash changes when chain changes | Integration | `tests/test_profile_codegen.py` | `test_hash_changes_when_chain_changes` |
| Rebuild atomicity preserved with profile codegen | Integration | `tests/test_profile_codegen.py` | `test_rebuild_atomicity_with_profiles` |
| Profile codegen runs after every CONFIG mutation | Integration | `tests/test_profile_codegen.py` | `test_profile_regen_on_mutation` |
| PREPROC.RESOLUTION has profile_hash column | Integration | `tests/test_schema_layout.py` | `test_resolution_has_profile_hash_column` |
| MY_PIPELINE exposes mode and profile_hash | Integration | `tests/test_schema_layout.py` | `test_pipeline_view_exposes_mode_and_hash` |
| MASTER reads mode and profile_hash from MY_PIPELINE | Integration | `tests/test_session_optimizer.py` | `test_master_reads_mode_and_hash_from_view` |
| MASTER delegates resolve→dispatch→swap to the shared MASTER_CORE module | Integration | `tests/test_master_skeleton.py` | `test_master_delegates_to_master_core` |
| Retirement stub uses the identical MASTER_CORE (stub and MASTER share one dispatch contract) | Integration | `tests/test_profile_cleanup.py` | `test_stub_uses_master_core` |
| MASTER swap logic passes luacheck | Unit | `tests/test_master_skeleton.py` | `test_master_passes_luacheck` |
| MASTER_CORE passes luacheck | Unit | `tests/test_master_skeleton.py` | `test_master_core_passes_luacheck` |
| Orphaned profile is retired (stub + orphaned_at), not dropped, on the orphaning refresh | Integration | `tests/test_profile_cleanup.py` | `test_orphan_retired_not_dropped` |
| Already-retired profile is not re-stamped | Integration | `tests/test_profile_cleanup.py` | `test_retired_profile_not_restamped` |
| Pinned session re-runs full MASTER logic for the current statement and re-points to the correct current target | Integration | `tests/test_profile_cleanup.py` | `test_retired_session_reruns_master_logic` |
| Retired stub transforms the triggering statement under the now-current pipeline (no untransformed gap) | Integration | `tests/test_profile_cleanup.py` | `test_stub_transforms_triggering_statement` |
| Retirement stub honors the control-statement guard | Integration | `tests/test_profile_cleanup.py` | `test_stub_honors_control_guard` |
| Retirement stub is fail-open on re-point failure while still transforming the statement | Integration | `tests/test_profile_cleanup.py` | `test_stub_fail_open` |
| Retired profile NOT reclaimed while an active session logged in before orphaned_at | Integration | `tests/test_profile_cleanup.py` | `test_not_reclaimed_while_pinnable_session_active` |
| Retired profile IS reclaimed once no active session logged in before orphaned_at | Integration | `tests/test_profile_cleanup.py` | `test_reclaimed_when_no_pinnable_session` |
| Zero active sessions makes all retired profiles reclaimable | Integration | `tests/test_profile_cleanup.py` | `test_zero_sessions_reclaims_all` |
| Re-adopted hash has orphaned_at cleared and real body restored, not dropped | Integration | `tests/test_profile_cleanup.py` | `test_readopted_hash_restored` |
| Cleanup never retires or drops a currently-referenced profile | Integration | `tests/test_profile_cleanup.py` | `test_current_profile_never_cleaned` |
| Cleanup atomicity — reader never sees RESOLUTION referencing a dropped hash | Integration | `tests/test_profile_cleanup.py` | `test_cleanup_atomicity` |
| Rebuild runs generate, retire, reclaim in fixed order within the atomic envelope | Integration | `tests/test_profile_cleanup.py` | `test_cleanup_phase_order_atomic` |
| PREPROC.PROFILES gains a nullable orphaned_at column via idempotent upgrade | Integration | `tests/test_schema_layout.py` | `test_profiles_has_orphaned_at_column` |
| orphaned_at column upgrade is a no-op on re-apply | Integration | `tests/test_schema_layout.py` | `test_orphaned_at_upgrade_idempotent` |
| Retirement stub passes luacheck | Unit | `tests/test_profile_cleanup.py` | `test_stub_passes_luacheck` |

### Manual Testing

| Feature | Command | Expected Output |
|---------|---------|-----------------|
| Self-disable | `ALTER SESSION SET sql_preprocessor_script = PREPROC_RT.MASTER; SELECT 1; SELECT 2;` (as a user with no rules, convenience mode, self_disable_allowed=TRUE) | Statement 1 returns 1; statement 2 returns 2 without invoking MASTER (observable by checking EXA_ALL_SESSIONS.SQL_PREPROCESSOR after statement 1) |
| Profile swap | `ALTER SESSION SET sql_preprocessor_script = PREPROC_RT.MASTER; SELECT 1; SELECT 2;` (as a user with at least one resolved rule, convenience mode) | Statement 1 dispatches correctly; after statement 1, EXA_ALL_SESSIONS shows `PREPROC_RT.P_<hash>` as the session preprocessor |
| Profile codegen | `EXECUTE SCRIPT PREPROC.REFRESH();` | Returns rows_inserted and last_refresh; `SELECT * FROM PREPROC.PROFILES` shows generated hashes; `SELECT SCRIPT_NAME FROM EXA_ALL_SCRIPTS WHERE SCRIPT_SCHEMA = 'PREPROC_RT' AND SCRIPT_NAME LIKE 'P_%'` shows at least one generated script |
| Never-drop invariant | Refresh twice with unchanged rules | `P_<hash>` script is still present after second refresh; no DROP SCRIPT logged |
| Enforced mode | Set mode to `enforced`; run statements as rule-bearing user | `EXA_ALL_SESSIONS.SQL_PREPROCESSOR` remains `PREPROC_RT.MASTER` throughout the session |
| Retire on orphan | Note a current `P_<hash>`; remove the rules that produced it; `EXECUTE SCRIPT PREPROC.REFRESH();` | `P_<hash>` still EXISTS in `EXA_ALL_SCRIPTS`; `SELECT orphaned_at FROM PREPROC.PROFILES WHERE profile_hash='<hash>'` is non-NULL; the script body is now the bounce stub |
| Retired-profile re-dispatch | In a session pinned to a now-retired `P_<hash>` whose user now resolves to a current chain with a REWRITE rule, run a transforming `SELECT`; then run another `SELECT` | The first `SELECT` is CORRECTLY TRANSFORMED by the stub (the now-current REWRITE tag appears in its output — not run unchanged), and `EXA_ALL_SESSIONS.SQL_PREPROCESSOR` is re-pointed to the correct current target (`PREPROC_RT.P_<new-hash>`, or `''`/`PREPROC_RT.MASTER` per resolution); the second `SELECT` runs on that current target |
| Reclaim | After the pinned session has ended (or with no active sessions older than `orphaned_at`), run `EXECUTE SCRIPT PREPROC.REFRESH();` | `P_<hash>` no longer appears in `EXA_ALL_SCRIPTS WHERE SCRIPT_SCHEMA='PREPROC_RT'`; its `PREPROC.PROFILES` row is gone |
| No premature reclaim | While a session that logged in before `orphaned_at` is still active, run `EXECUTE SCRIPT PREPROC.REFRESH();` | `P_<hash>` is still present; `PREPROC.PROFILES` row still present with non-NULL `orphaned_at` |
| Re-adoption | Re-add the rules that produced a retired `<hash>`; `EXECUTE SCRIPT PREPROC.REFRESH();` | `SELECT orphaned_at FROM PREPROC.PROFILES WHERE profile_hash='<hash>'` is NULL again; the `P_<hash>` body is the real baked dispatch body, not the stub |

### Checklist

| Step | Command | Expected |
|------|---------|----------|
| Build | `uv sync` | Exit 0 |
| Test | `uv run pytest` | 0 failures |
| Lint | `uv run ruff check . && uv run ruff format --check .` | 0 errors/warnings |
| Lua lint | `luacheck sql/lua/` | 0 errors/warnings |
