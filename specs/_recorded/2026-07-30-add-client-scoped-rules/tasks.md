# Tasks: add-client-scoped-rules

Make `PREPROC.CONFIG.client_pattern` settable through `RULE_ADD` and the
`PREPROC ADD RULE` sugar, visible in `RULE_LIST`, and validated once for every
write path.

Tests are folded into their subject task so each is built test-first (TDD).
Group B depends on Group A: the sugar emits the `RULE_ADD` call whose signature
Group A changes.

Tasks tagged `[expert]` need deep reasoning — Lua string parsing with fail-open
semantics, and a cross-file extraction that changes when a module is imported.

## Group A - Shared validator, RULE_ADD, RULE_LIST (foundation)

- [x] 1.1 `sql/lua/refresh_core.lua`: export `validate_client_pattern(cp)` as a
      top-level global function (plain `function name()` syntax so `import()`
      finds it in the module table), extend the `-- luacheck: globals rebuild`
      declaration, and widen the module header's Role section from "the
      resolution rebuild" to shared logic for `PREPROC.REFRESH` and the mutating
      admin scripts. Accept NULL (both Lua `nil` and the Exasol `null`
      sentinel); reject empty, whitespace-only, and `> 256` characters; never
      truncate; error messages name the cause and name NULL as the way to
      express "all clients" [expert]
- [x] 1.2 `sql/lua/rule_add.lua`: add the `client_pattern` parameter between
      `function_name` and `norefresh` (header comment block, `luacheck: read
      globals` line), move `import("PREPROC.REFRESH_CORE", "rc")` to the top so
      it precedes validation, call `rc.validate_client_pattern(client_pattern)`
      as Step 4 after script existence and before any DML, bind the value as a
      named `:param` replacing the hard-coded `NULL` literal, and issue the
      NULL and non-NULL `INSERT` shapes the way `rule_ensure.lua` already does.
      Delete the `client_pattern is NULL (not part of this control surface)`
      comment [expert]
- [x] 1.3 `sql/lua/rule_ensure.lua`: move its `import("PREPROC.REFRESH_CORE",
      "rc")` to the top and call the same `rc.validate_client_pattern()` as a
      new step after script existence, before the match query and any DML. No
      other change — its parameter list and upsert semantics are already correct
- [x] 1.4 `sql/lua/rule_list.lua`: add `client_pattern` to the `SELECT`, to the
      row marshalling loop, to the `exit()` column spec as
      `client_pattern VARCHAR(256)`, and to the Result-contract header comment.
      Position it after `scope_value`, mirroring `PREPROC.CONFIG` column order
- [x] 1.5 `src/preproc/install.py:348` and `src/preproc/bundle.py:122`: add
      `client_pattern` to the `RULE_ADD` `CREATE` header parameter list so it
      matches the `RULE_ENSURE` header two entries below it
- [x] 1.6 `tests/test_control_admin_scripts.py`: the eight `RULE_ADD` /
      `RULE_LIST` / shared-validator tests named in `plan.md` § Scenario
      Coverage, plus updating the existing `test_rule_add_*` fixtures and the
      header assertion at line 51 for the new arity
- [x] 1.7 `tests/test_cli_bundle.py:514` and `tests/test_profile_codegen.py:354`:
      update for the new `RULE_ADD` header and the new call arity

## Group B - The sugar (needs A)

- [x] 2.1 `sql/lua/control_sugar_v1.lua` (in place, ADR-038 pre-release
      exception): extend the `ADD RULE` arm to accept 11 or 13 words, branching
      on the count and on `upper_word(12) == "CLIENT"` the way the
      `INSTALL MODULE` arm already branches on 8-vs-6 for `FROM`. Add a
      single-quoted-literal capture helper (the existing `undelimit` handles
      only `"..."`): unwrap the surrounding `'`, unescape doubled `''`, and
      decline on a missing operand, an unquoted operand, or an unterminated span
      — the tokenizer runs an unterminated span to end-of-text, so the closing
      quote must be checked explicitly. Capture `gsub` results into exactly one
      local [expert]
