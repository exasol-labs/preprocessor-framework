# Plan: add-demo-suite

## Summary

Build a demonstration suite that showcases the framework's selling point — one config-driven lookup resolving different pipelines for different users, roles, and clients — by adding a TRANSLATE-phase `sqlglot` translation module, a REWRITE-phase audit module, a heavily commented SQL seed/teardown of the config model, and a `preproc bundle` verb that emits a single runnable `deploy_all.sql`.

## Design

### Context

The current demo is `DEMO_PING_V1` alone (one EXPAND module), which shows the dispatch mechanism but not the decision layer the framework exists for. The mission's core capabilities call out dialect translation (capability 4) and the role/user/client decision layer (capabilities 1–2), neither of which the current demo exercises. The change adds one demo module per remaining phase plus config that makes per-user pipeline differences visible, and packages deployment as one file.

- **Goals** — One demo module per phase (TRANSLATE, EXPAND-reused, REWRITE); a seed that documents the config model and shows divergent per-user pipelines; a single-file generated deploy artifact; updated operator docs.
- **Non-Goals** — No changes to MASTER dispatch semantics, the resolution algorithm, or the schema layout. No production (non-`DEMO_`) modules. No security/enforcement claims. No audit-log table writes. No new dialects beyond what `sqlglot` transpiles with zero extra code.

### Decision

#### Architecture

```
preproc install / deploy_all.sql
        │
        ▼
  PREPROC_RT (runtime, PUBLIC)                 PREPROC (admin, locked)
  ┌──────────────────────────────┐            ┌─────────────────────┐
  │ MASTER (dispatcher)           │            │ CONFIG  RESOLUTION  │
  │ DEMO_PING_V1      (EXPAND)    │            │ SETTINGS  PROFILES  │
  │ DEMO_TRANSLATE_V1 (TRANSLATE) │──UDF call─▶│ REFRESH  STATUS     │
  │   └▶ DEMO_SQLGLOT_TRANSPILE_V1│            └─────────────────────┘
  │ DEMO_AUDIT_V1     (REWRITE)   │                     ▲
  │ MY_PIPELINE (view)            │      sql/demo/seed.sql writes CONFIG,
  └──────────────────────────────┘      runs REFRESH, shows MY_PIPELINE
```

Statement flow under MASTER for a demo user: `--!dialect:tsql SELECT TOP 5 …` → TRANSLATE (`DEMO_TRANSLATE_V1` calls the UDF, `TOP`→`LIMIT`) → EXPAND (`DEMO_PING_V1`, no match) → REWRITE (`DEMO_AUDIT_V1` appends `/* PREPROC_AUDIT user=… */`) → `setsqltext`.

#### Patterns

| Pattern | Where | Why |
|---------|-------|-----|
| Explicit dialect marker (`--!dialect:<name>`) | `DEMO_TRANSLATE_V1` | Avoids always-transpile cost (~133 ms) and cross-dialect quote-corruption; demo-readable |
| `ERR:` sentinel return | `DEMO_SQLGLOT_TRANSPILE_V1` UDF | Lets the Lua module distinguish a real translation from a failure and fail closed |
| Fail-closed module | `DEMO_TRANSLATE_V1` | Never thread `ERR:` or corrupted text forward; return original unchanged on any doubt |
| `/`-delimited generated file | `preproc bundle` | Script/UDF bodies contain `;`; `/` terminator is the only client-runnable form |
| Generated, never hand-edited | `deploy_all.sql` | Single source of truth stays the `sql/` tree |
| Heavily-commented SQL seed | `sql/demo/seed.sql` | Doubles as config-model documentation, fits one-file philosophy |
| `DEMO_`-prefixed, `_V1`-suffixed objects | all new modules/roles/users | Safe to leave deployed; versioning discipline; never CREATE OR REPLACE live semantics |

### Consequences

