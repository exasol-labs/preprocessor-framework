# Plan: add-refresh-role-resolution

## Summary

Add the `refresh` slice: a `PREPROC_ADMIN`-owned database Lua script (`PREPROC.REFRESH`) that expands `ROLE`/`USER`-scoped `PREPROC.CONFIG` rules into the flat per-user `PREPROC.RESOLUTION` table — transitive nested-role expansion done in native SQL `CONNECT BY`, additive UNION (superset) of ROLE + USER rules in SQL-privilege style, atomic rebuild + `last_refresh` stamp — plus a DB-side `PREPROC.STATUS` staleness object, both callable from any SQL client by an admin; and upgrade the MASTER skeleton from pass-through to dispatching the resolved per-phase pipeline. All administration is DB-side; the Python CLI's only involvement is deploying these scripts at install time.

## Design

### Context

The bedrock slice created the two-schema layout and a safe pass-through MASTER, but nothing populates `PREPROC.RESOLUTION` and nothing dispatches it. This slice provides the decision layer's offline half (resolution) and activates the runtime half (dispatch). Three Exasol facts shape the design:

1. **Dictionary access needs `SELECT ANY DICTIONARY`.** `EXA_DBA_ROLE_PRIVS` (the role-grant graph) and `EXA_DBA_USERS` (the user universe) are visible only to holders of `SELECT ANY DICTIONARY`. Exasol Lua scripts run with **invoker rights**, so the refresh script can read those tables only if its *caller* holds the privilege. We grant `SELECT ANY DICTIONARY` to `PREPROC_ADMIN` and restrict refresh/status to that role — administration is never PUBLIC.
2. **Transitive expansion is native SQL `CONNECT BY`.** Exasol supports hierarchical queries (`CONNECT BY PRIOR` / `START WITH`) as native, unlicensed SQL. The transitive role-to-user closure is expressed as a `CONNECT BY` query over `EXA_DBA_ROLE_PRIVS`; the Lua script orchestrates (executes the SQL, owns the transaction) rather than walking the graph itself. *(This corrects the prior plan, which wrongly claimed `CONNECT BY` was a separately-licensed add-on and chose iterative Lua graph-walking. See decision-log.md "Correction to the prior plan".)*
3. **Atomicity within one script execution.** Autocommit is a client-layer setting a Lua script cannot read, but a script can group statements and issue an explicit `commit()`. The rebuild (`DELETE` all of `RESOLUTION`, reinsert resolved rows, stamp `SETTINGS.last_refresh` + fingerprint) runs as one transaction committed once at the end, so no reader observes a half-rebuilt resolution.

- **Goals**
  - Materialise enabled `CONFIG` into `RESOLUTION` per user, expanding nested roles transitively via SQL `CONNECT BY`.
  - Materialise the additive UNION (superset) of all enabled `ROLE`-derived and `USER`-scoped rules per user — no override or exclusion, SQL-privilege-style — deduplicating identical `(phase, script, function)` rows, in deterministic per-phase `rule_id`-ascending order via a new `ordinal` column.
  - Make the rebuild + `last_refresh` stamp atomic.
  - Upgrade MASTER to dispatch the resolved pipeline (phase order, client_pattern matching, per-rule fail-open) while retaining the guard and fail-open contract.
  - Expose refresh and staleness as DB-side scripts (`PREPROC.REFRESH`, `PREPROC.STATUS`) callable from any SQL client by an admin.
  - Shape those scripts so a future framework-controlled management syntax can map onto them cleanly.
- **Non-Goals**
  - No Python administration verbs (`preproc refresh` / `preproc status`) — administration is DB-side; the CLI only deploys.
  - No framework-controlled management syntax (e.g. intercepted `PREPROC REFRESH`) built in this slice — only the DB-side targets it will call.
  - No profile-script codegen / self-disable / convenience-mode short-circuit (later slice).
  - No TRANSLATE UDF (sqlglot) implementation — the dispatch path supports the `TRANSLATE` phase, but no TRANSLATE module ships here.
  - No scheduler — periodic refresh stays external (cron/CI invoking the DB-side script); this slice ships the entry point and the staleness signal only.
  - No `--activate`; activation remains a manual DBA step (ADR-003).