- [x] 2.2 Emit the `client_pattern` slot in the position between
      `function_name` and `norefresh`: the captured pattern re-quoted through
      the existing `quote()` helper, or `NULL` when the clause is absent
      (always-pass-the-slot). Confirm the `NOREFRESH` strip still runs before
      arm parsing so the arm only ever sees 11 or 13 words [expert]
- [x] 2.3 Fail-open decline for every malformed form, returning `nil` so the
      original text reaches the engine: keyword with no operand, unquoted
      operand, unterminated quote, unrecognised word in the `CLIENT` position,
      and any word count that is neither 11 nor 13 after the strip
- [x] 2.4 Extend the module header: the grammar line for `ADD RULE` gains
      `[CLIENT '<pattern>']`; the quoting-semantics block (section 10) records
      `CLIENT` as the second folding-exempt capture position alongside
      `FUNCTION`, with the reason (a self-reported application name, not an
      Exasol identifier); the pre-release-exception note continues to state that
      a POST-release grammar change requires `CONTROL_SUGAR_V2`
- [x] 2.5 Confirm no deploy-path change is needed: the seeded rules keep
      pointing at `CONTROL_SUGAR_V1` (`sql/install/70_control_sugar_seed.sql`),
      and `install.py` / `bundle.py` already emit that module unchanged
- [x] 2.6 `tests/test_command_sugar.py`: the eight `CLIENT`-clause tests named in
      `plan.md` § Scenario Coverage, plus updating the five existing expected
      expansions for the new arity — including the exact-string assertion at
      line 356, which becomes
      `EXECUTE SCRIPT PREPROC.RULE_ADD('ROLE', 'X', 'Y', 'Z', 'W', NULL, true)`

- [x] 2.7 `sql/lua/control_sugar_v1.lua`: DECLINE an empty `CLIENT ''` operand
      (fail-open `nil`) rather than carrying it through. Exasol collapses `''` to
      NULL, so carrying it through would reach `RULE_ADD` as NULL and silently
      create an all-clients rule -- re-opening the exact footgun ADR-062 closes.
      Whitespace-only still carries through to the validator's explanatory
      rejection. Correct the `control/command-sugar` delta line that specifies
      carry-through, and add the test [expert]

## Group C - Call sites and lint (needs A)

- [x] 3.1 Add the argument to each `RULE_ADD` call in `examples/demo_ping.sql`,
      `examples/demo_translate.sql`, `examples/demo_audit.sql`,
      `examples/confd_admin.sql` (two calls), and `examples/confd_admin.md`
      (two calls); update the surrounding prose in `examples/README.md`
- [x] 3.2 Verify the direct-DML sites need no change:
      `sql/install/70_control_sugar_seed.sql`, `sql/demo/seed.sql` (reserved
      band 9001–9005, `WHERE NOT EXISTS`-guarded, matched by
      `sql/demo/teardown.sql:29` — must stay direct DML, see `plan.md`
      § Consequences), and `demo/01_administration.sql:175` (quotes an error
      message, not a call)
- [x] 3.3 `luacheck sql/lua/` clean; `uv run ruff check .` and
      `uv run ruff format --check .` clean

## Group D - Docs and demo (needs A+B)

- [x] 4.1 `docs/operations.md`: the `RULE_ADD` row of the admin-script parameter
      table, the sugar mapping table at line 322 (add `[CLIENT '<pattern>']`),
      the `Adding a rule` example at line 377, and a new `client_pattern`
      subsection carrying R10 — scoping, NOT security: the client name is
      self-reported by the connecting tool and trivially spoofable, so it keeps
      conveniences out of batch jobs and never withholds a transformation from a
      principal
- [x] 4.2 `docs/how-it-works.md`: the `client_pattern matching` section gains a
      pointer to the operator-facing `CLIENT` clause and to `RULE_LIST`
- [x] 4.3 `README.md:312` Rules section and `docs/module-authoring.md:16`: add
      the clause to the surface description
- [x] 4.4 `demo/framework_demo.sql` beat 5: replace the `RULE_ENSURE` workaround
      and its "the one thing the sugar cannot express" narration with
      `PREPROC ADD RULE … FUNCTION help_menu CLIENT 'DbVisualizer'`, and replace
      the direct `SELECT … FROM PREPROC.CONFIG` at line 193 with
      `PREPROC LIST RULES`. Keep the literal-SUBSTRING explanation — it is still
      the point of the beat