| Decision | Alternatives Considered | Rationale |
|----------|------------------------|-----------|
| T-SQL `TOP n` / `GETDATE()` as the demo translation | `NOW()` (rejected: valid Exasol), Postgres `::` casts, MySQL backticks | `SELECT TOP n` is a hard Exasol syntax error and transpiles cleanly to `LIMIT n` with zero extra code — verified live; maximally compelling |
| Magic-comment dialect marker | Always-transpile for demo-role holders | Always-transpile is slow and risks corrupting valid Exasol SQL; marker is explicit, fail-closed, self-documenting |
| `preproc bundle` emits `/`-delimited file | `;`-split bundle; CLI-only deploy | Script bodies contain semicolons; CLI install stays for dev/CI per the interview |
| SQL seed/teardown scripts | Python seeding tool | Matches "administered through DB-side scripts" and one-file philosophy; readable as docs |
| Audit module appends an SQL comment | Injecting a column / table write | Comment keeps results identical and writes nothing — safest visible REWRITE; audit-log writes are out of scope |

## Features

| Feature | Status | Spec |
|---------|--------|------|
| dispatch/dialect-translation | NEW | `specs/_plans/add-demo-suite/dispatch/dialect-translation/spec.md` |
| dispatch/audit-rewrite | NEW | `specs/_plans/add-demo-suite/dispatch/audit-rewrite/spec.md` |
| install/cli-bundle | NEW | `specs/_plans/add-demo-suite/install/cli-bundle/spec.md` |
| demo/config-seed | NEW | `specs/_plans/add-demo-suite/demo/config-seed/spec.md` |
| install/cli-install | CHANGED | `specs/_plans/add-demo-suite/install/cli-install/spec.md` |

## Dependencies

- `sqlglot` ≥27.x in the Exasol default Python3 UDF container (verified 27.17.0 on 2025.2.x) — no vendoring.
- Existing `PREPROC.REFRESH` script (seed/teardown call it); existing schema-level PUBLIC grants (cover the new runtime objects automatically).

## Implementation Tasks

1. **TRANSLATE module + UDF**
   - [ ] 1.1 Add `sql/udf/demo_sqlglot_transpile_v1.sql` — `CREATE OR REPLACE PYTHON3 SCALAR SCRIPT PREPROC_RT.DEMO_SQLGLOT_TRANSPILE_V1(dialect VARCHAR(64), txt VARCHAR(2000)) RETURNS VARCHAR(2000)` importing `sqlglot`, returning the transpiled text or the `ERR:` sentinel on failure. [expert]
   - [ ] 1.2 Add `sql/lua/demo_translate_v1.lua` — `translate(sqltext)` detecting the `--!dialect:<name>` marker, stripping it, calling the UDF via pquery, and failing closed on the `ERR:` sentinel (return original unchanged). [expert]
   - [ ] 1.3 Verify the module passes luacheck (`luacheck sql/lua/`).

2. **REWRITE module**
   - [ ] 2.1 Add `sql/lua/demo_audit_v1.lua` — `tag(sqltext)` appending `/* PREPROC_AUDIT user=<CURRENT_USER> */`, deriving the user inside the module; single valid statement out.
   - [ ] 2.2 Verify the module passes luacheck.

3. **Install deployment**
   - [ ] 3.1 Extend `src/preproc/install.py` `_deploy_master` (or a new `_deploy_demo_modules`) to deploy `DEMO_TRANSLATE_V1`, `DEMO_AUDIT_V1`, and the UDF into `PREPROC_RT`, mirroring the existing `DEMO_PING_V1` wrapper pattern.
   - [ ] 3.2 Confirm schema-level PUBLIC grants already cover the new objects (verify; no grant change expected).

4. **Bundle generator**
   - [ ] 4.1 Add `src/preproc/bundle.py` — assemble `deploy_all.sql` from `sql/install/*.sql` (sorted) + `/`-delimited `CREATE OR REPLACE` wrappers for all Lua scripts and the UDF, reusing the exact wrapper signatures from `install.py`. Deterministic / reproducible output, no activation, no seed. [expert]
   - [ ] 4.2 Add the `bundle` subcommand to `src/preproc/cli.py` (arg: optional `--output`, default `deploy_all.sql`); report path + statement count; exit codes consistent with `install`.

5. **Demo config seed/teardown**
   - [ ] 5.1 Add `sql/demo/seed.sql` — create `DEMO_ANALYST`/`DEMO_ETL` roles, ≥2 demo users with different grants, INSERT TRANSLATE/EXPAND/REWRITE rules (≥1 `client_pattern`-scoped) into `PREPROC.CONFIG` using a reserved `rule_id` range, run `PREPROC.REFRESH`, include commented `MY_PIPELINE` SELECTs. Heavily commented as config-model docs. [expert]
   - [ ] 5.2 Add `sql/demo/teardown.sql` — idempotently remove demo CONFIG rows (reserved range), demo users, demo roles, then run `PREPROC.REFRESH`.

