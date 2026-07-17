# Decision Log: add-module-ecosystem

Date: 2026-07-17

## Interview

**Q:** What is the strategic framing — why a module ecosystem at all?
**A:** Exasol has exactly ONE `sql_preprocessor_script` slot (one system-wide, one
per session). Every preprocessor project today grabs it — this framework sets
`PREPROC_RT.MASTER` and self-swaps the session slot in convenience mode; others
(exasol-semantic-views) clear and set it to their own script. Two independent
preprocessor projects therefore cannot coexist: last writer wins. The framework's
core value is being the SOLE slot-owner that MULTIPLEXES/dispatches to every
registered syntax provider. The module ecosystem operationalizes that and gives
anyone an easy, scalable way to hook in.

**Q:** One integration contract or two ("bundled module" vs "subsystem")?
**A:** ONE contract. The earlier two-kinds split is explicitly rejected. A module
= a preprocessor function `fn(text) -> nil|string` in some Lua script + a
registration rule (scope, phase, script_name, function) via the existing
`PREPROC.RULE_ADD` / `PREPROC ADD RULE` surface. MASTER dispatches. The framework
doesn't care what is behind the script — a tiny transform or a whole subsystem
with its own catalog; the backing install is the author's concern.

**Q:** How does deployment fit the contract?
**A:** Deployment is orthogonal convenience, NOT part of the contract. A single
self-contained `.sql` module can be library/CLI-deployed (`preproc module add
<name>`); a project with its own installer self-deploys and then just registers
the rule. A `module.toml` field (`deploy_mode`) records which. It is not a
different concept.

**Q:** How many repos, and what does this plan own?
**A:** Two repos: `preprocessor-framework` (this repo — engine + CLI +
contract/spec) and a NEW central library repo (created separately, in parallel).
The plan must specify the library repo's full initial scaffolding as deliverables.

**Q:** What is a module's canonical artifact and layout?
**A:** ONE self-contained `.sql` file that IS the module (`CREATE OR REPLACE LUA
SCRIPT PREPROC_RT.<NAME>_V<N> AS <lua body>`). Standard layout:
`modules/<name>/{<name>_v<N>.sql, README.md, module.toml, tests/}`.

**Q:** What fields does module.toml carry?
**A:** At least: name, description, phase, script_name, function, version,
min_framework, suggested_scope (type+value), deploy_mode
(library-deployed | self-deployed); for self-deployed/external: source repo +
ref; for library-deployed artifacts: sha256.

**Q:** What is the registry?
**A:** A FEDERATED index over multiple sources: central-library modules
auto-indexed from their module.toml (CI-generated), plus external repos. It is
the curation/trust boundary — pin by tag, verify sha256.

**Q:** How do external repos get registered?
**A:** Option A: curated PR-an-entry (one block: repo@tag + sha + manifest
metadata; human review is the trust gate). NOT automated crawling.

