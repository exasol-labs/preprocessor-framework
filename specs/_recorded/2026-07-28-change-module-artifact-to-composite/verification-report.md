# Verification Report: change-module-artifact-to-composite

**Generated:** 2026-07-28

## Verdict

| Result | Details |
|--------|---------|
| **PASS** | All 52 plan scenarios have a passing test. Full suite green against a live Exasol instance: 436 passed, 0 failed. All 22 code-review findings resolved, including two that would have silently broken the feature's core guarantees. |

| Check | Status |
|-------|--------|
| Build (`uv sync`) | ✓ |
| Tests (live) | ✓ 436 passed / 0 failed / 32 skipped |
| Lint (`ruff check .`) | ✓ 0 errors |
| Format (`ruff format --check .`) | ✓ 45 files already formatted |
| Lua lint (`luacheck sql/lua/`) | ✓ 0 warnings / 0 errors in 22 files |
| Scenario Coverage | ✓ 52 / 52 |
| Manual Tests | ✓ 8 / 8 executed |

Verified against Exasol Personal at `18.195.244.225:8563` with BucketFS staging
enabled (`EXASOL_BFS_BUCKET=default`, PUT+GET round-trip confirmed) and a local
Lua 5.1 interpreter for cross-language conformance.

## Test Evidence

### Test Results

| Type | Run | Passed | Skipped |
|------|-----|--------|---------|
| Unit + Integration (live, full suite) | 468 | 436 | 32 |
| Offline only (no database) | 467 | 214 | 253 |
| Library repo (offline) | 17 | 13 | 4 pre-existing failures — see Notes |

The 32 live skips are environment-gated, not coverage gaps — sampled reasons: a
reachable HTTPS registry (`PREPROC_TEST_HTTPS_BASE`), the ability to mint a second
user for the non-admin privilege check (covered structurally by
`test_status_requires_admin`), and demo-only fixtures.

Verified directly rather than inferred: running only the 50 framework-side plan
scenario tests gives **49 passed, 1 skipped**, and that single skip is a transient
BucketFS staging timeout (`The read operation timed out`) in
`test_bucketfs_install_matches_cli_end_state`, not a gate. Re-run in isolation it
passes, together with `test_python_and_lua_splitters_agree_on_corpus` — 2 passed.
So every plan scenario has been observed passing live.

### Manual Tests

| Test | Command | Result |
|------|---------|--------|
| Composite validates and lists its inventory | `preproc module validate widget --registry <fixtures>` | ✓ exit 0, "artifact inventory matches the manifest (3 objects)" |
| Undeclared Python UDFs rejected statically | `preproc module validate sneaky --registry <fixtures>` | ✓ exit 1, names `SNEAKY_A_V1` and `SNEAKY_B_V1` |
| Back-compat: `cast_shorthand` unedited | `preproc module validate cast_shorthand --registry <library>` | ✓ exit 0, 1 object derived from `script_name` |
| Back-compat: `trailing_comma` unedited | `preproc module validate trailing_comma --registry <library>` | ✓ exit 0 (now reports v2) |
| Offline bundle of a composite | `preproc module bundle widget --output off.sql` | ✓ exit 0, "Wrote 6 statements" |
| Bundle is hand-runnable | re-split `off.sql` with the canonical splitter | ✓ 6 statements, 2 `--/` blocks, 2 lone `/`, no CRLF |
| Bundle carries the full inventory | inspect the generated `MODULE_REGISTER` | ✓ 12 arguments, 3-line `TYPE\|NAME` literal intact inside one statement |
| Index regeneration deterministic | `generate_index` twice, compare | ✓ byte-identical, `schema_version: 2` |

## Tool Evidence

### Linter

```
$ uv run ruff check .
All checks passed!

$ uv run ruff format --check .
45 files already formatted
```

### Lua linter

```
$ luacheck sql/lua/
Total: 0 warnings / 0 errors in 22 files
```

### Cross-language splitter conformance

```
$ PREPROC_TEST_LUA=/home/nc/.luaenv/bin/lua uv run pytest -k "lua_splitter or corpus"
21 passed, 1 skipped
```

Python and Lua splitters agree statement-for-statement on all 19 corpus cases and
both reject both error cases. Proven against a real Lua 5.1 interpreter locally,
not deferred to CI.

## Scenario Coverage

All 52 scenarios from the plan's Scenario Coverage table map to a test that
exists and passes. Audited programmatically against the test files.