6. **Documentation**
   - [ ] 6.1 Update `README.md` quickstart to feature the one-file `deploy_all.sql` deploy and the demo walkthrough (TRANSLATE/EXPAND/REWRITE).
   - [ ] 6.2 Update `docs/operations.md` — bundle generation + single-file deploy path; demo seed/teardown; the ~133 ms TRANSLATE cost note.
   - [ ] 6.3 Update `docs/how-it-works.md` — TRANSLATE/UDF architecture (marker trigger, fail-closed, `ERR:` sentinel) if it adds concepts not already covered.

7. **Tests** (see Scenario Coverage)
   - [ ] 7.1 `tests/test_dialect_translation.py` — UDF + module + pipeline scenarios. [expert]
   - [ ] 7.2 `tests/test_audit_rewrite.py` — audit module scenarios.
   - [ ] 7.3 `tests/test_cli_bundle.py` — bundle generation + reproducibility + deploy-equivalence scenarios.
   - [ ] 7.4 `tests/test_demo_seed.py` — seed/teardown scenarios.
   - [ ] 7.5 Extend `tests/test_cli_install.py` for the new install deltas.

## Parallelization

| Parallel Group | Tasks |
|----------------|-------|
| Group A | 1 (TRANSLATE module+UDF), 2 (REWRITE module) |
| Group B | 4 (bundle generator) |
| Group C | 3 (install deployment) — depends on A |
| Group D | 5 (seed/teardown) — depends on A + C |
| Group E | 6 (docs), 7 (tests) — depends on the features they cover |

Sequential dependencies:
- A → C (install deploys the new modules)
- C → D (seed needs the modules deployed and refresh runnable)
- A,C,D → E test tasks; B → 7.3

## Dead Code Removal

| Type | Location | Reason |
|------|----------|--------|
| (none) | — | Purely additive; `DEMO_PING_V1`, MASTER, and the schema layout are reused unchanged |

## Verification

### Scenario Coverage

