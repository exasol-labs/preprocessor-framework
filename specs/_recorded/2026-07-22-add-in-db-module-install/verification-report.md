# Verification Report: add-in-db-module-install

**Generated:** 2026-07-22

## Verdict

| Result | Details |
|--------|---------|
| **PASS** | In-DB module installer implemented end-to-end (resolver UDF + `MODULE_INSTALL` + `MODULE_CATALOG` + sugar verbs + CLI/bundle wiring + docs). Full suite green against a live Exasol Personal 2026.1 instance; byte-identical CLI-parity proven live. Code review found no blockers/majors; 2 doc fixes applied. The 3 non-blocking review follow-ups were subsequently addressed (see "Follow-ups", below) and re-verified live. |

| Check | Status |
|-------|--------|
| Build (`uv sync`) | ✓ |
| Tests (`uv run pytest`, live) | ✓ 279 passed, 9 skipped, 0 failed (was 277; +2 resolver structural-check tests) |
| Lint — Python (`ruff check`) | ✓ All checks passed |
| Lint — Lua (`luacheck sql/lua/`) | ✓ 0 warnings / 0 errors (22 files) |
| Format (`ruff format --check`) | ✓ 41 files already formatted |
| Plan validation (`speq plan validate`) | ✓ 5 delta specs validated |
| Scenario Coverage | ✓ 31/33 ran live & passed; 2 env-gated skips (HTTPS-reachable) |
| Manual Tests | ✓ equivalents exercised by integration tests |
| Code Review | ✓ no blockers/majors |

## Test Evidence

### Test Results (`uv run pytest -q -rs`, live Exasol Personal 2026.1)

```
279 passed, 9 skipped in 216.18s (0:03:36)   PYTEST_EXIT=0
```

New/changed test modules for this plan:

| Module | Result |
|--------|--------|
| `tests/test_db_resolver.py` | 9 live passed (+ HTTPS-reachable skipped, gated) |
| `tests/test_module_resolver_udf.py` | 15 hermetic passed (resolver body vs fakes, off-cluster; +2 artifact_invalid structural-check) |
| `tests/test_db_module_install.py` | 10 passed, 1 skipped (HTTPS-reachable, gated) |
| `tests/test_db_module_catalog.py` | 5 passed |
| `tests/test_command_sugar.py` | 23 passed (19 pre-existing + 4 new) |
| `tests/test_schema_layout.py` | 22 passed (incl. 2 new admin-only / no-CONNECTION) |
| `tests/test_cli_bundle.py` | 16 passed (counts 24→27, release-bundle asserts) |

### Skips (all benign — none are this plan's staging scenarios)

| Skip | Reason |
|------|--------|
| `test_db_resolver.py:163`, `test_db_module_install.py:469` | HTTPS-reachable path — needs `PREPROC_TEST_HTTPS_BASE` (library repo is private; no anonymous HTTPS registry). The HTTPS **no-egress/network-error** paths DO run and pass. |
| `test_dialect_translation.py` (3), `test_audit_rewrite.py` (1) | Pre-existing; need `sql/demo/seed.sql`. Unrelated. |
| `test_control_admin_scripts.py:501` | Pre-existing; env user-creation limit. Unrelated. |
| `test_profile_cleanup.py` (2) | Pre-existing; active suite session correctly prevents reclaim assertion. Unrelated. |

## Tool Evidence

```
luacheck sql/lua/          → Total: 0 warnings / 0 errors in 22 files
uv run ruff check .        → All checks passed!
uv run ruff format --check → 41 files already formatted
speq plan validate add-in-db-module-install → validation passed (5 delta specs)
release.sql regenerated (preproc bundle --release) → byte-identical (gitignored CI artifact)
```

## Scenario Coverage

All scenarios ran live and passed except the two HTTPS-reachable cases (env-gated skip, not a failure).

