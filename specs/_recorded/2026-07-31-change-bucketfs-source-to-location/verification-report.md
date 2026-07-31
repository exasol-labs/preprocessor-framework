# Verification Report: change-bucketfs-source-to-location

**Generated:** 2026-07-31

## Verdict

| Result | Details |
|--------|---------|
| **PASS** | The gating premise held under a genuinely falsifiable live test; the resolver reads a BucketFS source as a plain location with zero `exa` references; the full suite is green live (480 passed / 43 skipped / **0 failed**). Three items are open and none block: one plan-checklist row cannot execute (`pytest-cov` is not a project dependency), the demos ran beats 1–5 only (a `0.3.0` release could not be staged into the public bucket), and the live suite was observed to empty `PREPROC.CONFIG` — a pre-existing test-hygiene defect unrelated to this change. |

| Check | Status |
|-------|--------|
| Build (`uv sync`, fresh `release.sql` deploy) | ✓ 40/40 statements OK |
| Tests | ✓ 480 passed, 0 failed (live) |
| Lint (ruff, luacheck) | ✓ |
| Format (ruff) | ✓ |
| Scenario Coverage | ✓ all 19 scenarios have a passing test |
| Manual Tests | ◻️ partial — probes and provenance ✓, demo beat 6 not exercised |

## Test Evidence

### Coverage

| Type | Coverage % |
|------|------------|
| Unit | **not measured** — `pytest-cov` is not installed and `cov` appears nowhere in `pyproject.toml`, so `plan.md`'s `uv run pytest --cov=preproc` row cannot run: `pytest: error: unrecognized arguments: --cov=preproc`. Recorded as a plan defect; a dependency was deliberately **not** added to satisfy a checklist row. |
| Integration | not measured (same reason) |

### Test Results

| Type | Run | Passed | Ignored |
|------|-----|--------|---------|
| Offline resolver suite (hermetic, DB env stripped) | 50 | 50 | 0 skips |
| Full suite, no cluster | 521 | 240 | 281 skipped |
| Full suite, live cluster (authoritative) | 523 | **480** | 43 skipped, **0 failed** |

The offline resolver suite is the plan's R10 requirement: it runs with no cluster and **no skips**, and it exercises the real `<service>/<bucket>/<path>` derivation through the rebindable `MOUNT_ROOT` seam rather than bypassing it.

### Manual Tests

| Test | Result |
|------|--------|
| ConfD `ADD BUCKET … PRIVATE` provisioning; `SHOW SERVICE` reports `public \| false` | ✓ sugar form worked first try; corroborated by unauthenticated HTTP 403 on the bucket vs 200 on `default` |
| Premise: read-protected bucket readable by a granted caller, CONNECTION never named | ✓ see Gate below |
| Premise negative control: revoke → not readable | ✓ see Gate below |
| Hard break: two-segment source → `bad_source` | ✓ `malformed bucketfs source (expected bucketfs:<service>/<bucket>/<path>): 'bucketfs:PREPROC_BFS/preproc-lib-0.3.0.tar.gz'`. No `get_connection` error, no hang |
| Message contract: missing release → `source_unreadable` with all three elements | ✓ names the tarball path, the auto-extracted directory path, and the `CONNECTION TO 'bucketfs:bfsdefault/default' IDENTIFIED BY '<read-password>'` prerequisite |
| Sugar: `PREPROC CATALOG MODULES FROM '<location>'` | ✓ listed `cast_shorthand` and `trailing_comma` |
| `PREPROC INSTALL MODULE … FROM '<location>' FOR ROLE ANALYSTS` | ✓ `installed` |
| Provenance: `source_url` shows the location form, matching the runbook | ✓ captured live and written into `docs/air-gap-runbook.md`; the `TASK-7.2-PROVENANCE` placeholder marker is gone |
| Docs sweep grep | ✓ every hit classified as legitimate (see Notes) |
| Demos run clean from a fresh `release.sql` install | ◻️ **partial** — `demo/03_module_from_bucketfs.sql` and `demo/library_demo.sql` beats 1–5 ran; beat 6 (publishing a module) and the file-02 prerequisite were not exercised |

