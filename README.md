# preprocessor-framework

A config-driven Exasol SQL preprocessor framework. It turns Exasol's single
global SQL preprocessor slot into a modular, role- and client-aware
transformation pipeline: a MASTER Lua dispatcher reads the current user's
resolved rule set from a config-driven lookup and runs their transformations in
a fixed three-phase order — TRANSLATE → EXPAND → REWRITE — before the
statement reaches the engine.

The framework solves a composition and decision problem: Exasol allows only one
preprocessor script per system. This framework provides the dispatch layer that
routes per-role, per-user, and per-client transformations through that single
entry point, keeps the rule configuration fresh as database roles change, and
maintains a fail-open safety contract so a broken module never blocks a user's
statement.

## Prerequisites

- Exasol 2025.x instance (2025.2.0 verified)
- [uv](https://docs.astral.sh/uv/) for environment management
- DBA-level credentials on the target instance

## Quickstart

### 1. Set connection environment variables

```bash
export EXASOL_DSN=myhost:8563
export EXASOL_USER=sys
export EXASOL_PASSWORD=exasol
# Optional — set to 0 or false to disable TLS certificate verification:
# export EXASOL_TLS_VERIFY=false
```

### 2. Install the framework

```bash
uv sync
uv run preproc install
```

The installer creates schemas `PREPROC` and `PREPROC_RT`, all tables and views,
and deploys the MASTER dispatcher plus the demo modules `DEMO_PING_V1`
(EXPAND), `DEMO_TRANSLATE_V1` (TRANSLATE), `DEMO_AUDIT_V1` (REWRITE), and the
companion `DEMO_SQLGLOT_TRANSPILE_V1` UDF. It prints the activation statement
but does **not** execute it:

```
ALTER SYSTEM SET sql_preprocessor_script = PREPROC_RT.MASTER;
```

A DBA must run that statement manually to activate the preprocessor system-wide.

### 2a. Single-file deploy (alternative to CLI install)

If you prefer an operator-friendly single SQL file, generate the bundle first:

```bash
uv run preproc bundle --output deploy_all.sql
```

Then run `deploy_all.sql` in EXAplus or DbVisualiser against a clean database.
The file contains all schemas, tables, views, grants, and scripts — equivalent
to `preproc install` but self-contained and auditable. It does not activate the
preprocessor; the `ALTER SYSTEM` step below still applies.

### 3. Smoke test: PING → PONG

The `DEMO_PING_V1` module is installed by default. After activation, register a
rule that routes the `PING` command through it:

```sql
-- Insert a rule (DBA/PREPROC_ADMIN required)
INSERT INTO PREPROC.CONFIG
    (rule_id, scope_type, scope_value, client_pattern, phase, script_name, function_name, enabled, version)
VALUES
    (1, 'USER', 'SYS', NULL, 'EXPAND', 'PREPROC_RT.DEMO_PING_V1', 'ping', TRUE, 1);

-- Rebuild resolution from config
EXECUTE SCRIPT PREPROC.REFRESH;
```

Then, as the configured user:

```sql
PING
```

Returns:

```
PONG
```

## Demo walkthrough

Three demo modules ship with every install and showcase one phase each:

- **TRANSLATE — `DEMO_TRANSLATE_V1`**: a statement tagged with a dialect marker
  is transpiled to Exasol SQL by the companion `sqlglot` UDF. For example
  `--!dialect:tsql\nSELECT TOP 5 col FROM t` becomes `SELECT col FROM t LIMIT 5`.
  Unmarked statements pass through unchanged; a failed transpile fails closed.
- **EXPAND — `DEMO_PING_V1`**: the custom command `PING` expands to `SELECT
  'PONG'`.
- **REWRITE — `DEMO_AUDIT_V1`**: appends an inert audit comment
  `/* PREPROC_AUDIT user=<CURRENT_USER> */` to the statement; results are
  unchanged.

Seed a self-documenting demo configuration — two roles, two users with
different role sets, and rules across all three phases — by running
`sql/demo/seed.sql` as an admin (PREPROC_ADMIN or DBA) in your SQL client. The
two demo users then resolve to visibly different pipelines.

Remove everything the seed created by running `sql/demo/teardown.sql`. It is
idempotent and re-runs `PREPROC.REFRESH`. Neither script activates the
preprocessor.

## Documentation

- [docs/how-it-works.md](docs/how-it-works.md) — pipeline architecture, phase semantics, dispatch rules, and module authoring
- [docs/operations.md](docs/operations.md) — install, activate, add rules, refresh, status, and troubleshooting

## Development

```bash
# Install dependencies
uv sync

# Run tests (integration tests require a live DB via env vars; they skip otherwise)
uv run pytest

# Lint Python
uv run ruff check .

# Lint Lua
luacheck sql/lua/
```

Coverage report:

```bash
uv run pytest --cov=preproc --cov-report=term-missing
```
