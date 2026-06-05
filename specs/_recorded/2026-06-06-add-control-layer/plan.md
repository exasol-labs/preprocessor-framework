# Plan: add-control-layer

## Summary

Add a SQL-only administrative control surface for the preprocessor framework — every `PREPROC.CONFIG`/`SETTINGS` mutation goes through validating `EXECUTE SCRIPT` admin scripts — plus a dogfooded EXPAND convenience module that translates SQL-like `PREPROC …` commands into those same `EXECUTE SCRIPT` calls.

## Design

### Context

Today an operator changes framework behaviour by hand-writing `INSERT`/`UPDATE`/`DELETE` against `PREPROC.CONFIG` and then manually calling `PREPROC.REFRESH`. Direct DML is error-prone (no validation of phase/scope/script existence, manual `rule_id` allocation that can collide with the reserved demo range) and a single bad write impacts every user resolved from that rule. The framework already ships `REFRESH` and `STATUS` as DB-side admin scripts (ADR-007/008); the natural extension is to make the *entire* admin surface scripted, and then to dogfood it by having the framework's own EXPAND phase translate readable keyword commands into those scripts.

- **Goals**
  - Surface the full admin lifecycle (rule add/drop/enable/disable/list, settings mode + self-disable) as validating `EXECUTE SCRIPT` entry points in the `PREPROC` schema.
  - Auto-refresh inside each mutating script (opt-out via `NOREFRESH`) so resolution is never left stale.
  - Provide a production EXPAND module that expands `PREPROC …` commands to a single `EXECUTE SCRIPT` call each — eat-your-own-dog-food.
  - Make the scripted path the obviously-correct path through input validation.
- **Non-Goals**
  - Technical lockdown of direct CONFIG DML (impossible under invoker rights — convention + validation only).
  - Profile-script management commands (deferred; the surface is extensible).
  - Changing the install bootstrap seed (`sql/demo/seed.sql`) to route through the control scripts — installer/seed direct DML remains the sanctioned bootstrap.

### Decision

#### Architecture

```
SQL client
  │  PREPROC ADD RULE FOR ROLE ANALYSTS PHASE EXPAND SCRIPT … FUNCTION …;
  ▼
MASTER (EXPAND phase)
  │  dispatch CONTROL_SUGAR_V1.expand(sqltext)  ── first-match-terminal
  ▼
"EXECUTE SCRIPT PREPROC.RULE_ADD('ROLE','ANALYSTS','EXPAND', …)"   (single statement)
  ▼
PREPROC.RULE_ADD  (Lua, invoker rights)
  ├─ validate phase / scope_type / script existence (EXA_ALL_SCRIPTS)
  ├─ allocate rule_id (operator space, below 9000)
  ├─ INSERT one row into PREPROC.CONFIG
  └─ run refresh logic (unless NOREFRESH)  ── rebuilds PREPROC.RESOLUTION
```

Two layers, deployed by the same install/bundle path as existing scripts:
- **Admin control scripts** — `PREPROC` schema, unversioned `CREATE OR REPLACE LUA SCRIPT` (same precedent as REFRESH/STATUS). Mutating scripts call shared refresh logic internally.
- **Convenience module** — `PREPROC_RT.CONTROL_SUGAR_V1`, a production (no `DEMO_`) versioned dispatched EXPAND module, registered via an install-seeded CONFIG row scoped to `PREPROC_ADMIN`.

#### Patterns

| Pattern | Where | Why |
|---------|-------|-----|
| Validating script-as-API | `PREPROC.RULE_*`, `SET_*` | Makes the scripted path obviously-correct; no technical lockdown possible under invoker rights |
| Auto-refresh inside the mutating script | `RULE_ADD/DROP/ENABLE/DISABLE`, `SET_MODE` | Single-statement EXPAND output forbids chaining a separate REFRESH; refresh must run in-script |
| `NOREFRESH` opt-out | all mutating scripts + sugar suffix | Batch workflows refresh once at the end |
| Shared refresh logic | extracted from `refresh.lua` | Avoid duplicating the resolution rebuild in every mutating script |
| First-match-terminal EXPAND dogfood | `CONTROL_SUGAR_V1.expand` | Reuse the existing EXPAND contract (ADR-012); one command → one EXECUTE SCRIPT |
| Install-time bootstrap seed | `sql/install/*` CONFIG row for the sugar rule | Resolves chicken-and-egg: the sugar rule must exist before the sugar can register rules |
| Named `:param` binding | every `pquery` in the new scripts | Positional `?` silently fails under Exasol Lua |

### Consequences