| Scenario | Test Type | Test Location | Test Name |
|----------|-----------|---------------|-----------|
| The transpile UDF converts a tagged foreign-dialect statement to Exasol SQL | Integration | `tests/test_dialect_translation.py` | `test_transpile_udf_converts_tsql_top_to_limit` |
| The transpile UDF returns the ERR sentinel on unparseable input | Integration | `tests/test_dialect_translation.py` | `test_transpile_udf_returns_err_sentinel_on_garbage` |
| A marked statement is translated to runnable Exasol SQL through the pipeline | Integration | `tests/test_dialect_translation.py` | `test_marked_statement_translated_through_pipeline` |
| An unmarked statement passes through TRANSLATE unchanged | Integration | `tests/test_dialect_translation.py` | `test_unmarked_statement_passes_through_unchanged` |
| A failed transpilation fails closed and does not corrupt the statement | Integration | `tests/test_dialect_translation.py` | `test_failed_transpile_fails_closed` |
| DEMO_TRANSLATE_V1 and the transpile UDF are deployed by install | Integration | `tests/test_dialect_translation.py` | `test_translate_module_and_udf_deployed` |
| DEMO_TRANSLATE_V1 passes luacheck | Integration | `tests/test_dialect_translation.py` | `test_translate_module_passes_luacheck` |
| The audit module appends a marker comment to a statement | Integration | `tests/test_audit_rewrite.py` | `test_audit_appends_marker_comment` |
| The audit marker carries the current user identity | Integration | `tests/test_audit_rewrite.py` | `test_audit_marker_carries_current_user` |
| Audit rewrite runs after EXPAND in the pipeline | Integration | `tests/test_audit_rewrite.py` | `test_audit_runs_after_expand` |
| DEMO_AUDIT_V1 is deployed by install | Integration | `tests/test_audit_rewrite.py` | `test_audit_module_deployed` |
| DEMO_AUDIT_V1 passes luacheck | Integration | `tests/test_audit_rewrite.py` | `test_audit_module_passes_luacheck` |
| Bundle emits a single deployable SQL file | Integration | `tests/test_cli_bundle.py` | `test_bundle_emits_single_file` |
| Bundle contains the full install DDL in numbered order | Integration | `tests/test_cli_bundle.py` | `test_bundle_contains_install_ddl_in_order` |
| Bundle wraps every Lua script and the demo UDF as CREATE OR REPLACE | Integration | `tests/test_cli_bundle.py` | `test_bundle_wraps_scripts_and_udf` |
| Bundle uses slash delimiters so script bodies survive | Unit | `tests/test_cli_bundle.py` | `test_bundle_uses_slash_delimiters` |
| The generated bundle deploys an equivalent layout to CLI install | Integration | `tests/test_cli_bundle.py` | `test_bundle_deploys_equivalent_layout` |
| Bundle is reproducible and excludes activation and seed data | Integration | `tests/test_cli_bundle.py` | `test_bundle_reproducible_no_activation_no_seed` |
| Seed creates demo roles and users with distinct pipelines | Integration | `tests/test_demo_seed.py` | `test_seed_creates_roles_users_distinct_pipelines` |
| Seed inserts rules across all three phases | Integration | `tests/test_demo_seed.py` | `test_seed_inserts_rules_all_phases` |
| Seed includes a client_pattern-scoped rule | Integration | `tests/test_demo_seed.py` | `test_seed_includes_client_pattern_rule` |
| Seed runs refresh so resolution reflects the demo config | Integration | `tests/test_demo_seed.py` | `test_seed_runs_refresh` |
| Different demo users see different resolved pipelines | Integration | `tests/test_demo_seed.py` | `test_demo_users_see_different_pipelines` |
| Teardown removes everything the seed created | Integration | `tests/test_demo_seed.py` | `test_teardown_removes_demo_objects` |
| Teardown is idempotent | Integration | `tests/test_demo_seed.py` | `test_teardown_idempotent` |
| Install bootstraps a clean database (now deploys demo modules + UDF) | Integration | `tests/test_cli_install.py` | `test_install_deploys_demo_modules_and_udf` |
| Installed demo modules and UDF are executable by PUBLIC | Integration | `tests/test_cli_install.py` | `test_demo_modules_and_udf_public_executable` |

### Manual Testing

| Feature | Command | Expected Output |
|---------|---------|-----------------|
| dispatch/dialect-translation | `SELECT PREPROC_RT.DEMO_SQLGLOT_TRANSPILE_V1('tsql','SELECT TOP 5 col FROM t')` | `SELECT col FROM t LIMIT 5` |
| dispatch/dialect-translation | After activation + demo rule: submit `--!dialect:tsql` + `SELECT TOP 5 1 AS X` | Runs as `SELECT 1 AS X LIMIT 5` (no syntax error) |
| dispatch/audit-rewrite | After activation + demo rule: `SELECT 1` then inspect rewritten text | Statement carries `/* PREPROC_AUDIT user=<USER> */` |
| install/cli-bundle | `uv run preproc bundle --output /tmp/deploy_all.sql` | Reports path + statement count; file exists |
| install/cli-bundle | Run `/tmp/deploy_all.sql` in EXAplus against clean DB | Schemas/tables/scripts/UDF created; no ALTER SYSTEM run |
| demo/config-seed | Run `sql/demo/seed.sql` as admin, then `SELECT * FROM PREPROC.RESOLUTION` | Demo users show different resolved rules |
| demo/config-seed | Run `sql/demo/teardown.sql` | Demo roles/users/rules gone; RESOLUTION clean |
| install/cli-install | `uv run preproc install` then check `PREPROC_RT` scripts | `DEMO_TRANSLATE_V1`, `DEMO_AUDIT_V1`, `DEMO_SQLGLOT_TRANSPILE_V1` present |

### Checklist

| Step | Command | Expected |
|------|---------|----------|
| Build | `uv sync` | Exit 0 |
| Test | `uv run pytest` | 0 failures |
| Lint (Python) | `uv run ruff check . && uv run ruff format --check .` | 0 errors/warnings |
| Lint (Lua) | `luacheck sql/lua/` | 0 warnings, 0 errors |
| Coverage | `uv run pytest --cov=preproc --cov-report=term-missing` | New modules covered |
