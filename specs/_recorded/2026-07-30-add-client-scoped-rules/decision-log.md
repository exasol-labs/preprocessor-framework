# Decision Log: add-client-scoped-rules

Date: 2026-07-30

> **Note for the recorder:** decisions [1]–[4] are the promoting ones, intended
> as **ADR-060 through ADR-063**, continuing from ADR-059 in
> `specs/decision-log.md`. Nothing has been written into the permanent decision
> log during planning — promote at record time. Decisions [5]–[7] are local
> design and scope choices and do not promote.

## Interview

**Q:** What is the gap, in one sentence?
**A:** `PREPROC.CONFIG.client_pattern` is honoured end-to-end by dispatch, resolution and profile codegen, but no operator-facing surface can SET it: `PREPROC.RULE_ADD` has no such parameter, so the `PREPROC ADD RULE` sugar has no clause for it, and `PREPROC.RULE_LIST` does not even return the column. Found while building `demo/framework_demo.sql`, whose client-scoping beat is forced to drop out of the sugar into `EXECUTE SCRIPT PREPROC.RULE_ENSURE(...)` and narrate why.

**Q:** Should the sugar emit `RULE_ENSURE` when a `CLIENT` clause is present, leaving `RULE_ADD` untouched and avoiding a breaking change?
**A:** No. Add the parameter to `RULE_ADD` and accept the breaking signature change. Routing on clause presence would make one verb mean insert-without-clause and upsert-with-clause, so the operator would get a fresh `rule_id` or silently adopt an existing one depending on an unrelated clause. A loud arity error beats a silent semantic switch.

**Q:** What is the clause syntax, and how does it fit the existing grammar?
**A:** `PREPROC ADD RULE FOR (ROLE|USER) <v> PHASE <p> SCRIPT <s> FUNCTION <f> [CLIENT '<pattern>'] [NOREFRESH]`. The `ADD RULE` arm accepts 11 or 13 words after `PREPROC`, mirroring how the `INSTALL MODULE` arm already accepts 6 or 8 for its optional `FROM '<source>'`. `NOREFRESH` is already stripped before arm parsing, so the two optional features compose without a combinatorial explosion of counts. Rejected `WHEN CLIENT` (extra token, breaks the bare-keyword shape of `PHASE`/`SCRIPT`/`FUNCTION`) and `FOR CLIENT` (two `FOR` clauses in one command reads as a second scope).

**Q:** Is the `CLIENT` operand identifier-folded like the other captured values?
**A:** No — it is a single-quoted literal carried verbatim, exempt from folding, like `FUNCTION` and for the same class of reason: it denotes something outside Exasol's identifier namespace. It is matched as a literal SUBSTRING against `EXA_ALL_SESSIONS.CLIENT`, a self-reported application name. Measured values on Exasol 2026.1: `DbVisualizer 24.1.5`, `DBeaver 23.3.0`, `PyExasol 2.2.1`, `EXAplus`. Upper-casing would make `DBVISUALIZER` match nothing — and do so SILENTLY (the rule resolves, dispatch evaluates it, it never fires), which is the worst available failure mode. The tokenizer keeps a quoted span as one token, so a pattern containing spaces survives. The keyword itself is matched case-insensitively like every other keyword.

**Q:** `RULE_ENSURE` already accepts `client_pattern` but has no validation for it. Once `RULE_ADD` rejects empty/whitespace-only and over-length patterns, should `RULE_ENSURE` get the same validation?
**A:** **Share it now, both scripts.** Extract the validation once and call it from both `RULE_ADD` and `RULE_ENSURE`. Consistent surface, no way to write an empty pattern through any path. Accepted the slightly larger diff and the touch to the module-installer path. Implies: extract a `validate_client_pattern()` helper; call it from `rule_ensure.lua`; test that `RULE_ENSURE` rejects the same inputs.

**Q:** `sql/demo/seed.sql` creates its `client_pattern`-scoped rule by direct DML. Should this plan migrate it to `RULE_ADD`?
**A:** Initially answered "migrate it", then the premise was found to be wrong and the question was re-asked. Final answer: **leave the seed as direct DML.** Reasons found in the source: `rule_add.lua:55` sets `RESERVED_DEMO_FLOOR = 9000` and errors rather than crossing it, so `RULE_ADD` can NEVER allocate the seed's 9001–9005 ids; the seed's inserts are `WHERE NOT EXISTS`-guarded for idempotency whereas `RULE_ADD` always inserts; and `sql/demo/teardown.sql:29` deletes exactly `rule_id IN (9001, 9002, 9003, 9004, 9005)`. Migrating would need `RULE_ADD` to accept an explicit `rule_id`, deliberately contradicting the reserved-band invariant. Therefore no `demo/config-seed` delta — the seed stays an authorised direct-DML bootstrap boundary alongside `sql/install/70_control_sugar_seed.sql`, and `tasks.md` keeps it as a verify-only task.

