<div align="center">

# preprocessor-framework

**A config-driven SQL preprocessor framework for Exasol.**

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](./LICENSE)
[![Exasol](https://img.shields.io/badge/Exasol-2025.x-1ba0e2.svg)](https://www.exasol.com/)
[![spec|driven](https://img.shields.io/badge/spec-driven-blue)](./specs/)

</div>

## What it solves

Exasol allows exactly **one** preprocessor script per system — a single global
slot via `ALTER SYSTEM SET sql_preprocessor_script`. That makes it hard to run
different SQL transformations for different roles, users, or client
applications: every change means rewriting one monolithic script.

`preprocessor-framework` turns that single slot into a modular, role- and
client-aware pipeline. A `MASTER` dispatcher occupies the slot, looks up the
current user's resolved rule set from a config table, and runs their
transformation modules in a fixed three-phase order before the statement
reaches the engine:

```
Statement ──▶ MASTER dispatcher ──▶ TRANSLATE ──▶ EXPAND ──▶ REWRITE ──▶ Engine
                     │
                     └─ resolves rules per user / role / client from PREPROC.CONFIG
```

You add and remove transformations by editing rows in a config table and
running a refresh — never by rewriting the global script. The framework keeps
the resolution fresh as database roles change, and holds a **fail-open** safety
contract: a broken module is skipped, never blocking a user's statement.

## Quick Start

You deploy the framework as a **single SQL artifact** — `release.sql` — run
once against a clean Exasol instance by a DBA. No build toolchain required.

> **Prerequisites:** an Exasol 2025.x instance (2025.2.1 verified), DBA-level
> credentials, and a SQL client that handles script bodies — EXAplus or
> DbVisualizer.

### 1. Get the artifact

Download `release.sql` from the latest GitHub Release:

```
https://github.com/exasol-labs/preprocessor-framework/releases/latest/download/release.sql
```

`release.sql` is **generated from the `sql/` sources** and attached to every
tagged release, so it can never drift from the framework code.

### 2. Deploy it

Run `release.sql` in EXAplus or DbVisualizer against a **clean** instance. The
`/` on its own line is the statement delimiter — required because the Lua and
Python bodies contain semicolons.

This deploys the complete control plane: the `PREPROC_ADMIN` role, the `PREPROC`
(admin) and `PREPROC_RT` (runtime) schemas, all tables and views, the `MASTER`
dispatcher, the `REFRESH`/`STATUS` engine, the full rule/mode admin surface, and
the `PREPROC …` command sugar — then runs an initial `REFRESH`. It deploys **no**
transformation modules and does **not** activate the preprocessor.

> `release.sql` is clean-install only. To re-run idempotently or upgrade an
> existing install's columns, use the CLI installer instead (see
> [Development](#development)).

### 3. Activate the dispatcher

The deploy never touches the live preprocessor slot — that stays a deliberate,
separate DBA action. Activate it system-wide:

```sql
ALTER SYSTEM SET sql_preprocessor_script = PREPROC_RT.MASTER;
```

To test on a single session first, use `ALTER SESSION` instead of `ALTER SYSTEM`.
To deactivate, set the script to `NULL`.

### 4. Verify

As a DBA (or any `PREPROC_ADMIN` holder), confirm the control plane responds:

```sql
EXECUTE SCRIPT PREPROC.STATUS;     -- resolution current / stale / never-refreshed
EXECUTE SCRIPT PREPROC.RULE_LIST;  -- all configured rules
```

You now have a live, empty pipeline. Add transformation modules next.

## Adding transformations

A transformation is a Lua **module** (one phase, one job) plus a **rule** in
`PREPROC.CONFIG` that binds it to a scope (a role, a user, optionally a client
pattern) and a phase. After any config change you run a **refresh** to rebuild
the per-user resolution.

The fastest way to see this end to end is the bundled `PING` example.

### Deploy an example module

Each file in [`examples/`](examples/) is one self-contained, self-registering
module — it creates the script(s) and ends with the rule that registers it.
Grab the file from the repo and run it after `release.sql`:

| File | Phase | What it adds |
|------|-------|--------------|
| [`examples/demo_ping.sql`](examples/demo_ping.sql) | EXPAND | `PING` → `SELECT 'PONG'` (the canonical authoring reference) |
| [`examples/demo_translate.sql`](examples/demo_translate.sql) | TRANSLATE | dialect transpilation via a companion `sqlglot` UDF |
| [`examples/demo_audit.sql`](examples/demo_audit.sql) | REWRITE | appends an inert `/* PREPROC_AUDIT user=… */` comment |
| [`examples/confd_admin.sql`](examples/confd_admin.sql) | EXPAND | real-world add-on: `CONTROL DATABASE …` / `CONTROL BUCKETFS …` via the Exasol ConfD gateway |

Running `examples/demo_ping.sql` deploys `PREPROC_RT.DEMO_PING_V1` and registers
an EXPAND rule for it. Then, as a user the rule resolves to:

```sql
PING
```
```
PONG
```

Each example's rule is scoped to a role — a user must **hold** that role for the
rule to apply. Edit the trailing `RULE_ADD` call to change the scope. See
[`examples/README.md`](examples/README.md) for details.

### Manage rules from SQL

When the preprocessor is active and you hold `PREPROC_ADMIN`, the `PREPROC`
command sugar lets you manage the framework inline — no `EXECUTE SCRIPT`
boilerplate:

```sql
PREPROC ADD RULE FOR ROLE ANALYSTS PHASE EXPAND SCRIPT PREPROC_RT.MY_MODULE_V1 FUNCTION expand
PREPROC LIST RULES
PREPROC DISABLE RULE 3
PREPROC REFRESH
PREPROC STATUS
```

Every mutating command auto-refreshes; append `NOREFRESH` to batch changes and
refresh once at the end. The full command surface, the equivalent script calls,
and the `PREPROC.CONFIG` column reference are in
[docs/operations.md](docs/operations.md#control-surface).

> **Refresh is required** after adding/changing rules and after role grants
> change — it rebuilds resolution from config and the live role-grant graph. The
> sugar does it for you; schedule `EXECUTE SCRIPT PREPROC.REFRESH` (cron, CI, any
> SQL scheduler) to track ongoing role changes.

## Modes & overhead

The framework's posture is set with `PREPROC SET MODE` and stored in
`PREPROC.SETTINGS`:

- **convenience** (default) — sessions self-optimize after their first
  statement: a session with no rules clears its preprocessor, and a rule-bearing
  session repoints to a generated, hash-named profile script with its rule chain
  baked in. The per-statement `MASTER` lookup is paid **once**, not per
  statement. Profile scripts are created, retired, and reclaimed automatically.
- **strict** — no swap or self-disable; every statement pays the full `MASTER`
  lookup (~7.3 ms/stmt). Choose this when sessions must never repoint themselves.

> The `DEMO_TRANSLATE_V1` module calls a Python3 UDF per translated statement
> (~133–151 ms; no VM reuse across statements). Scope TRANSLATE rules narrowly
> with `client_pattern` — never apply them to all traffic.

See [session self-optimization](docs/how-it-works.md#session-self-optimization-zero-overhead-paths)
for the full lifecycle.

## Documentation

- [docs/how-it-works.md](docs/how-it-works.md) — pipeline architecture, phase semantics, dispatch rules, and module authoring
- [docs/operations.md](docs/operations.md) — install, activate, add rules, refresh, status, modes, and troubleshooting
- [examples/](examples/) — ready-to-run preprocessor modules, one per file, each self-registering

## Development

The CLI installer and the `release.sql` generator are for contributors and for
idempotent / upgrade installs. They need [uv](https://docs.astral.sh/uv/) and
DBA credentials supplied via environment variables:

```bash
export EXASOL_DSN=myhost:8563
export EXASOL_USER=sys
export EXASOL_PASSWORD=exasol
# Optional — set to 0/false to skip TLS certificate verification:
# export EXASOL_TLS_VERIFY=false
```

```bash
uv sync

# Idempotent install / column upgrade (creates schemas, tables, dispatcher,
# admin surface, and the demo modules). Prints the ALTER SYSTEM line; does not run it.
uv run preproc install

# Regenerate the single-file artifact locally:
uv run preproc bundle --release        # writes ./release.sql
```

Quality gates:

```bash
uv run pytest                                   # unit + (live-DB) integration tests
uv run pytest --cov=preproc --cov-report=term-missing
uv run ruff check .                             # lint Python
luacheck sql/lua/                               # lint Lua
```

Integration tests need a live DB via the env vars above; they skip otherwise. CI
builds `release.sql` on every push (the `release-sql` artifact) and publishes a
GitHub Release with it attached on every `v*` tag.