| Decision | Alternatives Considered | Rationale |
|----------|------------------------|-----------|
| New `control/` domain with `admin-scripts` + `command-sugar` features | Fold sugar into `dispatch/command-expansion` | The sugar is a distinct production capability with its own lifecycle; `command-expansion` documents the generic mechanism + demo, not this product command |
| Auto-refresh inside the mutating script | Emit two statements (mutate + REFRESH) from the sugar | EXPAND output must be a single statement; chaining is impossible, so refresh has to live in-script |
| Shared refresh logic extracted and imported | Duplicate the rebuild SQL in each script; or have each script emit `EXECUTE SCRIPT PREPROC.REFRESH` | Duplication drifts; a nested EXECUTE SCRIPT from inside a script is fragile — import the rebuild as a function |
| Unversioned admin scripts, versioned `CONTROL_SUGAR_V1` module | Version everything; version nothing | Matches existing precedent: dispatched modules are versioned (live-replace hazard), admin scripts are not (ADR-008) |
| Installer seeds the sugar rule via direct DML | Ship a separate "enable-sugar" command | Bootstrap chicken-and-egg — the registration command does not yet exist at first install; installer DML is the sanctioned bootstrap (consistent with demo seed) |
| Validation + convention, no DML lockdown | Definer-rights wrapper to block direct DML | Invoker rights make hard lockdown impossible; docs declare direct DML unsupported |

## Features

| Feature | Status | Spec |
|---------|--------|------|
| control/admin-scripts | NEW | `control/admin-scripts/spec.md` |
| control/command-sugar | NEW | `control/command-sugar/spec.md` |
| install/cli-install | CHANGED | `install/cli-install/spec.md` |
| install/cli-bundle | CHANGED | `install/cli-bundle/spec.md` |

## Dependencies

- Existing `refresh.lua` resolution logic (to be factored into a reusable form the mutating scripts can call).
- `EXA_ALL_SCRIPTS` dictionary view for script-existence validation (readable by the admin caller).
- Existing install/bundle deployment machinery in `src/preproc/install.py` and `src/preproc/bundle.py`.

## Migration

| Current | New |
|---------|-----|
| Operators hand-write CONFIG DML + manual `EXECUTE SCRIPT PREPROC.REFRESH` | `EXECUTE SCRIPT PREPROC.RULE_*`/`SET_*` (auto-refresh) or `PREPROC …` sugar commands |
| `sql/demo/seed.sql` direct INSERTs | Unchanged — installer/seed direct DML stays the sanctioned bootstrap; docs mark operator direct DML unsupported |

## Implementation Tasks

1. Spec authoring (done in this plan) — `control/admin-scripts`, `control/command-sugar`, install deltas.
2. Refresh logic refactor
   - [ ] 2.1 Factor the resolution rebuild + fingerprint + stamp out of `refresh.lua` into a form callable from other admin scripts (shared module or `import`-able function), preserving the existing atomicity and fingerprint-first ordering [expert]
   - [ ] 2.2 Confirm `PREPROC.REFRESH` still behaves identically (RETURNS TABLE contract, atomic single-commit) after the refactor
3. Admin control scripts (`sql/lua/`, deployed into `PREPROC`)
   - [ ] 3.1 `rule_add.lua` — validate phase/scope_type, validate script existence via `EXA_ALL_SCRIPTS`, allocate non-colliding operator `rule_id` below 9000, INSERT, auto-refresh unless `NOREFRESH`, return allocated `rule_id` [expert]
   - [ ] 3.2 `rule_drop.lua` — DELETE by `rule_id`, report rows removed, auto-refresh unless `NOREFRESH`
   - [ ] 3.3 `rule_enable.lua` / `rule_disable.lua` — flip `enabled`, auto-refresh unless `NOREFRESH`
   - [ ] 3.4 `rule_list.lua` — RETURNS TABLE, read-only, ordered by `rule_id`
   - [ ] 3.5 `set_mode.lua` — validate + update `SETTINGS.mode`, auto-refresh unless `NOREFRESH`
   - [ ] 3.6 `set_self_disable.lua` — update `SETTINGS.self_disable_allowed` only
   - [ ] 3.7 Ensure all new scripts use named `:param` binding and pass luacheck
4. Convenience EXPAND module
   - [ ] 4.1 `control_sugar_v1.lua` — parse `PREPROC …` grammar, map each subcommand to exactly one `EXECUTE SCRIPT PREPROC.<SCRIPT>(...)`, thread `NOREFRESH` suffix, return `nil` for non-`PREPROC` and unrecognised subcommands; capture `gsub` results to a single variable [expert]
   - [ ] 4.2 Author the module header as the production-module authoring reference (mirrors DEMO_PING_V1 contract block, no `DEMO_` prefix)
