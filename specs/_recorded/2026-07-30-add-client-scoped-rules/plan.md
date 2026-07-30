# Plan: add-client-scoped-rules

## Summary

`PREPROC.CONFIG.client_pattern` is honoured end-to-end by dispatch, resolution and profile codegen, but no operator-facing surface can SET it and `PREPROC.RULE_LIST` does not even return it. This plan gives `PREPROC.RULE_ADD` a `client_pattern` parameter, adds an optional `CLIENT '<pattern>'` clause to the `PREPROC ADD RULE` sugar, surfaces `CLIENT_PATTERN` in `RULE_LIST`, and shares one validator across every write path.

## Design

### Context

The dispatch half of `client_pattern` is complete and correct, and nothing in this plan touches it:

* `master.lua` resolves the connecting client from `EXA_ALL_SESSIONS` only when at least one resolved rule carries a non-NULL pattern (the `needs_client` optimisation), then matches with `string.find(client, pattern, 1, true)` — a literal substring, not a Lua pattern and not a wildcard.
* `refresh_core.lua` copies `client_pattern` from `CONFIG` into `RESOLUTION` verbatim and folds it into the profile manifest and hash, so two chains differing only by pattern hash differently and get distinct profiles.
* `PREPROC_RT.MY_PIPELINE` already exposes the column.

The operator half is missing, and is missing in three places at once:

* `PREPROC.RULE_ADD`'s parameter list is `(scope_type, scope_value, phase, script_name, function_name, norefresh)`. `rule_add.lua:118` documents the omission as deliberate — *"client_pattern is NULL (not part of this control surface)"* — and hard-codes a `NULL` literal into its `INSERT`.
* Because `RULE_ADD` has no such parameter, the sugar has no clause for it. Its `ADD RULE` arm is strict-positional, gated on `subcommand_count ~= 11`.
* `rule_list.lua:23` selects `rule_id, scope_type, scope_value, phase, script_name, function_name, enabled` — omitting `client_pattern`, so a client-scoped rule is invisible in the surface operators actually use.

The only way to create a client-scoped rule today is `PREPROC.RULE_ENSURE`, which exists for the module installer's idempotent-upsert needs and carries different semantics (returns an existing `rule_id` instead of allocating a new one). `demo/framework_demo.sql:181` is forced to drop out of the sugar into `EXECUTE SCRIPT PREPROC.RULE_ENSURE(...)` and narrate why, and `demo/framework_demo.sql:193` has to query `PREPROC.CONFIG` directly because `RULE_LIST` cannot show the column.

Two constraints shape every option:

* **Exasol Lua scripts have no optional parameters.** `EXECUTE SCRIPT` enforces an exact argument count. Adding a parameter to `RULE_ADD` is unavoidably a breaking signature change for every existing caller.
* **The framework's only cross-script Lua sharing mechanism is `import(name, alias)`** against a deployed script. There is no build-time textual include. `PREPROC.REFRESH_CORE` is the sole existing shared module, and every mutating admin script already imports it.

- **Goals** — make `client_pattern` settable through `RULE_ADD`, expressible in the sugar, observable in `RULE_LIST`, and impossible to persist in a form that silently misbehaves.
- **Non-Goals** — no change to dispatch, resolution, or profile codegen; no wildcard or regex matching (the literal-substring contract is unchanged); no new `deploy_mode`, no `CONTROL_SUGAR_V2`; no attempt to make `client_pattern` a security boundary; no migration of `sql/demo/seed.sql` off direct DML.

### Decision

Four coordinated changes plus one shared helper.

1. **`PREPROC.RULE_ADD` gains `client_pattern`**, inserted between `function_name` and `norefresh` so its parameter list becomes identical to `RULE_ENSURE`'s. NULL means "apply to all clients" — the existing `CONFIG` semantics, unchanged. This is a deliberate breaking signature change.
2. **The sugar gains an optional `CLIENT '<pattern>'` clause**, positioned after `FUNCTION <f>` and before the optional trailing `NOREFRESH`, landing in `CONTROL_SUGAR_V1` in place under the ADR-038 pre-release exception.
3. **`PREPROC.RULE_LIST` returns `CLIENT_PATTERN`**, so every rule is fully described by the surface that lists rules.
4. **One shared `validate_client_pattern()`** rejects empty, whitespace-only, and over-length patterns before any mutation, called from BOTH `rule_add.lua` and `rule_ensure.lua`.

