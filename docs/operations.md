# Operations

[← README](../README.md) | [How it works →](how-it-works.md)

There are two independent deploy surfaces, covered in different sections
below: the **engine deploy** — run [`release.sql`](#single-file-deploy-releasesql)
in a SQL client, no toolchain, DBA only — and **module install** — the
`preproc module` CLI ([§ Module management](#module-management)), which needs
`uv` and a live DB connection. A deployer who only ran `release.sql` does not
need `uv` at all unless they also want to install library modules or use the
CLI installer described immediately below.

## Installing

### Prerequisites

- An Exasol instance — verified on 2025.2.x and 2026.1 (DBA-level credentials
  required)
- uv installed locally
- Repository checked out

### Environment variables

| Variable | Required | Description |
|---|---|---|
| `EXASOL_DSN` | Yes | `host:port` of the Exasol cluster |
| `EXASOL_USER` | Yes | Database user (must have DBA or schema-creation privileges) |
| `EXASOL_PASSWORD` | Yes | Database password |
| `EXASOL_TLS_VERIFY` | No | Set to `0` or `false` to disable TLS certificate verification |

```bash
export EXASOL_DSN=myhost:8563
export EXASOL_USER=sys
export EXASOL_PASSWORD=exasol
```

### Running the installer

```bash
uv sync
uv run preproc install
```

Optional `--verbose` flag prints each SQL statement before execution:

```bash
uv run preproc install --verbose
```

**What the installer creates:**

| Object | Type |
|---|---|
| `PREPROC_ADMIN` | Role |
| `PREPROC` | Schema (owned by `PREPROC_ADMIN`) |
| `PREPROC.CONFIG` | Table |
| `PREPROC.RESOLUTION` | Table |
| `PREPROC.SETTINGS` | Table (seeded with one default row) |
| `PREPROC.PROFILES` | Table |
| `PREPROC.REFRESH_CORE` | Lua script (importable refresh module; no RETURNS TABLE) |
| `PREPROC.REFRESH` | Lua script (owned by `PREPROC_ADMIN`) |
| `PREPROC.STATUS` | Lua script (owned by `PREPROC_ADMIN`) |
| `PREPROC.RULE_ADD` | Lua script (add a CONFIG rule) |
| `PREPROC.RULE_DROP` | Lua script (delete a CONFIG rule) |
| `PREPROC.RULE_ENABLE` | Lua script (enable a CONFIG rule) |
| `PREPROC.RULE_DISABLE` | Lua script (disable a CONFIG rule) |
| `PREPROC.RULE_LIST` | Lua script (list all CONFIG rules) |
| `PREPROC.SET_MODE` | Lua script (set framework mode) |
| `PREPROC.SET_SELF_DISABLE` | Lua script (set self-disable policy) |
| `PREPROC.SET_SYS_GROUPING` | Lua script (set SYS role-grouping policy) |
| `PREPROC_RT` | Schema (PUBLIC-accessible) |
| `PREPROC_RT.MY_PIPELINE` | View (definer-rights; scoped to `CURRENT_USER`; exposes `mode`, `self_disable_allowed`, and `profile_hash`) |
| `PREPROC_RT.MASTER` | Lua script (dispatcher; also the source body for retirement stubs) |
| `PREPROC_RT.DEMO_PING_V1` | Lua script (demo EXPAND module) |
| `PREPROC_RT.DEMO_TRANSLATE_V1` | Lua script (demo TRANSLATE module) |
| `PREPROC_RT.DEMO_AUDIT_V1` | Lua script (demo REWRITE module) |
| `PREPROC_RT.DEMO_SQLGLOT_TRANSPILE_V1` | Python3 UDF (sqlglot dialect transpiler) |
| `PREPROC_RT.CONTROL_SUGAR_V1` | Lua script (EXPAND module for PREPROC sugar syntax) |

Install is idempotent: running it again against an already-installed database
completes with exit code 0 and changes nothing.

In addition to these fixed objects, `PREPROC.REFRESH` generates one
`PREPROC_RT.P_<hash>` **profile script** per distinct resolved rule chain (the
swap targets for convenience-mode sessions). These are created, retired, and
reclaimed automatically by the refresh path — operators do not manage them
directly. See [Session self-optimization](#session-self-optimization).

**Exit codes:**

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | Connection or SQL error |
| 2 | Missing environment variable (no connection attempted) |

## Single-file deploy (release.sql)

`release.sql` is the offline alternative to `preproc install`: one auditable SQL
file that deploys the complete framework. It is **generated from the `sql/`
sources**, never committed to the repository, so it can never drift from the
framework code. Obtain it either way:

```bash
uv run preproc bundle --release          # writes ./release.sql
```

or download a CI-built copy. CI produces it two ways:

- **Every push** — the `release` job uploads `release.sql` as the **`release-sql`**
  workflow artifact (Actions → workflow run → Artifacts); retained per the repo's
  artifact-retention policy.
- **On a version tag** (`v*`, e.g. `v1.2.0`) — the `github-release` job publishes
  a **GitHub Release** with `release.sql` attached as a permanent, versioned
  download (gated on the lint + unit checks). Cut one with:
  ```bash
  git tag v1.2.0 && git push origin v1.2.0
  ```

Run the file in EXAplus against a **clean** Exasol instance. Plain
SQL statements end with `;`; each Lua/Python script body is wrapped between a `--/`
line and a lone `/` line (the EXAplus block delimiter) because the bodies contain
semicolons.

> **Warning — GUI SQL clients corrupt the script bodies.** DbVisualiser, DBeaver,
> and similar JDBC tools treat `:name` as a bind-variable placeholder and, with
> parameterized SQL enabled, substitute on the colons *inside* the Lua/Python
> script bodies — silently corrupting the deployed scripts (symptoms:
> `object <NAME> not found`, `SyntaxError: expected ':'`). Before running
> `release.sql` in such a client, disable parameterized SQL (DbVisualiser:
> *SQL Commander → Parameterized SQL → off*). EXAplus does not do this
> substitution and needs no change.

It deploys the `PREPROC_ADMIN` role, both schemas, all tables, the `MY_PIPELINE`
view, schema grants, the `MASTER` dispatcher, the `REFRESH`/`STATUS` engine, the
full rule/mode admin control surface, and the `PREPROC ...` command sugar, then
runs a final `REFRESH`. It deploys **no** demo preprocessors and does **not**
activate the preprocessor (the `ALTER SYSTEM` step remains a separate DBA action).

`release.sql` is clean-install only: its `CREATE ROLE` / `CREATE SCHEMA` are not
guarded, and it omits the additive column upgrades. For an idempotent re-run, or
to upgrade an existing install's columns, use `uv run preproc install`.

The full `preproc bundle` (no `--release`) instead writes `deploy_all.sql`,
which also bundles the demo modules and the demo UDF — handy for a one-shot demo
environment, but not the lean framework release.

### Example preprocessors (examples/)

The `examples/` folder holds opt-in preprocessor modules, one per file, each
ending with the `RULE_ADD` command that registers it. Deploy the ones you want
after `release.sql`:

| File | Phase | Adds |
|------|-------|------|
| `examples/demo_ping.sql` | EXPAND | `PING` → `SELECT 'PONG'` |
| `examples/demo_translate.sql` | TRANSLATE | dialect transpilation (`sqlglot` UDF + module) |
| `examples/demo_audit.sql` | REWRITE | inert `/* PREPROC_AUDIT user=… */` annotation |
| `examples/confd_admin.sql` | EXPAND | `CONTROL DATABASE …` / `CONTROL BUCKETFS …` via the Exasol ConfD gateway (needs a `CONFD_CONNECTION`; see the file header) |

Each example's registration rule is scoped to `ROLE PREPROC_ADMIN` (the confd
example to `ROLE DBA`); edit the scope in the file's trailing `RULE_ADD` to suit,
and note a user must HOLD that role for the rule to resolve to them.

**TRANSLATE module cost note**: the `DEMO_TRANSLATE_V1` module calls a Python3
UDF per translated statement. In practice this costs ~133–151 ms per statement
(UDF VM startup, no reuse across statements). This is acceptable for a demo and
for narrowly scoped production rules, but should not be applied to all
statements. Scope TRANSLATE rules narrowly using `client_pattern` for
production use.

## Demo seed and teardown

`sql/demo/seed.sql` stands up a self-documenting demo configuration: two demo
roles (`DEMO_ANALYST`, `DEMO_ETL`), two demo users with different role sets,
and CONFIG rules spanning all three phases. After seeding, the two demo users
resolve to visibly different pipelines. Run as admin (PREPROC_ADMIN or DBA):

```sql
-- Run sql/demo/seed.sql in your SQL client
```

`sql/demo/teardown.sql` removes exactly what the seed created and re-runs
`PREPROC.REFRESH`. It is idempotent — safe to re-run after a partial seed.

Neither script activates the preprocessor.

## Activating the preprocessor

The installer prints but does not execute the activation statement. A DBA must
run it manually:

```sql
ALTER SYSTEM SET sql_preprocessor_script = PREPROC_RT.MASTER;
```

This is a system-wide change. Every statement on the instance will pass through
MASTER from this point on. If you want to limit activation to a single session
for testing:

```sql
ALTER SESSION SET sql_preprocessor_script = PREPROC_RT.MASTER;
```

To deactivate system-wide:

```sql
ALTER SYSTEM SET sql_preprocessor_script = NULL;
```

To deactivate for a session:

```sql
ALTER SESSION SET sql_preprocessor_script = NULL;
```

Note: the framework's own posture is set by `PREPROC.SET_MODE` and stored in
`PREPROC.SETTINGS.mode` — `'convenience'` (the default) or `'strict'`:

- **convenience** — sessions self-optimize after the first statement (no-rule
  sessions self-disable when `self_disable_allowed = TRUE`; rule-bearing sessions
  repoint to their baked `PREPROC_RT.P_<hash>` profile). See
  [Session self-optimization](#session-self-optimization).
- **strict** — no swap or self-disable; every statement pays the full MASTER
  lookup (~7.3 ms/stmt). Choose this when sessions must never repoint themselves.

This is independent of Exasol's engine-level
`-alterSystemProhibitsAlterSessionForPreprocessing=1` flag, which separately
blocks *users* from overriding the system preprocessor via `ALTER SESSION`. (The
framework's own convenience-mode swap is issued from inside the preprocessor and
is unaffected by that flag.)

## Control surface

`PREPROC_ADMIN` role holders interact with the framework through a set of
admin scripts deployed in the `PREPROC` schema. These are the sanctioned
management surface. **Direct DML on `PREPROC.CONFIG` or `PREPROC.RESOLUTION`
is unsupported** — use the scripts below. The installer seeds one bootstrap
rule (the `PREPROC_RT.CONTROL_SUGAR_V1` EXPAND rule for `PREPROC_ADMIN`) via
`sql/install/70_control_sugar_seed.sql`; that insert is the only authorised
direct-DML bootstrap boundary.

### Admin scripts (PREPROC schema)

| Script | Parameters | Description |
|---|---|---|
| `PREPROC.RULE_ADD` | `scope_type, scope_value, phase, script_name, function_name, norefresh` | Insert a new rule; auto-refresh unless `norefresh` is set |
| `PREPROC.RULE_DROP` | `rule_id, norefresh` | Delete a rule by ID; auto-refresh unless `norefresh` |
| `PREPROC.RULE_ENABLE` | `rule_id, norefresh` | Enable a disabled rule; auto-refresh unless `norefresh` |
| `PREPROC.RULE_DISABLE` | `rule_id, norefresh` | Disable a rule without deleting it; auto-refresh unless `norefresh` |
| `PREPROC.RULE_LIST` | — | Return all rules from CONFIG ordered by rule_id |
| `PREPROC.SET_MODE` | `mode, norefresh` | Set the framework mode (`'convenience'` or `'strict'`); auto-refresh unless `norefresh` |
| `PREPROC.SET_SELF_DISABLE` | `value` | Allow or disallow user self-disable (`TRUE` / `FALSE`) |
| `PREPROC.SET_SYS_GROUPING` | `value, norefresh` | Group `SYS` with `DBA` (`TRUE`, default) or isolate it from the entire role closure (`FALSE`); auto-refresh unless `norefresh` |
| `PREPROC.REFRESH` | — | Rebuild RESOLUTION from CONFIG and current role grants |
| `PREPROC.STATUS` | — | Report whether RESOLUTION is current or stale |

All mutating scripts return a result table confirming the change. All require
`PREPROC_ADMIN` (or `EXECUTE ANY SCRIPT`, or DBA).

The `norefresh` parameter accepts any non-NULL truthy value to suppress the
automatic `PREPROC.REFRESH` call. When omitted or NULL, refresh runs
automatically after the mutation. This is the `NOREFRESH` suffix of the sugar
commands.

### PREPROC sugar syntax (convenience mode)

When the preprocessor is active and the session user holds `PREPROC_ADMIN`,
the `PREPROC_RT.CONTROL_SUGAR_V1` EXPAND module intercepts statements that
begin with the keyword `PREPROC` and rewrites them as `EXECUTE SCRIPT` calls.
This gives an interactive shorthand without leaving SQL:

| Sugar statement | Equivalent script call |
|---|---|
| `PREPROC ADD RULE FOR (ROLE\|USER) scope_value PHASE phase SCRIPT script_name FUNCTION function_name` | `EXECUTE SCRIPT PREPROC.RULE_ADD(...)` |
| `PREPROC DROP RULE rule_id` | `EXECUTE SCRIPT PREPROC.RULE_DROP(rule_id, NULL)` |
| `PREPROC ENABLE RULE rule_id` | `EXECUTE SCRIPT PREPROC.RULE_ENABLE(rule_id, NULL)` |
| `PREPROC DISABLE RULE rule_id` | `EXECUTE SCRIPT PREPROC.RULE_DISABLE(rule_id, NULL)` |
| `PREPROC LIST RULES` | `EXECUTE SCRIPT PREPROC.RULE_LIST()` |
| `PREPROC REFRESH` | `EXECUTE SCRIPT PREPROC.REFRESH()` |
| `PREPROC STATUS` | `EXECUTE SCRIPT PREPROC.STATUS()` |
| `PREPROC SET MODE mode` | `EXECUTE SCRIPT PREPROC.SET_MODE(mode, NULL)` |
| `PREPROC SET SELF_DISABLE value` | `EXECUTE SCRIPT PREPROC.SET_SELF_DISABLE(value)` |
| `PREPROC SET SYS_GROUPING value` | `EXECUTE SCRIPT PREPROC.SET_SYS_GROUPING(value, NULL)` |

Append `NOREFRESH` to any mutating command to skip the automatic refresh:

```sql
PREPROC ADD RULE FOR ROLE ANALYSTS PHASE EXPAND SCRIPT PREPROC_RT.MY_MODULE_V1 FUNCTION expand NOREFRESH
PREPROC REFRESH
```

The sugar module is a plain EXPAND-phase module. It is only active when the
preprocessor is running and a `PREPROC_ADMIN`-scoped EXPAND rule points to
`PREPROC_RT.CONTROL_SUGAR_V1`. The installer seeds exactly this rule. Statements
that do not start with `PREPROC` are passed through unchanged.

### Adding a rule

Use the sugar or call the script directly:

```sql
-- Sugar (convenience mode, preprocessor active):
PREPROC ADD RULE FOR ROLE ANALYSTS PHASE EXPAND SCRIPT PREPROC_RT.MY_MODULE_V1 FUNCTION expand

-- Direct script call:
EXECUTE SCRIPT PREPROC.RULE_ADD('ROLE', 'ANALYSTS', 'EXPAND', 'PREPROC_RT.MY_MODULE_V1', 'expand', NULL);
```

Rules take effect after the next `PREPROC.REFRESH` run (automatic unless
`NOREFRESH` is used).

Column reference for `PREPROC.CONFIG`:

| Column | Value notes |
|---|---|
| `rule_id` | Unique integer; lower values run first within a phase |
| `scope_type` | `'ROLE'` to apply to all holders of a role (transitive); `'USER'` to apply to one user |
| `scope_value` | Role name or user name |
| `client_pattern` | Literal substring matched against client application name. NULL = always apply |
| `phase` | `'TRANSLATE'`, `'EXPAND'`, or `'REWRITE'` |
| `script_name` | Fully qualified script, e.g. `'PREPROC_RT.MY_MODULE_V1'` |
| `function_name` | Exported function name, e.g. `'expand'` |
| `enabled` | `TRUE` to include; `FALSE` to exclude from next refresh |
| `version` | Informational integer; no dispatch semantics |

### SYS-grouping (break-glass recovery)

`PREPROC.SETTINGS.group_sys_with_dba` controls whether the `SYS` account is
grouped with `DBA` during resolution. `SYS` cannot hold `PREPROC_ADMIN` but
does hold `DBA`, so with the seeded default `TRUE` (grouped), `SYS` resolves
every `DBA`-scoped, `PUBLIC`-scoped, and nested-role rule exactly like any
other `DBA` holder.

Set it to `FALSE` to isolate `SYS` as a guaranteed raw-SQL break-glass recovery
account: `SYS` is excluded from the ENTIRE role-grant closure (`DBA`, `PUBLIC`,
and nested roles alike) and sees untransformed SQL regardless of what rules
the framework carries — unless a `USER`-scoped rule explicitly names `SYS`,
which still resolves in either state.

```sql
EXECUTE SCRIPT PREPROC.SET_SYS_GROUPING(FALSE, NULL);   -- isolate SYS (auto-refreshes)
EXECUTE SCRIPT PREPROC.SET_SYS_GROUPING(TRUE, NULL);    -- re-group SYS with DBA (default)
```

Or via sugar: `PREPROC SET SYS_GROUPING FALSE`. Like `SET_MODE`, the setter
rejects a non-boolean value before writing (side-effect-free), auto-refreshes
unless `NOREFRESH` is appended, and is not folded into the state fingerprint —
`PREPROC.STATUS` reports `CURRENT` immediately after the refresh it triggers.
The value is not surfaced in the `STATUS` result set; observe it directly:

```sql
SELECT group_sys_with_dba FROM PREPROC.SETTINGS;
```

## Refresh

Refresh rebuilds `PREPROC.RESOLUTION` from `PREPROC.CONFIG` and the current
database role/user grants. This is required whenever:

- A new rule is added, changed, or disabled in `PREPROC.CONFIG`
- A user is granted or revoked a role that has associated rules
- A new database user is created who should inherit role-based rules

Requires `PREPROC_ADMIN` (or `EXECUTE ANY SCRIPT`, or DBA). Run from any SQL
client:

```sql
EXECUTE SCRIPT PREPROC.REFRESH;
```

The script returns the number of resolution rows produced and the new
`last_refresh` timestamp.

Refresh is a full rebuild: it deletes all existing RESOLUTION rows and
reinserts the resolved set atomically. No partial state is visible during the
rebuild.

## Status

Check whether RESOLUTION is current with respect to the current database
user/role state:

```sql
EXECUTE SCRIPT PREPROC.STATUS;
```

Returns a current/stale verdict and the `last_refresh` time. Reports
`never-refreshed` if no refresh has been run since install.

Status is read-only. It never modifies RESOLUTION or SETTINGS.

Requires `PREPROC_ADMIN` (or `EXECUTE ANY SCRIPT`, or DBA).

## Session self-optimization

In `convenience` mode the framework removes the per-statement MASTER lookup after
a session's first statement. Two policies and one automatic lifecycle are
relevant to operators.

**Mode** — `PREPROC.SET_MODE('convenience', NULL)` enables self-optimization;
`PREPROC.SET_MODE('strict', NULL)` disables it (every statement pays the full
lookup, no session ever repoints itself).

**Self-disable policy** — controls only the *no-rule* path. With
`PREPROC.SET_SELF_DISABLE(TRUE)`, a convenience-mode session whose user has no
rules clears its preprocessor after statement 1 (subsequent statements bypass the
framework entirely). With `FALSE` (the install default) such sessions stay on
MASTER. Rule-bearing sessions repoint to their profile regardless of this flag.

```sql
PREPROC SET MODE convenience
PREPROC SET SELF_DISABLE TRUE
```

**Profile scripts and their cleanup are automatic.** Every `PREPROC.REFRESH`
generates a `PREPROC_RT.P_<hash>` profile per distinct resolved chain, retires
profiles whose chains have disappeared (replacing the body with a stub that
behaves exactly like MASTER), and reclaims (drops) previously-retired profiles
once no session could still be pinned to them. A profile is **never dropped on
the refresh that orphans it**, so a long-lived pinned session is never stranded.
Operators do not create, edit, or drop these scripts.

Observe the current profiles and their lifecycle state:

```sql
-- As PREPROC_ADMIN: generated profiles and whether each is live or retired
SELECT profile_hash, created_at, orphaned_at FROM PREPROC.PROFILES;

-- The P_<hash> scripts currently deployed
SELECT SCRIPT_NAME FROM EXA_ALL_SCRIPTS
WHERE SCRIPT_SCHEMA = 'PREPROC_RT' AND SCRIPT_NAME LIKE 'P\_%' ESCAPE '\';
```

A non-NULL `orphaned_at` marks a retired profile awaiting reclaim; it is dropped
on a later refresh once no active session logged in before that timestamp.

## Periodic refresh

The framework ships the refresh entry point only. Schedule it via an external
cron job, CI pipeline, or any SQL-capable scheduler:

```bash
# Example: run refresh using exaplus
exaplus -c myhost:8563 -u admin -p secret \
    -sql "EXECUTE SCRIPT PREPROC.REFRESH"
```

If refresh is not run after role changes, users whose role memberships changed
will run a stale pipeline until the next refresh.

## Module management

The framework owns Exasol's one `sql_preprocessor_script` slot and
multiplexes to every registered syntax provider through `MASTER` dispatch (see
[README](../README.md#what-it-solves)). A **module** is that provider's
packaging: a Lua entry function `fn(text) -> nil|string`, a registration rule,
and a `module.toml` manifest. The `preproc module` CLI is the operator's
air-gap bridge for discovering, deploying, updating, and retiring modules
without hand-writing the SQL in the previous section. For the authoring side
(writing a conformant module, the manifest schema, versioning discipline), see
[docs/module-authoring.md](module-authoring.md).

### `preproc module` subcommands

All subcommands accept `--registry <path|url>` to select the federated index —
a local checkout (offline) or a pinned `https://` raw URL (online). It defaults
to the central library's index,
[`exasol-labs/preprocessor-library`](https://github.com/exasol-labs/preprocessor-library)
`registry/index.json`.

| Subcommand | Database? | What it does |
|---|---|---|
| `preproc module list` | No | List every module in the registry with phase and deploy mode |
| `preproc module search <term>` | No | Filter the registry listing by a substring of name or description |
| `preproc module add <name> [--scope TYPE:value]` | Yes | Deploy the module's artifact (if `library-deployed`), register its rule via `RULE_ENSURE`, write a provenance row, refresh |
| `preproc module update <name> [--scope TYPE:value]` | Yes | Deploy the registry's latest version alongside the current one, repoint the rule, refresh, then mark the old version retired |
| `preproc module remove <name>` | Yes | Drop the module's rule(s), refresh, mark its provenance retired (does not drop the script — see [Reclaim](#reclaiming-a-retired-version)) |
| `preproc module bundle <names...> --output <file>` | No | Emit one self-contained offline `.sql` (artifact + `RULE_ENSURE` calls) for hand-carrying to an air-gapped instance |
| `preproc module validate <name>` | Optional | Static conformance checks always; when a database is configured, also scratch-deploys the module and smoke-tests it, then tears the scratch copy down |
| `preproc module sync <manifest>` | Yes | Declaratively converge the database to a desired-state TOML file — add missing modules, update changed versions, retire dropped ones, in one pass with a single refresh |

`--scope` overrides a module's `suggested_scope` from its manifest — pass
`ROLE:<name>` or `USER:<name>`.

Every mutating verb (`add`, `update`, `remove`, `sync`) resolves the database
connection from the same environment variables as `preproc install` (see
[Environment variables](#environment-variables)) and exits with code `2` if
they are missing — no connection is attempted. `list`, `search`, `bundle`, and
static `validate` never touch a database.

### Online vs. air-gap flows

**Online** — the common case. Point at the default (or a pinned) registry URL
and let the CLI fetch, verify, and deploy over the DB connection in one step:

```bash
uv run preproc module add cast-shorthand
uv run preproc module list --registry https://raw.githubusercontent.com/exasol-labs/preprocessor-library/main/registry/index.json
```

**Air-gap** — the database must never reach the network. Run `bundle` on a
machine with GitHub access to produce a self-contained `.sql` (same
`--/`…`/` delimiter convention as `release.sql`), hand-carry it to the
air-gapped instance, and run it there directly — no CLI, no DB connection,
needed on that side:

```bash
uv run preproc module bundle cast-shorthand trailing-comma --output offline_modules.sql
# hand-carry offline_modules.sql to the air-gapped instance, then run it
# with EXAplus (a GUI client like DbVisualiser/DBeaver must have parameterized
# SQL disabled, or it corrupts the `:`-bearing script bodies — see the release.sql
# warning above)
```

An offline local checkout of the library repo also works as `--registry` for
either flow (`--registry /path/to/preprocessor-library`), which is how CI and
local development exercise the CLI without network access to GitHub.

### Provenance: `PREPROC_RT.INSTALLED_MODULES`

Every deploy/update/remove writes a row to `PREPROC.MODULES`, surfaced through
the read-only `PREPROC_RT.INSTALLED_MODULES` view (PUBLIC-readable, like
`MY_PIPELINE` — ADR-002):

```sql
SELECT name, version, source_repo, source_ref, sha256, script_name,
       function_name, phase, deploy_mode, status, installed_at, retired_at
FROM PREPROC_RT.INSTALLED_MODULES
ORDER BY name, version;
```

Unlike `MY_PIPELINE`, there is no `CURRENT_USER` filter — provenance is global,
and one module name can have more than one row: the currently `deployed`
version and any not-yet-reclaimed `retired` predecessor.

### Module lifecycle

Modules follow the same never-mutate-a-live-object discipline as the
framework's own profile scripts (generate → retire → reclaim):

1. **add** — deploy `_V1`, register the rule (`RULE_ENSURE`), write
   provenance, refresh.
2. **update** = deploy `_V<N+1>` → repoint the existing rule from `_V<N>` to
   `_V<N+1>` → refresh → mark `_V<N>` retired in provenance. Both versions'
   scripts exist simultaneously during the repoint; `_V<N>` is never
   `CREATE OR REPLACE`d.
3. **remove** — drop the rule(s) naming the module's script, refresh, mark
   provenance retired. The script itself is left in place (untouched
   sessions cannot be stranded) until reclaimed.
4. **reclaim** — the physical `DROP SCRIPT`, guarded: `PREPROC.MODULE_RECLAIM(name, version)`
   drops a retired version only when no enabled `PREPROC.CONFIG` rule and no
   live (non-orphaned) `PREPROC.PROFILES` manifest still reference it. Reclaim
   is a separate, later admin call — it is not run automatically by `remove`
   or `update`, mirroring the framework's own profile
   [retire-then-reclaim](how-it-works.md#profile-cleanup-retire-then-reclaim)
   invariant.

`preproc module sync <manifest>` drives this whole lifecycle declaratively:
diff the desired-state TOML against `INSTALLED_MODULES`, then add/update/retire
whatever differs, in a single convergence pass with exactly one refresh. Re-running
`sync` against an unchanged desired file is a true no-op (closes BUG-008):
`RULE_ENSURE` returns the existing rule id rather than duplicating it.

### Follow-ons

Two items from this plan are deliberately deferred, not forgotten:

- **exasol-semantic-views integration** — the module contract already
  accommodates a `self-deployed`, register-only module (see the `module.toml`
  example in [module-authoring.md](module-authoring.md#example-a-self-deployed-module-front-door-to-another-subsystem)),
  but actually wiring that subsystem's front door in as a module needs
  coordination with their team. Tracked as a follow-on spike.
- **Standalone pip-installable test kit** — the library repo's CI currently
  depends on this framework's package for connection/install helpers rather
  than a separately published, independently versioned test harness.
  Extracting that into its own pip-installable package is a follow-on; it
  would let module authors outside this repo test against the framework
  without a framework checkout.

## In-database module install

Everything in [Module management](#module-management) above runs from an
operator machine with a `uv`/Python toolchain and network reach to the DB. For
a DBA who works purely through a SQL client, the framework also owns an
ALTERNATIVE, entirely SQL-side install path: `PREPROC.MODULE_INSTALL` and
`PREPROC.MODULE_CATALOG`. Both paths converge on the same end-state — a
`PREPROC_RT.<NAME>_V<N>` script, a `PREPROC.CONFIG` rule, and a
`PREPROC.MODULES` provenance row — so `MASTER` dispatch is identical
regardless of which one deployed a module. The `preproc module` CLI is
unchanged and remains the default, fully-supported air-gap path; this section
covers the SQL-only alternative for a DBA who would rather never touch a
Python toolchain. See [How it works § In-database install
path](how-it-works.md#in-database-install-path) for the architecture behind
this section.

### Prerequisites: the BucketFS CONNECTION

The framework never creates a BucketFS CONNECTION object — that is an operator
responsibility, done once per cluster. Create it and grant the
`PREPROC_ADMIN` role ACCESS so `MODULE_INSTALL` / `MODULE_CATALOG` can read
it:

```sql
CREATE OR REPLACE CONNECTION PREPROC_BFS
    TO 'bucketfs:<service>/<bucket>' IDENTIFIED BY '<read-password>';
GRANT ACCESS ON CONNECTION PREPROC_BFS TO PREPROC_ADMIN;
```

Use the **native BucketFS address** — `bucketfs:<service>/<bucket>` (e.g.
`bucketfs:bfsdefault/preprocessor`), the same form the [Exasol
docs](https://docs.exasol.com/db/latest/database_concepts/bucketfs/database_access.htm)
use — **not** an `https://…:2581/…` URL. The database reads BucketFS natively
off the cluster-local mount (`/buckets/<service>/<bucket>/…`), so no host, no
port, and no network egress are involved; `<service>` is the BucketFS service
name (usually `bfsdefault`) and read access is governed entirely by this granted
CONNECTION and its read password.

The connection name (`PREPROC_BFS` above) is never hardcoded by the
framework — it is whatever the operator named it, referenced by the
`bucketfs:<CONNECTION_NAME>/<path>` source string described below.

### Staging a release tarball

Upload the library's release tarball (`preproc-lib-<ver>.tar.gz`) into the
bucket the CONNECTION points at, using your usual BucketFS upload path (e.g.
a `curl PUT` to the BucketFS write service, or any BucketFS client). The
`preprocessor-library` repo builds that tarball with `scripts/build_release.py`
(and attaches it to each `v*` GitHub Release); it lays out `registry/index.json`
plus each module artifact exactly as the resolver reads them.

Note on how BucketFS stores it: BucketFS auto-extracts an uploaded archive
into a directory on the UDF mount as soon as it lands, rather than leaving the
compressed file sitting there untouched. You still write the source string
naming the archive exactly as uploaded —
`bucketfs:PREPROC_BFS/preproc-lib-0.1.0.tar.gz` — and the resolver
transparently locates and reads the auto-extracted content behind that name;
this BucketFS-side extraction detail requires no different source syntax and
no extra operator step.

### Installing a module from SQL

Two equivalent forms — a direct script call, or the `PREPROC` sugar
(requires an active preprocessor and the caller holding `PREPROC_ADMIN`):

```sql
-- Direct script call:
EXECUTE SCRIPT PREPROC.MODULE_INSTALL('bucketfs:PREPROC_BFS/preproc-lib-0.1.0.tar.gz', 'cast_shorthand', 'ROLE:ANALYSTS');

-- Sugar:
PREPROC INSTALL MODULE cast_shorthand FROM 'bucketfs:PREPROC_BFS/preproc-lib-0.1.0.tar.gz' FOR ROLE ANALYSTS;
```

The third argument is a `TYPE:value` scope (`TYPE` ∈ `ROLE` | `USER`), the
same shape the sugar's `FOR ROLE|USER` clause folds into. Scope targets rule
**activation** — which users or roles receive the transform once installed —
not install permission; install permission is the `PREPROC_ADMIN` boundary
described below, independent of scope.

**The sugar form follows Exasol identifier rules.** Module names are
identifier-safe (letters, digits, underscores — no hyphens, enforced by the
module contract) and matched case-insensitively, so you write them **unquoted**
in any case — `cast_shorthand`, `CAST_SHORTHAND`, or `"cast_shorthand"` all
resolve the same module. The `FOR ROLE|USER` value is a role/user name, which
Exasol always upper-cases regardless of quoting — `FOR ROLE dba`,
`FOR ROLE DBA`, and `FOR ROLE "dba"` all target the `DBA` role. (The direct
`EXECUTE SCRIPT` form takes plain string literals and does no folding — pass
the exact registry key and scope you mean.)

`MODULE_INSTALL` returns a result row reporting the outcome:

| `module` | `version` | `phase` | `status` |
|---|---|---|---|

`status` is one of:

| Status | Meaning |
|---|---|
| `installed` | Fresh library-deployed install: script created, rule registered, provenance written |
| `already-current` | Re-running install for an identical version already installed — idempotent no-op |
| `registered-only` | A self-deployed module: rule + provenance registered, no script CREATE (the module's own installer already deployed the backing script) |

### Discovering modules before installing

Before installing, list what a source advertises and which of those modules
are already installed:

```sql
-- Direct script call:
EXECUTE SCRIPT PREPROC.MODULE_CATALOG('bucketfs:PREPROC_BFS/preproc-lib-0.1.0.tar.gz');

-- Sugar:
PREPROC CATALOG MODULES FROM 'bucketfs:PREPROC_BFS/preproc-lib-0.1.0.tar.gz';
```

Returns one row per module the source's registry index advertises, carrying
`name`, `version`, `phase`, `deploy_mode`, and an `installed` BOOLEAN column
(the "installed?" flag — `TRUE` only for a currently `deployed` version; a
`retired` provenance row does not count). `MODULE_CATALOG` is read-only: it
never touches `PREPROC.CONFIG`,
`PREPROC.SETTINGS`, `PREPROC.RESOLUTION`, or `PREPROC.MODULES`.

### HTTPS sources

Both `MODULE_INSTALL` and `MODULE_CATALOG` accept an `https://` source in
place of a `bucketfs:` one — a pinned GitHub raw URL, for instance. There is
no settings toggle to turn this on: supplying an `https:` source IS the
opt-in, and it is fetched directly whenever the cluster has outbound network
access. On a no-egress cluster, an `https:` source fails with a network error
and installs (or catalogs) nothing — no partial deploy is left behind.
BucketFS staging, described above, is the recommended path for a no-egress
cluster.

### Admin boundary

`PREPROC.MODULE_INSTALL`, `PREPROC.MODULE_CATALOG`, and the resolver UDF(s)
they drive live in the locked `PREPROC` schema and are never granted to
PUBLIC — exactly like the rest of the [control surface](#control-surface).
The caller must hold `PREPROC_ADMIN` (or, like the rest of the control
surface, be a DBA / superuser with `EXECUTE ANY SCRIPT`); a caller lacking
that is rejected with an insufficient-privileges error before anything is
read or written. This keeps
the resolver's BucketFS/network access reachable only through the admin
surface, never by an ordinary user.

## Deploying a module by hand (no registry)

For a one-off module that will never go through the registry — e.g. a private,
unpublished module — the manual path still works exactly as before:

1. Write the Lua module following the authoring contract in
   [how-it-works.md](how-it-works.md#module-authoring-contract) (or the fuller
   SDK reference in [module-authoring.md](module-authoring.md)).
2. Deploy it as a versioned script under a new name:

```sql
OPEN SCHEMA PREPROC_RT;
CREATE LUA SCRIPT PREPROC_RT.MY_COMMAND_V1() AS
<lua body here>
/
```

3. Insert a rule in `PREPROC.CONFIG` pointing to the new script and function.
4. Run `EXECUTE SCRIPT PREPROC.REFRESH`.
5. Test with a session-level activation before enabling system-wide.

To update an existing module, deploy as `MY_COMMAND_V2`, repoint the rule to
the new version, refresh, then retire `MY_COMMAND_V1` once no rule references
it. Never use `CREATE OR REPLACE` on a live module. This mirrors the `add`
step of the CLI lifecycle above, minus the registry lookup and provenance
row — a manually deployed module simply never appears in
`PREPROC_RT.INSTALLED_MODULES`.

## Troubleshooting

**User cannot run any statement after activation**

The most likely cause is that the user lacks EXECUTE or SELECT on `PREPROC_RT`.
Schema-level grants should have been applied by the installer:

```sql
GRANT EXECUTE ON SCHEMA PREPROC_RT TO PUBLIC;
GRANT SELECT ON SCHEMA PREPROC_RT TO PUBLIC;
```

Verify with:

```sql
SELECT * FROM EXA_ALL_SCHEMA_PRIVILEGES WHERE SCHEMA_NAME = 'PREPROC_RT';
```

**A custom command produces a syntax error**

The EXPAND phase is fail-open. If no module claims the statement, the original
text is passed to the engine unchanged. A syntax error from the engine means
either:

- The module is not deployed under the script name in the rule.
- The `function_name` in the rule does not match an exported global function in
  the module.
- No rule for the current user matches this statement (check RESOLUTION).
- The `client_pattern` on the rule does not match the connecting client.

Check which rules apply to the current user:

```sql
SELECT * FROM PREPROC_RT.MY_PIPELINE;
```

Check whether RESOLUTION is populated for the user:

```sql
-- As a PREPROC_ADMIN user:
SELECT * FROM PREPROC.RESOLUTION WHERE user_name = 'ALICE';
```

**A module throws but the statement still executes**

This is expected fail-open behaviour. MASTER catches the error via `pcall`,
skips the rule, and continues. Investigate the module logic directly — deploy a
test version that surfaces the error explicitly, or check the `PREPROC.CONFIG`
entry to confirm the script and function names are correct.

**RESOLUTION looks stale after a role change**

Run `EXECUTE SCRIPT PREPROC.STATUS` to confirm staleness. Then run
`EXECUTE SCRIPT PREPROC.REFRESH` to rebuild. Consider adding a scheduled job
so refresh runs automatically after role-grant operations.

**Checking which module will handle a statement (before activation)**

Write a unit test against the module's Lua function directly using pyexasol,
or test in a session with `ALTER SESSION SET sql_preprocessor_script = PREPROC_RT.MASTER`
before enabling system-wide.
