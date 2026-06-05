# Plan: add-install-two-schema-layout

## Summary

Bootstrap the preprocessor-framework with its foundational two-schema database layout (`PREPROC` admin + `PREPROC_RT` runtime), a safe pass-through MASTER dispatcher skeleton, and a re-runnable `preproc install` CLI verb. This is the bedrock slice: it creates schemas, tables, views, schema-level PUBLIC grants, and the MASTER script, but leaves preprocessor activation to the DBA.

## Design

### Context

Exasol exposes a single global SQL preprocessor (`sql_preprocessor_script`). The framework's value is a decision layer that dispatches per-user transformations from config — but that layer needs a stable, auditable foundation before any dispatch logic exists. Two hard constraints, both experimentally verified, force the architecture: Exasol Lua scripts run with **invoker rights** (a script cannot read admin tables a user lacks grants on), while **views run with definer rights** (the view owner's privileges apply). And once a preprocessor is active in enforced mode, a user missing EXECUTE on it cannot run *any* statement — so the schema-level PUBLIC grants are a survival invariant, not a convenience.

- **Goals**
  - Create the complete two-schema object layout as idempotent, auditable SQL DDL.
  - Encode the privilege bridge: definer-rights view (`MY_PIPELINE`) is the only path from invoker-rights scripts to admin tables.
  - Install MASTER as a *safe* skeleton that already honours the guard/fail-open rules.
  - Ship a re-runnable CLI install verb and the test scaffolding the whole project builds on.
- **Non-Goals**
  - No dispatch logic in MASTER (later slice) — pure pass-through here.
  - No activation of the preprocessor (`--activate` is explicitly deferred).
  - No `refresh`, `status`, or `explain` verbs; no profile codegen; no TRANSLATE UDF.

### Decision

A layered bootstrap: SQL DDL files are the source of truth, a thin pyexasol CLI applies them in dependency order and deploys the Lua script, and an env-var-driven pytest fixture proves the result against Docker or a live instance interchangeably.

#### Architecture

```
┌──────────────────┐   applies in order   ┌──────────────────────────┐
│ preproc install  │──────────────────────▶│ sql/install/*.sql (DDL)   │
│ (pyexasol, env)  │                       │ + sql/lua/master.lua      │
└──────────────────┘                       └──────────────────────────┘
        │                                              │
        │ prints (does not run)                        ▼
        ▼                              ┌────────────────────────────────┐
 ALTER SYSTEM SET sql_preprocessor_    │ PREPROC (admin, PREPROC_ADMIN)  │
 script = PREPROC_RT.MASTER            │  CONFIG RESOLUTION SETTINGS     │
                                       │  PROFILES                       │
                                       └────────────────────────────────┘
                                                   ▲ definer-rights read
                                       ┌────────────────────────────────┐
                                       │ PREPROC_RT (runtime, PUBLIC)    │
                                       │  MY_PIPELINE (view)             │
                                       │  MASTER (lua skeleton)          │
                                       │  GRANT EXECUTE/SELECT → PUBLIC  │
                                       └────────────────────────────────┘
```

#### Patterns

| Pattern | Where | Why |
|---------|-------|-----|
| DDL-as-source-of-truth, applied in filename order | `sql/install/NN_*.sql` | Auditable by DBAs; install verb is a thin applier, not a generator |
| Definer-rights view as privilege bridge | `PREPROC_RT.MY_PIPELINE` | Invoker-rights scripts cannot read admin tables; the view can, scoped by `CURRENT_USER` |
| Schema-level PUBLIC grants | `GRANT EXECUTE/SELECT ON SCHEMA PREPROC_RT` | Covers all future objects; missing grants = total lockout in enforced mode |
| Idempotent DDL (`IF NOT EXISTS` / `CREATE OR REPLACE` / guarded seed) | all install SQL | Re-run is a no-op; future DDL changes are an upgrade path |
| Fail-open skeleton with control-statement guard | `master.lua` | A misbehaving active preprocessor must never block statements or rewrite `ALTER SESSION/SYSTEM` |
| Env-var connection abstraction | `tests/conftest.py`, CLI | One code path for Docker (CI) and live instances (local); 12-factor |

### Consequences

