# Decision Log: change-module-artifact-to-composite

Date: 2026-07-27

> **Note for the recorder:** ADR-054 through ADR-059 were authored directly into
> `specs/decision-log.md` as part of this planning pass, at the user's explicit
> request. Entries [1]–[6] below are therefore already in the permanent record —
> do NOT append them a second time. Entries [7]–[9] are local design choices and
> do not promote.

## Interview

**Q:** What is the change, in one sentence?
**A:** Generalise the framework's module artifact contract from "exactly one `CREATE OR REPLACE LUA SCRIPT` statement" to a single `.sql` file containing 1..N statements (Lua scripts, Python UDFs, tables, …), so a module which is the front door to a whole subsystem can be `library-deployed` and installed from the registry — instead of being forced into `self-deployed`, where the library can only carry a pointer and never actually deliver the code.

**Q:** Should this introduce a third `deploy_mode` for composite modules?
**A:** No. Do not add a third `deploy_mode`. `library-deployed` is generalised in place; `self-deployed` stays exactly as-is. N=1 is simply the degenerate case.

**Q:** What exactly is the contradiction being fixed?
**A:** `specs/module/contract/spec.md:10` states there is "exactly ONE contract regardless of whether the module is a tiny transform or the front door to a whole subsystem", but `specs/module/contract/spec.md:100-105` requires exactly one `CREATE OR REPLACE LUA SCRIPT`. Both cannot hold. Any module needing a UDF or a table is silently pushed to `self-deployed`, which makes `preprocessor-library` a phone book rather than a distribution channel for precisely the modules most worth distributing.

**Q:** Is the documented justification for the one-statement rule sound?
**A:** No. `docs/module-authoring.md:70` justifies it as "trivially hand-carryable for air-gapped deployment and trivially hashed for integrity verification." Neither justification actually requires one statement — a multi-statement file is equally hand-carryable and hashes identically. The real cause is mechanical: `sql/lua/module_install.lua:230` executes a single `pquery(artifact_text)`.

**Q:** How should the object inventory be determined — parsed from the artifact, or declared in the manifest?
**A:** Both (D1). `module.toml` gains an `[[objects]]` array declaring every object the artifact creates (`type` + fully-qualified `name`). Tooling ALSO parses the artifact to derive the same list and fails if they disagree — the exact accept/reject discipline already used for `sha256`. Rationale: drift becomes impossible, the manifest stays human-reviewable before install, and `MODULE_RECLAIM` reads the declaration without re-parsing.

**Q:** Do companion objects need the module version suffix, or only the entry script?
**A:** All of them (D2). ADR-019's never-`CREATE OR REPLACE`-a-live-module discipline extends to every object in the artifact. v1 and v2 generations coexist with zero collisions and reclaim is unambiguous. A companion identical between v1 and v2 is still redeployed under the new versioned name. The entry Lua script must therefore reference its companions by their versioned names.

**Q:** May a composite artifact create objects outside `PREPROC_RT` / `PREPROC`?
**A:** Yes (D3). No namespace restriction. This widens `module_reclaim.lua`'s current "PREPROC_RT-owned artifacts only" drop guard. The unbounded namespace is made safe by D1: because the inventory is declared AND parse-verified, reclaim always knows exactly which objects it owns. Treat the security posture explicitly — installing a module becomes arbitrary DDL under the `PREPROC_ADMIN` boundary, so the spec should address how a reviewer/operator sees the object list before installing (e.g. `MODULE_CATALOG` surfacing it).

**Q:** How are statements separated within the file?
**A:** The EXAplus `--/` … `/` block-marker convention (D4). Already emitted by the framework's own `release.sql` and by `confd-via-sql`'s `build.py`; `module_install.lua` already strips a lone `/` terminator, so it half-knows the format. Decisive advantage: the artifact stays directly runnable by hand in EXAplus/DbVisualiser, preserving the air-gap story. Plain statements end with `;`; statements whose bodies contain semicolons are wrapped in `--/` … `/`.

**Q:** Is there a latent bug to fix as part of this?
**A:** Yes. `src/preproc/module/manifest.py:58` `_CREATE_SCRIPT_RE` matches only `CREATE OR REPLACE LUA SCRIPT`. An artifact with 1 Lua script + N Python UDFs therefore passes CLI validation today and only fails later at DB install when `pquery` receives multiple statements. The validator and the executor already disagree about what an artifact is. The new parser must recognise every DDL form the contract admits.

**Q:** What is the atomicity story? Can the install be wrapped in a transaction?
**A:** No — atomicity must be constructed, not inherited, and an earlier analysis got this wrong. DML inside a Lua script IS transactional and rollback-able (`refresh_core.lua`; `specs/decision-log.md:269`; note `refresh_core.lua:775` — there is no `commit()` global, COMMIT is issued as SQL via `pquery`). DDL is NOT: `refresh_core.lua:111,117,742` and `module_reclaim.lua:68` document that `CREATE OR REPLACE` / `DROP SCRIPT` implicitly commit preceding DML and cannot be rolled back. A composite artifact is mostly DDL, so failure at statement k cannot be rolled back to undo 1..k-1. All-or-nothing must be built from: pre-flight validation of all N statements + the inventory before the first DDL; compensating cleanup of objects created by 1..k-1 on failure at k; idempotent re-run via `CREATE OR REPLACE`; and rule registration last, so a partially installed module is inert rather than half-active.