**Q:** V1 in place, or a new `CONTROL_SUGAR_V2`?
**A:** In place. The project is pre-first-release, so this falls under the same ADR-038 exception already granted to `PREPROC SET SYS_GROUPING` and the module-lifecycle verbs: no active session can bind a stale module body. No `CONTROL_SUGAR_V2`, no rule repointing, no retirement lifecycle. The module header must still record that a POST-release grammar change requires V2, so the exception is not mistaken for the rule.

**Q:** Does anything in dispatch, resolution, or profile codegen change?
**A:** No. `master.lua`'s `needs_client` optimisation and literal-substring match, and `refresh_core.lua`'s copy of the column into `RESOLUTION` and into the profile manifest and hash, all already satisfy their specs. No deltas for `dispatch/*`, `refresh/*`, `runtime/*`, or `install/*`.

**Q:** Where does the shared validator physically live, given Exasol Lua has no build-time include? (Planner decision — the interview settled that it must be shared, not where.)
**A:** In `PREPROC.REFRESH_CORE`, exported alongside `rebuild()`. See decision [4] for the alternatives and why they lose.

**Q:** Is `client_pattern` a security boundary?
**A:** No. The client name is self-reported by the connecting tool and trivially spoofable. Documenting that is an obligation of this plan (a requirement), not a mechanism to add.

## Design Decisions

### [1] `RULE_ADD` takes `client_pattern`, rather than the sugar switching to `RULE_ENSURE`

- **Decision:** Add `client_pattern` to `PREPROC.RULE_ADD` between `function_name` and `norefresh`, making its parameter list identical to `RULE_ENSURE`'s. Accept the breaking signature change — Exasol Lua has no optional parameters and `EXECUTE SCRIPT` enforces an exact argument count.
- **Alternatives:** Leave `RULE_ADD` alone and have the sugar emit `RULE_ENSURE` whenever a `CLIENT` clause is present — rejected: it makes one verb mean two things, since `RULE_ADD` allocates and returns a NEW `rule_id` while `RULE_ENSURE` upserts and returns an EXISTING one on a match. `PREPROC ADD RULE …` would insert and `PREPROC ADD RULE … CLIENT 'x'` would upsert, so whether the operator got a new rule or silently adopted an existing one would depend on an unrelated clause.
- **Rationale:** A stale six-argument call raises "got 6 expected 7" immediately and visibly, at the call, with no partial write. A silent semantic switch is a worse defect than a loud arity error. Aligning the two parameter lists also leaves `RULE_ADD` and `RULE_ENSURE` differing in exactly one dimension — insert vs upsert — which is the distinction they exist to draw.
- **Promotes to ADR:** yes

### [2] The `CLIENT` operand is a quoted literal, exempt from identifier folding

- **Decision:** `CLIENT` takes a single-quoted literal carried through verbatim: no folding, no trimming, and the quoted span kept as one token so embedded whitespace survives. The keyword itself is matched case-insensitively like every other keyword. This is the second capture position after `FUNCTION` that is deliberately exempt from folding, recorded as such in the sugar module's quoting-semantics header.
- **Alternatives:** Fold to UPPER like every other captured value — rejected: `DBVISUALIZER` is not a substring of `DbVisualizer 24.1.5`, so a folded pattern matches nothing and does so silently; the rule resolves, dispatch evaluates it, and `RULE_LIST` shows a correct-looking rule that never fires. `WHEN CLIENT '<p>'` — rejected: an extra token for no parse benefit, and it breaks the bare-keyword-plus-operand shape of `PHASE`/`SCRIPT`/`FUNCTION`. `FOR CLIENT '<p>'` — rejected: `FOR` already introduces the scope, so two `FOR` clauses invite reading the second as another scope rather than a narrowing of the first. An unquoted operand — rejected: it could not carry the spaces real client strings contain.
- **Rationale:** Silent non-matching is the worst outcome available here, worse than a parse error, because the operator sees a correctly-listed rule that does nothing. The single-quoted form also reuses machinery that already exists and is already tested — `INSTALL MODULE`'s `FROM '<source>'` keeps a quoted span intact so `bucketfs:`/`https:` sources survive — so there is no new tokenizer behaviour, and it visually distinguishes an opaque external string from the identifiers around it.
- **Promotes to ADR:** yes

### [3] An empty `client_pattern` is rejected, and an over-length one is rejected rather than truncated

- **Decision:** Reject an empty or whitespace-only `client_pattern` with a validation error before any mutation, naming NULL as the way to express "all clients". Reject a pattern longer than the `VARCHAR(256)` column width; never truncate.
- **Alternatives:** Silently coerce an empty pattern to NULL, or store it and let it mean "always" — both rejected: they produce a rule whose displayed scope contradicts its behaviour. Truncate an over-length pattern to the column width — rejected: clipping a SUBSTRING pattern WIDENS the matched set, so `'DbVisualizer-Prod'` becomes `'DbVisualizer'` and fires for every DbVisualizer session.
- **Rationale:** `string.find(client, "", 1, true)` returns 1, so `client_pattern = ''` applies to every client — exactly what NULL already means — while reading in `RULE_LIST` output as a restriction. This framework's failure-mode instinct is consistently to prefer a loud error over a silent wrong answer: the module contract forbids defensive `pcall` for that reason, and `SET_MODE` validates against a whitelist before writing. Truncation is a sharper version of the same problem, because it quietly grants a transformation to more sessions than the operator asked for.
- **Promotes to ADR:** yes