| Decision | Alternatives Considered | Rationale |
|----------|------------------------|-----------|
| Env-var auth (`EXASOL_DSN/USER/PASSWORD`) | CLI flags; config file | 12-factor; composable with `.env` locally and CI secrets; no secrets on argv |
| Install creates but does not activate | Auto-activate; `--activate` flag now | Activation is system-wide and risky; DBA owns the cutover. Print the statement, defer the flag |
| Idempotent re-run as the upgrade path | Separate `upgrade` verb; migrations table | Simpler; DDL diffs are the upgrade; `CREATE OR REPLACE` + guarded seed make re-run safe |
| Roll our own pytest fixture | `pytest-exasol-backend` plugin | Minimal dependency surface; identical connection path Docker vs. live; full control over skip behavior |
| MASTER skeleton ships guards now | Bare `return getsqltext()` skeleton | The guard + fail-open contract is the dangerous part; encode and test it before dispatch lands on top |

## Features

| Feature | Status | Spec |
|---------|--------|------|
| schema-layout | NEW | `install/schema-layout/spec.md` |
| master-skeleton | NEW | `install/master-skeleton/spec.md` |
| cli-install | NEW | `install/cli-install/spec.md` |
| project-bootstrap | NEW | `install/project-bootstrap/spec.md` |

## Dependencies

- `uv`, Python 3.12, `pyexasol`, `pytest`, `ruff` (Python toolchain)
- `luacheck` (Lua static analysis; system or luarocks install)
- Docker with the `exasol/docker-db` image (CI integration tier); Exasol 2025.x+ for live runs

## Implementation Tasks

1. **Project bootstrap**
   1. Create `pyproject.toml` (uv): deps pyexasol/pytest/ruff, `preproc` console entry point, ruff config.
   2. Create the directory skeleton: `src/preproc/`, `sql/install/`, `sql/lua/`, `sql/udf/`, `tests/`, `docs/`.
   3. Add `.luacheckrc` configured for Exasol preprocessor globals.
   4. Build the pytest connection fixture in `tests/conftest.py`: env-var driven, yields/closes a pyexasol connection, skips cleanly when unset. [expert]
   5. Add CI workflow skeleton that runs `uv sync`, ruff, luacheck, and pytest against `exasol/docker-db`.

2. **Schema layout DDL** (`sql/install/`)
   1. `10_schemas.sql` — `PREPROC_ADMIN` role; `PREPROC` and `PREPROC_RT` schemas; ownership.
   2. `20_tables.sql` — `CONFIG`, `RESOLUTION`, `SETTINGS`, `PROFILES` (`IF NOT EXISTS`).
   3. `30_settings_seed.sql` — guarded single-row `SETTINGS` seed (no duplicate on re-run).
   4. `40_views.sql` — `CREATE OR REPLACE VIEW PREPROC_RT.MY_PIPELINE` with `CURRENT_USER` filter, phase ordering.
   5. `50_grants.sql` — schema-level `EXECUTE`/`SELECT` on `PREPROC_RT` to PUBLIC.

3. **MASTER skeleton** (`sql/lua/master.lua`)
   1. Pass-through MASTER: read `MY_PIPELINE`, return `sqltext` unchanged; guard `ALTER SESSION/SYSTEM`; fail open on lookup error; correct `setsqltext`/`gsub` handling. [expert]
   2. Wire MASTER deployment (`CREATE OR REPLACE LUA SCRIPT PREPROC_RT.MASTER`) into the install applier.

4. **CLI install verb** (`src/preproc/`)
   1. Connection module: read `EXASOL_DSN/USER/PASSWORD`, validate presence, build pyexasol connection, clear errors.
   2. Install command: apply `sql/install/*.sql` in filename order, deploy MASTER, print (do not run) the activation statement; idempotent.
   3. CLI entry point wiring (`preproc install`) with non-zero exit on missing env / connection failure.

5. **Tests**
   1. Integration tests for schema-layout scenarios (objects, ownership, grants, view scoping, idempotency).
   2. Integration tests for MASTER behaviour (pass-through, ALTER guard, fail-open) against an activated session-scoped preprocessor in the test schema. [expert]
   3. Integration tests for `preproc install` (clean bootstrap, prints-not-activates, re-run, missing env, unreachable).
   4. Unit tests for env-var validation (pure, no I/O).
   5. luacheck invocation wired as part of the lint step / a test.

## Parallelization

| Parallel Group | Tasks |
|----------------|-------|
| Group A (scaffold) | 1.1, 1.2, 1.3 |
| Group B (SQL + Lua authoring) | 2.1–2.5, 3.1 |
| Group C (CLI + fixture) | 1.4, 4.1, 4.2, 4.3 |
| Group D (tests) | 5.1, 5.2, 5.3, 5.4, 5.5 |

Sequential dependencies:
- Group A → all others (need the project skeleton).
- Group B → 3.2 and Group D (DDL/Lua must exist before the applier deploys them and before integration tests run).
- Group C → Group D (the fixture and CLI must exist before their tests).
- 1.5 (CI) last — wires up the commands the other groups produce.