5. Bootstrap seed
   - [ ] 5.1 Add a guarded (`WHERE NOT EXISTS`) install-time CONFIG INSERT registering the EXPAND sugar rule scoped to `PREPROC_ADMIN`, in `sql/install/` so it is idempotent and picked up by both install and bundle
6. Deployment wiring
   - [ ] 6.1 Extend `src/preproc/install.py` `_deploy_admin_scripts` to deploy the new control scripts and `_deploy_master` (or equivalent) to deploy `CONTROL_SUGAR_V1`
   - [ ] 6.2 Extend `src/preproc/bundle.py` `_collect_script_statements` with byte-identical wrappers for the new scripts and module
7. Docs
   - [ ] 7.1 Document the control surface and the `PREPROC …` sugar in `docs/operations.md`; declare direct CONFIG DML unsupported and state the installer/seed bootstrap boundary
8. Tests — see Verification (integration tests per scenario, manual invocation per feature).

## Parallelization

| Parallel Group | Tasks |
|----------------|-------|
| Group A | 2.1, 2.2 (refresh refactor — prerequisite for mutating scripts) |
| Group B | 3.1–3.7 (admin scripts; depend on Group A) |
| Group C | 4.1, 4.2 (sugar module; can proceed in parallel with B once script names/signatures are fixed) |
| Group D | 5.1, 6.1, 6.2, 7.1 (seed, deployment, docs; depend on B and C) |

Sequential dependencies:
- Group A → Group B (mutating scripts call the shared refresh logic)
- Group B (script names + signatures) → Group C (sugar emits those exact calls)
- Groups B, C → Group D (deployment wires the finished scripts/module; seed references the module name)

## Dead Code Removal

| Type | Location | Reason |
|------|----------|--------|
| (none) | — | Net-additive change; `refresh.lua` is refactored in place, not removed. `sql/demo/seed.sql` direct DML is intentionally retained as the bootstrap path |

## Verification

### Scenario Coverage

| Scenario | Test Type | Test Location | Test Name |
|----------|-----------|---------------|-----------|
| RULE_ADD inserts a validated rule and auto-refreshes | Integration | `tests/test_control_admin_scripts.py` | `test_rule_add_inserts_and_refreshes` |
| RULE_ADD auto-allocated rule_ids do not collide | Integration | `tests/test_control_admin_scripts.py` | `test_rule_add_allocates_distinct_rule_ids` |
| RULE_ADD rejects an invalid phase | Integration | `tests/test_control_admin_scripts.py` | `test_rule_add_rejects_invalid_phase` |
| RULE_ADD rejects an invalid scope type | Integration | `tests/test_control_admin_scripts.py` | `test_rule_add_rejects_invalid_scope_type` |
| RULE_ADD rejects a non-existent script | Integration | `tests/test_control_admin_scripts.py` | `test_rule_add_rejects_missing_script` |
| RULE_ADD with NOREFRESH skips the auto-refresh | Integration | `tests/test_control_admin_scripts.py` | `test_rule_add_norefresh_skips_refresh` |
| RULE_DROP removes a rule and auto-refreshes | Integration | `tests/test_control_admin_scripts.py` | `test_rule_drop_removes_and_refreshes` |
| RULE_DROP of an unknown rule_id is a reported no-op | Integration | `tests/test_control_admin_scripts.py` | `test_rule_drop_unknown_is_noop` |
| RULE_DISABLE flips the enabled flag and auto-refreshes | Integration | `tests/test_control_admin_scripts.py` | `test_rule_disable_flips_and_refreshes` |
| RULE_ENABLE flips the enabled flag and auto-refreshes | Integration | `tests/test_control_admin_scripts.py` | `test_rule_enable_flips_and_refreshes` |
| RULE_LIST returns the current rules and is read-only | Integration | `tests/test_control_admin_scripts.py` | `test_rule_list_returns_rows_readonly` |
| SET_MODE updates the deployment mode and auto-refreshes | Integration | `tests/test_control_admin_scripts.py` | `test_set_mode_updates_and_validates` |
| SET_SELF_DISABLE updates the self-disable flag | Integration | `tests/test_control_admin_scripts.py` | `test_set_self_disable_updates_flag` |
| A non-admin caller cannot mutate config through a control script | Integration | `tests/test_control_admin_scripts.py` | `test_non_admin_rejected` |
| Control scripts pass luacheck | Integration | `tests/test_control_admin_scripts.py` | `test_control_scripts_pass_luacheck` |
| ADD RULE command expands to a RULE_ADD execute-script call | Integration | `tests/test_command_sugar.py` | `test_add_rule_expands_to_rule_add` |
| DROP RULE command expands to a RULE_DROP execute-script call | Integration | `tests/test_command_sugar.py` | `test_drop_rule_expands_to_rule_drop` |
| DISABLE and ENABLE RULE commands expand to their control scripts | Integration | `tests/test_command_sugar.py` | `test_disable_enable_expand` |
| LIST RULES command expands to a RULE_LIST execute-script call | Integration | `tests/test_command_sugar.py` | `test_list_rules_expands` |
| REFRESH and STATUS commands expand to their admin scripts | Integration | `tests/test_command_sugar.py` | `test_refresh_status_expand` |
| SET MODE and SET SELF_DISABLE commands expand to settings scripts | Integration | `tests/test_command_sugar.py` | `test_set_commands_expand` |
| A NOREFRESH suffix is threaded into the control-script call | Integration | `tests/test_command_sugar.py` | `test_norefresh_suffix_threaded` |
| A non-PREPROC statement is declined | Integration | `tests/test_command_sugar.py` | `test_non_preproc_declined` |
| An unrecognised PREPROC subcommand surfaces a loud error | Integration | `tests/test_command_sugar.py` | `test_unrecognised_subcommand_declined` |
| The convenience module passes luacheck | Integration | `tests/test_command_sugar.py` | `test_sugar_passes_luacheck` |
| The convenience EXPAND rule is seeded at install as the bootstrap path | Integration | `tests/test_command_sugar.py` | `test_sugar_bootstrap_rule_seeded` |
| Install deploys the control scripts and convenience module | Integration | `tests/test_cli_install.py` | `test_install_deploys_control_layer` |
| Install seeds the convenience EXPAND bootstrap rule | Integration | `tests/test_cli_install.py` | `test_install_seeds_sugar_rule` |
| Bundle wraps every Lua script and the demo UDF as CREATE OR REPLACE | Integration | `tests/test_cli_bundle.py` | `test_bundle_wraps_control_layer` |
| The generated bundle deploys an equivalent layout to CLI install | Integration | `tests/test_cli_bundle.py` | `test_bundle_equivalent_to_install` |