**Disclosed version substitution.** Every live probe used `preproc-lib-0.3.1.tar.gz`, not the `0.3.0` the plan's table names. The public `default` bucket held only a pre-existing `0.3.1` object, and staging a `0.3.0` was impossible because `EXASOL_BFS_WRITE_PASSWORD` for that bucket could not be obtained (deriving it requires base64-decoding a ConfD secret, which the environment blocks). The two releases carry the same `cast_shorthand` / `trailing_comma` sha256, so this is a version substitution, not a masked failure. It is called out at every site it touches.

## The gate: R11, verified and falsifiable

The plan rested on an unverified platform claim, and the standard test bucket is publicly readable — so the existing suite would have passed whether or not the claim held. Verification was therefore a gate, not a checkbox.

| Observation | Result |
|---|---|
| Granted → readable | `bucket_isdir=True`, index read `OK 1607 bytes`, and `/proc/mounts` shows `/buckets/bfsdefault/preproc-protected` present |
| Revoked → NOT readable | `bucket_isdir=False`, `FileNotFoundError: [Errno 2]`, and the bind mount is **gone** from `/proc/mounts` |

`/buckets` is a tmpfs and each accessible bucket is an individual read-only bind mount that the grant adds and the revoke removes — the mechanism, not merely the symptom.

**The negative control required a non-superuser.** `SYS` reads a read-protected bucket without any grant, so a grant/revoke assertion run as `SYS` cannot fail and is not a test. Evidence was produced as a purpose-made non-superuser, and `tests/test_db_resolver.py::test_read_protected_bucket_resolves_only_while_granted` preserves that constraint.

**Correction to the recorded evidence (2026-07-31).** The first write-up of control A claimed `SYS` read the bucket with "no CONNECTION to it in existence at all". That overstates `SYS`'s reach. Re-probing showed the mount is conditioned on the CONNECTION *object existing*; what `SYS` bypasses is only the *grant*. R11 is unaffected — the revoke removed a grant while the CONNECTION continued to exist — but the original wording would have led anyone re-deriving the evidence to conclude the platform had changed. Both affected passages in `decision-log.md` are corrected and the correction is dated.

## Tool Evidence

### Linter

```
$ uv run ruff check .
All checks passed!

$ luacheck sql/lua/
Total: 0 warnings / 0 errors in 22 files
```

### Formatter

```
$ uv run ruff format --check .
50 files already formatted
```

### Contract greps

```
$ grep -n "get_connection" sql/udf/module_resolver_v1.sql   -> no output
$ grep -n "exa\."         sql/udf/module_resolver_v1.sql    -> no output
$ grep -n "_mount_base"   sql/udf/module_resolver_v1.sql    -> no output
$ grep -n "MOUNT_ROOT"    sql/udf/module_resolver_v1.sql
626:MOUNT_ROOT = '/buckets'          <- defined once
665:    mount_base = MOUNT_ROOT + '/' + service + '/' + bucket   <- read once
(two further hits are comment mentions)
```

The deployed `SCRIPT_TEXT` on the live instance was independently confirmed to contain 0 occurrences of `get_connection` and 0 of `exa.`.

## Scenario Coverage

All 19 scenarios in `plan.md` § Verification → Scenario Coverage have a passing test.