| Feature | Scenarios | Location | Passing |
|---------|-----------|----------|---------|
| module/contract | 11 | `tests/test_module_contract.py`, `tests/test_db_module_install.py` | 11 / 11 |
| module/db-install | 8 | `tests/test_db_module_install.py` | 8 / 8 |
| module/db-resolver | 5 | `tests/test_db_resolver.py` | 5 / 5 |
| module/db-catalog | 3 | `tests/test_db_module_catalog.py` | 3 / 3 |
| module/lifecycle | 7 | `tests/test_module_lifecycle.py` | 7 / 7 |
| module/cli | 7 | `tests/test_module_cli.py`, `tests/test_cli_bundle.py` | 7 / 7 |
| module/registry | 5 | `tests/test_module_registry.py` | 5 / 5 |
| install/schema-layout | 2 | `tests/test_schema_layout.py` | 2 / 2 |
| library/scaffolding | 4 | library repo `tests/test_library_modules.py` | 4 / 4 |

Two coverage rows were corrected during this audit: the `validate` scenario named
`test_validate_scratch_deploy_tears_down_all_objects`, a test abandoned when the
scratch-deploy approach was replaced (see Notes). It now names the two tests that
actually assert the guarantee.

## Requirements

| Requirement | Evidence |
|-------------|----------|
| Back-compat, manifests | `cast_shorthand` and `trailing_comma` `module.toml` validate unedited; `git diff modules/cast_shorthand/module.toml` empty |
| Back-compat, indexes | `test_schema_version_1_index_still_reads`, `test_schema_version_1_entry_single_statement`, `test_catalog_v1_entry_single_object` all pass live |
| Back-compat, provenance | `test_null_inventory_row_reclaims_entry_script` passes live |
| Air-gap | Bundle re-splits into exactly the statements the generator reported, delimiters preserved, no CRLF, inventory literal intact |
| Integrity | `sha256` remains a whole-file digest; `test_sha256_mismatch_rejected_multi_statement` and `test_sha256_mismatch_aborts_before_write` pass |
| Parity | `test_bucketfs_install_matches_cli_end_state` passes live |

## Code Review

22 findings, all resolved. Two were severe enough that shipping them would have
broken the feature's central promises:

1. **`sync` and `update` recorded a NULL inventory.** Both had the pre-flighted
   plan in scope and discarded `plan.objects`. A composite installed via
   `preproc module sync` would later reclaim only its entry script, delete the
   provenance row, and permanently orphan every companion object with no record
   they existed. The recorded inventory *is* this feature's ownership boundary.
2. **The offline bundle emitted 11 arguments to a 12-parameter script**, breaking
   the air-gap path end to end — a stated plan requirement — aborting at
   registration after every artifact statement had already committed.

Also found and fixed: the resolver skipped the `_V<N>` and duplicate-object
checks the CLI enforces (a parity break that made the widened reclaim boundary
unsafe via the DB path); an unchecked inventory width whose failure landed *after*
rule registration, leaving a module live with no provenance row — the exact
inverse of the "a partial install is inert" guarantee; and a lossy first-dot split
for a quoted schema containing a dot, where reclaim would report success while the
real table survived untracked.

Three tests that could not fail for the interesting reason were sharpened, each
verified RED before its fix.

## Notes

### Live validation promises less than the plan asked for

There is no `--live` flag. `preproc module validate <name>` always runs the static
checks, and additionally runs the live checks when a database is configured in the
environment — so live validation is a mode entered by having `EXASOL_*` set, not
one the operator requests.

The plan specified that the live check "deploys the whole artifact to a scratch
generation". That is not soundly implementable, and the original attempt was not
merely incomplete — it was unsafe. It appended `__VALIDATE` to each statement's own
creation site while executing all statements live, which produced: false failures
on conformant composite modules whose statements cross-reference each other; real
objects written into live schemas for a module-owned-schema artifact; syntactically
invalid SQL and DROPs naming objects it had not created for quoted identifiers; and
`GRANT`/`INSERT` statements executed verbatim against live objects.

Live validation now executes nothing the artifact wrote. It compiles every
`SCRIPT` statement under a disposable name inside one throwaway
`PREPROC_VALIDATE_<entropy>` schema, probes the entry function through MASTER's own
`import` binding, checks the returned type against the phase contract, and drops
the schema afterward. `TABLE`/`VIEW`/`FUNCTION`, schema/connection DDL, and
non-creating statements are reported as not deployed rather than faked. For a
composite, an invocation error is reported `inconclusive`, never as a failure.
Single-statement modules behave exactly as before.