#### Architecture

```
PREPROC ADD RULE … CLIENT 'DbVisualizer'        (operator, any SQL client)
        │
        ▼  EXPAND dispatch
┌──────────────────────────────┐
│ PREPROC_RT.CONTROL_SUGAR_V1  │  ADD RULE arm: 11 or 13 words
│  tokenize → strip NOREFRESH  │  CLIENT operand: single-quoted, NOT folded
│  → parse arm → emit 1 stmt   │  always-pass-the-slot: NULL when absent
└──────────────┬───────────────┘
               ▼  exactly one statement
   EXECUTE SCRIPT PREPROC.RULE_ADD('ROLE','DBA','EXPAND',…,'help_menu','DbVisualizer',NULL)
               │
               ▼
┌──────────────────────────────┐      import("PREPROC.REFRESH_CORE","rc")
│ PREPROC.RULE_ADD             │─────────────────┐
│  validate phase              │                 ▼
│  validate scope_type         │   ┌─────────────────────────────────┐
│  validate script exists      │   │ PREPROC.REFRESH_CORE            │
│  rc.validate_client_pattern()│◀──│  validate_client_pattern(cp)    │
│  ── all before any DML ──    │   │  rebuild()                      │
│  INSERT INTO PREPROC.CONFIG  │   └─────────────────────────────────┘
│  rc.rebuild()                │                 ▲
└──────────────────────────────┘                 │
┌──────────────────────────────┐                 │
│ PREPROC.RULE_ENSURE          │─────────────────┘  same validator, same rejections
└──────────────────────────────┘

PREPROC.RULE_LIST ──▶ + CLIENT_PATTERN column   (read-only, unchanged contract)

UNCHANGED: master.lua needs_client + literal-substring match;
           refresh_core rebuild copying client_pattern into RESOLUTION,
           the profile manifest, and the profile hash.
```

#### Patterns

| Pattern | Where | Why |
|---------|-------|-----|
| Validate-before-mutate | `rule_add.lua`, `rule_ensure.lua` | Preserves the documented invariant that a rejected call leaves `CONFIG` and `RESOLUTION` exactly as they were. `client_pattern` joins the chain after script existence, still before any DML. |
| Shared module via `import()` | `refresh_core.lua` exports `validate_client_pattern` | The framework's only cross-script Lua sharing mechanism. Both writers already import `REFRESH_CORE`; the import moves to the top of each script so validation precedes DML. |
| Always-pass-the-slot | `control_sugar_v1.lua` `ADD RULE` arm | Exasol enforces an exact argument count, so an absent `CLIENT` emits `NULL` and an absent `NOREFRESH` emits `NULL`. |
| Dual accepted token count | `control_sugar_v1.lua` `ADD RULE` arm | Established by `INSTALL MODULE` (8 with `FROM`, 6 without). `ADD RULE` accepts 13 with `CLIENT`, 11 without. |
| `NOREFRESH` stripped before arm parsing | `control_sugar_v1.lua` | Existing behaviour; keeps the two optional features from multiplying accepted counts, and means `CLIENT` is never parsed at a shifted index. |
| Folding exemption | `CLIENT` operand | Second exempt capture position after `FUNCTION`, for the same class of reason: the value denotes something outside Exasol's identifier namespace. |
| Named `:param` binding | `rule_add.lua` `INSERT` | Positional `?` silently fails under Exasol Lua. |
| Single-capture `gsub` | `control_sugar_v1.lua` | `gsub` returns two values; the module contract requires capture into exactly one local. |
| Reject, never truncate | `validate_client_pattern` | Truncating a SUBSTRING pattern widens the matched set — the failure is silent and permissive, the worst combination. |

### Consequences

