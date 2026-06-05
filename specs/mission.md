# Mission: preprocessor-framework

> A customer-deployable framework that turns Exasol's single global SQL preprocessor into a modular, role- and client-aware transformation pipeline — dispatching per-user function sets from a config-driven lookup with measured, minimal overhead.

*(Working title — product name to be decided before any public release.)*

## Problem Statement

Exasol's SQL preprocessor is one global Lua script per system (or session). Customers who want per-role syntactic sugar, per-client dialect translation, and custom administrative commands face two distinct gaps:

1. **Execution mechanism** — only one script can run. Composing multiple independent transformations means hand-writing a monolith: unmaintainable, untestable, wasteful for users who need no preprocessing.
2. **Decision layer** — nothing in the database decides *which* transformations apply to *which* user, role, or connecting client, keeps that configuration fresh as roles change, or makes the result observable and operable.

This framework provides the decision layer and, today, emulates composition via function dispatch inside a MASTER script. The native "Chained Preprocessors" roadmap feature addresses gap 1 only — when it lands, the framework's dispatch mechanism gets simpler and more valuable (assign resolved script chains directly instead of dispatching functions), but the decision layer it exists for remains. The framework's measurements additionally serve as evidence for what the core feature should deliver (non-overridable scoped assignment, zero-lookup dispatch, warm UDF VM reuse).

Who experiences it: Exasol customers running mixed workloads — BI users connecting with foreign-dialect tools (e.g. Tableau generating Databricks SQL), analysts wanting SQL conveniences, operators wanting custom commands — all on one database.

## Target Users

| Persona | Goal | Key Workflow |
|---------|------|--------------|
| DBA / platform operator | Give different user groups different SQL transformations without writing Lua | Install via CLI; manage rules in config tables scoped by role/user/client; run `refresh` after role changes |
| BI / analytics end user | Run their tool's native SQL dialect or use SQL sugar, unknowingly | Connect and query; the pipeline transforms statements transparently |
| Module author (SE / consultant / power DBA) | Add a custom transformation for one customer scenario | Write one Lua module function following authoring rules; register it in config; deploy versioned |
| Exasol PM / engineering | Quantify what native Chained Preprocessors must deliver | Read measured overhead numbers and workaround inventory from this repo |

## Core Capabilities

1. **Modular dispatch** — a MASTER preprocessor resolves which transformation functions apply to the current user/client via one catalog lookup and runs only those, in a fixed phase order (TRANSLATE → EXPAND → REWRITE).
2. **Scoped configuration** — rules defined per role, user, or connecting client; role→user expansion (incl. nested roles) is flattened offline into a resolution table; users see only their own pipeline through a CURRENT_USER-filtered view.
3. **Session-level zero-overhead paths** — in convenience mode, no-rule sessions self-disable and rule-bearing sessions swap to generated, hash-named profile scripts with their function set baked in (+0.5 ms/stmt instead of +7.3 ms).
4. **Dialect translation** — statements from foreign-dialect clients (e.g. Databricks SQL) are transpiled to Exasol SQL via a sqlglot Python UDF, AST-based, fail-closed.
5. **Language extension (EXPAND)** — non-standard syntax that is *not* valid Exasol SQL — whole-statement custom commands (e.g. a backup-start command or an `LS` command; exact syntax to be specified by the product owner) expanded to `EXECUTE SCRIPT` helpers (first match terminal), and token-level conveniences (commutative, stackable) — is transformed into runnable Exasol SQL. Capability for command expansion is gated by ordinary grants on the helper.
6. **Self-administration** — install, refresh, status, and explain operations available as CLI verbs and as framework-intercepted SQL commands (`EXPLAIN PREPROCESSOR …`, `SHOW PREPROCESSOR PIPELINE`).

## Out of Scope

- **Security/policy enforcement in convenience mode** — users can `ALTER SESSION` the preprocessor away; the framework is a convenience layer unless the instance runs enforced mode. It is never an RLS/CLS substitute.
- **Anything requiring core engine changes** — optimizer/execution directives, catalog storage gaps, general type-coercion rules, persistent concurrency-safe objects, schedulers.
- **A scheduler** — periodic refresh is delegated to external cron/CI; the framework ships the refresh entry point only.
- **GUI / web administration** — CLI and SQL only.
- **Exasol versions before 2025.x** — older default UDF containers (sqlglot availability) and parameter behaviors are not supported or tested.
- **Per-statement audit logging of transformations** — `EXPLAIN PREPROCESSOR` is the inspection mechanism; no compile-time log-table writes (transaction-conflict hazard).

## Domain Glossary

| Term | Definition |
|------|------------|
| Preprocessor | Exasol's compile-time Lua hook (`sql_preprocessor_script`) that rewrites statement text before compilation |
| MASTER | The dispatcher preprocessor script; resolves and runs the user's pipeline |
| Module | A Lua script exporting transformation function(s); the unit of authoring, versioning, and registration |
| Phase | Fixed pipeline stage, cut along "was the input already valid Exasol SQL?": TRANSLATE (foreign-dialect statements transpiled to Exasol SQL; max 1/user; fail-closed) → EXPAND (input is NOT valid Exasol SQL — language extensions that make new syntax runnable: whole-statement custom commands, first-match-terminal, plus commutative token conveniences; fail-open is inherently safe — an un-expanded statement dies in the engine with a loud syntax error) → REWRITE (input IS valid Exasol SQL — transforms that modify its behaviour: policy/filter injection, table redirection, guardrails; currently a defined-but-empty slot; fail-open-SENSITIVE because an un-applied REWRITE silently runs the original = policy bypass, so its contract must be designed fail-closed-capable from day one) |
| Resolution table | Flattened runtime config: (user, client_pattern, phase, script, function); rebuilt by refresh, never edited directly |
| Profile script | Generated preprocessor `P_<hash>` with a rule-set baked in; hash is a verifiable manifest over the ordered `module@version.function` list |
| Convenience mode | Default deployment: session-level preprocessor changes allowed → self-disable and profile swapping work; users can opt out |
| Enforced mode | `-alterSystemProhibitsAlterSessionForPreprocessing=1`: no session overrides → real boundary, but every statement pays the lookup (~7 ms) |
| Refresh | Offline job: role→user flattening, resolution rebuild, profile regeneration, settings sync (incl. mode detection from `EXA_SYSTEM_EVENTS`) |
| Runtime schema (`PREPROC_RT`) | PUBLIC-accessible: MASTER, modules, profiles, filtered views. Schema-level EXECUTE/SELECT grants cover future objects |
| Admin schema (`PREPROC`) | Locked: config/resolution/settings/profiles tables + refresh/generator scripts; PREPROC_ADMIN only |