The `module/cli` spec delta was reworded to match, and a scenario added asserting
that live validation never creates, replaces, or drops a real object of the
module. Without that correction, `/speq:record` would have merged the unsafe
description into the permanent spec library as the durable contract.

### The full-suite run caught what targeted runs hid

`tests/test_db_module_catalog.py` deployed `MODULE_REGISTER` with 11 parameters.
Because that is `CREATE OR REPLACE` against a session-scoped shared instance, it
replaced the correct 12-parameter script for every test that ran afterward —
9 failures across 3 unrelated files. Every targeted per-file run passed, since
each file deployed its own version last. All four deploy paths now declare
`objects` and no 11-parameter header remains.

### Objects column widened to VARCHAR(200000)

`VARCHAR(4000)` held ~39 objects at realistic name lengths, constraining exactly
the subsystem-module case ADR-057 blesses. Widened across all **eight** load-bearing
declarations (five schema/CAST/EMITS sites plus three separately pinned
`OBJECTS_MAX_CHARS` copies, two of which cannot import the canonical constant).
After the width gate landed, the smallest declaration silently becomes the real
limit, so `test_every_pinned_objects_width_equals_the_canonical_constant` now scans
every shipped SQL/Lua/Python file and fails on any drift. The idempotent widening
uses `ALTER TABLE PREPROC.MODULES MODIFY (objects VARCHAR(200000))`, which is
declarative and therefore convergent on re-run, and converts values in place rather
than dropping the column.

### Library repo

`trailing_comma`'s in-flight fix was promoted to `_V2` rather than mutating the
live `_V1` body. V1 is restored byte-identical to publication (`50358aa9…`
verifies); V2 carries the fix at `44f3bd0f…`. Note the registry globs one
`module.toml` per module directory and so keys by name alone — **V2 replaces V1 in
the index rather than coexisting.** V1's artifact remains on disk, so an install
pinned to its digest still resolves to identical bytes, but v1 is no longer
installable from the registry.

Four library-repo test failures are **pre-existing**, reproduced at library HEAD
paired with framework HEAD (both pre-plan): three trace to `modules/_template`'s
`_`-prefixed name failing the framework's `_NAME_RE` (introduced by commit
`0a9d16e`, unrelated to this plan), one to `index_drift` defaulting to
`ref="main"` while the committed index is stamped `v0.2.1`. Left untouched.

### The two repos must land together

Deleting `verify_artifact_script_name` broke the library repo's entire test suite
— 4 collection errors, 0 tests running — because four library files imported it.
Fixed here, but measured: library HEAD against the new framework fails collection
outright. The library's `index.json` at `schema_version: 2` and its
`verify_artifact_inventory` imports do not work against a released framework
predating this change, so the two repos need to be released as a pair.

## Follow-ups (not in this plan's scope)

| Item | Why it matters |
|------|----------------|
| Regenerate `release.sql` | It declares `MODULE_REGISTER` with 9 parameters, missing `source_url`/`library_version` from before this plan as well as `objects`. It is a generated release artifact, so stale rather than broken — but a hand-carried install from the current file fails at registration. |
| `SET DEFINE OFF;` in generated `.sql` | EXAplus substitutes `&`-prefixed user variables by default, so a quoted identifier containing `&` would be silently mangled. Same class as the `:name` DbVisualiser corruption already documented in `release.sql`'s header, and it affects `release.sql` too. Low likelihood, real. |
| `validate.py` static width check | The encoded-inventory limit is enforced in `deploy.plan_artifact` and both in-DB layers, but a publisher does not see it at `validate` time. |
| `modules/_template` name | Its `_` prefix fails the framework's `_NAME_RE`, causing 3 pre-existing library test failures. Either rename it or exempt template directories. |
| `derive_object` on a comment-only block | Yields the comment as a statement, which is then sent to `pquery`. Harmless (Exasol tolerates it) but untidy; deferred because `artifact.py` is byte-identity-locked to the resolver embed. |
| O(n²) re-slicing in `artifact.py` | Two scans re-slice where the Lua sibling deliberately carries an index. A 2 MB single-line artifact costs ~1.6 s inside the UDF. Not a correctness issue and far from the MASTER dispatch cliff; deferred for the same byte-identity reason. |

## Ready for

`/speq:record change-module-artifact-to-composite`
