# Tasks: add-in-db-module-install

Implementation of the in-database module installer (framework repo, Phase 1).
Tests are folded into their subject task so each is built test-first (TDD),
satisfying both the code-guardrails RED→GREEN discipline and the plan's
"each Group E test needs its subject" dependency.

## Group A — Resolver UDF (foundation)
- [x] 1.1 Resolver Python3 UDF `sql/udf/module_resolver_v1.sql` in PREPROC, admin-only [expert]
- [x] 4.1 `tests/test_db_resolver.py` + shared BucketFS-tarball staging fixture [expert]

## Group B — Orchestrator + Catalog (need the resolver)
- [x] 1.2 `sql/lua/module_install.lua` (PREPROC.MODULE_INSTALL): 5-step re-home, abort-before-write [expert]
- [x] 4.2 `tests/test_db_module_install.py` (parity, sha-abort, self-deployed, https-no-egress, non-admin, idempotent) [expert]
- [x] 1.3 `sql/lua/module_catalog.lua` (PREPROC.MODULE_CATALOG): index-only, installed? flag, read-only
- [x] 4.3 `tests/test_db_module_catalog.py`

## Group C — CLI wiring + count tests (need A+B)
- [x] 2.1 Wire UDF + Lua into `install.py` / `bundle.py`; admin-only grants (never PUBLIC)
- [x] 4.5 Extend `tests/test_schema_layout.py` (admin-only ownership; install DDL creates no CONNECTION)
- [x] 2.2 Update `tests/test_cli_bundle.py` counts + release-bundle asserts; regenerate `release.sql`

## Group D — Sugar grammar (needs B)
- [x] 3.1 Add INSTALL MODULE / CATALOG MODULES verbs to `sql/lua/control_sugar_v1.lua` in place
- [x] 4.4 Extend `tests/test_command_sugar.py` with expansion scenarios

## Group F — Docs + verification (needs C+D+E)
- [x] 5.1 Docs: `docs/operations.md` + `docs/how-it-works.md` in-DB install sections
- [x] 5.2 Final gate: luacheck, ruff check, ruff format --check, pytest, speq plan validate

## Phase 4 — Code Review
- [x] R.1 Review all changed files (code-reviewer) — no blockers/majors; 2 doc fixes applied

## Phase 6 — Verification Report
- [x] V.1 verification-report.md

## Review follow-ups (nice-to-have, not blocking; spec-compliant as-is)
- [x] Resolver: assert artifact CREATE target == declared script_name (structural parity with CLI `verify_artifact_script_name`) — new `artifact_invalid` error_kind; 2 hermetic tests
- [x] MODULE_CATALOG: scan all resolver rows for ERROR (symmetry with MODULE_INSTALL)
- [x] Style nits: single-source `_entry_metadata` collapses the hand-counted emit columns; named `ERROR_MESSAGE_MAX` constant for message truncation
