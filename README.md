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

In convenience mode it also self-optimizes per session: after the first
statement, a session with no rules clears its preprocessor entirely and a
rule-bearing session repoints to a generated, hash-named profile script with its
rule chain baked in — so the per-statement MASTER lookup is paid once, not on
every statement. See [docs/how-it-works.md](docs/how-it-works.md#session-self-optimization-zero-overhead-paths).

## Prerequisites

- Exasol 2025.x instance (2025.2.1 verified)
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

If you prefer a single SQL file over the CLI, use `release.sql` — one file that
deploys the complete framework: the `PREPROC_ADMIN` role, both schemas, all
tables, the `MY_PIPELINE` view, grants, the `MASTER` dispatcher, the
`REFRESH`/`STATUS` engine, the full rule/mode admin control surface, and the
`PREPROC ...` command sugar, followed by a final `REFRESH`. It deploys **no**
demo preprocessors and does not activate the preprocessor (the `ALTER SYSTEM`
step below still applies).

`release.sql` is **generated from the `sql/` sources**, not committed to the
repo, so it can never drift from the framework code. Get it either way:

```bash
# Generate locally:
uv run preproc bundle --release          # writes ./release.sql
```

Or download the **`release-sql`** artifact built by CI on every push (Actions →
a workflow run → Artifacts). Then run it in EXAplus or DbVisualiser against a
**clean** database (the `/` on its own line is the statement delimiter).

`release.sql` is clean-install only (its `CREATE ROLE`/`CREATE SCHEMA` are not
guarded, and it omits the upgrade-only column ALTERs). For idempotent re-runs
and column upgrades on an existing install, use `uv run preproc install`.

### 2b. Optional add-ons: examples/

The [`examples/`](examples/) folder holds ready-to-run preprocessor modules,
**one per file**, each ending with the command that registers it with the
framework. Deploy only the ones you want, after `release.sql`:

| File | Phase | What it adds |
|------|-------|--------------|
| [`examples/demo_ping.sql`](examples/demo_ping.sql) | EXPAND | `PING` → `SELECT 'PONG'` (the canonical authoring reference) |
| [`examples/demo_translate.sql`](examples/demo_translate.sql) | TRANSLATE | dialect transpilation via a companion `sqlglot` UDF |
| [`examples/demo_audit.sql`](examples/demo_audit.sql) | REWRITE | appends an inert `/* PREPROC_AUDIT user=… */` comment |
| [`examples/confd_admin.sql`](examples/confd_admin.sql) | EXPAND | real-world add-on: `CONTROL DATABASE …` / `CONTROL BUCKETFS …` via the Exasol ConfD gateway |

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
- [examples/](examples/) — ready-to-run preprocessor modules, one per file, each self-registering

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
