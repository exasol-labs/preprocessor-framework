# Verification Report: add-client-scoped-rules

**Verdict: PASS.** All 28 tasks complete. 455 tests pass, 43 skipped, 0 failed against a
live Exasol 2026.1. `ruff check`, `ruff format --check`, `luacheck sql/lua/` and
`speq plan validate` all clean. Every scenario in both spec deltas has a passing
test, and all six manual-verification steps were executed against a fresh install.

No correctness defects were found in production code by the code review; all four
findings were test-quality and are fixed.

Date: 2026-07-30. Instance: `exasol/docker-db:latest-2026.1`, recreated clean.

---

## What shipped

`PREPROC.CONFIG.client_pattern` was honoured end to end by dispatch, resolution and
profile codegen but could not be SET through the sanctioned surface. Now:

| Change | Where |
|---|---|
| `RULE_ADD` gains `client_pattern` (6th arg, before `norefresh`) | `sql/lua/rule_add.lua` |
| One shared `validate_client_pattern()` guarding every write path | `sql/lua/refresh_core.lua`, called by `rule_add.lua` + `rule_ensure.lua` |
| `RULE_LIST` returns `CLIENT_PATTERN` | `sql/lua/rule_list.lua` |
| `PREPROC ADD RULE … [CLIENT '<pattern>']` | `sql/lua/control_sugar_v1.lua` (in place, ADR-038 pre-release exception) |
| `CREATE` header parameter lists | `src/preproc/install.py`, `src/preproc/bundle.py` |

## 5a. Automated checks

| Gate | Result |
|---|---|
| `uv run pytest -q` | **455 passed, 43 skipped, 0 failed** (457 s) |
| `uv run ruff check .` | All checks passed |
| `uv run ruff format --check .` | 50 files already formatted |
| `luacheck sql/lua/` | 0 warnings / 0 errors in 22 files |
| `speq plan validate add-client-scoped-rules` | passed, 2 delta specs validated |
| `uv run preproc bundle --release` | 40 statements; 89 `client_pattern` occurrences (was 0 — the checked-in artifact had been stale) |
| `demo/framework_demo.sql` ASCII | no non-ASCII lines |

## 5b. Scenario coverage audit

Both deltas fully covered. `control/admin-scripts` (7 scenarios) →
`tests/test_control_admin_scripts.py`; `control/command-sugar` (9 scenarios,
including the one added during implementation) → `tests/test_command_sugar.py`.

The load-bearing test is
`test_client_scoped_rule_created_via_sugar_gates_dispatch`: it creates the rule
**through the sugar** and then observes the engine's behaviour, driving sugar →
`RULE_ADD` → CONFIG → refresh → RESOLUTION → MASTER. Its value was proven by
injection, not assumed:

* Folding injected at the **sugar** (`quote(string.upper(...))`) — 6 tests fail.
* Folding injected **downstream** in `refresh_core.lua`'s CONFIG→RESOLUTION copy —
  **only this test fails** within `test_command_sugar.py`. No string-level test can
  see past the emitted text.

Both injections were reverted and the files confirmed byte-identical.

The 6.1 review fix was proven the same way: deleting the section-10
folding-exemption paragraph from `control_sugar_v1.lua` now fails
`test_client_clause_lands_in_v1_in_place`. The previous assertion left it green.

## 5c. Manual verification

Run as `SYS` from a session reporting `DbVisualizer 24.1.5`, against a fresh
install:

| # | Step | Result |
|---|---|---|
| 1 | `PREPROC ADD RULE … FUNCTION help_menu CLIENT 'DbVisualizer'` | rule_id 3 allocated |
| 2 | `PREPROC LIST RULES` | `(3, 'ROLE', 'DBA', 'DbVisualizer', 'EXPAND', 'PREPROC_RT.DEMO_HELP_V1', 'help_menu', True)` — casing intact, `CLIENT_PATTERN` present, NULL for the two seeded rules |
| 3 | `… CLIENT ' '` (whitespace-only) | rejected: `PREPROC.REFRESH_CORE: invalid client_pattern: an empty or whitespace-only pattern matches EVERY client; pass NULL to apply the rule to all clients`. No row written |
| 4 | `… CLIENT ''` (empty) | **declined by the sugar**, fail-open → engine syntax error. No row written |
| 5 | `HELP;` from three clients | `DbVisualizer 24.1.5` → fires; `DBeaver 23.3.0` → syntax error; `EXAplus` → syntax error |
| 6 | cleanup | CONFIG back to the two seeded rules |

Step 5 is the feature working as designed: the rule resolves for all three sessions
(resolution is client-agnostic), and the match happens at dispatch time.

Group E separately validated the full demo from a fresh `release.sql` install:
`release.sql` 40/40 statements OK; `framework_demo.sql` 45 executable statements,
0 unexpected failures, 1 expected non-fire (beat 5's `HELP` under a non-matching
client). Rule ids unchanged — `PREPROC DROP RULE 7` still targets the broken module.

## Decisions taken during implementation

