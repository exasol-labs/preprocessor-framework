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
IS the module**: exactly one statement,

```sql
CREATE OR REPLACE LUA SCRIPT PREPROC_RT.<NAME>_V<N> AS
<lua body>
```

Nothing else belongs in that file — no `OPEN SCHEMA`, no registration
statement, no companion objects. One auditable file mirrors the engine's own
`release.sql` philosophy: it is trivially hand-carryable for air-gapped
deployment and trivially hashed for integrity verification (see `sha256`
below). Registration (the rule) is a separate step, issued by the operator's
CLI or by hand — never baked into the artifact.

The version suffix `_V<N>` is **mandatory**, both in the deployed script name
and in the artifact's filename. It exists to enforce the
never-`CREATE OR REPLACE`-a-live-module discipline: a new behaviour ships as
`_V<N+1>`, never by replacing a live `_V<N>` in place. See
[Versioning discipline](#versioning-discipline) below.

## Standard layout

Each module lives under:

```
modules/<name>/
├── <name>_v<N>.sql   # the canonical artifact
├── module.toml        # the contract manifest
├── README.md          # what it does, how to use it
└── tests/              # module-level tests
```

`<name>` is kebab-case (e.g. `cast-shorthand`). The deployed script name is
`PREPROC_RT.<NAME>_V<N>`, where `<NAME>` is the upper-snake-case form of
`<name>` (e.g. `PREPROC_RT.CAST_SHORTHAND_V1`).

## `module.toml` schema

| Field | Required | Type | Notes |
|---|---|---|---|
| `name` | Yes | string | kebab-case module name, matches the `modules/<name>/` directory |
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

`phase`, the `_V<N>`/`version` agreement, and the `deploy_mode` field rules
are all validated by `src/preproc/module/manifest.py` — see
[Validation](#validation) below.

### Example: a library-deployed TRANSLATE module

```toml
name = "cast-shorthand"
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
name = "semantic-views"
description = "Registers the semantic-views EXPAND rule; the subsystem installs its own script."
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

## Deploy modes

`deploy_mode` is deployment convenience, not a second contract:

- **`library-deployed`** — the module ships its whole Lua body in the one
  `.sql` artifact. The manifest MUST declare `sha256` (the hex digest of that
  artifact's bytes) so the operator's CLI can verify integrity before
  deploying — the database itself never fetches or verifies anything; the CLI
  machine is the trust boundary.
- **`self-deployed`** — the module is installed by its own project's
  installer; the framework CLI only registers its rule and records
  provenance. The manifest MUST declare `source` (`repo` and `ref`) so an
  operator can find and pin the installer; `sha256` is not required.

## Versioning discipline

Modules are versioned objects, exactly like the framework's own admin
scripts. **Never `CREATE OR REPLACE` a live module.** To ship a behaviour
change:

1. Author the new behaviour as `<NAME>_V<N+1>` — a new script, a new
   `module.toml` with `version = N+1`, a new artifact file.
2. Deploy it alongside the old version (both scripts exist simultaneously).
3. Repoint the `PREPROC.CONFIG` rule to the new `script_name` / `function`.
4. Run `PREPROC.REFRESH`.
5. Retire `<NAME>_V<N>` once no enabled rule and no live profile manifest
   references it.

This mirrors the framework's own profile generate → retire → reclaim
lifecycle (see
[how-it-works.md § Profile cleanup](how-it-works.md#profile-cleanup-retire-then-reclaim)):
a version is never dropped while something could still be pointing at it.

## Validation

`src/preproc/module/manifest.py` is the reference validator and the API
downstream tooling (the registry reader, the `preproc module` CLI) builds on:

- `load_manifest(path)` / `parse_manifest(data)` — parse and validate a
  `module.toml`, raising `ManifestError` (with every violation collected, not
  just the first) when the contract is not met.
- `verify_artifact_script_name(manifest, artifact_text)` — confirms the
  artifact is exactly one `CREATE OR REPLACE LUA SCRIPT` statement and that
  its script name matches the manifest.
- `verify_artifact_sha256(manifest, artifact_bytes)` — for a
  `library-deployed` module, confirms the artifact's bytes hash to the
  manifest's declared `sha256`.

See `tests/test_module_contract.py` for worked examples of every accept/reject
scenario.

## Air-gap posture

The manifest carries everything the operator's CLI machine needs to fetch,
verify, and deploy or register a module. The database itself never fetches
anything over the network — it only ever receives SQL statements issued by
the CLI (or hand-carried via an offline bundle). This mirrors the framework's
own air-gap invariant for `release.sql`.
