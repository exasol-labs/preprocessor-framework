# Verification Report: add-module-ecosystem

**Generated:** 2026-07-17

## Verdict

| Result | Details |
|--------|---------|
| **PASS** — verified against live Exasol 2026.1.0 | Full suite green (**235 passed, 7 skipped, 0 failed**) against a Docker Exasol on `localhost:9563`. The module ecosystem was also proven end-to-end: `preproc module add cast-shorthand` from the library registry deployed + registered the module, and `SELECT 42::DECIMAL(3,0)` returned `42` through MASTER dispatch. Code review found no correctness bugs. Three real defects were found and fixed during live verification (see below). |

| Check | Status |
|-------|--------|
| Build (`uv sync`) | ✓ |
| Tests (live 2026.1.0) | ✓ 235 passed, 7 skipped, 0 failed |
| Lint (`ruff check .`) | ✓ clean |
| Format (`ruff format --check .`) | ✓ clean |
| Lua lint (`luacheck sql/lua/`) | ✓ 0/0 (20 files) |
| Code review | ✓ no confirmed correctness bugs |
| Scenario Coverage | ✓ all module scenarios execute and pass |
| End-to-end demo | ✓ library `add` → dispatch → `::` cast works; `remove` retires cleanly |
| Library scaffolding tests | ✓ 9 passed (in the library repo) |

## Defects found during live verification (all fixed)

1. **Reserved-word column (blocker).** `PREPROC.MODULES` used `source` and `ref` — both Exasol reserved words (`EXA_SQL_KEYWORDS`) — so `run_install()` failed at the DDL. Renamed the **columns** to `source_repo` / `source_ref` across `22_modules_table.sql`, `40_views.sql`, `module_register.lua`, and the lifecycle test. (The Lua script *parameters* named `source`/`ref` are accepted by Exasol and were left as-is.)
2. **Test-harness reserved word.** The contract EXPAND harness used a `matched BOOLEAN` result column — `MATCHED` is reserved (MERGE). Renamed to `did_match` (positional reads unaffected).
3. **`validate.py` luacheck rejected every conformant module.** It linted the extracted body in a bare tmp dir, so luacheck flagged the mandatory global entry function (and would flag every injected preprocessor global). Fixed to lint with a generated `.luacheckrc` mirroring the framework's (preprocessor `read_globals` + the entry function as an allowed global).

Post-review quality fixes: excluded `_`-prefixed dirs (`modules/_template`) from the registry index so the scaffold is not operator-visible/installable (index regenerated); corrected `deploy.module_register` return annotation `tuple[bool]`→`bool`.

## What shipped

**Framework repo** — new `module/` domain: contract (`module.toml` + single-`.sql` + `fn(text)->nil|string`), registry (federated `index.json`, sha256, offline), `preproc module` CLI (`list/search/add/update/remove/bundle/validate/sync`), DB lifecycle (`PREPROC.MODULES` + `PREPROC_RT.INSTALLED_MODULES`, `RULE_ENSURE`, `MODULE_REGISTER/RETIRE/RECLAIM`). Ergonomics removed from the engine. `release.sql` regenerated (33→37 statements). `docs/module-authoring.md`, `docs/operations.md`, `README.md` updated.

**Library repo** (`exasol-labs/preprocessor-library`) — `modules/{cast-shorthand,trailing-comma,_template}/`, `registry/index.json`, `CONTRIBUTING.md`, CI workflow, `tests/test_scaffolding.py`.

## Accepted follow-ons (from code review — non-blocking)

- **No `preproc module reclaim` verb.** Physical drop of retired `_V<N>` scripts is a separate guarded admin call (`EXECUTE SCRIPT PREPROC.MODULE_RECLAIM`), mirroring profile reclaim; retired scripts persist until then. Documented in operations.md. A `reclaim` CLI verb could be added later.
- **Interrupted update edge case.** If a process dies between `MODULE_REGISTER(V+1)` and `MODULE_RETIRE(V)`, both rows stay `deployed` (provenance leak, not a dispatch bug). Could self-heal by retiring lower `deployed` versions during `sync` converge.
- **`validate._BODY_RE`** anchors on the first end-of-line `AS`; anchoring on the `CREATE OR REPLACE LUA SCRIPT ... AS` match would be more robust for artifacts with unusual leading comments.
- **exasol-semantic-views integration** and the **standalone pip test-kit** remain documented out-of-scope follow-ons (the contract already fits self-deployed register-only modules).

## Notes
- The 7 skipped tests are pre-existing conditional skips unrelated to this slice.
- The end-to-end demo mutated the shared DB only after the test suite completed (to avoid cross-contamination) and was cleaned up (`preproc module remove` → rule dropped, module `retired`).