- [x] 4.5 `demo/framework_demo.md`: update the beat 5 row and the
      what-to-point-at section; the beat now demonstrates both folding-exempt
      positions in one command (`help_menu` stays lowercase, `DbVisualizer`
      keeps its casing)

## Group E - End to end (needs everything)

- [x] 5.1 `tests/test_command_sugar.py`:
      `test_client_scoped_rule_created_via_sugar_gates_dispatch` — create the
      rule through the SUGAR, then confirm it fires for a matching client and
      not for a non-matching one, using
      `tests/test_master_dispatch.py:484` `_connect_with_client_name()`. This is
      the test that proves the folding exemption end-to-end rather than at the
      string level
- [x] 5.2 Re-run the demo validation: `framework_demo.sql` clean from a fresh
      `release.sql` install, with beat 5 driven by the sugar and
      `PREPROC LIST RULES` showing `CLIENT_PATTERN`

## Parallelization

Group A is the critical path; 1.1 gates 1.2 and 1.3. Groups B and C can start
once 1.1–1.5 land. Group D needs the final grammar from B for 4.1 and 4.4, but
4.2 and 4.3 can proceed after A. Group E is last.

## Out of scope

- Any change to dispatch, resolution, or profile codegen — the `client_pattern`
  half of those already satisfies its specs and is covered by existing tests.
- Wildcard or regex matching. The literal-substring contract is unchanged.
- Migrating `sql/demo/seed.sql` off direct DML — impossible without giving
  `RULE_ADD` an explicit `rule_id`, which would contradict the reserved-band
  invariant at `sql/lua/rule_add.lua:55`. No `demo/config-seed` delta.
- `studies/2026-06-persona-ux/*` — dated research records, not rewritten.
- Splitting `control/admin-scripts`, which reaches 26 scenarios after this plan.
  A library-organisation decision for the user at record time, not a task here.

## Group F - Code-review fixes

- [x] 6.1 `tests/test_command_sugar.py:976` in `test_client_clause_lands_in_v1_in_place`:
      the folding-exemption guard `assert "CLIENT" in header and "FUNCTION" in
      header` is near-vacuous -- both words occur repeatedly in that header for
      unrelated reasons, so deleting the entire section-10 folding-exemption
      paragraph leaves the test green. Assert a distinctive phrase from that
      paragraph instead. This guards the exact silent-failure mode the plan exists
      to prevent, so it must have teeth
- [x] 6.2 `tests/test_command_sugar.py:967`: replace the magic
      `assert seed.count(...) == 4` (2 rules x 2 occurrences, coupled to the
      seed's `WHERE NOT EXISTS` idempotency style) with `>= 2`, keeping the
      companion `"CONTROL_SUGAR_V2" not in seed` assertion that carries the real
      meaning
- [x] 6.3 Move `_connect_with_client_name` from `tests/test_master_dispatch.py`
      into `tests/_dbfixtures.py` and import it from both modules. The current
      cross-test-module import of a private name is the only one in the suite and
      breaks the established `tests/_*.py` shared-helper convention
- [x] 6.4 `tests/test_command_sugar.py`: drop the duplicated baseline assertion at
      lines 300 and 371 (the same `CLIENT 'DbVisualizer'` expansion is asserted
      byte-identically in four tests; 283 and 336 keep it as their control)

## Follow-ups deliberately NOT done in this plan

- The dual-`pquery` INSERT branch in `rule_add.lua` duplicates ~22 lines to vary
  one token, and could collapse to an interpolated placeholder. Not changed here:
  the idiom is entrenched in `rule_ensure.lua` (twice), so fixing it properly
  means touching that file too, which is outside this plan's scope.
- `demo/framework_demo.md` carries typographic dashes and arrows. This is
  intentional: the ASCII constraint exists because DBVisualizer reads the `.sql`
  as latin-1, and only the `.sql` is executed there. The `.md` is read in a
  browser or editor. `framework_demo.sql` is verified pure ASCII.