**Exasol collapses `''` to NULL** (`'' IS NULL` → YES, verified). Discovered
independently by two agents. Consequences:

1. The empty half of the "rejects empty or whitespace-only" guard is **unreachable
   through SQL** — `RULE_ADD('…','', …)` receives the `null` sentinel and is
   accepted as all-clients. The `""` check remains as defence-in-depth for an
   in-DB Lua caller. Whitespace-only is the reachable half and is rejected.
2. Left alone, `PREPROC ADD RULE … CLIENT ''` would have **silently created an
   all-clients rule** — re-opening the exact footgun the empty-pattern guard
   exists to close, through a different door. Task 2.7 was added mid-plan: the
   sugar now declines an empty operand. The `control/command-sugar` delta was
   corrected (it had specified carry-through) and the plan's Manual Testing table
   was fixed, where it wrongly expected rejection.

**The shared validator lives in `PREPROC.REFRESH_CORE`.** A dedicated
`PREPROC.CONTROL_VALIDATE` script would have needed `resources.py` / `install.py` /
`bundle.py` / `release.sql` entries and created a new object in the locked `PREPROC`
schema, dragging `install/schema-layout` into scope for a twelve-line function.
Reviewed for coupling: the validator shares no state with `rebuild()` — no table
access, no transaction, no dependence on its SQL constants — so the two exports are
co-located, not entangled.

**The two `import()` hoists are behaviour-neutral.** `refresh_core.lua`'s top level
holds only `local` string constants and function definitions, so `import()` binds
names and does no work; `refresh.lua` already imports at top level. One observable
change, in the safe direction: on the `norefresh` path both writers now load
`REFRESH_CORE` where they previously did not, so a deployment missing that script
fails **before** any mutation rather than after a committed INSERT.

## Code review

No correctness defects in production code. All nine repo invariants verified
empirically — named `:param` binding, the `gsub` two-value rule, no defensive
`pcall`, validate-before-mutate with a pure validator, sugar fail-open across nine
malformed forms, no folding of the `CLIENT` operand, over-length rejected not
truncated, ADR-038 recorded in the module header, and `framework_demo.sql` ASCII.

Four findings, all test-quality, all fixed as Group F:

| # | Finding | Fix |
|---|---|---|
| 6.1 | Folding-exemption guard was near-vacuous — deleting the whole paragraph left it green | Asserts two distinctive phrases; teeth proven by deletion |
| 6.2 | `seed.count(...) == 4` coupled the test to the seed's idempotency style | `>= 2`, companion V2 assertion retained |
| 6.3 | Only cross-`test_*.py` import in the suite, reaching for a private name | `connect_with_client_name` moved to `tests/_dbfixtures.py` |
| 6.4 | Same baseline assertion duplicated in four tests | Dropped from the two where it was subsumed |

## Known issues, not defects of this plan

1. **The full test suite leaves `PREPROC.CONFIG` empty.**
   `tests/test_profile_codegen.py::clean_profiles` issues a bare
   `DELETE FROM PREPROC.CONFIG`, wiping the install seed, so `PREPROC ADD RULE`
   does not work after a suite run until `uv run preproc install` re-seeds. This
   bit the first manual-verification attempt. Pre-existing; also caused transient
   `Transaction collision` failures when agents ran concurrently against one
   instance. Worth its own hygiene fix.
2. **`demo/framework_demo.md` contains typographic dashes and arrows.** Deliberate:
   the ASCII constraint exists because DBVisualizer reads the `.sql` as latin-1,
   and only the `.sql` is executed there. `framework_demo.sql` is verified pure
   ASCII.
3. **`rule_add.lua`'s dual-`pquery` INSERT** duplicates ~22 lines to vary one
   token. Not changed: the idiom is entrenched in `rule_ensure.lua` (twice), so
   fixing it properly means touching that file too — outside this plan's scope.
4. **`control/admin-scripts` reaches 26 scenarios**, which trips `speq record`'s
   >10 library-organisation signal. A user decision at record time.
5. **No `module/lifecycle` delta**, deliberately. `RULE_ENSURE`'s contract is
   documented there, so a reader of that feature will not learn it validates
   `client_pattern`; the shared-validator scenario in `control/admin-scripts` names
   both scripts instead.

## Deferred

`sql/demo/seed.sql` stays direct DML — `RULE_ADD` can never allocate its reserved
9001–9005 ids (`RESERVED_DEMO_FLOOR = 9000`), it relies on `WHERE NOT EXISTS`
idempotency, and `teardown.sql` deletes by fixed id. Migrating would require an
explicit-`rule_id` parameter, contradicting the reserved-band invariant.
`studies/2026-06-persona-ux/*` keeps its six-arg calls as dated research records.

## Ready for

`/speq:record add-client-scoped-rules`

Four decisions promote to **ADR-060 through ADR-063** (continuing from ADR-059);
nothing has been written to the permanent `specs/decision-log.md` during planning
or implementation. Note for the recorder: the `control/admin-scripts` scenario text
for the empty-pattern guard should acknowledge the Exasol `''`≡NULL equivalence.
