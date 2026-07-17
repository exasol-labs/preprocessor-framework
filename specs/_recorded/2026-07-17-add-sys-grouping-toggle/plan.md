# Plan: add-sys-grouping-toggle

## Summary

Closes the open policy tail of BUG-002 by making SYS-grouping configurable: a new `PREPROC.SETTINGS.group_sys_with_dba` boolean decides whether the `SYS` account is grouped with `DBA` during resolution (the current, default behavior) or isolated as a raw-SQL break-glass recovery user excluded from the entire ROLE-grant closure. The toggle defaults to TRUE (grouped), is set through a new `PREPROC.SET_SYS_GROUPING` admin script mirroring `SET_MODE` (with a matching `PREPROC SET SYS_GROUPING <bool>` command-sugar verb added to `CONTROL_SUGAR_V1` in place), and is applied purely in the resolution SQL — never folded into the state fingerprint.

## Design

### Context

BUG-002 (studies/2026-06-persona-ux) was closed for onboarding by seeding the control-sugar bootstrap for both `PREPROC_ADMIN` and `DBA`, so `SYS` — which cannot hold `PREPROC_ADMIN` but does hold `DBA` — can use the `PREPROC …` sugar. That fix left a policy tail: because resolution walks the role-grant closure and `SYS` holds `DBA`, every `DBA`-scoped (and `PUBLIC`-scoped, and nested-role) rule now reaches `SYS`. Some operators want `SYS` to remain a guaranteed raw-SQL recovery account, seeing untransformed SQL regardless of what rules the framework carries. This plan makes that a per-deployment choice.

- **Goals** — Let an operator isolate `SYS` from the ENTIRE role closure (DBA + PUBLIC + nested) as a guaranteed break-glass path; preserve today's grouped behavior by default and on upgrade; apply the policy in a single static resolution SQL statement with no Lua branching; keep the change fully callable from any SQL client via a validating admin script.
- **Non-Goals** — A general excluded-users list (SYS only); folding the toggle into the resolution state fingerprint (`mode` is likewise excluded); surfacing the toggle value in the `PREPROC.STATUS` result set (considered and declined — see Consequences); a `CONTROL_SUGAR_V2` module (the verb is added to live `V1` in place, a pre-release exception — see Consequences); per-user rule subtraction of any kind (the additive UNION model of ADR-009 is unchanged for every account except the SYS-only ROLE-arm gate).

### Decision

#### Architecture

```
PREPROC.SETTINGS
  + group_sys_with_dba BOOLEAN (nullable)
      fresh install : seeded TRUE (grouped)
      upgraded      : added nullable; existing row NULL; COALESCE→TRUE at read

REFRESH_CORE.rebuild()  (unchanged step order; only RESOLUTION_SQL changes)
  RESOLUTION_SQL (single static statement)
      scoped_rules =
        ┌ ROLE-closure arm  (closure ⋈ CONFIG scope_type='ROLE' ⋈ EXA_DBA_USERS u)
        │     + AND ( u.user_name <> 'SYS'
        │             OR COALESCE((SELECT group_sys_with_dba
        │                          FROM PREPROC.SETTINGS), TRUE) = TRUE )
        │     → when toggle FALSE, drops SYS from DBA + PUBLIC + nested at once
        │       (all flow through this one arm)
        └ USER-scoped arm  (CONFIG scope_type='USER')   ← UNTOUCHED
              → a USER rule with scope_value='SYS' still resolves
      deduped / ordinal CTEs downstream ← UNAFFECTED

PREPROC.SET_SYS_GROUPING(value, norefresh)  RETURNS TABLE   (models SET_MODE)
      1. validate value ∈ {TRUE, FALSE}      (side-effect-free reject otherwise)
      2. UPDATE PREPROC.SETTINGS SET group_sys_with_dba = :value
      3. norefresh=TRUE ? COMMIT : import REFRESH_CORE → rc.rebuild()
      4. exit({{value}}, "group_sys_with_dba BOOLEAN")

CONTROL_SUGAR_V1  (edited in place — pre-release exception, no V2)
      + CONTROL.set_sys_grouping = "PREPROC.SET_SYS_GROUPING"
      + verb: SET SYS_GROUPING <bool> [NOREFRESH]  (mutating → accepts NOREFRESH like SET MODE)
      → EXECUTE SCRIPT PREPROC.SET_SYS_GROUPING(<word(3) UNQUOTED>, <true|NULL>)

install.py / bundle.py
      + deploy CREATE OR REPLACE LUA SCRIPT PREPROC.SET_SYS_GROUPING(value, norefresh)
      + SET_SYS_GROUPING added to _FULL_SCRIPT_ORDER and _RELEASE_SCRIPT_ORDER
      (CONTROL_SUGAR_V1 already in both orders — the edited source flows through unchanged)
release.sql  ← REGENERATED (adds the new script + the SETTINGS column/seed + the V1 sugar edit)
```