| Domain | Feature | Scenario | Test Location | Test Name | Passes |
|--------|---------|----------|---------------|-----------|--------|
| module | db-resolver | Resolves a BucketFS-staged tarball (CHANGED) | `tests/test_db_resolver.py` | `test_resolver_reads_bucketfs_release` | Pass |
| module | db-resolver | Resolves a BucketFS-staged tarball (CHANGED) | `tests/test_module_resolver_udf.py` | `test_resolves_extracted_release`, `test_resolves_tarball_release` | Pass |
| module | db-resolver | A BucketFS source is the location, never a CONNECTION name | `tests/test_module_resolver_udf.py` | `test_bucketfs_path_makes_no_connection_lookup` | Pass |
| module | db-resolver | A two-segment BucketFS source is malformed | `tests/test_module_resolver_udf.py` | `test_two_segment_source_is_bad_source` | Pass |
| module | db-resolver | A two-segment BucketFS source is malformed | `tests/test_db_resolver.py` | `test_resolver_rejects_legacy_connection_form` | Pass |
| module | db-resolver | A source segment that escapes the bucket is malformed | `tests/test_module_resolver_udf.py` | `test_traversal_segment_is_bad_source` (9 params) | Pass |
| module | db-resolver | Unreadable location names both paths + the credential prerequisite | `tests/test_module_resolver_udf.py` | `test_unreadable_location_message_names_paths_and_grant` | Pass |
| module | db-resolver | Unreadable location names both paths + the credential prerequisite | `tests/test_db_resolver.py` | `test_resolver_unreadable_location_reports_credential_hint` | Pass |
| module | db-resolver | A read-protected bucket is readable through a granted CONNECTION the source never names | `tests/test_db_resolver.py` | `test_read_protected_bucket_resolves_only_while_granted` | Pass |
| module | db-install | Installs from a BucketFS source, end-state byte-identical (CHANGED) | `tests/test_db_module_install.py` | `test_install_from_bucketfs_matches_cli_end_state` | Pass |
| module | db-install | A malformed source aborts before any database write | `tests/test_db_module_install.py` | `test_install_malformed_bucketfs_source_aborts_before_write` | Pass |
| module | db-install | An unreadable location surfaces the credential prerequisite | `tests/test_db_module_install.py` | `test_install_unreadable_location_reports_credential_hint` | Pass |
| module | db-catalog | Catalogs a BucketFS source's modules with installed state (CHANGED) | `tests/test_db_module_catalog.py` | `test_catalog_lists_bucketfs_modules` | Pass |
| module | db-catalog | A malformed source is reported without a write | `tests/test_db_module_catalog.py` | `test_catalog_malformed_bucketfs_source_reports_without_write` | Pass |
| control | module-sugar | INSTALL MODULE expands to MODULE_INSTALL (CHANGED) | `tests/test_command_sugar.py` | `test_install_module_expands_to_module_install` | Pass |
| control | module-sugar | FOR USER scope threads a USER-typed scope argument (CHANGED) | `tests/test_command_sugar.py` | `test_install_module_for_user_threads_user_scope` | Pass |
| control | module-sugar | CATALOG MODULES expands to MODULE_CATALOG (CHANGED) | `tests/test_command_sugar.py` | `test_catalog_modules_expands_to_module_catalog` | Pass |
| control | module-sugar | The source token stays opaque at any path depth | `tests/test_command_sugar.py` | `test_module_source_token_carried_verbatim_at_any_depth` | Pass |
| install | schema-layout | The BucketFS CONNECTION is never created or named by the framework | `tests/test_schema_layout.py` | `test_install_ddl_creates_no_connection`, `test_no_sql_source_names_a_connection_in_a_bucketfs_path` | Pass |

## Contract checks

| Check | Result |
|---|---|
| Task 0.1 live evidence (grant → OK, revoke → `source_unreadable`) in `decision-log.md` under Design Decision [1], with date and instance | ✓ plus a dated correction |
| `grep -n "get_connection"` returns nothing | ✓ |
| `grep -n "exa\."` returns nothing | ✓ |
| `_mount_base` no longer exists | ✓ |
| `MOUNT_ROOT` defined once, read only by the BucketFS reader | ✓ |
| A two-segment source yields `bad_source` and never touches the filesystem | ✓ proven by an `os` tripwire **with a positive control** |
| The `source_unreadable` message carries all three elements | ✓ asserted with path labels, so it cannot pass vacuously |
| Exactly one `source_unreadable` kind; no bucket-existence probe added | ✓ |
| `make_fake_exa` gone and unimported | ✓ |
| Offline suite runs with no cluster and exercises the derivation | ✓ 50 passed, 0 skips |
| `tests/conftest.py` still creates and grants the CONNECTION; the source does not name it | ✓ |
| The sugar's `.lua` body unchanged apart from a comment | ✓ |
| The dual-meaning passage is gone from `docs/air-gap-runbook.md` | ✓ |
| Docs still instruct the operator to create and grant the CONNECTION (R13) | ✓ all 6 blocks preserved |
| `studies/**` carries a dated superseded note and is otherwise untouched | ✓ historical prose genuinely unrewritten |