### [4] One shared `client_pattern` validator, hosted in `PREPROC.REFRESH_CORE`

- **Decision:** `validate_client_pattern()` is implemented exactly once, exported from `PREPROC.REFRESH_CORE` alongside `rebuild()`, and called from BOTH `rule_add.lua` and `rule_ensure.lua`. Each script's `import("PREPROC.REFRESH_CORE", "rc")` moves to the top of the file so validation precedes any DML.
- **Alternatives:** A new deployed `PREPROC.CONTROL_VALIDATE` script — rejected: it needs entries in `resources.py`, `install.py`, `bundle.py` and `release.sql`, and it creates a new object in the locked `PREPROC` schema, which drags `install/schema-layout` and `test_schema_layout.py` into scope for a twelve-line function. Duplicate the validator in both files — rejected: the divergence hazard this framework consistently refuses, most recently for the artifact splitter. Validate only in `RULE_ADD` and treat `RULE_ENSURE` as a follow-on — rejected by the user in the interview: it leaves the module-installer path able to persist `client_pattern = ''`.
- **Rationale:** `import(name, alias)` against a deployed script is the framework's ONLY cross-script Lua sharing mechanism — there is no build-time textual include. `REFRESH_CORE` is the sole existing shared module, both writers already import it, and its header already justifies itself as the place logic goes rather than being duplicated across the mutating admin scripts; the same argument extends to the validator. `import()` binds the module's functions and performs no work of its own (`refresh.lua:52` already imports it at top level), so moving the import earlier costs a module load on the `NOREFRESH` path and changes no behaviour. `REFRESH_CORE`'s stated role widens from "the resolution rebuild" to "shared logic for `PREPROC.REFRESH` and the mutating admin scripts", and its header must say so.
- **Promotes to ADR:** yes

### [5] `RULE_LIST` returns `CLIENT_PATTERN`

- **Decision:** `PREPROC.RULE_LIST` returns `CLIENT_PATTERN`, positioned after `scope_value` to mirror `PREPROC.CONFIG` column order. The permanent scenario's "at least" hedge on the column list is replaced by the exact set, so the column is a requirement rather than a permission.
- **Alternatives:** Leave `RULE_LIST` alone since the permanent spec says "at least" these columns — rejected: a client-scoped rule would remain invisible in the surface operators actually use, which is half of the gap this plan exists to close. `demo/framework_demo.sql:193` currently queries `PREPROC.CONFIG` directly for exactly this reason.
- **Rationale:** A rule that dispatches conditionally but lists identically to an unconditional one is a trap. Setting the column without being able to see it would only move the gap.
- **Promotes to ADR:** no

### [6] In-place `CONTROL_SUGAR_V1` edit under the pre-release exception

- **Decision:** The `CLIENT` clause is added to `PREPROC_RT.CONTROL_SUGAR_V1` in place: no new module version, no repointing of the seeded EXPAND rules, no retirement lifecycle.
- **Alternatives:** `CONTROL_SUGAR_V2` with the full deploy-repoint-retire flow — rejected as unnecessary pre-release, and it would spend the versioning machinery on a change no live session can observe.
- **Rationale:** The same ADR-038 exception already granted to `PREPROC SET SYS_GROUPING` and the module-lifecycle verbs: the project has not released, so the never-`CREATE OR REPLACE`-a-live-module hazard of ADR-019 does not apply. The module header must keep recording that a POST-release grammar change requires V2, so the exception is not mistaken for the rule, and this plan spends it on nothing beyond the `CLIENT` clause.
- **Promotes to ADR:** no

### [7] `sql/demo/seed.sql` stays direct DML; no `demo/config-seed` delta

- **Decision:** The demo seed keeps creating its `client_pattern`-scoped rule by direct DML. It is a verify-only task, and this plan authors no `demo/config-seed` delta.
- **Alternatives:** Migrate the seed to `RULE_ADD` now that the parameter exists — rejected on three independent grounds found in the source: `rule_add.lua:55`'s `RESERVED_DEMO_FLOOR = 9000` means `RULE_ADD` can never allocate the seed's 9001–9005 ids; the seed's inserts are `WHERE NOT EXISTS`-guarded for idempotency whereas `RULE_ADD` always inserts; and `sql/demo/teardown.sql:29` deletes exactly those five ids. Give `RULE_ADD` an explicit `rule_id` parameter to make the migration possible — rejected: it would deliberately contradict the reserved-band invariant.
- **Rationale:** The seed is an authorised direct-DML bootstrap boundary, alongside `sql/install/70_control_sugar_seed.sql`. The reserved band exists precisely so demo rules are not operator rules; making `RULE_ADD` able to write into it would dissolve that separation to save a few lines of SQL.
- **Promotes to ADR:** no

## Review Findings

<!-- Populated by speq-implement after code review. -->