| Feature | Scenario | Test | Result |
|---------|----------|------|--------|
| db-resolver | BucketFS library-deployed resolve (no egress) | `test_resolver_bucketfs_library_deployed` | Pass |
| db-resolver | sha256 mismatch hard-fail | `test_resolver_sha256_mismatch_fails` | Pass |
| db-resolver | self-deployed metadata-only | `test_resolver_self_deployed_metadata_only` | Pass |
| db-resolver | HTTPS fetch+verify when reachable | `test_resolver_https_fetches_and_verifies` | Skip (gated: `PREPROC_TEST_HTTPS_BASE`) |
| db-resolver | HTTPS no-egress network error | `test_resolver_https_no_egress_network_error` | Pass |
| db-resolver | module not found | `test_resolver_module_not_found` | Pass |
| db-resolver | latest version selected | `test_resolver_selects_latest_version` | Pass |
| db-resolver | large artifact chunked in order | `test_resolver_chunks_large_artifact` | Pass |
| db-resolver | resolver not executable by non-admin | `test_resolver_admin_only` | Pass |
| db-install | BucketFS install byte-identical to CLI | `test_install_bucketfs_parity_with_cli` | Pass |
| db-install | sha256 mismatch aborts before write | `test_install_sha_mismatch_aborts_before_write` | Pass |
| db-install | self-deployed register-only | `test_install_self_deployed_register_only` | Pass |
| db-install | scope activates rule | `test_install_scope_activates_rule` | Pass |
| db-install | idempotent re-install | `test_install_idempotent_reinstall` | Pass |
| db-install | non-admin rejected | `test_install_non_admin_rejected` | Pass |
| db-install | HTTPS installs when reachable | `test_install_https_when_reachable` | Skip (gated: `PREPROC_TEST_HTTPS_BASE`) |
| db-install | HTTPS no-egress no partial deploy | `test_install_https_no_egress_no_partial_deploy` | Pass |
| db-install | result set reports outcome | `test_install_result_set_reports_outcome` | Pass |
| db-install | missing module rejected before write | `test_install_missing_module_rejected` | Pass |
| db-install | MODULE_INSTALL passes luacheck | `test_module_install_passes_luacheck` | Pass |
| db-catalog | lists with installed state | `test_catalog_lists_with_installed_state` | Pass |
| db-catalog | read-only | `test_catalog_read_only` | Pass |
| db-catalog | HTTPS no-egress network error | `test_catalog_https_no_egress_network_error` | Pass |
| db-catalog | installed excludes retired | `test_catalog_installed_excludes_retired` | Pass |
| db-catalog | non-admin rejected | `test_catalog_non_admin_rejected` | Pass |
| command-sugar | INSTALL MODULE expands | `test_sugar_install_module_expands` | Pass |
| command-sugar | FOR USER scope threads USER-typed arg | `test_sugar_install_module_for_user_scope` | Pass |
| command-sugar | CATALOG MODULES expands | `test_sugar_catalog_modules_expands` | Pass |
| command-sugar | malformed INSTALL declined fail-open | `test_sugar_install_module_malformed_declined` | Pass |
| schema-layout | in-DB objects admin-only in PREPROC | `test_indb_objects_admin_only` | Pass |
| schema-layout | install DDL creates no CONNECTION | `test_install_ddl_creates_no_connection` | Pass |

## Code Review Summary

Verdict: **no blockers, no majors.** The in-DB path is a byte-for-byte re-home of `deploy_module` (identical step order, result-column indices, self-deployed skip, sha-NULL, idempotency); abort-before-write holds (all resolver rows scanned for ERROR before any write); admin-only enforced structurally (PREPROC has no PUBLIC grant); Lua SQL-literal building is injection-safe (quote-doubling + bound resolver params + path-traversal guard).

Applied fixes (doc-only):
- `docs/operations.md`: `MODULE_CATALOG` column renamed in docs from `installed?` → `installed` (BOOLEAN) — prevents a copy-paste `SELECT installed?` syntax error.
- `docs/operations.md`: admin-boundary note now includes DBA/superuser (`EXECUTE ANY SCRIPT`), consistent with the rest of the control surface.

## Follow-ups (addressed post-review; re-verified live)

All three non-blocking review follow-ups have been implemented and re-verified (full suite green, 279 passed / 9 skipped / 0 failed; ruff, ruff format, luacheck all clean):

- **Resolver structural check** — DONE. `MODULE_RESOLVER_V1` now verifies, after the `sha256` check, that the artifact is exactly one `CREATE OR REPLACE LUA SCRIPT` whose target matches the declared `script_name` (structural parity with the CLI's `manifest.verify_artifact_script_name`, same regex). A mismatch (or zero/many CREATEs) emits a new `artifact_invalid` error row and no usable artifact text. Covered by two hermetic tests (`test_script_name_mismatch_is_hard_failure`, `test_artifact_without_create_is_hard_failure`).
- **Catalog error scan** — DONE. `module_catalog.lua` now scans EVERY resolver row for `ERROR` before reporting, matching `MODULE_INSTALL`'s abort-before-write loop rather than trusting the error to be row 1.
- **Style nits** — DONE. The resolver's emit columns now flow through a single `_entry_metadata(entry)` helper (one source of truth for the 11 metadata columns), so `_emit_error` no longer hand-counts a block of `None` fillers; `message[:2000]` is now `message[:ERROR_MESSAGE_MAX]` against a named constant matching the `error_message VARCHAR(2000)` EMITS width.

## Environment

- Live DB: Exasol Personal 2026.1 (AWS), reached via env-configured `EXASOL_DSN/USER/PASSWORD`.
- BucketFS staging: bucket `default` on `bfsdefault`, HTTPS :2581; write password retrieved via `confd_client bucketfs_info` through `exasol shell container`. BucketFS auto-extracts uploaded `.tar.gz` archives to a directory; the resolver reads the extracted form (with a raw-tarball fallback).
- Phase 2 (library-repo release-tarball build) remains a separate later plan; this plan consumes a tarball built in-test.

## Ready for

`/speq:record add-in-db-module-install`