#### Patterns

| Pattern | Where | Why |
|---------|-------|-----|
| Pure-SQL policy predicate | `RESOLUTION_SQL` in `refresh_core.lua` | Keeps `RESOLUTION_SQL` a single static statement (no Lua string branching); the toggle is read as a scalar subquery over the single-row SETTINGS |
| Single-arm gate | ROLE-closure UNION arm only | DBA, PUBLIC, and nested-role derivations all flow through the one closure arm, so a single `grantee <> 'SYS'` gate excludes SYS from the FULL closure at once; the USER arm is untouched so USER-`SYS` rules survive |
| COALESCE-to-TRUE at the read site | `RESOLUTION_SQL` predicate | Makes a missing/NULL toggle (upgraded install) behave as grouped with no backfill UPDATE required — the single guarantee lives where the value is consumed |
| Validate-first setter | `set_sys_grouping.lua` (mirrors `set_mode.lua`) | A rejected call is side-effect-free (validation before any UPDATE); the boolean domain `{TRUE, FALSE}` is the whitelist |
| Auto-refresh in the mutating script | `set_sys_grouping.lua` (ADR-018) | The toggle changes resolution, so the setter rebuilds (with a `NOREFRESH` opt-out), unlike the pure `SET_SELF_DISABLE` write |
| Fingerprint exclusion | `FINGERPRINT_SQL` unchanged in `refresh_core.lua` and `status.lua` | Mirrors `mode`: the setter always rebuilds+restamps so STATUS reports CURRENT afterward; the two verbatim FINGERPRINT_SQL copies stay identical |

### Consequences