**Q:** Is the splitter needed in more than one language?
**A:** Yes — Python (CLI path, `deploy.py`/`validate.py`) and Lua (in-DB path, `module_install.lua`). Divergence between them is a correctness hazard: `specs/module/db-install` asserts the two paths reach a byte-identical end state. Preserve that guarantee.

**Q:** Does chunking still hold for a much larger artifact?
**A:** Confirm it. `module_resolver_v1.sql` chunks the artifact across EMITS rows; confd-via-sql's equivalent bundle is ~280 KB. Confirm the chunking and reassembly path holds at that size, and that splitting happens after reassembly.

**Q:** Is `[[objects]]` optional for N=1, or must `cast_shorthand` and `trailing_comma` be updated?
**A:** (Planner decision, per the instruction to decide and state it explicitly.) Optional at N=1, required at N>1 — see decision [7]. The stated invariant is that those two modules "must keep working untouched", which required-always would violate.

**Q:** Does the registry index need to carry the object inventory, and is that a schema bump?
**A:** (Planner decision, per the instruction to check.) Yes and yes — see decision [8].

**Q:** What is explicitly out of scope?
**A:** `confd-via-sql` migrating onto the new contract as the proving case (3 EXPAND Lua modules, 8 Python UDFs, 5 `PREPROC.ADMIN_*` Lua scripts, all gaining `_V<N>` suffixes under D2), and the `preprocessor-library` registry entry / PR for it. Record both as follow-ons.

## Design Decisions

### [1] Composite artifact: one file, 1..N statements

- **Decision:** The canonical module artifact becomes a single `.sql` file carrying one or more statements, exactly one of which is the `CREATE OR REPLACE LUA SCRIPT` entry script. N=1 remains valid unchanged. No third `deploy_mode`.
- **Alternatives:** A third `deploy_mode` for composite modules — rejected, reintroduces the two-kinds split ADR-040 already rejected. A packaged archive format — rejected, adds tooling weight and stops the artifact being hand-runnable. Multi-file artifacts — rejected, complicates hashing and hand-carry.
- **Rationale:** Resolves a direct contradiction inside `module/contract`, and turns `preprocessor-library` into a real distribution channel for subsystem modules.
- **Already recorded as:** ADR-054 in `specs/decision-log.md` (authored during this planning pass; do NOT append again)
- **Promotes to ADR:** no

### [2] Object inventory declared in the manifest and parse-verified

- **Decision:** `module.toml` gains `[[objects]]`; tooling independently derives the inventory from the artifact and rejects any disagreement in either direction.
- **Alternatives:** Derive-only — rejected, leaves nothing human-reviewable before install and forces reclaim to re-parse an artifact the database never retains. Declare-only — rejected, permits silent drift between manifest and artifact.
- **Rationale:** Mirrors the accept/reject discipline already applied to `sha256`; makes drift impossible; gives reclaim an exact, trustworthy undo list.
- **Already recorded as:** ADR-055 in `specs/decision-log.md` (authored during this planning pass; do NOT append again)
- **Promotes to ADR:** no

### [3] Every object carries the module version suffix

- **Decision:** Every object an artifact creates carries the module's `_V<N>` suffix, not only the entry script. A companion identical between generations is still redeployed under the new versioned name.
- **Alternatives:** Version only the entry script — rejected, unversioned companions collide across generations and make reclaim ambiguous; a v2 install would overwrite a live v1 companion, which is exactly what ADR-019 forbids.
- **Rationale:** Extends the never-mutate-a-live-module discipline to the whole artifact so generations coexist cleanly.
- **Already recorded as:** ADR-056 in `specs/decision-log.md` (authored during this planning pass; do NOT append again)
- **Promotes to ADR:** no

### [4] Unrestricted target namespace, inventory-bounded reclaim

- **Decision:** A composite artifact may create objects in any schema the installing admin can write. `module_reclaim.lua`'s `PREPROC_RT`-only guard is replaced by the recorded inventory as the ownership boundary. `MODULE_CATALOG` surfaces the inventory pre-install as the review gate.
- **Alternatives:** Restrict to `PREPROC_RT` / `PREPROC` — rejected, a subsystem module legitimately owns its own schema, and the restriction would push exactly those modules back to `self-deployed`.
- **Rationale:** Safe because the inventory is declared and parse-verified, so reclaim knows precisely what it owns. The security posture is explicit: installing a library-deployed module is arbitrary DDL under the `PREPROC_ADMIN` boundary, which is why the inventory must be reviewable before install.
- **Already recorded as:** ADR-057 in `specs/decision-log.md` (authored during this planning pass; do NOT append again)
- **Promotes to ADR:** no