## Dead Code Removal

| Type | Location | Reason |
|------|----------|--------|
| — | — | Greenfield slice; no existing code to remove |

## Verification

### Scenario Coverage

| Scenario | Test Type | Test Location | Test Name |
|----------|-----------|---------------|-----------|
| Admin and runtime schemas are created | Integration | `tests/test_schema_layout.py` | `test_schemas_created` |
| PREPROC_ADMIN role owns the admin schema | Integration | `tests/test_schema_layout.py` | `test_admin_role_owns_schema` |
| Admin configuration tables are created | Integration | `tests/test_schema_layout.py` | `test_admin_tables_created` |
| SETTINGS is seeded with a single default row | Integration | `tests/test_schema_layout.py` | `test_settings_seeded_single_row` |
| The CURRENT_USER-filtered pipeline view is created | Integration | `tests/test_schema_layout.py` | `test_my_pipeline_view_created` |
| Schema-level PUBLIC grants cover the runtime schema | Integration | `tests/test_schema_layout.py` | `test_public_schema_grants` |
| A non-admin user reads only their own pipeline rows | Integration | `tests/test_schema_layout.py` | `test_my_pipeline_filters_current_user` |
| Re-applying the layout is idempotent | Integration | `tests/test_schema_layout.py` | `test_layout_idempotent` |
| MASTER script is installed into the runtime schema | Integration | `tests/test_master_skeleton.py` | `test_master_script_installed` |
| A normal statement passes through unchanged | Integration | `tests/test_master_skeleton.py` | `test_normal_statement_passthrough` |
| ALTER SESSION and ALTER SYSTEM statements are guarded | Integration | `tests/test_master_skeleton.py` | `test_alter_statements_guarded` |
| Lookup failure fails open | Integration | `tests/test_master_skeleton.py` | `test_lookup_failure_fails_open` |
| MASTER passes luacheck | Integration | `tests/test_master_skeleton.py` | `test_master_passes_luacheck` |
| Install bootstraps a clean database | Integration | `tests/test_cli_install.py` | `test_install_clean_database` |
| Install prints the activation statement without executing it | Integration | `tests/test_cli_install.py` | `test_install_prints_not_activates` |
| Install is safe to re-run | Integration | `tests/test_cli_install.py` | `test_install_rerun_idempotent` |
| Missing connection environment variables fail fast | Unit | `tests/test_cli_install.py` | `test_missing_env_fails_fast` |
| Unreachable instance reports a connection error | Integration | `tests/test_cli_install.py` | `test_unreachable_instance_errors` |
| uv sync produces a working environment | Integration | `tests/test_project_bootstrap.py` | `test_uv_sync_environment` |
| The connection fixture targets the env-configured instance | Integration | `tests/test_project_bootstrap.py` | `test_connection_fixture_targets_env` |
| Integration tests skip cleanly when no instance is configured | Unit | `tests/test_project_bootstrap.py` | `test_integration_skips_without_instance` |
| luacheck configuration is present and passes | Integration | `tests/test_project_bootstrap.py` | `test_luacheck_passes` |
| Lint and format checks pass on the source tree | Integration | `tests/test_project_bootstrap.py` | `test_ruff_clean` |

### Manual Testing

| Feature | Command | Expected Output |
|---------|---------|-----------------|
| project-bootstrap | `uv sync` | Exit 0; environment resolves with pyexasol/pytest/ruff |
| schema-layout | `preproc install` then query `EXA_ALL_SCHEMAS` for `PREPROC`/`PREPROC_RT` | Both schemas present; `PREPROC.SETTINGS` has one row |
| cli-install | `EXASOL_DSN=... EXASOL_USER=... EXASOL_PASSWORD=... preproc install` | Exit 0; prints `ALTER SYSTEM SET sql_preprocessor_script = PREPROC_RT.MASTER`; setting unchanged |
| cli-install | `unset EXASOL_DSN; preproc install` | Non-zero exit; message naming the missing variable; no connection attempted |
| master-skeleton | `luacheck sql/lua/` | Exit 0; zero warnings |
| master-skeleton | In a test schema: activate MASTER session-scoped, run `SELECT 1` | Returns `1`; statement text unchanged |

### Checklist

| Step | Command | Expected |
|------|---------|----------|
| Build | `uv sync` | Exit 0 |
| Test | `uv run pytest` | 0 failures |
| Lint (Python) | `uv run ruff check .` | 0 errors |
| Lint (Lua) | `luacheck sql/lua/` | 0 warnings |
| Format | `uv run ruff format --check .` | No changes |