| Decision | Alternatives Considered | Rationale |
|----------|------------------------|-----------|
| Full-closure exclusion when isolated (DBA + PUBLIC + nested) | Exclude only `DBA`-scoped rules; exclude a configurable rule subset | The product owner chose the cleanest guaranteed recovery path: `SYS` sees raw SQL unless a USER-scoped rule names it. Excluding only `DBA` would still let a `PUBLIC`- or nested-role rule reach `SYS`, defeating the break-glass guarantee |
| SYS-only, not a general excluded-users list | A `PREPROC.EXCLUDED_USERS` table/column | `SYS` is the single Exasol account that cannot be granted `PREPROC_ADMIN`, so it is the only account needing role-independent recovery; a general list is scope creep with no requested use case |
| Default TRUE (grouped); NULL→TRUE on upgrade | Default FALSE (isolated); NOT NULL column with a default | No behavior change on upgrade is a hard requirement; a nullable column + COALESCE matches the established additive-upgrade pattern (a NOT NULL add fails on the existing SETTINGS row) |
| Toggle excluded from the state fingerprint | Fold `group_sys_with_dba` into `FINGERPRINT_SQL` | `mode` is likewise absent; setting the toggle always triggers rebuild+restamp so STATUS is CURRENT afterward, and the two verbatim FINGERPRINT_SQL copies (refresh_core.lua, status.lua) MUST stay identical — folding it in would require editing both and risk drift for no benefit |
| `SET_SYS_GROUPING` models `SET_MODE` (validate → update → rebuild/norefresh → RETURNS TABLE) | Model `SET_SELF_DISABLE` (pure write, no refresh) | The toggle changes resolution (SYS's rows), so it MUST refresh; `SET_MODE` is the exact template (it has the `norefresh` + `rc.rebuild()` structure); `SET_SELF_DISABLE` deliberately does not refresh and is the wrong template |
| Toggle NOT surfaced in `PREPROC.STATUS` result | Add a `group_sys_with_dba` column to the STATUS result set | Keeps the STATUS contract stable (ADR-008) and the FINGERPRINT_SQL copies identical; the value is directly observable via `SELECT group_sys_with_dba FROM PREPROC.SETTINGS`. Can be added in a later STATUS-shape slice if operators ask |
| `PREPROC SET SYS_GROUPING` sugar added to live `CONTROL_SUGAR_V1` in place | Defer the sugar; new `CONTROL_SUGAR_V2` + reseed + retire | The project is pre-first-release, so there are NO active sessions and the never-`CREATE OR REPLACE`-a-live-module hazard (in-flight statement binding a stale body, ADR-019) does not apply. Editing `V1` + regenerating `release.sql` is harmless now; a full `V2` dance is disproportionate for one break-glass verb. This is a pre-release exception ONLY — any post-release grammar change MUST still use `CONTROL_SUGAR_V2` |

## Features

| Feature | Status | Spec |
|---------|--------|------|
| `refresh/role-resolution` | CHANGED | `specs/_plans/add-sys-grouping-toggle/refresh/role-resolution/spec.md` |
| `control/admin-scripts` | CHANGED | `specs/_plans/add-sys-grouping-toggle/control/admin-scripts/spec.md` |
| `install/schema-layout` | CHANGED | `specs/_plans/add-sys-grouping-toggle/install/schema-layout/spec.md` |
| `control/command-sugar` | CHANGED | `specs/_plans/add-sys-grouping-toggle/control/command-sugar/spec.md` |

## Migration

| Current | New |
|---------|-----|
| `PREPROC.SETTINGS` — `(mode, self_disable_allowed, last_refresh, state_fingerprint)` | Add `group_sys_with_dba BOOLEAN` nullable in `sql/install/20_tables.sql` (fresh) and via `--@tolerate-exists ALTER TABLE ... ADD COLUMN` in `sql/install/25_column_upgrades.sql` (upgrade) |
| `sql/install/30_settings_seed.sql` — seeds `(mode, self_disable_allowed, last_refresh)` | Also seed `group_sys_with_dba = TRUE` |
| `RESOLUTION_SQL` ROLE-closure arm — `JOIN EXA_DBA_USERS u ON u.user_name = closure.grantee` | Append `AND (u.user_name <> 'SYS' OR COALESCE((SELECT group_sys_with_dba FROM PREPROC.SETTINGS), TRUE) = TRUE)`; USER arm and downstream CTEs unchanged |
| _(new)_ `PREPROC.SET_SYS_GROUPING` — does not exist | New `sql/lua/set_sys_grouping.lua`; deployed as `CREATE OR REPLACE LUA SCRIPT PREPROC.SET_SYS_GROUPING(value, norefresh) RETURNS TABLE` by `install.py` and `bundle.py` |
| `sql/lua/control_sugar_v1.lua` — no `SET SYS_GROUPING` verb | Edited in place: add `set_sys_grouping` to the `CONTROL` map and a `SET SYS_GROUPING <bool> [NOREFRESH]` branch emitting `EXECUTE SCRIPT PREPROC.SET_SYS_GROUPING(<value unquoted>, <true\|NULL>)`. No `CONTROL_SUGAR_V2` (pre-release exception) |
| `release.sql` — committed generated artifact without the new script/column/sugar verb | Regenerated to include the SET_SYS_GROUPING CREATE, the SETTINGS column/seed changes, and the edited `CONTROL_SUGAR_V1` body |

## Implementation Tasks

### Group 1 — SETTINGS column, seed, and upgrade (prerequisite for everything)

- [ ] 1.1 Add `group_sys_with_dba BOOLEAN` (nullable) to `PREPROC.SETTINGS` in `sql/install/20_tables.sql`
- [ ] 1.2 Extend the seed in `sql/install/30_settings_seed.sql` to insert `group_sys_with_dba = TRUE` (add the column to the INSERT list and the SELECT; keep the `WHERE NOT EXISTS` guard)
- [ ] 1.3 Add `--@tolerate-exists` + `ALTER TABLE PREPROC.SETTINGS ADD COLUMN group_sys_with_dba BOOLEAN` to `sql/install/25_column_upgrades.sql`
- [ ] 1.4 Write/extend integration tests in `tests/test_schema_layout.py`: column present and nullable; fresh-install seed defaults it TRUE (extend `test_settings_seeded_single_row`); upgrade is an idempotent no-op on re-apply and a NULL is treated as grouped

### Group 2 — Resolution predicate (requires Group 1 column)

- [ ] 2.1 Add the SYS-grouping gate to `RESOLUTION_SQL` in `sql/lua/refresh_core.lua`: append `AND (u.user_name <> 'SYS' OR COALESCE((SELECT group_sys_with_dba FROM PREPROC.SETTINGS), TRUE) = TRUE)` to the ROLE-closure arm's `JOIN EXA_DBA_USERS u` predicate; RESOLUTION_SQL MUST remain a single static statement; the USER-scoped arm and the deduped/ordinal CTEs MUST stay byte-for-byte the same [expert]
- [ ] 2.2 Update the `RESOLUTION_SQL` header comment in `refresh_core.lua` to document the SYS-grouping gate, its single-arm placement, and the COALESCE-to-TRUE default
- [ ] 2.3 Write role-resolution integration tests in `tests/test_role_resolution.py` covering the full matrix: grouped default (SYS gets DBA + PUBLIC rows); isolated excludes SYS from DBA + PUBLIC + nested; USER-scoped `SYS` rule survives isolation; a non-SYS DBA holder is unaffected; a NULL toggle is treated as grouped [expert]

### Group 3 — SET_SYS_GROUPING admin script (requires Group 1)

- [ ] 3.1 Author `sql/lua/set_sys_grouping.lua` modeled on `sql/lua/set_mode.lua`: `luacheck: globals rc` / `read globals value norefresh`; validate `value` is a genuine boolean (`value == true or value == false`), else `error()` naming the offending value BEFORE any UPDATE; `UPDATE PREPROC.SETTINGS SET group_sys_with_dba = :value`; then `norefresh == true` → `COMMIT`, else `import("PREPROC.REFRESH_CORE","rc")` + `rc.rebuild()`; `exit({{value}}, "group_sys_with_dba BOOLEAN")`
- [ ] 3.2 Register in `src/preproc/install.py`: add `_SET_SYS_GROUPING_LUA_PATH`, add it to the `required` file list + read its body, deploy `CREATE OR REPLACE LUA SCRIPT PREPROC.SET_SYS_GROUPING(value, norefresh) RETURNS TABLE AS`, and update the `_deploy_admin_scripts` docstring enumeration and the verbose print string
- [ ] 3.3 Register in `src/preproc/bundle.py`: add the path const, add a `SET_SYS_GROUPING` entry to `_script_statements` with the `(value, norefresh) RETURNS TABLE` header, and add `"SET_SYS_GROUPING"` to both `_FULL_SCRIPT_ORDER` and `_RELEASE_SCRIPT_ORDER`
- [ ] 3.4 Update the comment in `sql/install/65_admin_script_grant.sql` to list `SET_SYS_GROUPING` among the auto-refreshing mutating scripts (comment only — no grant change: admin scripts are owned by `PREPROC_ADMIN` and carry no per-script EXECUTE grant, same as `SET_MODE`)
- [ ] 3.5 Write control-admin-scripts integration tests in `tests/test_control_admin_scripts.py`: updates the flag and auto-refreshes (RESOLUTION reflects the change); rejects a non-boolean value side-effect-free (SETTINGS + RESOLUTION unchanged); `NOREFRESH` commits without rebuild (last_refresh unchanged); leaves `mode`/`self_disable_allowed` unchanged and STATUS reports CURRENT after the refresh
- [ ] 3.6 Update deploy tests: add `SET_SYS_GROUPING` to the deployed-scripts assertion in `tests/test_cli_install.py`; bump the full-bundle statement count (19 → 20) and add a `SET_SYS_GROUPING` presence assertion in `tests/test_cli_bundle.py` (both full and release bundles)
- [ ] 3.7 Add the `PREPROC SET SYS_GROUPING` verb to `sql/lua/control_sugar_v1.lua` IN PLACE (no `V2`): add `set_sys_grouping = "PREPROC.SET_SYS_GROUPING"` to the `CONTROL` map, and a `verb == "SET" and upper_word(2) == "SYS_GROUPING"` branch — subcommand-count check consistent with `SET MODE` (3 tokens after `PREPROC` before the `NOREFRESH` strip), `value = word(3)` passed UNQUOTED (mirror `SET SELF_DISABLE`, not `SET MODE`'s `quote(...)`), and always append the `norefresh` slot (`norefresh and "true" or "NULL"`) so the emitted `EXECUTE SCRIPT PREPROC.SET_SYS_GROUPING(value, slot)` meets Exasol's exact-arg-count rule
- [ ] 3.8 Write command-sugar integration tests in `tests/test_command_sugar.py`: `PREPROC SET SYS_GROUPING FALSE` → `EXECUTE SCRIPT PREPROC.SET_SYS_GROUPING(FALSE, NULL)`; `TRUE` variant; and `PREPROC SET SYS_GROUPING FALSE NOREFRESH` → `... (FALSE, true)` (assert value is unquoted and NOREFRESH is threaded, not emitted)

### Group 4 — Regenerate release.sql and documentation (requires Groups 1-3)

- [ ] 4.1 Regenerate `release.sql` via `preproc bundle --release` (the documented generator) after all SQL/Lua changes; verify by diff that the only changes are the `PREPROC.SET_SYS_GROUPING` CREATE block, the SETTINGS column/seed changes, and the edited `CONTROL_SUGAR_V1` body (the upgrade-only `25_column_upgrades.sql` ALTER is intentionally absent from release.sql); commit the regenerated artifact
- [ ] 4.2 Update `docs/operations.md`: document `group_sys_with_dba`, `PREPROC.SET_SYS_GROUPING`, the grouped default, and the SYS break-glass recovery semantics (isolated = raw SQL unless a USER-scoped rule names SYS)
- [ ] 4.3 Update `docs/how-it-works.md`: note the SYS-grouping policy in the resolution/role-closure section
- [ ] 4.4 Update `README` if it enumerates the admin control surface or SETTINGS columns (add `SET_SYS_GROUPING` / `group_sys_with_dba`); skip if not enumerated there

## Parallelization

| Parallel Group | Tasks |
|----------------|-------|
| Group 1 (DDL/seed/upgrade) | 1.1, 1.2, 1.3 can run concurrently; 1.4 after them |
| Group 2 + Group 3 in parallel | Once Group 1's column exists, the resolution predicate (2.x) and the setter + deploy wiring (3.x) are independent and can proceed concurrently |
| Group 4 | After Groups 1-3 are complete (release.sql regeneration must capture all SQL/Lua changes) |

Sequential dependencies:
- Group 1 → Group 2, Group 3 (the column must exist before the predicate reads it and the setter writes it)
- Group 2, Group 3 → Group 4.1 (regeneration must include the new script, the SQL, and the edited `CONTROL_SUGAR_V1` body)
- Group 3.1 → Group 3.2, 3.3 (the setter Lua source must exist before install/bundle reference it)
- Group 3.7, 3.8 (the `CONTROL_SUGAR_V1` edit + its expansion test) are independent of the setter Lua and can run concurrently with the rest of Group 3; they need no `install.py`/`bundle.py` wiring because `CONTROL_SUGAR_V1` is already in both deployment orders

## Dead Code Removal

| Type | Location | Reason |
|------|----------|--------|
| None | — | This slice is purely additive: a new SETTINGS column, one new resolution predicate, and one new admin script. No existing code is removed. |

## Verification

### Scenario Coverage

| Scenario | Test Type | Test Location | Test Name |
|----------|-----------|---------------|-----------|
| SYS resolves DBA-derived rules when grouping is enabled (default) | Integration | `tests/test_role_resolution.py` | `test_sys_grouped_receives_dba_and_public_rules` |
| SYS is excluded from the entire role closure when grouping is disabled | Integration | `tests/test_role_resolution.py` | `test_sys_excluded_from_full_closure_when_isolated` |
| A USER-scoped rule for SYS still resolves when grouping is disabled | Integration | `tests/test_role_resolution.py` | `test_sys_user_scoped_rule_survives_isolation` |
| Non-SYS DBA holders are unaffected by the grouping toggle | Integration | `tests/test_role_resolution.py` | `test_non_sys_dba_holder_unaffected_by_toggle` |
| A missing or NULL grouping value is treated as grouped | Integration | `tests/test_role_resolution.py` | `test_null_grouping_treated_as_grouped` |
| SET_SYS_GROUPING updates the grouping flag and auto-refreshes | Integration | `tests/test_control_admin_scripts.py` | `test_set_sys_grouping_updates_and_refreshes` |
| SET_SYS_GROUPING rejects a non-boolean value side-effect-free | Integration | `tests/test_control_admin_scripts.py` | `test_set_sys_grouping_rejects_non_boolean` |
| SET_SYS_GROUPING with NOREFRESH commits without rebuilding | Integration | `tests/test_control_admin_scripts.py` | `test_set_sys_grouping_norefresh_skips_refresh` |
| SET_SYS_GROUPING changes only the grouping flag among operator settings | Integration | `tests/test_control_admin_scripts.py` | `test_set_sys_grouping_leaves_other_settings_and_status_current` |
| Admin configuration tables are created (SETTINGS gains group_sys_with_dba) | Integration | `tests/test_schema_layout.py` | `test_settings_has_group_sys_with_dba_column` |
| SETTINGS is seeded with a single default row (group_sys_with_dba defaults TRUE) | Integration | `tests/test_schema_layout.py` | `test_settings_seeded_single_row` |
| PREPROC.SETTINGS gains a group_sys_with_dba column via an idempotent upgrade | Integration | `tests/test_schema_layout.py` | `test_group_sys_with_dba_upgrade_idempotent` |
| SET SYS_GROUPING command expands to a SET_SYS_GROUPING execute-script call | Integration | `tests/test_command_sugar.py` | `test_set_sys_grouping_expands` |
| A NOREFRESH suffix on SET SYS_GROUPING threads true | Integration | `tests/test_command_sugar.py` | `test_set_sys_grouping_norefresh_threaded` |
| SET_SYS_GROUPING is deployed by install | Integration | `tests/test_cli_install.py` | `test_install_deploys_control_layer` (extended) |
| SET_SYS_GROUPING is present in the generated bundles; counts match | Integration | `tests/test_cli_bundle.py` | `test_bundle_count_matches_statements` / release presence check (extended) |

### Manual Testing

| Feature | Command | Expected Output |
|---------|---------|-----------------|
| `install/schema-layout` | After `uv run preproc install`: `SELECT group_sys_with_dba FROM PREPROC.SETTINGS;` | Exactly one row, value `TRUE` |
| `control/admin-scripts` | `EXECUTE SCRIPT PREPROC.SET_SYS_GROUPING(FALSE, NULL);` then `SELECT group_sys_with_dba FROM PREPROC.SETTINGS;` then `EXECUTE SCRIPT PREPROC.STATUS;` | Script returns `FALSE`; SETTINGS shows `FALSE`; STATUS reports `CURRENT` (rebuild restamped) |
| `control/admin-scripts` (rejection) | `EXECUTE SCRIPT PREPROC.SET_SYS_GROUPING('nope', NULL);` | Raises a validation error naming the bad value; `SELECT group_sys_with_dba FROM PREPROC.SETTINGS` is unchanged |
| `refresh/role-resolution` | With a `ROLE DBA` EXPAND rule present: `EXECUTE SCRIPT PREPROC.SET_SYS_GROUPING(FALSE, NULL);` then `SELECT COUNT(*) FROM PREPROC.RESOLUTION WHERE user_name='SYS';` then `SET_SYS_GROUPING(TRUE, NULL)` and recount | Count is `0` while isolated (no ROLE-derived rows for SYS); the SYS rows reappear after re-grouping |
| `control/command-sugar` | As a caller under an active preprocessor holding the sugar rule: `PREPROC SET SYS_GROUPING FALSE;` and `PREPROC SET SYS_GROUPING FALSE NOREFRESH;` | The first expands to and runs `EXECUTE SCRIPT PREPROC.SET_SYS_GROUPING(FALSE, NULL)` (isolates SYS + refreshes); the second threads `true` into the second slot (commits without rebuild) |

### Checklist

| Step | Command | Expected |
|------|---------|----------|
| Build | `uv sync` | Exit 0 |
| Test | `uv run pytest` | 0 failures |
| Lint & Format | `uv run ruff check . && uv run ruff format --check .` | 0 errors/warnings |
| Lua lint | `luacheck sql/lua/` | 0 errors/warnings (includes the new `set_sys_grouping.lua`) |
| Release artifact | `preproc bundle --release` then `git diff release.sql` | Only the SET_SYS_GROUPING CREATE, the SETTINGS column/seed additions, and the edited `CONTROL_SUGAR_V1` body |

## Deferrals & Out of Scope

- **Command sugar IS included, added to `CONTROL_SUGAR_V1` in place — no `V2`.** Because the project is pre-first-release there are no active sessions, so the never-`CREATE OR REPLACE`-a-live-module hazard (ADR-019) does not apply and editing `V1` + regenerating `release.sql` is harmless. This is a strictly pre-release exception: any POST-RELEASE grammar change MUST still go through a new `CONTROL_SUGAR_V2` (module + repointed seed + retire once unreferenced). Recorded as an ADR-worthy decision in the decision log.
- **STATUS result shape is unchanged.** The toggle is not added to the `PREPROC.STATUS` result set (kept stable per ADR-008) and not folded into `FINGERPRINT_SQL` (both verbatim copies stay identical). Observe the value via `SELECT group_sys_with_dba FROM PREPROC.SETTINGS`.
- **No demo-seed / teardown change.** The demo config-seed operates in the reserved 9000–9999 band and has no SYS interaction; `tests/test_demo_seed.py` and `studies/2026-06-persona-ux/app_show.sql` are unaffected.
- **No new grants.** Admin scripts are owned by `PREPROC_ADMIN` with no per-script EXECUTE grant; `SET_SYS_GROUPING` inherits the same ownership model as `SET_MODE`. `sql/install/65_admin_script_grant.sql` changes only in a comment.
