# Module Authoring

[← README](../README.md) | [How it works](how-it-works.md) | [Operations](operations.md)

This is the SDK-facing contract for anyone writing a **module**: a syntax or
policy provider that hooks into the framework's single dispatch slot. There is
exactly **one** contract, whether the module is a ten-line transform or the
front door to a whole subsystem. What is behind the entry function is the
author's concern, not the contract's.

A module is three things:

1. A Lua entry function `fn(text) -> nil|string`, living in a versioned Lua
   script.
2. A registration rule (scope, phase, script, function) added through the
   existing `PREPROC ADD RULE` / `PREPROC.RULE_ADD` surface — see
   [Adding a rule](operations.md#adding-a-rule).
3. A `module.toml` manifest describing the artifact and how it is deployed.

Deployment mode (how the artifact gets into a database) is orthogonal
metadata on the manifest, not a second contract.

## The function contract

The entry function receives the current statement text as its single string
argument and returns either `nil` or a string:

```lua
function my_function(sqltext)
    ...
end
```

Rules, by phase:

| Phase | Nil return meaning | Must return a string? |
|---|---|---|
| `TRANSLATE` | Not applicable | **Always** — return the input unchanged on any path where you don't transform it |
| `EXPAND` | "Not my command, keep scanning" | Only when it matches (terminal for the phase) |
| `REWRITE` | Not applicable | **Always** — return the input unchanged on any path where you don't transform it |

Read the full phase semantics (run-all vs. first-match-terminal, fail-open
posture per phase) in
[how-it-works.md § Phase pipeline](how-it-works.md#phase-pipeline).

**No defensive `pcall`.** A module must not wrap its own body in `pcall`.
MASTER's dispatch-level `pcall` is the framework's only error boundary — it
skips a throwing rule and continues (fail-open for EXPAND/REWRITE,
fail-closed-by-return for TRANSLATE, see
[how-it-works.md § Fail-open safety contract](how-it-works.md#fail-open-safety-contract)).
A module-level `pcall` converts a visible fault into a silent wrong answer and
is a contract violation, not a defensive improvement.

Two authoring traps worth restating from
[how-it-works.md § Module authoring contract](how-it-works.md#module-authoring-contract):
functions must be plain top-level globals (a `local` function is invisible to
`import`), and `string.gsub` returns two values — never forward its return
directly.

## The canonical artifact

The canonical artifact of a module is **one self-contained `.sql` file that
IS the module**, carrying **one or more** statements:

```sql
CREATE OR REPLACE LUA SCRIPT PREPROC_RT.<NAME>_V<N> AS
<lua body>
```

Exactly one of those statements MUST be the `CREATE OR REPLACE LUA SCRIPT
<script_name>` that defines the module's entry script — the function the
registered rule calls. The rest MAY create any other object the module needs:
further Lua scripts, Python UDFs, tables, views. A ten-line transform has an
artifact of N=1 — still the common case, and it needs no manifest change to
stay conformant. A module that is the front door to a whole subsystem can now
ship its Python UDFs and tables in the same artifact, instead of being pushed
into `self-deployed` for the sole reason that it has more than one object to
deliver.

Nothing else belongs in that file — no `OPEN SCHEMA`, no registration
statement. Registration (the rule) is a separate step, issued by the
operator's CLI or by hand — never baked into the artifact.

Statements are separated using the EXAplus block-marker convention the
framework's own `release.sql` already emits: a statement whose body contains
semicolons (a Lua or Python script body, typically) is wrapped between a line
consisting solely of `--/` and a line consisting solely of `/`; any other
statement is terminated by `;`. Neither marker line, nor the terminating `;`,
is part of the parsed statement text:

```sql
--/
CREATE OR REPLACE LUA SCRIPT PREPROC_RT.<NAME>_V<N> AS
function <fn>(sqltext)
    return sqltext;
end
/

CREATE OR REPLACE PYTHON3 SCALAR SCRIPT PREPROC_RT.<HELPER>_V<N>(...)
RETURNS ... AS
def run(ctx):
    ...
;
```

This is what keeps the artifact directly runnable by hand in EXAplus or
DbVisualiser — the actual reason it survives an air-gapped hand-carry. An
earlier version of this document justified the one-statement rule itself as
"trivially hand-carryable ... and trivially hashed"; that reasoning does not
hold up, because a multi-statement file is exactly as hand-carryable and
`sha256` (see below) hashes the whole file byte-for-byte regardless of
statement count. The one-statement rule this replaces was never really about
air-gap or hashing — it was a mechanical consequence of the in-database
installer executing the whole artifact through a call that only accepted one
statement. That limitation is gone: the installer now splits the artifact and
installs every statement in file order.

Which objects the artifact creates is not left to be inferred by parsing
alone: `module.toml` declares an `[[objects]]` array naming every object, and
tooling independently parses the artifact to derive the same list — the
module is rejected when the two disagree in **either** direction, exactly the
same accept/reject discipline already applied to `sha256`. See
[`module.toml` schema](#moduletoml-schema) for the field and
[Validation](#validation) for the function that checks it. `[[objects]]` MAY
be omitted when the artifact carries exactly one statement — the inventory is
then derived as the single entry named by `script_name`, which is exactly
what every existing single-statement module already satisfies — and is
REQUIRED once the artifact carries more than one statement.

The version suffix `_V<N>` is **mandatory** on **every** object the artifact
creates — the entry script and every companion — not only the entry script,
both in the deployed object name and (for the entry script) in the artifact's
filename. It exists to enforce the never-`CREATE OR REPLACE`-a-live-module
discipline: a new behaviour ships as `_V<N+1>` objects, never by replacing a
live `_V<N>` object in place, so a v1 and a v2 generation coexist with zero
collisions — even a companion whose body is byte-identical between the two
generations is redeployed under the new versioned name. The entry script MUST
reference its companions by their versioned names. See
[Versioning discipline](#versioning-discipline) below.

An artifact MAY create objects in any schema the installing admin can write —
it is not restricted to `PREPROC_RT`/`PREPROC`. Installing a
`library-deployed` module is therefore arbitrary DDL executed under the
`PREPROC_ADMIN` boundary; what makes that reviewable is that the object
inventory is declared before install and surfaced by
`PREPROC.MODULE_CATALOG`, so an operator sees exactly which objects an
install would create before running it.

## Standard layout

Each module lives under:

```
modules/<name>/
├── <name>_v<N>.sql   # the canonical artifact
├── module.toml        # the contract manifest
├── README.md          # what it does, how to use it
└── tests/              # module-level tests
```

`<name>` is `snake_case` and must be identifier-safe (a letter, then
letters/digits/underscores — no hyphens) so it can be used unquoted in
`PREPROC INSTALL MODULE <name>`; e.g. `cast_shorthand`. The deployed script name
is `PREPROC_RT.<NAME>_V<N>`, where `<NAME>` is the upper-case form of `<name>`
(e.g. `PREPROC_RT.CAST_SHORTHAND_V1`).

## `module.toml` schema

| Field | Required | Type | Notes |
|---|---|---|---|
| `name` | Yes | string | `snake_case` module name — must be identifier-safe (a letter, then letters/digits/underscores; NO hyphens) so it works unquoted in `PREPROC INSTALL MODULE <name>`; matched case-insensitively; matches the `modules/<name>/` directory |
| `description` | Yes | string | one-line human summary |
| `phase` | Yes | string | one of `TRANSLATE`, `EXPAND`, `REWRITE` |
| `script_name` | Yes | string | fully qualified, e.g. `PREPROC_RT.CAST_SHORTHAND_V1`; must end in `_V<N>` |
| `function` | Yes | string | exported Lua function name, e.g. `cast_shorthand` |
| `version` | Yes | integer | MUST equal the `_V<N>` suffix in `script_name` (and in the artifact filename) |
| `min_framework` | Yes | string | semantic version of the lowest framework release the module supports |
| `suggested_scope` | Yes | table | `{ type = "ROLE" \| "USER", value = "<name>" }` — a default scope suggestion for `PREPROC ADD RULE`; the operator may deploy with a different scope |
| `deploy_mode` | Yes | string | `library-deployed` or `self-deployed` |
| `sha256` | Conditional | string | hex digest of the artifact `.sql` file's bytes. **Required** when `deploy_mode = "library-deployed"`. Ignored (not required) for `self-deployed` |
| `source` | Conditional | table | `{ repo = "...", ref = "..." }` — the module's own repo and pinned ref. **Required** when `deploy_mode = "self-deployed"` |
| `objects` | Conditional | array of tables | `[[objects]]`, one entry per object the artifact creates: `{ type = "...", name = "..." }`, e.g. `{ type = "LUA SCRIPT", name = "PREPROC_RT.CONTROL_DATABASE_V1" }`. Every declared `name` MUST carry the module's `_V<N>` suffix. **MAY be omitted** when the artifact carries exactly one statement — the inventory is then derived as the single entry named by `script_name`. **REQUIRED** once the artifact carries more than one statement |

`phase`, the `_V<N>`/`version` agreement, the `deploy_mode` field rules, and
the `[[objects]]` inventory are all validated by
`src/preproc/module/manifest.py` — see [Validation](#validation) below.

### Example: a library-deployed TRANSLATE module

```toml
name = "cast_shorthand"
description = "Rewrites PostgreSQL-style expr::type cast shorthand to CAST(expr AS type)."
phase = "TRANSLATE"
script_name = "PREPROC_RT.CAST_SHORTHAND_V1"
function = "cast_shorthand"
version = 1
min_framework = "1.0.0"
sha256 = "…64 hex chars…"
deploy_mode = "library-deployed"

[suggested_scope]
type = "ROLE"
value = "PREPROC_ADMIN"
```

### Example: a self-deployed module (front door to another subsystem)

```toml
name = "semantic_views"
description = "Registers the semantic_views EXPAND rule; the subsystem installs its own script."
phase = "EXPAND"
script_name = "SEMANTIC_VIEWS_RT.ENTRY_V3"
function = "expand"
version = 3
min_framework = "1.0.0"
deploy_mode = "self-deployed"

[suggested_scope]
type = "ROLE"
value = "SEMANTIC_VIEWS_USER"

[source]
repo = "exasol/semantic-views"
ref = "v3.0.0"
```

A `self-deployed` module's own installer deploys `SEMANTIC_VIEWS_RT.ENTRY_V3`;
the framework CLI only registers the rule (`RULE_ENSURE`) and writes the
provenance row — it never fetches or deploys code for a `self-deployed`
module.

### Example: a composite library-deployed module (Lua entry + Python UDF)

A module that is the front door to a subsystem can ship its Lua entry script
and its Python companions in one artifact and stay `library-deployed`. The
manifest declares the full inventory:

```toml
name = "control_database"
description = "Front door to the confd subsystem: expands control-database directives."
phase = "EXPAND"
script_name = "PREPROC_RT.CONTROL_DATABASE_V1"
function = "control_database"
version = 1
min_framework = "1.0.0"
sha256 = "…64 hex chars, over the whole composite artifact…"
deploy_mode = "library-deployed"

[suggested_scope]
type = "ROLE"
value = "PREPROC_ADMIN"

[[objects]]
type = "LUA SCRIPT"
name = "PREPROC_RT.CONTROL_DATABASE_V1"

[[objects]]
type = "PYTHON3 SCALAR SCRIPT"
name = "PREPROC_RT.CONFD_JOB_V1"
```

and the matching artifact — `control_database_v1.sql` — carries both
statements, each wrapped in `--/` … `/` because each body contains a
semicolon:

```sql
--/
CREATE OR REPLACE LUA SCRIPT PREPROC_RT.CONTROL_DATABASE_V1 AS
function control_database(sqltext)
    return sqltext;
end
/

--/
CREATE OR REPLACE PYTHON3 SCALAR SCRIPT PREPROC_RT.CONFD_JOB_V1(p VARCHAR(100))
RETURNS VARCHAR(100) AS
def run(ctx):
    return ctx.p;
/
```

The entry script is the object named by `script_name`; the Python UDF is a
companion the entry script calls at runtime by its versioned name,
`PREPROC_RT.CONFD_JOB_V1`. Both objects carry `_V1`, and a v2 of this module
would ship both under `_V2`, even if the UDF's body did not change.

## Deploy modes

`deploy_mode` is deployment convenience, not a second contract. There is no
third mode for a composite artifact — a subsystem module (Lua entry script
plus Python UDFs plus tables) is simply `library-deployed` with more than one
declared object:

- **`library-deployed`** — the module ships its whole body — every object
  its artifact creates, not only the entry script — in the one `.sql`
  artifact. The manifest MUST declare `sha256` (the hex digest of the whole
  artifact's bytes, unaffected by how many statements it carries) so the
  operator's CLI can verify integrity before deploying — the database itself
  never fetches or verifies anything; the CLI machine is the trust boundary.
  Before this contract generalised to a composite artifact, a subsystem
  module with a UDF or a table was silently forced into `self-deployed`,
  where the library could only carry a pointer and never actually deliver
  the code; that restriction is gone.
- **`self-deployed`** — the module is installed by its own project's
  installer; the framework CLI only registers its rule and records
  provenance. The manifest MUST declare `source` (`repo` and `ref`) so an
  operator can find and pin the installer; `sha256` is not required. This mode
  is unchanged by the composite artifact.

## Versioning discipline

Modules are versioned objects, exactly like the framework's own admin
scripts. **Never `CREATE OR REPLACE` a live module.** To ship a behaviour
change:

1. Author the new behaviour as `<NAME>_V<N+1>` — a new script, a new
   `module.toml` with `version = N+1`, a new artifact file. Every object the
   new artifact creates carries `_V<N+1>`, not only the entry script: a
   companion redeploys under the new versioned name even when its body is
   byte-identical to the previous generation's, and the new entry script
   references its companions by their new versioned names.
2. Deploy it alongside the old version (both generations exist
   simultaneously — every `_V<N>` object and every `_V<N+1>` object are live
   at once).
3. Repoint the `PREPROC.CONFIG` rule to the new `script_name` / `function`.
4. Run `PREPROC.REFRESH`.
5. Retire `<NAME>_V<N>` once no enabled rule and no live profile manifest
   references it, then reclaim it — every object recorded in its inventory is
   dropped, in the reverse of the order the artifact created them.

This mirrors the framework's own profile generate → retire → reclaim
lifecycle (see
[how-it-works.md § Profile cleanup](how-it-works.md#profile-cleanup-retire-then-reclaim)):
a version is never dropped while something could still be pointing at it.

## Validation

`src/preproc/module/manifest.py` is the reference validator and the API
downstream tooling (the registry reader, the `preproc module` CLI) builds on:

- `load_manifest(path)` / `parse_manifest(data)` — parse and validate a
  `module.toml`, raising `ManifestError` (with every violation collected, not
  just the first) when the contract is not met. This is also where a declared
  `[[objects]]` entry is checked: its `type` and fully-qualified `name`, and
  that the name carries the module's `_V<N>` suffix.
- `verify_artifact_inventory(manifest, artifact_text) -> list[ObjectSpec]` —
  splits the artifact into its statements (`src/preproc/module/artifact.py`'s
  `split_statements`), derives the object each statement creates
  (`derive_objects`), and confirms the derived inventory agrees with the
  manifest's declared `[[objects]]` in **both** directions — an object the
  artifact creates but the manifest doesn't declare, or vice versa, is a
  rejection either way. It also confirms exactly one derived object is the
  `LUA SCRIPT` named by `script_name`. Returns the derived inventory, in file
  order, on success; raises `ArtifactError` naming every disagreement
  otherwise.
- `verify_artifact_sha256(manifest, artifact_bytes)` — for a
  `library-deployed` module, confirms the artifact's bytes hash to the
  manifest's declared `sha256`; the digest covers the whole file regardless of
  how many statements it carries.

See `tests/test_module_contract.py` for worked examples of every accept/reject
scenario.

## Air-gap posture

The manifest carries everything the operator's CLI machine needs to fetch,
verify, and deploy or register a module. The database itself never fetches
anything over the network — it only ever receives SQL statements issued by
the CLI (or hand-carried via an offline bundle). This mirrors the framework's
own air-gap invariant for `release.sql`.