### Decision

Resolution and staleness logic live entirely in the database (`PREPROC.REFRESH` and `PREPROC.STATUS`, owned by `PREPROC_ADMIN`), callable from any SQL client by an admin. Transitive role expansion is a SQL `CONNECT BY` query the refresh script issues; the script owns the atomic rebuild transaction. The Python CLI gains no runtime verbs — install deploys the two new scripts. MASTER gains a dispatch loop over `MY_PIPELINE` rows layered on top of the already-tested guard/fail-open base.

#### Architecture

```
  any SQL client (admin) ──EXECUTE SCRIPT──▶ PREPROC.REFRESH (Lua, admin)
                                              │  SQL CONNECT BY over
                                              │   EXA_DBA_ROLE_PRIVS  ─┐ transitive
                                              │   START WITH user/role │ role closure
                                              │  ⨝ enabled PREPROC.CONFIG
                                              │  UNION + dedup + ordinal
                                              │  DELETE+INSERT RESOLUTION,
                                              │  stamp last_refresh + fingerprint
                                              │  (one transaction, commit)
                                              └────────────┬─────────────
  any SQL client (admin) ──EXECUTE SCRIPT──▶ PREPROC.STATUS │ (read-only)
   recompute fingerprint, compare ──────────────────────────┘ writes
                                              ┌────────────▼─────────────
                                              │ PREPROC.RESOLUTION (+ordinal)
                                              │ PREPROC.SETTINGS  (+fingerprint)
                                              └────────────┬─────────────
                                 definer-rights read │ (CURRENT_USER)
                                              ┌────────────▼─────────────
   every statement ──▶ PREPROC_RT.MASTER ── reads ── PREPROC_RT.MY_PIPELINE
        guard → lookup → dispatch phases TRANSLATE→EXPAND→REWRITE → setsqltext

  preproc install (Python, deployment only) ── ships DDL + REFRESH + STATUS + MASTER
```

#### Patterns

| Pattern | Where | Why |
|---------|-------|-----|
| Privileged DB script gated to a role | `PREPROC.REFRESH` / `PREPROC.STATUS` owned by `PREPROC_ADMIN`, `SELECT ANY DICTIONARY` granted to that role, not PUBLIC | Invoker-rights Lua can read the dictionary only if the caller holds the privilege; administration is admin-only |
| Transitive closure in native SQL CONNECT BY | role-to-user expansion query in `PREPROC.REFRESH` | `CONNECT BY` is native unlicensed Exasol SQL; declarative closure beats hand-rolled Lua traversal |
| DB-side-only administration | refresh + status are DB scripts callable from any SQL client; CLI deploys only | Keeps the management surface in the database; enables a future framework-controlled management syntax |
| Syntax-mappable admin entry points | stable names `PREPROC.REFRESH` / `PREPROC.STATUS`, simple result-returning contracts | A later intercepted `PREPROC REFRESH` statement can translate cleanly onto these calls (mission: `EXPLAIN/SHOW PREPROCESSOR`) |
| Additive UNION resolution (SQL-privilege model) | resolution materialisation | A user's pipeline is the superset of all enabled ROLE-derived + USER-scoped rules; no override/exclusion, just like SQL privileges. Admins reduce scope by removing roles / disabling rules, never by per-user subtraction |
| Deduplicate identical `(phase, script, function)` | resolution materialisation | The same function reaching a user via multiple roles or role+user paths must run once, not repeatedly; lowest contributing `rule_id` wins the `ordinal` |
| Deterministic ordering via `ordinal` column | `PREPROC.RESOLUTION.ordinal`, surfaced by `MY_PIPELINE ORDER BY phase, ordinal` | Within a phase, order is pure `rule_id` ascending with scope type ignored; `MY_PIPELINE` does not expose `rule_id`, so `ordinal` is the view-friendly carrier of that order |
| Atomic full rebuild | DELETE + INSERT + stamp + single `commit()` in the script | A half-rebuilt resolution must never be observable; rollout/rollback is a config flip |
| State fingerprint for staleness | `SETTINGS` fingerprint written at refresh, recomputed by `PREPROC.STATUS` | Cheap drift detection without diffing every grant; delegated periodic refresh needs a "do I need to run" signal |
| Dispatch layered on safe base | MASTER dispatch loop above the existing guard/fail-open code | ADR-004: the safety contract was tested first; dispatch is added without weakening it |