| Decision | Alternatives Considered | Rationale |
|----------|------------------------|-----------|
| Add `client_pattern` to `RULE_ADD`, accepting a breaking signature change | Leave `RULE_ADD` alone and have the sugar emit `RULE_ENSURE` when a `CLIENT` clause is present | Routing on clause presence makes one verb mean two things: `PREPROC ADD RULE …` would insert and allocate a fresh `rule_id`, while `PREPROC ADD RULE … CLIENT 'x'` would upsert and possibly adopt an existing rule. Whether the operator got a new rule would depend on an unrelated clause, silently. A stale six-argument call instead raises an argument-count error at the call with no partial write. Loud beats silent. |
| `CLIENT '<pattern>'` as a bare keyword plus single-quoted literal | `WHEN CLIENT '<p>'`; `FOR CLIENT '<p>'`; an unquoted operand | `WHEN` adds a token for no parse benefit and breaks the bare-keyword shape of `PHASE`/`SCRIPT`/`FUNCTION`. `FOR` already introduces the scope, so two `FOR` clauses in one command invite reading the second as another scope rather than a narrowing of the first. Quoting reuses the tokenizer behaviour that already carries `FROM '<source>'` intact, so a pattern with spaces survives with no new tokenizer work, and it visually marks the value as an opaque external string. |
| The `CLIENT` operand is exempt from identifier folding | Fold to UPPER like every other captured value | Measured client strings are `DbVisualizer 24.1.5`, `DBeaver 23.3.0`, `PyExasol 2.2.1`, `EXAplus`. `DBVISUALIZER` is not a substring of `DbVisualizer 24.1.5`, so a folded pattern matches nothing — and does so silently: the rule resolves, dispatch evaluates it, it never fires, and `RULE_LIST` shows a correct-looking rule that does nothing. |
| One shared validator, hosted in `PREPROC.REFRESH_CORE` | A new `PREPROC.CONTROL_VALIDATE` script; duplicate the validator in both files; validate only in `RULE_ADD` | A new deployed script means new entries in `resources.py`, `install.py`, `bundle.py`, `release.sql` AND a new object in the locked `PREPROC` schema — which would drag `install/schema-layout` and `test_schema_layout.py` into scope for a twelve-line function. Duplication is the divergence hazard this framework consistently refuses. Validating only in `RULE_ADD` leaves the installer path able to persist `client_pattern = ''`. `REFRESH_CORE` is already the shared module every mutating admin script imports, and its header already justifies itself as the place logic goes rather than being duplicated across those scripts. |
| Reject an empty/whitespace-only pattern rather than coercing it to NULL | Coerce silently to NULL; store it and let it mean "always" | `string.find(client, "", 1, true)` returns 1, so `client_pattern = ''` applies to every client — exactly what NULL already means — while reading in `RULE_LIST` output as a restriction. Both alternatives produce a rule whose displayed scope contradicts its behaviour. |
| Reject an over-length pattern rather than truncating | Truncate to `VARCHAR(256)` | Clipping `'DbVisualizer-Prod'` to `'DbVisualizer'` makes the rule fire for every DbVisualizer session. Truncation is never the safe default for a matching pattern because it widens the matched set. |
| In-place edit to `CONTROL_SUGAR_V1` | `CONTROL_SUGAR_V2` with rule repointing and a retirement lifecycle | The project is pre-first-release, so no active session can bind a stale module body — the same ADR-038 exception already spent on `PREPROC SET SYS_GROUPING` and the module-lifecycle verbs. The module header must keep recording that a POST-release grammar change requires V2, so the exception is not mistaken for the rule. |
| Leave `sql/demo/seed.sql` as direct DML | Migrate the seed's client-scoped rule to `RULE_ADD` | `rule_add.lua:55` sets `RESERVED_DEMO_FLOOR = 9000` and errors rather than crossing it, so `RULE_ADD` can never allocate the seed's 9001–9005 ids. The seed's inserts are `WHERE NOT EXISTS`-guarded for idempotency whereas `RULE_ADD` always inserts, and `sql/demo/teardown.sql:29` deletes exactly `rule_id IN (9001..9005)`. Migrating would require `RULE_ADD` to accept an explicit `rule_id`, deliberately contradicting the reserved-band invariant. |

Further consequences:

* `RULE_LIST` gains a column. The scripted surface returns a table, so name-addressed callers are unaffected; a caller reading by ordinal position sees a shifted result set.
* `RULE_ENSURE` keeps its `client_pattern` parameter and its upsert semantics. After this plan the two scripts differ only in insert-vs-upsert, which is the distinction they should have differed on all along.
* `REFRESH_CORE`'s stated role widens from "the resolution rebuild" to "shared logic for `PREPROC.REFRESH` and the mutating admin scripts". Its header must be updated to say so.
* Both writers now import `REFRESH_CORE` unconditionally rather than only on the refresh path. `import()` binds the module's functions and performs no work of its own (`refresh.lua:52` already imports it at top level), so the `NOREFRESH` path gains a module load and no behaviour change.

## Features

| Feature | Status | Spec |
|---------|--------|------|
| `control/admin-scripts` | CHANGED | `specs/_plans/add-client-scoped-rules/control/admin-scripts/spec.md` |
| `control/command-sugar` | CHANGED | `specs/_plans/add-client-scoped-rules/control/command-sugar/spec.md` |

No new features. No delta for `dispatch/*`, `refresh/*`, `runtime/*`, `install/*`, or `demo/config-seed`: the dispatch, resolution and profile-codegen halves of `client_pattern` already satisfy their specs and are exercised by existing scenarios, no new object is created in either schema, and the demo seed stays an authorised direct-DML bootstrap boundary.

## Requirements

| Requirement | Details |
|-------------|---------|
| R1 | `RULE_ADD` MUST accept a `client_pattern` argument between `function_name` and `norefresh` and persist it to `PREPROC.CONFIG.client_pattern` byte-for-byte. |
| R2 | A NULL `client_pattern` MUST persist as NULL and mean "all clients". |
| R3 | An empty or whitespace-only `client_pattern` MUST be rejected before any mutation, with an error naming NULL as the way to express "all clients". |
| R4 | A `client_pattern` longer than the `VARCHAR(256)` column width MUST be rejected before any mutation, never truncated. |
| R5 | R3 and R4 MUST be enforced by ONE shared validator called from both `RULE_ADD` and `RULE_ENSURE`. |
| R6 | The sugar MUST accept an optional `CLIENT '<pattern>'` clause after `FUNCTION`, carrying the quoted value verbatim without identifier folding. |
| R7 | The sugar MUST compose `CLIENT` with `NOREFRESH` in all four presence combinations, and MUST NOT mistake a quoted `'NOREFRESH'` pattern for the suffix. |
| R8 | The sugar MUST decline fail-open (return `nil`) on a malformed `CLIENT` clause: missing operand, unquoted operand, unterminated quote, or a word count that is neither 11 nor 13 after the `NOREFRESH` strip. |
| R9 | `RULE_LIST` MUST return `CLIENT_PATTERN` for every rule. |
| R10 | Documentation MUST state that `client_pattern` is scoping, not a security boundary — the client name is self-reported and trivially spoofable. |
| R11 | The grammar change MUST land in `CONTROL_SUGAR_V1` in place, and the module header MUST keep recording that a post-release grammar change requires `CONTROL_SUGAR_V2`. |

## Dependencies

None external. Sequenced internally: the shared validator and `RULE_ADD` (R1–R5) before the sugar (R6–R8), because the sugar emits the call whose signature changes. R9 is independent.

## Migration

Every in-repo `RULE_ADD` call site needs the extra argument. Verified by `grep -rn "RULE_ADD(" --include='*.sql' --include='*.py' --include='*.lua' --include='*.md'`:

| Site | Change |
|------|--------|
| `sql/lua/control_sugar_v1.lua` | The emitted `EXECUTE SCRIPT`; edited in place. |
| `src/preproc/install.py:348` | `CREATE` header parameter list. |
| `src/preproc/bundle.py:122` | `CREATE` header parameter list. |
| `examples/demo_ping.sql:167` | Trailing `RULE_ADD` call in the header comment block. |
| `examples/demo_translate.sql:147` | Same. |
| `examples/demo_audit.sql:77` | Same. |
| `examples/confd_admin.sql:1087-1088` | Two `RULE_ADD` calls. |
| `examples/confd_admin.md:37-38` | Two `RULE_ADD` calls in the operator instructions. |
| `examples/README.md:6,78` | Describes the trailing `RULE_ADD` call operators are told to edit. |
| `docs/operations.md:322` | Sugar mapping table — add the `[CLIENT '<pattern>']` clause. |
| `docs/operations.md:377` | `Adding a rule` example, plus the admin-script parameter table and a `client_pattern` subsection carrying R10. |
| `docs/how-it-works.md` | `client_pattern matching` section gains a pointer to the operator-facing clause. |
| `docs/module-authoring.md:16` | References the `PREPROC ADD RULE` / `PREPROC.RULE_ADD` surface. |
| `README.md:312` | Rules section example list. |
| `demo/framework_demo.sql:181-194` | Beat 5 drops the `RULE_ENSURE` workaround for the sugar, and the direct `PREPROC.CONFIG` query becomes `PREPROC LIST RULES`. |
| `demo/framework_demo.md` | Beat 5 row and the what-to-point-at section. |
| `tests/test_cli_bundle.py:514` | Asserts the `RULE_ADD` `CREATE` header. |
| `tests/test_control_admin_scripts.py:51,173,222,238,254` | Header assertion and four `EXECUTE SCRIPT` fixtures. |
| `tests/test_command_sugar.py:163,176,195,244,356` | Expected expansions; line 356 asserts the exact string `EXECUTE SCRIPT PREPROC.RULE_ADD('ROLE', 'X', 'Y', 'Z', 'W', true)`, which becomes `(…, 'W', NULL, true)`. |
| `tests/test_profile_codegen.py:354` | Uses a six-argument `RULE_ADD` as fixture setup. |

Verify-only, expected to need no change:

| Site | Why |
|------|-----|
| `sql/install/70_control_sugar_seed.sql` | Seeds the bootstrap rule by direct DML, not `RULE_ADD`. |
| `sql/demo/seed.sql` | Direct DML in the reserved 9001–9005 band; an authorised bootstrap boundary. See Consequences. |
| `demo/01_administration.sql:175` | Quotes a `RULE_ADD` error message, not a call. |

Explicitly out of scope: `studies/2026-06-persona-ux/*` (dated research records — `app_show.sql`, `BUG_LOG.md`, the persona write-ups). They document what the surface looked like when the study ran and are not rewritten.

No data migration: `PREPROC.CONFIG.client_pattern` already exists and is already NULL for every rule created without one.

## Implementation Tasks

See `tasks.md`.

## Parallelization

| Parallel Group | Tasks |
|----------------|-------|
| Group A | Shared validator, `RULE_ADD`, `RULE_LIST`, CLI headers (1.1–1.5) |
| Group B | Sugar grammar (2.1–2.5) |
| Group C | Call sites and lint (3.1–3.3) |
| Group D | Docs and demo (4.1–4.5) |
| Group E | End-to-end and demo re-validation (5.1–5.2) |

Sequential dependencies:

- Group A → Group B (the sugar emits the call whose signature A changes)
- Group A → Group C
- Group A + Group B → Group D (the demo beat needs both halves; docs need the final grammar)
- Everything → Group E

Group C and Group D can run concurrently with each other once their prerequisites land.

## Dead Code Removal

| Type | Location | Reason |
|------|----------|--------|
| Comment | `sql/lua/rule_add.lua:118` | *"client_pattern is NULL (not part of this control surface)"* — the statement this plan reverses. |
| Literal | `sql/lua/rule_add.lua:125` | The hard-coded `NULL` in the `INSERT` value list, replaced by a bound parameter. |
| Narration | `demo/framework_demo.sql:181-186` | The "this is the one thing the `PREPROC ADD RULE` sugar cannot express" note and the `RULE_ENSURE` workaround it introduces. |
| Query | `demo/framework_demo.sql:193-194` | The direct `SELECT … FROM PREPROC.CONFIG`, which only existed because `RULE_LIST` omitted the column. |

`RULE_ENSURE` is NOT removed — it remains the module installer's idempotent path.

## Verification

### Scenario Coverage

Every scenario is covered by an integration test against a real Exasol instance: these are `EXECUTE SCRIPT` semantics and dispatched-module Lua string parsing, which unit tests cannot faithfully reproduce. The two test files already exist and follow the one-file-per-feature convention.

