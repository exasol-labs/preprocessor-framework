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

This framework's
core value is being the **sole owner of that one slot** and the
**multiplexer** that dispatches to every registered syntax provider through
`MASTER`. Anyone who wants to hook a syntax transform into the slot does so as
a **module** — a stable, versioned, registry-discoverable unit — rather than
fighting for the slot directly. See
[Bring your own preprocessor](#bring-your-own-preprocessor) below.

## Quick Start

You deploy the framework as a **single SQL artifact** — `release.sql` — run
once against a clean Exasol instance by a DBA. No build toolchain required.

### 1. Get the artifact

Download `release.sql` from the latest GitHub Release:

```
https://github.com/exasol-labs/preprocessor-framework/releases/latest/download/release.sql
```

### 2. Deploy it

Run `release.sql` in a SQL Client of your choice. This deploys the complete control plane: the `PREPROC_ADMIN` role, the `PREPROC`
(admin) and `PREPROC_RT` (runtime) schemas, all tables and views, the `MASTER`
dispatcher, the `REFRESH`/`STATUS` engine, the full rule/mode admin surface, and
the `PREPROC …` command sugar — then runs an initial `REFRESH`. It deploys **no**
transformation modules and does **not** activate the preprocessor.

> `release.sql` is clean-install only. All tables will be re-created, so be careful if you only need to update the framework to update only the needed objects. The script should be executed with a DBA role.

> ⚠️ **Turn off named-parameter substitution in your SQL client.** Many GUI SQL
> clients (DbVisualiser, DBeaver, and other JDBC tools) treat `:name` as a
> bind-variable placeholder. `release.sql` contains Lua and Python script bodies
> full of literal colons (`'bucketfs:'`, `if …:`, `def …:`), so a client with
> parameterization **on** will substitute on those colons and silently corrupt
> the deployed scripts — you'll later see errors like `object <NAME> not found`
> or `SyntaxError: expected ':'`. Before running the file, disable parameterized
> SQL (DbVisualiser: *SQL Commander → Parameterized SQL → off*; DBeaver:
> *Preferences → Editors → SQL Editor → uncheck "Use named parameters"*). EXAplus
> does not do this substitution and needs no change.

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

Once the preprocessor is active you can use the shorter `PREPROC STATUS;` /
`PREPROC LIST RULES;` sugar instead. `SYS` works out of the box here: Exasol will
not let you grant `PREPROC_ADMIN` to `SYS`, so the installer also seeds the
command surface for `DBA`, which `SYS` holds.

**The one query worth remembering** — "does the preprocessor affect *me*, right
now?" Any user can ask, no admin rights needed:

```sql
SELECT * FROM PREPROC_RT.MY_PIPELINE;
```

Each row is one transformation that will run on your statements, in order. **No
rows means nothing applies to you** — which is the expected answer for most users,
and the first thing to check when a module "isn't working" (usually: you don't
hold the role its rule is scoped to).

You now have a live, empty pipeline. Add transformation modules next.

### Or just run the demo

If you would rather see the whole thing working than read about it,
**[`demo/`](demo/)** is a guided, runnable suite — every statement verified end
to end against Exasol 2026.1. It runs entirely at **session scope**, so it never
touches the system-wide setting.

- **~5 min** — [`demo/video_5min.sql`](demo/video_5min.sql): a 29-statement,
  six-beat cut. One session, no tab switching.
- **~20 min** — the technical sell: [`01`](demo/01_administration.sql) (activate
  + a live fail-open proof), [`02`](demo/02_module_from_github.sql) (install a
  published module), [`05`](demo/05_own_preprocessor.sql) (write your own in ~20
  lines).
- **~45 min** — the full evaluation, all seven files, for a DBA or security
  reviewer who will ask hard questions.

See [`demo/README.md`](demo/README.md) for the file-by-file map, the throwaway
Docker recipe, and the prerequisites for the BucketFS and ConfD files.

## Bring your own preprocessor

**This is what the framework is for.** Exasol gives you one
`sql_preprocessor_script` slot; the framework owns it and multiplexes it, so
*your* SQL transformation becomes a **module** you register against that slot
instead of a monolith you have to own and defend. Everything below is about
getting your own code into that pipeline.

A transformation is a Lua **module** (one phase, one job) bound to a **scope** (a
role, a user, optionally a client pattern) by a **rule** in `PREPROC.CONFIG`.
After any rule change you run a **refresh** to rebuild the per-user resolution
(the SQL sugar in [Administration](#administration) does it for you).

### The whole contract, in one file

A module is a Lua function that takes the statement text and returns either a
replacement string or `nil`. That is the entire interface:

```lua
function my_guard(sqltext)
    if string.find(string.upper(sqltext), "^%s*DROP%s+TABLE") then
        return "SELECT 'blocked by my_guard' AS refused"
    end
    return nil          -- not my statement: keep scanning
end
```

Deploy it and register it — two statements, no toolchain, no manifest:

```sql
--/
CREATE OR REPLACE LUA SCRIPT PREPROC_RT.MY_GUARD_V1 AS
function my_guard(sqltext)
    ...
end
/

PREPROC ADD RULE FOR ROLE DBA PHASE EXPAND
    SCRIPT PREPROC_RT.MY_GUARD_V1 FUNCTION my_guard;
```

Now `DROP TABLE …` returns `blocked by my_guard` for every user holding `DBA`.
That is the complete loop: **write a function → deploy it → add a rule.**

[`examples/demo_ping.sql`](examples/demo_ping.sql) is the same thing as a
copyable file — one self-contained, self-registering module that creates its
script and ends with the rule that registers it. Run it after `release.sql` and
`PING;` returns `PONG`. [`demo/05_own_preprocessor.sql`](demo/05_own_preprocessor.sql)
walks the whole loop in ~20 lines with commentary.

Note the rule is scoped to a **role**, and a user must *hold* that role for it to
apply — scope it to `USER <name>` while you are still testing.

### Then package it, when you're ready

The two statements above are enough forever, for a module you deploy by hand. You
add a **manifest** (`module.toml`) when you want the module to be *distributable*
— installable by name, integrity-checked, version-tracked, and reclaimable as a
unit:

| You want | You need |
|---|---|
| A transform running on your own cluster | the two statements above — nothing else |
| The same transform installable by name, on many clusters | `+ module.toml`, served from a registry index |
| More than one object (a UDF, a table, a schema) as one unit | `+ an [[objects]] inventory` |
| Others to install it from the public catalog | `+ a PR to preprocessor-library` |

Read the contract in
**[docs/module-authoring.md](docs/module-authoring.md)** — the `fn(text)->nil|string`
signature per phase, the fail-open rules, the "no defensive `pcall`" rule, the
`module.toml` schema, and the `_V<N>` versioning discipline. Two traps worth
knowing before you write a line: functions must be **plain top-level globals**
(a `local` function is invisible to `import`), and `string.gsub` returns **two**
values — never `return` it directly.

### Host your own module registry

A packaged module does not have to go anywhere near the public library. A
**registry is just a `registry/index.json` plus the artifacts it points at**, so
your own internal catalog — private modules, your own versioning, your own
review process — is a first-class path, not a workaround:

```sql
-- your own index, served over HTTPS
PREPROC CATALOG MODULES FROM 'https://git.internal/preproc-modules/raw/v2/registry/index.json';
PREPROC INSTALL MODULE my_guard
  FROM 'https://git.internal/preproc-modules/raw/v2/registry/index.json' FOR ROLE DBA;

-- or staged in BucketFS, for a cluster with no egress
PREPROC INSTALL MODULE my_guard FROM 'bucketfs:MY_BFS/own-lib-2.0.0.tar.gz' FOR ROLE DBA;
```

Your index declares its own `library_version`, and each install records it
alongside the effective source URL in `PREPROC_RT.INSTALLED_MODULES` — so your
internal releases are as auditable as the public library's. The public library is
simply the default source when you omit `FROM`; nothing about the mechanism is
specific to it.

For the index format and the tarball layout, see
[docs/operations.md § Module management](docs/operations.md#module-management)
and the library's
[CONTRIBUTING.md](https://github.com/exasol-labs/preprocessor-library/blob/main/CONTRIBUTING.md),
which documents the same manifest your own registry uses.

### Or start from a ready-made module

If someone has already solved your problem, take theirs.
[`exasol-labs/preprocessor-library`](https://github.com/exasol-labs/preprocessor-library)
is an open-source catalog of ready-to-use preprocessors — and a useful reference
for how a packaged module is put together. On an internet-connected cluster you
install one straight from SQL — no CLI, no toolchain. Browse, then install:

```sql
PREPROC CATALOG MODULES;                                   -- list the library (newest release)
PREPROC INSTALL MODULE cast_shorthand FOR ROLE ANALYSTS;   -- deploy + register in one step
```

With no source given, the framework resolves the public library at its `latest`
release over HTTPS and records the concrete release it installed. Supply an
explicit source to pin a version, point at
[your own registry](#host-your-own-module-registry), use a fork, or install from
BucketFS on an air-gapped cluster:

```sql
PREPROC INSTALL MODULE cast_shorthand
  FROM 'https://raw.githubusercontent.com/exasol-labs/preprocessor-library/v0.3.0/registry/index.json'
  FOR ROLE ANALYSTS;
```

An install is DDL run under `PREPROC_ADMIN`, and a module may ship more than one
object — the library's `confd_control` installs 15, turning cluster
administration into `CONTROL DATABASE …` / `CONTROL BUCKETFS …` SQL. So
`PREPROC CATALOG MODULES` shows each module's full declared object inventory
before you install, and an install is refused if the artifact disagrees with what
its manifest declared. Afterwards, `PREPROC_RT.INSTALLED_MODULES` records the
version, the resolved library release, the effective source URL, the verified
sha256, and the deployed inventory.

> **`min_framework` is advisory — nothing enforces it.** Verified on 2026.1: a
> module declaring `min_framework = "99.0.0"` installs cleanly on framework
> 0.7.1 and reports `installed`. It fails later, at use, not at install. Check a
> module's `min_framework` against your own framework version yourself —
> `confd_control` needs **0.7.0+**.

Prefer a toolchain? The `preproc module` CLI does the same from your machine
(`uv run preproc module add cast_shorthand`) and adds `list` / `search` /
`update` / `remove`, a declarative `sync`, and an offline `bundle` for
air-gapped installs. Full reference:
[docs/operations.md § Module management](docs/operations.md#module-management)
and [§ In-database module install](docs/operations.md#in-database-module-install).

### Sharing what you built

If your module is generally useful, publishing it means others can
`PREPROC INSTALL MODULE` it by name. The library's
[CONTRIBUTING.md](https://github.com/exasol-labs/preprocessor-library/blob/main/CONTRIBUTING.md)
walks through both routes: a library-deployed PR (your artifact lives there), or a
curated `registry/external/` entry (your artifact stays in your own repo and the
library carries only a pointer). Keeping it internal is equally valid — see
[Host your own module registry](#host-your-own-module-registry).

## Administration

Everything you configure at runtime is done from SQL. When the preprocessor is
active and you hold `PREPROC_ADMIN` **or `DBA`**, the `PREPROC` command sugar manages rules,
modules, refresh, status, and mode inline — no `EXECUTE SCRIPT` boilerplate.
Each command maps to exactly one admin script, so the scripted form
(`EXECUTE SCRIPT PREPROC.RULE_ADD(...)`) works identically if you prefer it.

### Rules

A rule binds a module (a phase plus a script and its entry function) to a
**scope**: a `ROLE` or a `USER`, optionally narrowed by a `CLIENT '<pattern>'`
clause matching the connecting application. A user receives a rule when they
hold that role (or are that user); the three phases always run in the order
TRANSLATE → EXPAND → REWRITE.

```sql
PREPROC ADD RULE FOR ROLE ANALYSTS PHASE EXPAND SCRIPT PREPROC_RT.MY_MODULE_V1 FUNCTION expand;
PREPROC ADD RULE FOR USER BOB PHASE TRANSLATE SCRIPT PREPROC_RT.MY_XLATE_V1 FUNCTION translate;
PREPROC ADD RULE FOR ROLE ANALYSTS PHASE EXPAND SCRIPT PREPROC_RT.MY_MODULE_V1 FUNCTION expand
    CLIENT 'DbVisualizer';  -- narrow to a client application too (scoping, not security)
PREPROC LIST RULES;        -- every configured rule and its scope
PREPROC DISABLE RULE 3;    -- keep the row, stop applying it
PREPROC ENABLE RULE 3;
PREPROC DROP RULE 3;       -- remove it
```

### Modules

Install and browse community-library modules from the same surface (see
[Or start from a ready-made module](#or-start-from-a-ready-made-module)):

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

- [demo/](demo/) — **start here to see it work**: a guided, runnable demo suite verified against Exasol 2026.1, with 5-minute / 20-minute / 45-minute tracks
- [docs/how-it-works.md](docs/how-it-works.md) — pipeline architecture, phase semantics, dispatch rules, and module authoring
- [docs/operations.md](docs/operations.md) — install, activate, add rules, refresh, status, modes, module management, and troubleshooting
- [docs/air-gap-runbook.md](docs/air-gap-runbook.md) — **no outbound internet?** the whole deploy-and-install procedure in one ordered page, verified on 2026.1
- [docs/module-authoring.md](docs/module-authoring.md) — the module contract SDK: the `fn(text)->nil|string` function contract, the `module.toml` manifest schema, and versioning discipline
- [examples/](examples/) — single-file example preprocessors to copy from
- [preprocessor-library](https://github.com/exasol-labs/preprocessor-library) — the community module library; install with `PREPROC INSTALL MODULE` or `preproc module add`

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