## Notes

### Verification depth worth recording

The two riskiest claims were each attacked rather than asserted:

- **The test seam.** Group B mutated the resolver body in memory 8 ways; the reviewer independently mutated it 14 ways. Every mutation — transposed `service`/`bucket`, absolute-path bypass, probe-before-parse, reintroduced `exa.get_connection`, inlined `MOUNT_ROOT`, deleted traversal check, dropped credential hint — was caught by the test that owns the corresponding requirement. No mutation slipped through.
- **The R11 test's dependency.** The review found that `test_read_protected_bucket_resolves_only_while_granted` never staged the release it asserted on, silently free-riding on an artefact left on one instance. On any other instance it would fail *looking exactly like the premise being falsified*. The fix was proven by deleting the leftover, observing the unmodified test fail as predicted, then passing three consecutive times with the bucket verified empty beforehand.

### Grep-sweep classification

`plan.md`'s sweep gate ("no hits outside `specs/_recorded/**` and `studies/**`") cannot pass as written. Every hit is legitimate and falls into four buckets:

1. **Permanent specs** (8 literals in `specs/control/module-sugar/`, `specs/module/db-{catalog,install,resolver}/`) — stale **by design** until `/speq:record` merges this plan's deltas. Verified the deltas carry the new form. **Not hand-edited.**
2. **This plan's own files** — plan, tasks, decision log *describing* the old form.
3. **Four `tests/` literals** whose rejection is under test, each with an in-file comment saying so.
4. **One verbatim error message** in `docs/air-gap-runbook.md` demonstrating the retired form being rejected.

The gate wording should be amended to exempt `specs/**` pre-record.

### Open items — none blocking

1. **`pytest-cov` is not a project dependency**, so the plan's coverage row cannot run. A dependency was not added to satisfy it. Either add `pytest-cov` or drop the row.
2. **Live-suite hygiene defect (pre-existing, out of scope).** Running the full live suite left `PREPROC.CONFIG` completely empty — including the two shipped sugar seeds — so some test deletes rows it does not own, or a teardown over-matches. Unrelated to this change (a second fresh install was done before the manual probes so they ran against known-clean state), but worth its own fix.
3. **Demo replay is partial.** Beats 1–5 of both demos ran; beat 6 and the file-02 prerequisite did not. Blocked by the unavailable `default`-bucket write password.
4. **ADR numbers are not yet cited in the new resolver comments.** Deliberate — the numbers (ADR-065/066/067) are only assigned when `/speq:record` promotes the decisions. The miscitation the review found (`ADR-064` where the hard break is decision [2]) is fixed to `ADR-065`.

### Unrelated edits riding along in the working tree

Per the user's instruction these stay and are called out here for the commit message:

- `demo/framework_demo.sql` — `CLIENT 'DbVisualizer'` → `'dbvis'` (~line 187) and a deleted comment (~line 190). Pre-existing uncommitted user edits, unrelated to the source grammar.
- `uv.lock` — one line, `preprocessor-framework` `0.7.1` → `0.8.0`. `pyproject.toml` at HEAD already said `0.8.0`; the committed lock was stale and a `uv run` refreshed it.

### Live instance state

`bfsdefault/preproc-protected` remains (empty), consumed by `bucketfs_protected_config()`. Two modules (`cast_shorthand` v1, `trailing_comma` v2) and their CONFIG rules remain from the demo/probe runs. The ConfD add-on installed for Task 0.0 was removed by the fresh `release.sql` install; `examples/confd_admin.md`'s `bucket_add` marker was promoted from ◻️ to ✅ on the strength of that live exercise.