### Consequences

| Decision | Alternatives Considered | Rationale |
|----------|------------------------|-----------|
| Grant `SELECT ANY DICTIONARY` to `PREPROC_ADMIN`; refresh/status gated to that role | Definer-rights view over the dictionary tables exposed to PUBLIC; per-table SELECT grants | Dictionary read is administrative; exposing role/user topology to PUBLIC leaks security structure. The role grant is the minimal, auditable bridge |
| Transitive expansion in native SQL `CONNECT BY` | Iterative Lua graph-walking (prior plan); `WITH RECURSIVE` CTE | `CONNECT BY` is native Exasol SQL — no license. Declarative SQL closure is simpler and faster than re-implementing traversal in Lua. The prior plan's "paid add-on" premise was factually wrong |
| All administration DB-side; Python CLI is deployment-only | Thin Python `EXECUTE SCRIPT` wrapper verbs (prior plan); split Python/DB administration | Owner directive: management lives in the database so the framework can later control its own management syntax; no external runtime needed for routine admin |
| Additive UNION (superset) of ROLE + USER rules, deduplicated, ordered by `rule_id` asc | USER-scoped rule overrides/replaces ROLE-derived rules for the phase (prior plan — rejected by the product owner); role-rules-first ordering (rejected on follow-up) | SQL-privilege semantics: a user gets the union of everything granted, never a per-user subtraction. Override/exclusion has no SQL analogue and surprises admins. Dedup prevents the same function running twice. Pure `rule_id` order keeps sequence admin-controlled and scope-agnostic |
| Add `ordinal` column to `RESOLUTION` (ships in DDL) | Order by `rule_id` carried through; rely on `phase` only | `MY_PIPELINE` does not expose `rule_id`, and `ORDER BY phase` cannot break ties; an explicit ordinal is the clean, view-friendly carrier of the `rule_id`-ascending order |
| Atomic rebuild via explicit `commit()` in the script | Per-row autocommit; `MERGE`/upsert without delete | Full DELETE+reinsert is simplest correct rebuild and drops orphans for free; one commit guarantees atomic visibility |
| Fingerprint-based staleness in `PREPROC.STATUS` | Diff dictionary against resolution inputs every call; timestamp-only ("you might be stale"); Python status verb | Fingerprint is O(grants) and gives a definitive current/stale answer cheaply; computed where dictionary access already lives |

## Features

| Feature | Status | Spec |
|---------|--------|------|
| refresh/role-resolution | NEW | `refresh/role-resolution/spec.md` |
| refresh/status-staleness | NEW | `refresh/status-staleness/spec.md` |
| install/master-skeleton | CHANGED | `install/master-skeleton/spec.md` |
| install/schema-layout | CHANGED | `install/schema-layout/spec.md` |

## Dependencies

- Builds on the bedrock slice (`add-install-two-schema-layout`): two-schema layout, `MY_PIPELINE` view, CLI install applier, env-var connection, pytest fixtures.
- `SELECT ANY DICTIONARY` must be grantable to a role on the target instance (standard on Exasol 2025.x).
- Native `CONNECT BY` hierarchical-query support (standard, unlicensed, on Exasol 2025.x).
- Existing toolchain: `uv`, Python 3.12, `pyexasol`, `pytest`, `ruff`, `luacheck`, `exasol/docker-db`.

## Migration

| Current | New |
|---------|-----|
| `PREPROC.RESOLUTION` (user_name, client_pattern, phase, script_name, function_name) | + `ordinal` column (within-phase deterministic order) |
| `PREPROC.SETTINGS` (mode, self_disable_allowed, last_refresh) | + state-fingerprint column for staleness |
| `PREPROC_RT.MY_PIPELINE` (`ORDER BY phase`) | exposes `client_pattern`; `ORDER BY phase, ordinal` |
| `PREPROC.REFRESH` / `PREPROC.STATUS` do not exist | new `PREPROC_ADMIN`-owned Lua scripts |
| `master.lua` pass-through skeleton | dispatch loop over resolved pipeline |