---

## Tech Stack

| Layer | Technology | Purpose |
|-------|------------|---------|
| In-database | Exasol Lua scripting + `sqlparsing` | MASTER, modules, profile scripts (invoker rights — drives the two-schema design) |
| Translation | Python 3 scalar UDF + sqlglot (≥27.x, in default container, native `exasol` dialect) | AST-based dialect transpilation |
| DDL | Plain SQL files (source of truth) | Auditable install/upgrade artifacts |
| CLI / orchestration | Python 3.12, pyexasol | Thin CLI wrapping the SQL: `install / refresh / status / explain` |
| Packaging / env | uv | Dependency and environment management |
| Testing | pytest + pyexasol fixtures against Exasol Docker (`exasol/docker-db`) | Integration tests are the primary tier; CI uses Docker, local runs may target any instance via env vars |
| Lua lint | luacheck | Static checks for modules and MASTER |
| Target platform | Exasol 2025.x+ | Verified on 2025.2.0 |

## Commands

```bash
# Environment
uv sync

# Test (integration; spins up / targets DB per EXASOL_DSN env)
uv run pytest

# Lint & Format
uv run ruff check . && uv run ruff format --check .
luacheck sql/lua/

# Coverage
uv run pytest --cov=preproc --cov-report=term-missing
```

## Project Structure

```
preprocessor-framework/
├── specs/              # speq mission + feature specs
├── sql/                # source of truth, auditable by DBAs
│   ├── install/        # numbered DDL: schemas, tables, views, grants
│   ├── lua/            # MASTER + module scripts (deployed via CLI)
│   └── udf/            # TRANSPILE and other UDF definitions
├── src/preproc/        # Python CLI (install/refresh/status/explain) + profile codegen
├── tests/              # pytest integration suite
└── docs/               # operator guide, module authoring rules, mode trade-offs
```

## Architecture

Two-schema, config-driven dispatch (full design rationale and measurements: `Projects/Preprocessor Framework/Preprocessor Framework.md` in the owner's vault — key content to be ported into `docs/`):

- **Runtime schema `PREPROC_RT`** (PUBLIC): MASTER dispatcher, module scripts, generated profile scripts, CURRENT_USER-filtered views. Views are the privilege bridge — they read admin tables with owner rights (scripts cannot; invoker-rights verified).
- **Admin schema `PREPROC`** (locked): config, resolution, settings, profiles tables; refresh + generator scripts.
- **Data flow**: statement → MASTER → one pquery on the filtered view → phase pipeline (TRANSLATE fail-closed → EXPAND fail-open-safe → REWRITE fail-open-sensitive, skip-on-failure subject to per-phase/per-rule contract) → `setsqltext`. EXPAND must precede REWRITE so behaviour-modifying rules always operate on valid, fully-expanded SQL. Convenience mode short-circuits: first statement swaps the session to a profile script or disables outright.
- **Refresh flow**: CLI or intercepted command → role flattening (nested roles) → resolution rebuild → profile codegen (hash-manifest naming, orphan drop) → settings sync — all transactional, so rollout/rollback is a config flip.
- **Deployment discipline**: modules are versioned objects (`NAME_V2` alongside `NAME_V1`); never `CREATE OR REPLACE` a live module; canary via scoping rules.

## Constraints

- **Technical**: no session-persistent Lua state (drives the whole profile/self-disable design); scripts are invoker-rights; Lua scripting has no filesystem/BucketFS access; preprocessor output must remain a single statement; `gsub`-style multi-return Lua pitfalls in module authoring.
- **Operational**: a user lacking EXECUTE on an active preprocessor cannot run *any* statement — schema-level PUBLIC grants are a framework invariant verified by `status`; config-table writes must stay rare and fast (transaction-conflict surface).
- **Performance budgets (measured, Exasol 2025.2.0)**: profile script +0.5 ms/stmt; MASTER lookup +7.3 ms/stmt (enforced-mode floor); TRANSLATE via UDF +133 ms/stmt (no warm VM reuse — scope it narrowly via client_pattern).
- **Business**: customer-deployable quality — versioned releases, upgrade path, operator documentation; MIT licensed (Exasol Labs).

## External Dependencies

| Service | Purpose | Failure Impact |
|---------|---------|----------------|
| Exasol instance (2025.x+) | The runtime; everything executes in-database | Framework inert; nothing to install or test against |
| sqlglot in default UDF container | TRANSLATE phase transpilation | TRANSLATE rules fail closed (statements error); other phases unaffected |
| `exasol/docker-db` image | CI integration tests | CI blocked; local tests can target a live instance via env vars |
| External scheduler (customer's cron/CI) | Periodic refresh safety net | Resolution staleness until manual/runbook refresh; grant-lag failures surface for new users |