### Manual Testing

| Feature | Command | Expected Output |
|---------|---------|-----------------|
| control/admin-scripts | `EXECUTE SCRIPT PREPROC.RULE_ADD('ROLE','ANALYSTS','EXPAND','PREPROC_RT.DEMO_PING_V1','ping');` | Returns the allocated `rule_id` (operator space, < 9000); `PREPROC.RESOLUTION` updated |
| control/admin-scripts | `EXECUTE SCRIPT PREPROC.RULE_LIST;` | One row per rule, ordered by `rule_id`, including the just-added rule |
| control/admin-scripts | `EXECUTE SCRIPT PREPROC.RULE_DROP(<rule_id>);` | Reports 1 row removed; rule no longer in `RULE_LIST` |
| control/command-sugar | (with sugar rule active) `PREPROC LIST RULES;` | Expands to `EXECUTE SCRIPT PREPROC.RULE_LIST` and returns the rule rows |
| control/command-sugar | `PREPROC DROP RULE 42;` | Expands to `EXECUTE SCRIPT PREPROC.RULE_DROP(42)`; rule 42 removed |
| control/command-sugar | `PREPROC REFRESH;` | Expands to `EXECUTE SCRIPT PREPROC.REFRESH`; resolution rebuilt |
| install/cli-install | `preproc install --verbose` then `SELECT * FROM PREPROC.CONFIG WHERE script_name='PREPROC_RT.CONTROL_SUGAR_V1';` | Install completes; one enabled EXPAND rule scoped to `PREPROC_ADMIN` present |
| install/cli-bundle | `preproc bundle && grep -c 'CONTROL_SUGAR_V1\|RULE_ADD' deploy_all.sql` | Bundle written; wrappers for the new scripts and module present |

### Checklist

| Step | Command | Expected |
|------|---------|----------|
| Build / env | `uv sync` | Exit 0 |
| Test | `uv run pytest` | 0 failures |
| Lint (Python) | `uv run ruff check . && uv run ruff format --check .` | 0 errors, no changes |
| Lint (Lua) | `luacheck sql/lua/` | 0 warnings, 0 errors |
| Coverage | `uv run pytest --cov=preproc --cov-report=term-missing` | New scripts/module covered |