DDL changes are additive and idempotent (`CREATE TABLE IF NOT EXISTS` will NOT add the new columns on an already-installed DB — ship the column additions as guarded `ALTER TABLE ... ADD COLUMN` statements that tolerate "already exists", consistent with the existing `--@tolerate-exists` applier contract).

## Implementation Tasks

1. **Schema / DDL changes** (`sql/install/`)
   1. Add the `ordinal` column to `PREPROC.RESOLUTION` and the fingerprint column to `PREPROC.SETTINGS` — for fresh installs in `20_tables.sql`, and as guarded `ALTER TABLE ... ADD COLUMN` (`--@tolerate-exists`) for upgrades of existing installs.
   2. Update `40_views.sql`: `MY_PIPELINE` exposes `client_pattern` and orders by `phase, ordinal`.
   3. Add `60_dictionary_grant.sql`: `GRANT SELECT ANY DICTIONARY TO PREPROC_ADMIN` (idempotent / tolerate-exists).
   4. Confirm the refresh and status script objects are created with the correct ownership and are NOT granted to PUBLIC (they live in the locked `PREPROC` schema, which has no PUBLIC grant — verify no inherited grant exposes them).

2. **Refresh database script** (`sql/lua/refresh.lua`)
   1. Build the transitive role-to-user closure with a SQL `CONNECT BY` query over `EXA_DBA_ROLE_PRIVS` (`START WITH` direct grants, `CONNECT BY PRIOR` the grantee→granted edge), plus direct `USER`-scoped matches; the Lua script issues this query, it does not walk the graph itself. [expert]
   2. Join the closure against enabled `PREPROC.CONFIG`; materialise the additive UNION of all `ROLE`-derived and `USER`-scoped rules (no override/exclusion), deduplicate identical `(phase, script, function)` rows keeping the lowest contributing `rule_id`, and assign deterministic `ordinal` per phase by `rule_id` ascending (scope type ignored). [expert]
   3. Atomic rebuild: `DELETE` all `RESOLUTION`, insert resolved rows, compute and write the state fingerprint + `last_refresh`, commit once; return the row count + new `last_refresh` so any SQL client sees the outcome. [expert]
   4. Factor the staleness fingerprint into a shared helper/definition so refresh (writer) and status (reader) compute it identically (count + hash over ordered user/grant identifiers).
   5. Name and shape the script as a stable, syntax-mappable admin entry point (no required params for the common refresh; result row reporting outcome) so a future management-syntax layer can translate onto it.

3. **Status database script** (`sql/lua/status.lua`)
   1. Recompute the current fingerprint from `EXA_DBA_USERS` / `EXA_DBA_ROLE_PRIVS`, compare against the stored `SETTINGS` fingerprint, and return a current/stale verdict + `last_refresh`; never-refreshed (NULL `last_refresh`) reports stale. Read-only — no writes to `RESOLUTION` or `SETTINGS`. [expert]
   2. Share the fingerprint definition from task 2.4; name/shape `PREPROC.STATUS` as a stable, syntax-mappable admin entry point.

4. **Deploy the new DB-side scripts via the install applier** (`src/preproc/`, `sql/lua/`)
   1. Wire `CREATE OR REPLACE LUA SCRIPT PREPROC.REFRESH` and `PREPROC.STATUS` deployment into the existing install applier (open `PREPROC` schema, deploy alongside MASTER). No new CLI verbs — deployment only.

5. **MASTER dispatch upgrade** (`sql/lua/master.lua`)
   1. After the guard and successful lookup, iterate `MY_PIPELINE` rows: skip the all-NULL no-rules sentinel, group by phase, apply `TRANSLATE`→`EXPAND`→`REWRITE` in order, within phase in `ordinal` order, threading text rule-to-rule; final `setsqltext` (bind gsub result to a local first). [expert]
   2. Per-rule `client_pattern` matching: dispatch only rules whose pattern is NULL or matches the connecting client. [expert]
   3. Per-rule fail-open: wrap each module dispatch in pcall; on error skip that rule and continue; never block the statement. Retain the existing guard and view-lookup fail-open. [expert]
   4. Keep luacheck clean.

