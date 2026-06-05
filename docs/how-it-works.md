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
| `PREPROC.RESOLUTION` | Flattened per-user rules — rebuilt by `REFRESH`, never edited directly |
| `PREPROC.SETTINGS` | Mode, self-disable flag, last refresh time, state fingerprint |
| `PREPROC.PROFILES` | Generated profile scripts (hash → manifest) |
| `PREPROC.REFRESH` | DB-side Lua script that rebuilds RESOLUTION |
| `PREPROC.STATUS` | DB-side Lua script that reports staleness |

**`PREPROC_RT`** (runtime schema, PUBLIC-accessible)

EXECUTE and SELECT on this schema are granted to PUBLIC. Holds:

| Object | Purpose |
|---|---|
| `PREPROC_RT.MASTER` | The dispatcher preprocessor script |
| `PREPROC_RT.MY_PIPELINE` | Definer-rights view: current user's resolved pipeline |
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

ROLE-scoped rules expand transitively through nested role grants (Exasol
`CONNECT BY` over `EXA_DBA_ROLE_PRIVS`). A user's resolved pipeline is the
union of all role-derived and user-scoped rules that apply to them. Duplicates
are collapsed: if the same `(phase, script, function)` reaches a user through
multiple paths it appears exactly once (lowest `rule_id` wins the ordinal).

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
