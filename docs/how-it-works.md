# How It Works

[← README](../README.md) | [Operations →](operations.md)

## Overview

The framework wraps Exasol's `sql_preprocessor_script` system parameter with a
dispatch layer. When active, Exasol invokes `PREPROC_RT.MASTER` for every SQL
statement before compilation. MASTER performs a single table lookup, then runs
the calling user's transformation functions in a fixed phase order before
handing the statement back to the engine.

## Two-schema layout

The framework uses two schemas with distinct access levels.

**`PREPROC`** (admin schema, locked)

Owned by the `PREPROC_ADMIN` role. Not accessible to PUBLIC. Holds:

| Object | Purpose |
|---|---|
| `PREPROC.CONFIG` | Rule definitions — the operator-editable source of truth |
| `PREPROC.RESOLUTION` | Flattened per-user rules (incl. each chain's `profile_hash`) — rebuilt by `REFRESH`, never edited directly |
| `PREPROC.SETTINGS` | Mode, self-disable flag, last refresh time, state fingerprint |
| `PREPROC.PROFILES` | Profile manifest registry (`profile_hash` → `manifest`, `created_at`, `orphaned_at`) driving the retire-then-reclaim lifecycle |
| `PREPROC.REFRESH_CORE` | Importable Lua module: the atomic rebuild + profile codegen + cleanup |
| `PREPROC.REFRESH` | DB-side Lua script that rebuilds RESOLUTION |
| `PREPROC.STATUS` | DB-side Lua script that reports staleness |

**`PREPROC_RT`** (runtime schema, PUBLIC-accessible)

EXECUTE and SELECT on this schema are granted to PUBLIC. Holds:

| Object | Purpose |
|---|---|
| `PREPROC_RT.MASTER` | The dispatcher preprocessor script — also the single source for every retirement stub (see [session self-optimization](#session-self-optimization-zero-overhead-paths)) |
| `PREPROC_RT.MY_PIPELINE` | Definer-rights view: current user's resolved pipeline, plus `mode`, `self_disable_allowed`, and `profile_hash` |
| `PREPROC_RT.P_<hash>` | Generated per-chain profile scripts (one per distinct resolved rule chain); the swap target for convenience-mode sessions |
| `PREPROC_RT.DEMO_PING_V1` | Demo command module (EXPAND phase) |
| `PREPROC_RT.DEMO_TRANSLATE_V1` | Demo TRANSLATE module |
| `PREPROC_RT.DEMO_AUDIT_V1` | Demo REWRITE module |
| `PREPROC_RT.DEMO_SQLGLOT_TRANSPILE_V1` | Companion Python3 UDF for dialect transpilation |

The two-schema split is a direct consequence of Exasol's invoker-rights model:
MASTER runs with the invoker's (calling user's) privileges. A regular user has
no grants on `PREPROC.*` tables and can never see other users' rules. MASTER
can only reach admin data through `PREPROC_RT.MY_PIPELINE`, which is a
definer-rights view that reads `PREPROC.RESOLUTION` and `PREPROC.SETTINGS`
under the view owner's privileges, scoped to `CURRENT_USER`.

## Phase pipeline

Each statement passes through three phases in fixed order:

```
Statement → TRANSLATE → EXPAND → REWRITE → Engine
```

**TRANSLATE**

Input is assumed to be a foreign-dialect SQL statement (e.g. Databricks SQL)
that is not valid Exasol SQL. The module transpiles it to Exasol SQL.
**Run-all**: every matching TRANSLATE rule runs and threads its returned text
forward to the next (not first-match-terminal); a user may have multiple
TRANSLATE rules. Fail-closed is a *module* contract, not a dispatch one: the
module must return the original text unchanged on any failure so corrupted or
wrong-dialect text never reaches the engine masquerading as a translation. At
the dispatch level TRANSLATE is fail-open like the other phases — a throwing
module is skipped via `pcall`, leaving the text unchanged.

**EXPAND**

Input is assumed to be not valid Exasol SQL — a custom command or language
extension. The module must either recognise the statement (return a non-nil
expansion string, terminal for this phase) or decline it (return `nil`,
continue to the next rule). Uses first-match-terminal semantics: the first
matching module ends the EXPAND scan. REWRITE still runs on the expanded text.
Fail-open and inherently safe: an unmatched custom command reaches the engine
verbatim and produces a loud syntax error — it cannot silently corrupt data.

**REWRITE**

Input is valid Exasol SQL. The module modifies behaviour: policy injection,
table redirection, guardrails. Every REWRITE rule in the user's pipeline runs
(no terminal semantics). Fail-open-sensitive: a skipped REWRITE silently runs
the original statement, which is a policy bypass risk. Design REWRITE modules
with this in mind.

## Dispatch semantics per phase

| Phase | Dispatch model | Nil return meaning | Module error |
|---|---|---|---|
| TRANSLATE | Run-all (text threading) | Not applicable | Skip rule, continue (module fails closed by returning original text) |
| EXPAND | First-match-terminal | "Not my command, keep scanning" | Skip rule, try next |
| REWRITE | Run-all | Not applicable | Skip rule, continue |

Within a phase, rules execute in ascending `ordinal` order. The `ordinal` is
assigned at refresh time based on `rule_id` ascending; the operator controls
execution sequence by assigning lower `rule_id` values to rules that should run
first.

## Rules table: PREPROC.CONFIG

This is the only table the operator edits. Columns:

| Column | Type | Notes |
|---|---|---|
| `rule_id` | DECIMAL(18,0) | Determines within-phase ordinal. Lower runs first. |
| `scope_type` | VARCHAR(4) | `'ROLE'` or `'USER'` |
| `scope_value` | VARCHAR(128) | Role name or user name |
| `client_pattern` | VARCHAR(256) | Optional literal substring to match against client application name. NULL means apply always. |
| `phase` | VARCHAR(32) | `'TRANSLATE'`, `'EXPAND'`, or `'REWRITE'` |
| `script_name` | VARCHAR(128) | Fully qualified script name, e.g. `'PREPROC_RT.DEMO_PING_V1'` |
| `function_name` | VARCHAR(128) | Exported function name within the script, e.g. `'ping'` |
| `enabled` | BOOLEAN | Disabled rules are excluded at refresh time |
| `version` | DECIMAL(9,0) | Informational; no dispatch semantics |

CONFIG is never read at statement time. It is the input to `PREPROC.REFRESH`,
which rebuilds `PREPROC.RESOLUTION`. MASTER reads only `PREPROC_RT.MY_PIPELINE`.

## Resolution: PREPROC.RESOLUTION

The flat, per-user runtime table MASTER reads (through the view). Rebuilt
atomically by `PREPROC.REFRESH`. Columns:

| Column | Type | Notes |
|---|---|---|
| `user_name` | VARCHAR(128) | Concrete database user |
| `client_pattern` | VARCHAR(256) | Copied from CONFIG; NULL = always apply |
| `phase` | VARCHAR(32) | `'TRANSLATE'`, `'EXPAND'`, or `'REWRITE'` |
| `script_name` | VARCHAR(128) | Fully qualified module script |
| `function_name` | VARCHAR(128) | Function to call |
| `ordinal` | DECIMAL(9,0) | Within-phase dispatch order (derived from `rule_id`) |
| `profile_hash` | VARCHAR(64) | Content hash of the user's full ordered chain; names the generated `PREPROC_RT.P_<hash>` profile script. NULL for users with no rules. |

ROLE-scoped rules expand transitively through nested role grants (Exasol
`CONNECT BY` over `EXA_DBA_ROLE_PRIVS`). A user's resolved pipeline is the
union of all role-derived and user-scoped rules that apply to them. Duplicates
are collapsed: if the same `(phase, script, function)` reaches a user through
multiple paths it appears exactly once (lowest `rule_id` wins the ordinal).

A rule scoped to `ROLE` `PUBLIC` applies to **every** user, unconditionally.
PUBLIC membership is implicit and never appears in `EXA_DBA_ROLE_PRIVS`, so the
refresh closure injects a synthetic `PUBLIC → user` edge for every user in
`EXA_DBA_USERS`; PUBLIC then resolves like a role held by all users (and any
real role granted *to* PUBLIC fans out to all users the same way).

`SYS` cannot hold `PREPROC_ADMIN` but does hold `DBA`, so it flows through the
same ROLE-closure arm as every other `DBA`-derived, `PUBLIC`-derived, and
nested-role rule. `PREPROC.SETTINGS.group_sys_with_dba` (seeded `TRUE`) governs
whether that closure includes `SYS`: grouped (`TRUE`, the default) resolves
`SYS` like any other `DBA` holder; isolated (`FALSE`) excludes `SYS` from the
**entire** ROLE-closure arm at once — DBA, PUBLIC, and nested roles alike — so
`SYS` sees raw, untransformed SQL as a guaranteed break-glass recovery path. A
`USER`-scoped rule naming `SYS` is unaffected by the toggle and still resolves
either way, since it flows through the separate USER-scoped union arm. See
[SYS-grouping (break-glass recovery)](operations.md#sys-grouping-break-glass-recovery)
for the operator-facing setter.

## client_pattern matching

`client_pattern` is compared against the connecting client's application
identifier as a **literal substring** (`string.find(client, pattern, 1, true)`).
It is not a Lua regular expression. A NULL pattern matches all clients. Matching
happens inside MASTER at dispatch time, per rule, per statement.

## Fail-open safety contract

MASTER enforces three layers of fail-open behaviour:

1. **Control-statement guard** — `ALTER SESSION` and `ALTER SYSTEM` are returned
   immediately without any lookup or dispatch. These must pass through untouched
   or the system can break its own session state.

2. **Lookup fail-open** — the `MY_PIPELINE` query is wrapped in `pcall`. Any
   error (thrown or pquery-not-ok) returns the original statement unchanged.

3. **Per-rule fail-open** — each module call is wrapped in `pcall`. A missing or
   erroring module skips that rule and continues; it never blocks the statement.

`setsqltext` is called only when dispatch actually changed the statement text.

## Session self-optimization (zero-overhead paths)

The MASTER lookup costs ~7.3 ms per statement. In **convenience** mode the
framework pays that cost only on a session's **first** statement, then
self-optimizes for the rest of the session. In **strict** mode no optimization
happens and every statement pays the full lookup (the deliberate trade-off for a
posture that never lets a session repoint itself).

After dispatching statement 1, MASTER (still in convenience mode) does one of:

| Session | Action after statement 1 | Subsequent per-statement cost |
|---|---|---|
| No resolved rules, `self_disable_allowed = TRUE` | `ALTER SESSION SET sql_preprocessor_script = ''` — clears the preprocessor | ~0 ms (no preprocessor) |
| No resolved rules, `self_disable_allowed = FALSE` | nothing — stays on MASTER | ~7.3 ms |
| Has resolved rules | `ALTER SESSION SET sql_preprocessor_script = PREPROC_RT.P_<hash>` — repoints to the baked profile | ~0.5 ms (no MY_PIPELINE lookup) |

The swap is issued via `pquery` *after* dispatch and *before* `setsqltext`, and
is wrapped in `pcall`: a failed swap (e.g. the profile script is unexpectedly
missing) leaves the session on MASTER — fail-open, never blocking the statement.
The `ALTER SESSION` it issues is itself caught by MASTER's control-statement
guard, so there is no recursion.

### Profile scripts (`PREPROC_RT.P_<hash>`)

Profiles are **generated eagerly during `REFRESH`**, under the admin caller's
rights — MASTER (invoker rights) cannot create scripts, so it never generates
them on demand. For each distinct resolved chain, `REFRESH_CORE` computes a
content hash over the ordered `(phase, ordinal, script_name, function_name,
client_pattern)` manifest and `CREATE OR REPLACE`s `PREPROC_RT.P_<hash>` with the
chain's rules **baked into a static Lua table**. A profile reproduces MASTER's
dispatch semantics exactly (guard, client matching, phase order, per-rule
fail-open, EXPAND first-match-terminal) but issues **no `MY_PIPELINE` lookup** —
that absent per-statement query is the entire speedup. Two chains that differ in
rules, order, or `client_pattern` hash differently and get distinct profiles;
identical chains share one. Codegen happens inside the same atomic rebuild as the
RESOLUTION write, so a committed `profile_hash` never names a script that does
not yet exist.

### Profile cleanup: retire then reclaim

When a chain disappears (rules removed/changed), its `P_<hash>` may still be the
active preprocessor of a live session, so it is **never dropped on the refresh
that orphans it**. Instead `REFRESH_CORE` runs a generational lifecycle inside
the atomic rebuild, in fixed order:

1. **Generate** — `CREATE OR REPLACE` the real baked body for every current
   chain; clear `orphaned_at` for any re-adopted hash.
2. **Retire** — for each hash no longer referenced and not already retired,
   replace its body with the **retirement stub** and stamp `orphaned_at =
   CURRENT_TIMESTAMP`.
3. **Reclaim** — for each hash retired on a *prior* refresh that is provably
   unpinned, `DROP SCRIPT` it and delete its `PREPROC.PROFILES` row.

The **retirement stub is byte-for-byte `PREPROC_RT.MASTER`'s own body**, read
back from the catalog (`EXA_ALL_SCRIPTS`) at refresh time. A session still pinned
to a retired profile therefore behaves exactly like MASTER on its next statement:
it re-runs the full resolve → dispatch → swap for that statement (transforming it
under the *now-current* pipeline — no untransformed gap) and repoints itself to
the correct current target. Because the stub is sourced from MASTER, there is one
source of truth for the dispatch contract; a change to `master.lua` propagates to
every stub created or refreshed afterward.

The reclaim criterion is **login-time based**: a retired hash is dropped only
when no active session has `LOGIN_TIME < orphaned_at` (any session that could
still be pinned must have logged in before the orphaning). A `reclaim_cutoff`
captured at the start of each rebuild excludes hashes retired by the *current*
refresh, making never-same-refresh-drop true by construction. Zero qualifying
sessions ⇒ all retired profiles are reclaimable.

> **Design note.** An earlier design factored the dispatch logic into an
> importable `PREPROC_RT.MASTER_CORE` module that both MASTER and the stub would
> `import`. This proved infeasible: in the Exasol preprocessor context, dynamic
> `import()` of a rule module works only at top-level script scope, never from
> inside a function of an imported module — so the wrapper silently fail-opened
> every dispatch. MASTER is therefore the single canonical **inline** dispatcher,
> and the stub is sourced from MASTER's catalog body.

## In-database install path

Alongside the CLI's `preproc module add` (operator machine, Python/uv
toolchain, network reach to the DB), the framework also supports installing a
module entirely from SQL: `EXECUTE SCRIPT PREPROC.MODULE_INSTALL(source,
module_name, scope)`, or the equivalent `PREPROC INSTALL MODULE …` sugar.
This section covers the architecture behind that path; see [Operations §
In-database module install](operations.md#in-database-module-install) for the
operator-facing how-to.

### Capability split: a resolver UDF reads, an orchestrator writes

Exasol Lua scripts have no filesystem, BucketFS, or network access (mission
Constraints; ADR-001) — only a UDF can read BucketFS or reach the network,
and only a Lua `EXECUTE SCRIPT` can run DDL. The in-database install path
therefore splits along that capability line:

- A Python3 **resolver UDF**, admin-only in `PREPROC`, reads a source
  (`bucketfs:` via the cluster-local mount, or `https:` via a network fetch),
  parses `registry/index.json`, selects the requested module's entry, and
  verifies its `sha256`. It returns the artifact text plus registry metadata,
  or a structured failure indicator — it never raises.
- A Lua **orchestrator**, `PREPROC.MODULE_INSTALL`, drives the resolver and
  then re-homes the CLI's five-step `deploy_module` sequence into the
  database: `OPEN SCHEMA` / `CREATE OR REPLACE` the versioned script,
  `RULE_ENSURE` the activation rule, `MODULE_REGISTER` the provenance row,
  and a final `REFRESH`. `PREPROC.MODULE_CATALOG` drives the same resolver
  read-only, for discovery, without ever reaching the write steps.

This re-homes the sequence rather than vendoring the CLI's Python package
into a UDF — the end-state is byte-identical to a CLI `preproc module add`:
the same `PREPROC_RT.<NAME>_V<N>` script, `PREPROC.CONFIG` rule, and
`PREPROC.MODULES` provenance row, so `MASTER` / `MY_PIPELINE` dispatch
behaves identically regardless of which path installed a module.

### Abort-before-write

Any resolver failure — a `sha256` mismatch, the module missing from the
index, an unreadable source, or an unreachable HTTPS host / network error —
aborts the orchestrator before it reaches the write steps. Nothing is
created: no script, no `PREPROC.CONFIG` rule, no `PREPROC.MODULES` provenance
row. This mirrors the CLI's own `fetch_and_verify_artifact` invariant of
leaving the database consistent on any verification failure — the in-database
path re-homes that same ordering (verify fully, then write) rather than
writing optimistically and rolling back.

### Air-gap safety

A BucketFS read is cluster-local: the resolver reads the CONNECTION-mounted
file through the filesystem the UDF already runs on, performing no network
egress. This is what makes the BucketFS-staged path air-gap-safe even though
it is now a UDF, not the CLI, doing the reading. An `https:` source is the
one path that does leave the cluster, and it is used only when the operator
supplies an `https:` source — no settings toggle exists to gate it — so a
no-egress cluster simply fails the GET rather than silently falling back to
anything.

The privilege boundary underneath both source kinds is the same one
`PREPROC.REFRESH` / `REFRESH_CORE` already rely on: Exasol has no
definer-rights Lua scripts (ADR-001), so `MODULE_INSTALL` and
`MODULE_CATALOG` run with INVOKER rights, and the caller must hold
`PREPROC_ADMIN` — the role that owns `PREPROC` / `PREPROC_RT`, holds
`CREATE SCRIPT`, and (once the operator grants it) ACCESS on the BucketFS
CONNECTION. `REFRESH_CORE` already proves a Lua admin script can `CREATE OR
REPLACE` scripts at runtime under this exact role, when it generates
`PREPROC_RT.P_<hash>` profile scripts during every refresh — `MODULE_INSTALL`
reuses the identical boundary rather than inventing a new one. The resolver
UDF(s) are admin-only in `PREPROC` and never granted to PUBLIC, so a
non-admin cannot reach BucketFS or the network through this surface either.

## Module authoring contract

A module is a Lua script in `PREPROC_RT`. Its exported functions are loaded by
MASTER via `import(script_name, alias)`, which populates a global table.
Functions must be declared at top level as plain globals:

```lua
function ping(sqltext)   -- correct: visible in module table
    ...
end
```

A `local` function or a function inside a table literal will not be found.

**Function signature**: `fn(sqltext)` — receives the current statement text.
Do not call `sqlparsing.getsqltext()` inside a module.

**Return contract**:

- EXPAND: return `nil` to decline; return a non-nil string to accept (terminal
  for the phase).
- TRANSLATE / REWRITE: return the (possibly modified) statement text. Do not
  return nil.

**gsub trap** — `string.gsub` returns two values (result string, match count).
Never pass a `gsub` call directly as a function argument or as a `return`
statement:

```lua
-- Wrong: passes the count as a second value
return text:gsub("^%s+", "")

-- Correct: capture the string first
local s = text:gsub("^%s+", "")
return s
```

**Single-statement output** — a module must return exactly one SQL statement.
The framework does not split on semicolons; returning multiple statements
concatenated will produce a syntax error in the engine.

**Let errors propagate** — MASTER wraps calls in `pcall`. Do not add a
defensive `pcall` inside your module: it converts a visible fault into a silent
wrong answer.

**Versioning discipline** — modules are versioned objects. Never use
`CREATE OR REPLACE` on a live module. To change a module, author a new version
under a new name (e.g. `DEMO_PING_V2`), repoint the rule, and retire the old
version once no rule references it.

## DEMO_PING_V1 walkthrough

`PREPROC_RT.DEMO_PING_V1` is the canonical EXPAND module reference. It
exports one function: `ping`.

```lua
function ping(sqltext)
    local trimmed = sqltext:gsub("^%s*(.-)%s*$", "%1")
    if string.upper(trimmed) == "PING" then
        return "SELECT 'PONG'"
    end
    return nil
end
```

When MASTER dispatches an EXPAND rule pointing at `PREPROC_RT.DEMO_PING_V1 /
ping`:

1. It calls `import("PREPROC_RT.DEMO_PING_V1", "preproc_module")`, which loads
   the script's globals into `preproc_module`.
2. It calls `preproc_module.ping(sqltext)`.
3. If the statement is exactly `PING` (case-insensitive, whitespace-trimmed),
   `ping` returns `"SELECT 'PONG'"`. MASTER sets that as the new statement text
   and stops scanning EXPAND rules.
4. For any other statement, `ping` returns `nil`. MASTER continues to the next
   EXPAND rule.

After EXPAND, the REWRITE phase runs on the expanded text (in this demo there
are no REWRITE rules, so `SELECT 'PONG'` reaches the engine unchanged and
returns the value `PONG`).

## DEMO_TRANSLATE_V1 walkthrough

`PREPROC_RT.DEMO_TRANSLATE_V1` is the canonical TRANSLATE module reference. It
demonstrates dialect transpilation via an explicit marker trigger and a
companion Python3 UDF.

**Trigger**: the dialect marker comment `--!dialect:<name>` at the start of
the statement (after optional whitespace). Without this marker the module
returns the statement byte-for-byte unchanged and the UDF is never called.
Always-transpile was rejected: it costs ~133 ms/statement and risks corrupting
already-valid Exasol SQL through cross-dialect quote-escaping.

**Translation path**:
1. Module detects `--!dialect:tsql` prefix and strips the marker line.
2. Calls `PREPROC_RT.DEMO_SQLGLOT_TRANSPILE_V1(dialect, stripped_text)` via pquery.
3. UDF calls `sqlglot.transpile(text, read=dialect, write='exasol')` and returns
   the transpiled string or `ERR:<message>` on failure.

**Fail-closed contract**: if the UDF returns the `ERR:` sentinel (or if pquery
fails), the module returns the *original* statement unchanged. It never threads
the `ERR:` text or corrupted SQL forward. A `SELECT TOP 5 …` that cannot be
transpiled arrives at the engine as `SELECT TOP 5 …` — which produces a normal
Exasol syntax error, not silently wrong results.

**Example**:
```
Input:  --!dialect:tsql
        SELECT TOP 5 col FROM t
Output: SELECT col FROM t LIMIT 5
```

T-SQL `SELECT TOP n` is a hard Exasol syntax error and transpiles cleanly to
`LIMIT n` with zero extra code — the most compelling demo possible.