6. **Tests** (`tests/`)
   1. `test_role_resolution.py` — integration: direct grant, nested/transitive grant via CONNECT BY, no-grant exclusion, USER+ROLE additive union (DAVE gets a,b,c,d ordered by rule_id), multi-role union + ordinal order, multi-path dedup, disabled exclusion, full-rebuild/no-orphans, atomic stamp, callable-from-any-client-by-admin, admin-privilege requirement. Build role/user fixtures and tear them down. [expert]
   2. `test_status_staleness.py` — integration: current after refresh, stale after role grant, stale after new user, read-only, never-refreshed, admin-privilege requirement; invoked as a DB-side script. [expert]
   3. `test_master_dispatch.py` — integration: phase-order dispatch, within-phase ordinal order, client_pattern match/no-match, failing-function skip fail-open, no-rules passthrough unchanged, guard + lookup fail-open still hold. [expert]
   4. Extend `test_schema_layout.py` — `ordinal`/fingerprint columns present, `MY_PIPELINE` ordering + client_pattern exposure, `PREPROC.REFRESH` and `PREPROC.STATUS` created and not PUBLIC-executable, `SELECT ANY DICTIONARY` granted to `PREPROC_ADMIN` and not PUBLIC.

## Parallelization

| Parallel Group | Tasks |
|----------------|-------|
| Group A (DDL) | 1.1, 1.2, 1.3, 1.4 |
| Group B (refresh script) | 2.1, 2.2, 2.3, 2.4, 2.5 |
| Group C (status script) | 3.1, 3.2 |
| Group D (MASTER dispatch) | 5.1, 5.2, 5.3, 5.4 |
| Group E (tests) | 6.1, 6.2, 6.3, 6.4 |

Sequential dependencies:
- Group A → Groups B/C/D (the `ordinal`/fingerprint columns and view shape must exist before refresh writes them, status reads them, and MASTER reads them).
- Group B (2.4 fingerprint helper) → Group C (status shares the fingerprint definition).
- Groups B, C → Task 4 (the scripts must exist before the applier deploys them).
- Groups A, B, C, D, Task 4 → Group E (objects and logic must exist before integration tests).

## Dead Code Removal

| Type | Location | Reason |
|------|----------|--------|
| Comment | `sql/lua/master.lua` "Dispatch extension point (later slice)" block | Replaced by the actual dispatch loop in task 5.1 |
| Feature delta | `specs/_plans/.../refresh/cli-refresh/spec.md` | Deleted — refresh is DB-side only; no Python verb |
| Plan tasks | prior `preproc refresh` / `preproc status` CLI tasks | Removed — administration moved DB-side; only deployment of the scripts remains |
| — | — | Otherwise additive; no behavior removed |

## Verification

### Scenario Coverage