| Scenario | Test Type | Test Location | Test Name |
|----------|-----------|---------------|-----------|
| RULE_ADD inserts a validated rule and auto-refreshes (CHANGED) | Integration | `tests/test_control_admin_scripts.py` | `test_rule_add_inserts_and_refreshes` (update) |
| RULE_ADD persists a client_pattern verbatim | Integration | `tests/test_control_admin_scripts.py` | `test_rule_add_persists_client_pattern_verbatim` |
| RULE_ADD treats a NULL client_pattern as all clients | Integration | `tests/test_control_admin_scripts.py` | `test_rule_add_null_client_pattern_means_all_clients` |
| RULE_ADD rejects an empty or whitespace-only client_pattern | Integration | `tests/test_control_admin_scripts.py` | `test_rule_add_rejects_empty_client_pattern` |
| RULE_ADD rejects an over-length client_pattern without truncating | Integration | `tests/test_control_admin_scripts.py` | `test_rule_add_rejects_overlength_client_pattern` |
| RULE_ADD validates the client_pattern before any side effect | Integration | `tests/test_control_admin_scripts.py` | `test_rule_add_client_pattern_validated_before_side_effects` |
| RULE_ADD keeps its parameter list aligned with RULE_ENSURE | Integration | `tests/test_control_admin_scripts.py` | `test_rule_add_parameter_list_matches_rule_ensure` |
| A single shared validator governs client_pattern on every write path | Integration | `tests/test_control_admin_scripts.py` | `test_client_pattern_validation_shared_by_rule_add_and_rule_ensure` |
| RULE_LIST returns the current rules and is read-only (CHANGED) | Integration | `tests/test_control_admin_scripts.py` | `test_rule_list_returns_rows_readonly` (extend to assert `CLIENT_PATTERN`) |
| ADD RULE command expands to a RULE_ADD execute-script call (CHANGED) | Integration | `tests/test_command_sugar.py` | `test_add_rule_expands_to_rule_add` (update) |
| ADD RULE with a CLIENT clause threads a client_pattern argument | Integration | `tests/test_command_sugar.py` | `test_add_rule_client_clause_threads_pattern` |
| The CLIENT value is carried verbatim and never identifier-folded | Integration | `tests/test_command_sugar.py` | `test_add_rule_client_value_carried_verbatim` |
| CLIENT composes with NOREFRESH in either combination | Integration | `tests/test_command_sugar.py` | `test_add_rule_client_composes_with_norefresh` |
| A quoted pattern is never mistaken for the NOREFRESH suffix | Integration | `tests/test_command_sugar.py` | `test_add_rule_quoted_norefresh_pattern_not_stripped` |
| A malformed CLIENT clause is declined fail-open | Integration | `tests/test_command_sugar.py` | `test_malformed_add_rule_client_clause_declined` |
| An empty CLIENT operand is declined because Exasol collapses it to NULL | Integration | `tests/test_command_sugar.py` | `test_add_rule_empty_client_operand_declined` |
| The CLIENT keyword is matched case-insensitively like every other keyword | Integration | `tests/test_command_sugar.py` | `test_add_rule_client_keyword_case_insensitive` |
| The CLIENT grammar change is an in-place V1 edit under the pre-release exception | Integration | `tests/test_command_sugar.py` | `test_client_clause_lands_in_v1_in_place` |

One end-to-end test proves the whole chain rather than any single scenario, and is the reason the folding exemption is not merely asserted at the string level:

| Purpose | Test Type | Test Location | Test Name |
|---------|-----------|---------------|-----------|
| A rule created through the SUGAR dispatches for a matching client and not for a non-matching one | Integration | `tests/test_command_sugar.py` | `test_client_scoped_rule_created_via_sugar_gates_dispatch` |

The harness already exists: `tests/test_master_dispatch.py:484` `_connect_with_client_name()` opens a pyexasol connection whose reported `CLIENT` is exactly the given string, and `test_dispatch_client_pattern_match` / `test_dispatch_client_pattern_no_match` / `test_client_pattern_literal_substring` already cover the dispatch side by seeding `RESOLUTION` directly. What is new here is the full path: sugar → `RULE_ADD` → `CONFIG` → refresh → `RESOLUTION` → dispatch.

