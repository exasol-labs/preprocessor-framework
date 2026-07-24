<div align="center">

# preprocessor-framework

**A config-driven SQL preprocessor framework for Exasol.**

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](./LICENSE)
[![Exasol](https://img.shields.io/badge/Exasol-2025.x%20%7C%202026.1-1ba0e2.svg)](https://www.exasol.com/)
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

**Positioning:** Exasol's single-slot constraint means two independent
preprocessor projects normally cannot coexist — whichever sets
`sql_preprocessor_script` last wins, clobbering the other. This framework's
core value is being the **sole owner of that one slot** and the
**multiplexer** that dispatches to every registered syntax provider through
`MASTER`. Anyone who wants to hook a syntax transform into the slot does so as
a **module** — a stable, versioned, registry-discoverable unit — rather than
fighting for the slot directly. See
[Add a transformation](#add-a-transformation) below.

## Quick Start

You deploy the framework as a **single SQL artifact** — `release.sql` — run
once against a clean Exasol instance by a DBA. No build toolchain required.

> **Prerequisites:** an Exasol instance — verified on 2025.2.x and 2026.1
> (2025.2.1 and 2026.1.0 confirmed live) — DBA-level credentials, and a SQL
> client that handles script bodies — EXAplus or DbVisualizer.

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

To try it on just your own session first — no system-wide change — run the same
against the session instead:

```sql
ALTER SESSION SET sql_preprocessor_script = PREPROC_RT.MASTER;
```

To deactivate, set the slot back to empty (`ALTER SYSTEM` or `ALTER SESSION SET
sql_preprocessor_script = '';`).

### 4. Verify

As a DBA (or any `PREPROC_ADMIN` holder), confirm the control plane responds:

```sql
EXECUTE SCRIPT PREPROC.STATUS;     -- resolution current / stale / never-refreshed
EXECUTE SCRIPT PREPROC.RULE_LIST;  -- all configured rules
```

You now have a live, empty pipeline. Add transformation modules next.

## Add a transformation

A transformation is a Lua **module** (one phase, one job) bound to a **scope** (a
role, a user, optionally a client pattern) by a **rule** in `PREPROC.CONFIG`.
After any rule change you run a **refresh** to rebuild the per-user resolution
(the SQL sugar in [Administration](#administration) does it for you).

### Run the bundled example

[`examples/demo_ping.sql`](examples/demo_ping.sql) is the canonical authoring
reference — one self-contained, self-registering module that creates its script
and ends with the rule that registers it. Grab it from the repo and run it after
`release.sql`: it deploys `PREPROC_RT.DEMO_PING_V1` and registers an EXPAND rule.
Then, for any user holding the rule's role:

```sql
PING
```
```
PONG
```

The rule is scoped to a role — a user must **hold** that role for it to apply;
edit the trailing `RULE_ADD` call to change the scope. More bundled examples
(dialect transpilation, audit tagging, and the real-world Exasol ConfD gateway)
live in [`examples/`](examples/README.md).

### Install a module from the community library

Published modules live in
[`exasol-labs/preprocessor-library`](https://github.com/exasol-labs/preprocessor-library)
— versioned, registry-discoverable syntax providers (the `expr::type` cast
shorthand and optional-trailing-comma ergonomics now live there, not in this
repo's `examples/`). On an internet-connected cluster you install one straight
from SQL — no CLI, no toolchain. Browse, then install:

```sql
PREPROC CATALOG MODULES;                                   -- list the library (newest release)
PREPROC INSTALL MODULE cast_shorthand FOR ROLE ANALYSTS;   -- deploy + register in one step
```

With no source given, the framework resolves the public library at its `latest`
release over HTTPS and records the concrete release it installed. Supply an
explicit source to pin a version, use a fork, or install from BucketFS on an
air-gapped cluster:

```sql
PREPROC INSTALL MODULE cast_shorthand
  FROM 'https://raw.githubusercontent.com/exasol-labs/preprocessor-library/v0.2.0/registry/index.json'
  FOR ROLE ANALYSTS;
```

Prefer a toolchain? The `preproc module` CLI does the same from your machine
(`uv run preproc module add cast_shorthand`) and adds `list` / `search` /
`update` / `remove`, a declarative `sync`, and an offline `bundle` for
air-gapped installs. Full reference:
[docs/operations.md § Module management](docs/operations.md#module-management)
and [§ In-database module install](docs/operations.md#in-database-module-install).

### Write your own module

To hook your own syntax transform into Exasol's single preprocessor slot you
write a **module** — you never fight for the slot directly (see
[Positioning](#what-it-solves) above). The contract is small: a Lua
`fn(text) -> nil | string` function, a `module.toml` manifest, and `_V<N>`
versioning discipline.

- **[docs/module-authoring.md](docs/module-authoring.md)** — the module contract SDK: the function contract, the manifest schema, and versioning.
- **[examples/demo_ping.sql](examples/demo_ping.sql)** — a complete, minimal, self-registering module to copy from.
- **[Publish it to the library](https://github.com/exasol-labs/preprocessor-library/blob/main/CONTRIBUTING.md)** — the library's CONTRIBUTING guide walks through adding your module (or registering an external repo) so others can `PREPROC INSTALL MODULE` it.

## Administration

Everything you configure at runtime is done from SQL. When the preprocessor is
active and you hold `PREPROC_ADMIN`, the `PREPROC` command sugar manages rules,
modules, refresh, status, and mode inline — no `EXECUTE SCRIPT` boilerplate.
Each command maps to exactly one admin script, so the scripted form
(`EXECUTE SCRIPT PREPROC.RULE_ADD(...)`) works identically if you prefer it.

### Rules

A rule binds a module (a phase plus a script and its entry function) to a
**scope**: a `ROLE` or a `USER`, optionally narrowed by a client pattern. A user
receives a rule when they hold that role (or are that user); the three phases
always run in the order TRANSLATE → EXPAND → REWRITE.

```sql
PREPROC ADD RULE FOR ROLE ANALYSTS PHASE EXPAND SCRIPT PREPROC_RT.MY_MODULE_V1 FUNCTION expand;
PREPROC ADD RULE FOR USER BOB PHASE TRANSLATE SCRIPT PREPROC_RT.MY_XLATE_V1 FUNCTION translate;
PREPROC LIST RULES;        -- every configured rule and its scope
PREPROC DISABLE RULE 3;    -- keep the row, stop applying it
PREPROC ENABLE RULE 3;
PREPROC DROP RULE 3;       -- remove it
```

### Modules

Install and browse community-library modules from the same surface (see
[Install a module from the community library](#install-a-module-from-the-community-library)):

```sql
PREPROC CATALOG MODULES;                                   -- browse the library
PREPROC INSTALL MODULE cast_shorthand FOR ROLE ANALYSTS;   -- install + activate
```

Provenance for every installed module — version, the resolved library release,
the source it came from, and sha256 — is queryable via the PUBLIC view
`PREPROC_RT.INSTALLED_MODULES`.

### Refresh, status, and mode

```sql
PREPROC REFRESH;                 -- rebuild resolution from config + the live role-grant graph
PREPROC STATUS;                  -- current / stale / never-refreshed
PREPROC SET MODE convenience;    -- performance posture: convenience (default) or strict
```

Every mutating command **auto-refreshes**; append `NOREFRESH` to batch several
changes and refresh once at the end.

> **Refresh is required** after changing rules *and* after role grants change —
> resolution is built from config and the live grant graph. The sugar refreshes
> for you on each change; to track ongoing role changes, schedule
> `EXECUTE SCRIPT PREPROC.REFRESH` (cron, CI, any SQL scheduler).

The full command surface, the equivalent script calls, the `convenience`/`strict`
mode trade-offs and per-statement overhead, and the `PREPROC.CONFIG` column
reference are in
[docs/operations.md § Control surface](docs/operations.md#control-surface).

## Documentation

- [docs/how-it-works.md](docs/how-it-works.md) — pipeline architecture, phase semantics, dispatch rules, and module authoring
- [docs/operations.md](docs/operations.md) — install, activate, add rules, refresh, status, modes, module management, and troubleshooting
- [docs/module-authoring.md](docs/module-authoring.md) — the module contract SDK: the `fn(text)->nil|string` function contract, the `module.toml` manifest schema, and versioning discipline
- [examples/](examples/) — ready-to-run preprocessor modules, one per file, each self-registering
- [exasol-labs/preprocessor-library](https://github.com/exasol-labs/preprocessor-library) — the community module library; browse or install with `preproc module add`

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