| Scenario | Test Type | Test Location | Test Name |
|----------|-----------|---------------|-----------|
| A direct role grant resolves to the holder | Integration | `tests/test_role_resolution.py` | `test_direct_role_grant_resolves` |
| A nested role grant resolves transitively via CONNECT BY | Integration | `tests/test_role_resolution.py` | `test_nested_role_grant_resolves_transitively` |
| A user without the granting role gets no row | Integration | `tests/test_role_resolution.py` | `test_user_without_role_excluded` |
| A USER-scoped rule unions with ROLE-derived rules for the same phase | Integration | `tests/test_role_resolution.py` | `test_user_unions_with_role_same_phase` |
| Multiple role-derived rules for one phase coexist in deterministic order | Integration | `tests/test_role_resolution.py` | `test_multi_role_rules_union_ordered` |
| The same function reaching a user by multiple paths is deduplicated | Integration | `tests/test_role_resolution.py` | `test_multi_path_function_deduplicated` |
| Disabled rules are excluded | Integration | `tests/test_role_resolution.py` | `test_disabled_rules_excluded` |
| Refresh fully rebuilds, never appends | Integration | `tests/test_role_resolution.py` | `test_refresh_full_rebuild_no_orphans` |
| Rebuild and timestamp are atomic | Integration | `tests/test_role_resolution.py` | `test_rebuild_and_timestamp_atomic` |
| Refresh is callable from any SQL client by an admin | Integration | `tests/test_role_resolution.py` | `test_refresh_callable_by_admin_any_client` |
| Refresh requires admin privileges | Integration | `tests/test_role_resolution.py` | `test_refresh_requires_admin` |
| Status reports current immediately after refresh | Integration | `tests/test_status_staleness.py` | `test_status_current_after_refresh` |
| Status reports stale after a new role grant | Integration | `tests/test_status_staleness.py` | `test_status_stale_after_role_grant` |
| Status reports stale after a new user is created | Integration | `tests/test_status_staleness.py` | `test_status_stale_after_new_user` |
| Status is read-only | Integration | `tests/test_status_staleness.py` | `test_status_read_only` |
| Status before any refresh reports never-refreshed | Integration | `tests/test_status_staleness.py` | `test_status_never_refreshed` |
| Status requires admin privileges | Integration | `tests/test_status_staleness.py` | `test_status_requires_admin` |
| MASTER dispatches the resolved pipeline in phase order | Integration | `tests/test_master_dispatch.py` | `test_dispatch_phase_order` |
| Dispatch honours client_pattern matching | Integration | `tests/test_master_dispatch.py` | `test_dispatch_client_pattern_match` |
| A failing module function is skipped, fail-open | Integration | `tests/test_master_dispatch.py` | `test_dispatch_failing_function_skipped` |
| A normal statement passes through unchanged (no-rules) | Integration | `tests/test_master_dispatch.py` | `test_no_rules_passthrough_unchanged` |
| ALTER SESSION and ALTER SYSTEM statements are guarded | Integration | `tests/test_master_dispatch.py` | `test_alter_statements_guarded` |
| Lookup failure fails open | Integration | `tests/test_master_dispatch.py` | `test_lookup_failure_fails_open` |
| Admin configuration tables are created | Integration | `tests/test_schema_layout.py` | `test_admin_tables_have_ordinal_and_fingerprint` |
| The CURRENT_USER-filtered pipeline view is created | Integration | `tests/test_schema_layout.py` | `test_my_pipeline_orders_phase_ordinal` |
| The refresh and status scripts are created in the admin schema | Integration | `tests/test_schema_layout.py` | `test_refresh_status_scripts_created_not_public` |
| PREPROC_ADMIN can read the dictionary tables refresh requires | Integration | `tests/test_schema_layout.py` | `test_preproc_admin_dictionary_grant` |

### Manual Testing

| Feature | Command | Expected Output |
|---------|---------|-----------------|
| refresh/role-resolution | Seed a `ROLE`-scoped rule + grant the role to a test user, then (as a `PREPROC_ADMIN` holder, any SQL client) `EXECUTE SCRIPT PREPROC.REFRESH`; query `PREPROC.RESOLUTION` | A row for the test user derived from the rule; transitive (nested-role) holders included; result reports rows produced + new `last_refresh` |
| refresh/role-resolution | `EXECUTE SCRIPT PREPROC.REFRESH` as a non-admin user | Insufficient-privileges error; `PREPROC.RESOLUTION` unchanged |
| refresh/status-staleness | `EXECUTE SCRIPT PREPROC.STATUS` immediately after a refresh | Reports current; shows `last_refresh` |
| refresh/status-staleness | Grant a role to a user, then `EXECUTE SCRIPT PREPROC.STATUS` | Reports stale; indicates a refresh is needed |
| install/master-skeleton | Seed an EXPAND rule for the current user, refresh, activate MASTER session-scoped, submit the matching statement | Statement text is transformed by the module function; phases applied in order |
| install/schema-layout | `preproc install` then inspect `PREPROC.RESOLUTION` / `PREPROC.SETTINGS` columns and `PREPROC` scripts | `ordinal` and fingerprint columns present; `PREPROC.REFRESH` and `PREPROC.STATUS` scripts exist, not PUBLIC-executable |

### Checklist

| Step | Command | Expected |
|------|---------|----------|
| Build | `uv sync` | Exit 0 |
| Test | `uv run pytest` | 0 failures |
| Lint (Python) | `uv run ruff check .` | 0 errors |
| Lint (Lua) | `luacheck sql/lua/` | 0 warnings |
| Format | `uv run ruff format --check .` | No changes |