### Manual Testing

| Feature | Command | Expected Output |
|---------|---------|-----------------|
| `control/command-sugar` | `PREPROC ADD RULE FOR ROLE DBA PHASE EXPAND SCRIPT PREPROC_RT.DEMO_HELP_V1 FUNCTION help_menu CLIENT 'DbVisualizer';` | One row with the allocated `rule_id`. Exercises both folding-exempt positions in one command: `help_menu` stays lowercase and `DbVisualizer` keeps its casing. |
| `control/admin-scripts` | `PREPROC LIST RULES;` | The new rule's `CLIENT_PATTERN` reads `DbVisualizer`; every other rule reads NULL. No direct `PREPROC.CONFIG` query needed. |
| `control/admin-scripts` | `EXECUTE SCRIPT PREPROC.RULE_ADD('ROLE','DBA','EXPAND','PREPROC_RT.DEMO_HELP_V1','help_menu','',NULL);` | **Accepted**, with `client_pattern` stored as SQL NULL — an all-clients rule. Exasol collapses `''` to NULL in transit (`'' IS NULL` returns YES), so the validator never sees an empty string through SQL. Verified live 2026-07-30. This is why the SUGAR declines an empty `CLIENT ''` operand instead of carrying it through (see `control/command-sugar`). |
| `control/admin-scripts` | `EXECUTE SCRIPT PREPROC.RULE_ADD('ROLE','DBA','EXPAND','PREPROC_RT.DEMO_HELP_V1','help_menu',' ',NULL);` | Rejected with a validation error naming NULL as the way to express "all clients". No new row in `PREPROC.CONFIG`. Whitespace-only is the reachable half of the empty-pattern guard. |
| `control/admin-scripts` | `EXECUTE SCRIPT PREPROC.RULE_ADD('ROLE','DBA','EXPAND','PREPROC_RT.DEMO_HELP_V1','help_menu',NULL);` (six args, the pre-delta form) | Fails loudly with an argument-count error. Confirms the breaking change is loud, not silent. |
| `control/command-sugar` | Type `HELP;` in DbVisualizer, then in DBeaver or EXAplus | Expands in DbVisualizer; a plain syntax error elsewhere. Measured client strings: `DbVisualizer 24.1.5`, `DBeaver 23.3.0`, `PyExasol 2.2.1`, `EXAplus`. |

### Checklist

| Step | Command | Expected |
|------|---------|----------|
| Environment | `uv sync` | Exit 0 |
| Test | `uv run pytest` | 0 failures |
| Lint (Python) | `uv run ruff check .` | 0 errors |
| Format | `uv run ruff format --check .` | No changes |
| Lint (Lua) | `luacheck sql/lua/` | 0 warnings |
| Demo | `framework_demo.sql` run clean from a fresh `release.sql` install | Beat 5 driven by the sugar, no `RULE_ENSURE` workaround |

Contract checks to confirm before calling the plan done:

* [ ] `RULE_ADD`'s parameter list is byte-identical to `RULE_ENSURE`'s.
* [ ] `validate_client_pattern` exists in exactly one place and is called by both writers.
* [ ] Empty, whitespace-only, and over-length patterns rejected before mutation through BOTH writers.
* [ ] Sugar accepts the 11- and 13-word forms, each with and without `NOREFRESH`.
* [ ] A pattern containing a space survives tokenisation verbatim; `CLIENT 'NOREFRESH'` is not stripped as the suffix.
* [ ] The `CLIENT` value is not upper-cased anywhere on the path.
* [ ] A malformed `CLIENT` clause declines fail-open; no malformed `EXECUTE SCRIPT` is ever emitted.
* [ ] `CLIENT ''` declines (Exasol collapses `''` to NULL, so carrying it through would bypass the validator and create an all-clients rule); `CLIENT ' '` still carries through to the validator's explanatory rejection.
* [ ] `RULE_LIST` returns `CLIENT_PATTERN`.
* [ ] Every Migration call site updated; every verify-only site confirmed unchanged.
* [ ] The sugar module header records `CLIENT` as a folding-exempt position AND still requires `CONTROL_SUGAR_V2` for a post-release grammar change.
* [ ] Docs state the scoping-not-security position.