### [5] EXAplus `--/` … `/` block markers as the statement separator

- **Decision:** Statements whose bodies contain semicolons are wrapped between a lone `--/` line and a lone `/` line; all others are terminated by `;`.
- **Alternatives:** Bare `;` splitting — rejected, cannot survive semicolons inside Lua and Python bodies. A JSON/TOML statement array — rejected, stops the file being hand-runnable and kills the air-gap story. A packaged archive — rejected for the same reason.
- **Rationale:** Already emitted by the framework's own `release.sql` and by `confd-via-sql`'s `build.py`, and `module_install.lua` already strips a lone `/`. The artifact stays directly runnable by hand in EXAplus/DbVisualiser, which is what actually preserves hand-carry.
- **Already recorded as:** ADR-058 in `specs/decision-log.md` (authored during this planning pass; do NOT append again)
- **Promotes to ADR:** no

### [6] Composite-install atomicity is constructed, not inherited

- **Decision:** All-or-nothing is built from four mechanisms: pre-flight validation of all N statements and the inventory before the first DDL; compensating reverse-order cleanup of objects created by statements 1..k-1 on failure at k; idempotent re-run via `CREATE OR REPLACE`; and rule registration last so a partial install is inert.
- **Alternatives:** Wrap the install in a transaction — rejected as a false guarantee: Exasol DDL implicitly commits preceding DML and cannot itself be rolled back, as the framework's own `refresh_core.lua:111,117,742` and `module_reclaim.lua:68` document and visibly work around. Leave partial state and require manual cleanup — rejected, violates the existing "leave the DB consistent on failure" invariant in `module/cli`.
- **Rationale:** DML in a Lua script IS rollback-able and DDL is not; a composite artifact is mostly DDL. The framework already orders rule registration last — this makes an accident into a stated guarantee.
- **Already recorded as:** ADR-059 in `specs/decision-log.md` (authored during this planning pass; do NOT append again)
- **Promotes to ADR:** no

### [7] `[[objects]]` optional at N=1, required at N>1

- **Decision:** `[[objects]]` MAY be omitted when the artifact carries exactly one statement, in which case the inventory is derived as the single object named by `script_name`. It is REQUIRED once the artifact carries more than one statement. `cast_shorthand` and `trailing_comma` are therefore NOT edited.
- **Alternatives:** Require `[[objects]]` always — rejected, it would force edits to the two existing library modules, violating the stated invariant that they keep working untouched, and would churn their manifests for zero information gain.
- **Rationale:** At N=1 the inventory is fully determined by `script_name`, so the declaration carries no information the manifest does not already have. Requiring it only where it adds information keeps N=1 a true degenerate case.
- **Promotes to ADR:** no

### [8] Registry index carries `objects`; `schema_version` 1 → 2, readers accept both

- **Decision:** Each library-deployed index entry gains an `objects` list; the index declares `schema_version: 2`. Readers accept 1 and 2, interpreting a version-1 entry as a single-statement module whose inventory is the object named by `script_name`.
- **Alternatives:** Add the field without bumping — rejected, `MODULE_CATALOG` depends on `objects` for its pre-install review story, so the field changes what an entry means, which is precisely what `schema_version` exists to signal. Hard-break at 2 — rejected, would strand already-published indexes.
- **Rationale:** The inventory must travel in the index because the in-DB resolver verifies against it and `MODULE_CATALOG` never fetches an artifact. Accepting version 1 preserves back-compat at negligible cost.
- **Promotes to ADR:** no

### [9] Canonical Python splitter embedded verbatim into the resolver UDF

- **Decision:** The splitter has one canonical Python implementation in `src/preproc/module/artifact.py`. `sql/udf/module_resolver_v1.sql` embeds it verbatim between markers, and a CI test asserts the embedded copy is byte-identical. The Lua splitter is an independent implementation pinned to the same conformance corpus.
- **Alternatives:** Three independent implementations (CLI Python, UDF Python, Lua) — rejected, triples the divergence surface the interview flagged as a correctness hazard. Vendor the `preproc` package into the UDF — rejected, `module/db-resolver` explicitly re-implements rather than vendoring.
- **Rationale:** The resolver UDF is Python but cannot import `preproc`, so a naive reading of "two languages" would actually yield three implementations. Verbatim embed plus an equality check collapses the two Python sites into one source of truth, leaving exactly one cross-language boundary for the corpus to police.
- **Promotes to ADR:** no

## Follow-Ons

- Migrate `confd-via-sql` onto the composite contract as the proving case: 3 EXPAND Lua modules, 8 Python UDFs, 5 `PREPROC.ADMIN_*` Lua scripts, all gaining `_V<N>` suffixes under D2.
- Author the `preprocessor-library` registry entry / PR for `confd-via-sql`.

## Review Findings

<!-- Populated by speq-implement after code review. -->