**Q:** What about air-gapped customer databases?
**A:** Air-gap is a HARD constraint: many customer DBs have no internet egress.
The DB must NEVER fetch code. The operator's CLI machine is the bridge (talks to
GitHub and to the DB over SQL). Offline path: `preproc module bundle <names...>`
emits a self-contained `.sql` (same pattern as the engine's release.sql) to
hand-carry in.

**Q:** How is "update a module" handled?
**A:** It MUST honour the never-mutate-a-live-module discipline (ADR-019): deploy
`_V<N+1>` → repoint the CONFIG rule → retire `_V<N>`. Mirror the generational
profile retire/reclaim machinery in `sql/lua/refresh_core.lua`
(generate/retire/reclaim, never-same-refresh-drop).

**Q:** Where does fetch/deploy run?
**A:** In the Python CLI/CI, NOT the DB (matches ADR-006/007: CLI is
deployment-only, the DB is the management surface).

**Q:** Does the framework's own syntactic sugar become a module?
**A:** No. `control_sugar_v1` stays in the framework repo — it is engine
infrastructure, not a module.

**Q:** Which module is module #1?
**A:** (Correction, supersedes any earlier note.) Module #1 = the SQL-ergonomics
preprocessors — `cast-shorthand` and `trailing-comma`. Do NOT touch confd in this
plan at all — no confd migration, no confd module, leave it entirely alone. The
demo modules (demo_ping/translate/audit) are framework TEST FIXTURES and stay in
the framework repo.

**Q:** Is the semantic-views integration in scope?
**A:** No — context only. The contract/manifest MUST accommodate a "self-deployed
+ register-only" module so semantic-views could hook in later, but the actual
integration needs their team and is a documented out-of-scope follow-on spike.

## Design Decisions

### [1] Framework as the single-slot multiplexer (ADR-039)

- **Decision:** Position the framework as the sole owner of Exasol's one
  preprocessor slot; the module ecosystem is how every other syntax provider
  multiplexes through MASTER instead of fighting for the slot.
- **Alternatives:** Let each project keep grabbing the slot (status quo —
  projects cannot coexist); a per-session convention (still last-writer-wins).
- **Rationale:** Only one dispatcher can occupy the slot; making it a shared,
  extensible multiplexer is the framework's core value proposition.
- **Promotes to ADR:** yes

### [2] One integration contract, not two (ADR-040)

- **Decision:** A module is always `fn(text) -> nil|string` in a Lua script +
  a registration rule + a `module.toml`. No "bundled vs subsystem" split.
- **Alternatives:** Two contracts (a lightweight bundled kind and a heavyweight
  subsystem kind) — explicitly rejected by the owner.
- **Rationale:** The dispatcher already treats every rule uniformly; what is
  behind the script is the author's concern. One surface is smaller to learn,
  document, and keep stable.
- **Promotes to ADR:** yes

### [3] Deploy mode is orthogonal metadata (ADR-041)

- **Decision:** `deploy_mode` (`library-deployed` | `self-deployed`) is a
  `module.toml` field describing how the artifact gets into the DB, not a second
  contract. Self-deployed modules are register-only for the CLI.
- **Alternatives:** Encode deployment differences as distinct module types.
- **Rationale:** Keeps the contract single; lets a subsystem (its own installer)
  and a one-file module share the exact same registration and dispatch path. This
  is what makes a future semantic-views hook-in a register-only case.
- **Promotes to ADR:** yes

### [4] Canonical artifact = one self-contained .sql; standard layout (ADR-042)

- **Decision:** The module is one `CREATE OR REPLACE LUA SCRIPT
  PREPROC_RT.<NAME>_V<N>` file under `modules/<name>/{<name>_v<N>.sql,
  module.toml, README.md, tests/}`.
- **Alternatives:** Multi-file artifacts; a packaged archive format.
- **Rationale:** One auditable file mirrors the engine's `release.sql`
  philosophy, is trivially hand-carryable for air-gap, and is easy to hash and
  verify.
- **Promotes to ADR:** yes

### [5] Federated registry with curated PR-an-entry (Option A) (ADR-043)

- **Decision:** A generated federated index over central-library modules
  (auto-indexed) and external repos (one curated `registry/external/<name>.toml`
  block per module, human-reviewed). Pin by tag; verify sha256. No crawling.
- **Alternatives:** Automated discovery/crawling of repos; a fully manual list.
- **Rationale:** Human review is the trust gate; auto-indexing keeps the library
  effortless; pinning + sha make a registry state reproducible and tamper-evident.
- **Promotes to ADR:** yes

### [6] Registry hosted in the central library repo (ADR-044)

- **Decision:** The registry index lives in the central library repo
  (`registry/index.json`, CI-generated), not in the framework repo and not in a
  separate dedicated registry repo (yet).
- **Alternatives:** A file in the framework repo; a dedicated
  `preprocessor-registry` repo.
- **Rationale:** Library CI already has all manifests locally, so auto-indexing
  is trivial and always in sync; framework hosting would couple engine releases
  to registry churn and invert ownership; a dedicated repo adds a third repo and
  cross-repo sync for no MVP benefit. The index format is repo-agnostic, so a
  future split is a lift-and-shift.
- **Promotes to ADR:** yes

### [7] Air-gap discipline — the DB never fetches; the CLI is the bridge (ADR-045)

- **Decision:** All fetch/verify/deploy happens on the operator's CLI machine;
  the database only receives SQL. `preproc module bundle` emits a self-contained
  `.sql` for no-egress databases; `--registry <path>` reads a local checkout.
- **Alternatives:** DB-side fetch (e.g. a UDF pulling from GitHub).
- **Rationale:** Many customer DBs have no internet egress (hard constraint); the
  DB as a management-only surface matches ADR-006/007.
- **Promotes to ADR:** yes

### [8] Module V<N> lifecycle mirrors profile generate/retire/reclaim (ADR-046)

- **Decision:** Update is deploy `_V<N+1>` → repoint the CONFIG rule → refresh →
  mark-retire `_V<N>` → guarded reclaim (drop only when no enabled rule and no
  non-orphaned PROFILES manifest reference it). Provenance is recorded in
  `PREPROC.MODULES` with a PUBLIC-readable `PREPROC_RT.INSTALLED_MODULES` view.
- **Alternatives:** `CREATE OR REPLACE` a live version (violates ADR-019);
  immediate drop on retire (catastrophic for pinned sessions, mirrors the
  never-same-refresh-drop hazard of ADR-022).
- **Rationale:** A convenience-mode session can be pinned to a profile that baked
  the old version; dropping it out from under that session breaks dispatch. The
  profile machinery already solved exactly this generational problem.
- **Promotes to ADR:** yes

### [9] Idempotent RULE_ENSURE / declarative sync (closes BUG-008) (ADR-047)

- **Decision:** Add `PREPROC.RULE_ENSURE` (idempotent on the natural rule key,
  returns the existing rule_id when present) and make `preproc module sync`
  declarative and idempotent. `RULE_ADD` keeps its allocate-new behaviour.
- **Alternatives:** Make `RULE_ADD` itself idempotent (breaks its documented
  behaviour and tests); leave dedupe to the CLI (fragile, races).
- **Rationale:** BUG-008: `RULE_ADD` stacks duplicate rules, making a
  TRANSLATE/REWRITE module run N times and defeating declarative re-apply. A
  purpose-built ensure surface is the clean fix and the foundation `sync` needs.
- **Promotes to ADR:** yes

### [10] Ergonomics migrated out of the framework as a clean move (module #1) (ADR-048)

- **Decision:** Remove `cast-shorthand` and `trailing-comma` from the framework
  (examples, `sql/lua` copies, tests, `ergonomics/` specs) and author them as two
  independent modules directly in the existing `exasol-labs/preprocessor-library`
  repo (`modules/cast-shorthand/`, `modules/trailing-comma/`). Leave a
  moved-to-library pointer in `examples/README.md`. Supersedes ADR-032's
  in-framework `ergonomics/` domain.
- **Alternatives:** A deprecation shim / keep a copy in the framework.
- **Rationale:** Ergonomics was never in `release.sql`/`deploy_all.sql`
  (examples-only), so removal changes no engine bundle output or statement count;
  keeping a copy reintroduces the exact drift the ecosystem eliminates; ergonomics
  is the ideal proving case because it is self-contained and already fully in-repo.
- **Promotes to ADR:** yes

### [11] Library deliverables authored directly in the existing library repo (two repos)

- **Decision:** Author the library's complete initial tree directly in the
  existing `exasol-labs/preprocessor-library` checkout
  (`/home/nc/Github/preprocessor-library`), so this plan spans TWO git repos:
  framework-repo changes (contract, CLI, registry consumption, DB
  lifecycle/provenance, ergonomics removal) commit here; library-repo
  deliverables (module dirs, `module.toml`, tests, CONTRIBUTING, CI, generated
  index) commit in the library checkout. Tasks are prefixed `[library-repo]` /
  `[framework-repo]` so the implement phase knows where each file lands.
- **Alternatives:** A `library-staging/` transfer dir inside the framework repo
  (the earlier proposal, made when the repo did not yet exist); a prose-only
  file manifest.
- **Rationale:** The repo now exists, so stage-and-copy indirection is
  unnecessary; authoring in place gives real, testable files with the correct
  git home and a reviewable diff per repo.
- **Promotes to ADR:** no

### [12] Full test kit is a follow-on; ship a CI bridge now

- **Decision:** Do not extract a pip-installable published test harness in this
  slice; the library CI depends on the framework package for connection/install
  helpers and copies the fixture pattern. The standalone harness is a follow-on.
- **Alternatives:** Extract + publish + version the harness now.
- **Rationale:** Packaging/versioning a new distributable would bloat the
  foundational slice; the CI bridge unblocks library module testing immediately.
- **Promotes to ADR:** no

### [13] semantic-views integration is an out-of-scope follow-on spike

- **Decision:** Do not build the semantic-views hook-in; only ensure the contract
  accommodates a self-deployed + register-only module. Document the integration
  (stop grabbing the slot, register `SEMANTIC_ADMIN.SEMANTIC_PREPROCESSOR` as a
  rule, adopt role-scoped activation) as a follow-on needing their team.
- **Alternatives:** Attempt the integration in this slice.
- **Rationale:** It depends on another team's subsystem and would balloon scope;
  the register-only deploy mode is the seam that makes it possible later.
- **Promotes to ADR:** no

## Review Findings

<!-- Populated by speq-implement after code review. -->
