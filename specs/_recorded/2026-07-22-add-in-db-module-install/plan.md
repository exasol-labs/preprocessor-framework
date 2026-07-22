# Plan: add-in-db-module-install

## Summary

Ships an ALTERNATIVE to the existing `preproc` CLI (which stays 100% unchanged):
a DBA installs a preprocessor module entirely from SQL — via `EXECUTE SCRIPT
PREPROC.MODULE_INSTALL(...)` or the intercepted `PREPROC INSTALL MODULE …
FROM … FOR (ROLE|USER) …` statement — with no external Python/uv toolchain,
from either a BucketFS-staged release tarball (air-gap-safe) or an HTTPS fetch
from GitHub (used simply by supplying an `https:` source, for internet-connected
clusters). This is Phase 1, framework-repo only; the library-repo packaging that
produces the release tarball is a separate later plan.

## Design

### Context

`preproc module add` deploys a module by fetching + verifying the artifact on the
operator's CLI machine and then issuing SQL — the air-gap bridge of ADR-045/006/007.
That path requires a Python/uv environment on an operator machine with a checkout
or network reach. Many DBAs manage Exasol purely through a SQL client and would
rather stage a release once in BucketFS and install from SQL, never touching a
Python toolchain. The framework's own strategic direction (ADR-007) is to "own its
management surface end-to-end … intercepting and controlling its own management
syntax from within the preprocessor." ADR-007 deliberately kept *deployment* in the
CLI; this plan extends that same "the DB owns its management surface" vision to
deployment by adding a DB-side deployment ALTERNATIVE — it does not supersede
ADR-007 or the CLI.

The hard constraint is that Exasol Lua scripts have no filesystem/BucketFS/network
access (mission Constraints; ADR-001) — only UDFs (Python) can read BucketFS or
reach the network, and only Lua `EXECUTE SCRIPT`s can run DDL. The existing engine
already proves a Lua admin script CREATEs scripts at runtime: `REFRESH_CORE`
issues `CREATE OR REPLACE LUA SCRIPT PREPROC_RT.P_<hash>` during every refresh
(`sql/lua/refresh_core.lua:608`), running under the `PREPROC_ADMIN` role. The design
therefore splits the work along the capability line: a Python **resolver UDF** reads
+ verifies the artifact, and a Lua **orchestrator** re-homes the CLI's five-step
`deploy_module` sequence into the DB.

- **Goals**
  - A single SQL entry point `PREPROC.MODULE_INSTALL(source, module_name, scope)`
    whose end-state is BYTE-IDENTICAL to a CLI `preproc module add`: a
    `PREPROC_RT.<NAME>_V<N>` script + a `PREPROC.CONFIG` rule + a `PREPROC.MODULES`
    provenance row, so `MASTER` / `MY_PIPELINE` dispatch is unchanged.
  - Two source flavors under one entry point: a native BucketFS read of a
    cluster-replicated release tarball (no network egress — air-gap-safe), and an
    HTTPS fetch that runs whenever the source uses the `https:` scheme (no toggle;
    on a no-egress cluster it simply fails with a network error and deploys
    nothing) — lowest-friction UX for internet-connected clusters.
  - Mandatory `sha256` verification that aborts BEFORE any DB write on mismatch
    (parity with `deploy.fetch_and_verify_artifact`), and a self-deployed
    register-only path (parity with `deploy.py:208-211`).
  - A discoverability surface: `PREPROC.MODULE_CATALOG(source)` lists a source's
    advertised modules with an `installed?` flag before installing.
  - An intercepted management statement `PREPROC INSTALL MODULE … FROM … FOR
    (ROLE|USER) …` that rewrites into the `EXECUTE SCRIPT` call, mapping cleanly
    onto the ADR-008 stable admin-entry-point contract.
- **Non-Goals**
  - Phase 2: the preprocessor-library repo's release-tarball build + CI packaging
    (a separate later plan). This plan consumes a release tarball; it does not
    produce one.
  - Any change to the `preproc` CLI, its deploy path, or `bundle`/`release.sql`
    behaviour beyond additively deploying the new DB objects.
  - Deprecating the air-gap CLI path — the CLI + `bundle` + local `--registry`
    remains the default, first-class, fully-supported air-gap path.
  - Module update/retire/reclaim via SQL — this plan installs (deploy + register);
    the existing `module/lifecycle` update flow (ADR-046) is unchanged and not
    re-homed here.

### Decision

The load-bearing ADRs (ADR-049 … ADR-053) are recorded in
`specs/_plans/add-in-db-module-install/decision-log.md`.

#### Architecture

```
 Operator (SQL client only)         Exasol DB (no egress for the BucketFS path)
 ┌──────────────────────────┐   ┌────────────────────────────────────────────────┐
 │ PREPROC INSTALL MODULE …  │   │ CONTROL_SUGAR_V1 (EXPAND) rewrites the statement │
 │  FROM 'bucketfs:CONN/..'  │──▶│   ─────────────▶ EXECUTE SCRIPT PREPROC.MODULE_INSTALL(src,name,scope)
 │  FOR ROLE ANALYSTS        │   │                                     │            │
 └──────────────────────────┘   │  PREPROC.MODULE_INSTALL (Lua, invoker rights)    │
 ┌──────────────────────────┐   │   1. resolve+verify ─▶ PREPROC.MODULE_RESOLVER   │
 │ EXECUTE SCRIPT            │   │        (Python UDF, admin-only)                  │
 │  PREPROC.MODULE_INSTALL   │──▶│        ├─ bucketfs: native tarfile read via      │
 │   (src, name, 'ROLE:x')   │   │        │   CONNECTION mount /buckets/… (NO egress)│
 └──────────────────────────┘   │        └─ https:  urllib GET (runs iff scheme https)│
                                 │      abort BEFORE any write on sha/notfound/netfail│
                                 │   2. OPEN SCHEMA PREPROC_RT; CREATE OR REPLACE …  │
                                 │   3. EXECUTE SCRIPT PREPROC.RULE_ENSURE(…)        │
                                 │   4. EXECUTE SCRIPT PREPROC.MODULE_REGISTER(…)    │
                                 │   5. EXECUTE SCRIPT PREPROC.REFRESH               │
                                 │  End-state == CLI `preproc module add`           │
                                 └────────────────────────────────────────────────┘
```

Data flow: the orchestrator drives the resolver UDF to read `registry/index.json`
and the requested artifact from the source, verify its `sha256`, and return the
artifact text + metadata; the orchestrator then runs the exact five-step
`deploy_module` sequence in the DB. A self-deployed module skips step 2. Any
resolver failure aborts before step 2, so nothing is deployed and no rule or
provenance is written.

#### Patterns

| Pattern | Where | Why |
|---------|-------|-----|
| Capability split: Python reads, Lua writes DDL | `module/db-resolver` + `module/db-install` | Lua has no BucketFS/network access; only UDFs read sources, only Lua `EXECUTE SCRIPT`s run DDL (ADR-001) |
| Re-home `deploy_module`, don't vendor it | `module/db-install` | Reimplement the ~3 resolver functions + 5-step sequence inside the DB; keep the CLI untouched (Phase 1) |
| Abort-before-write on verify failure | `module/db-resolver` + `module/db-install` | Parity with `deploy.fetch_and_verify_artifact` — a bad `sha256` never leaves a half-installed DB |
| Air-gap-safe native BucketFS read | `module/db-resolver` | A UDF reading a cluster-replicated BucketFS file involves no network egress (refines ADR-045) |
| Implicit HTTPS activation by source scheme | `module/db-resolver` + `module/db-install` | No toggle: an `https:` source is fetched when supplied; a no-egress cluster fails the GET (further departure from ADR-045, chosen for lowest-friction UX) |
| Invoker-rights under the PREPROC_ADMIN boundary | `module/db-install`, `install/schema-layout` | Exasol has no definer-rights scripts (ADR-001); resolver UDFs are admin-only, exactly the boundary `REFRESH_CORE` already uses to CREATE scripts |
| Intercepted statement maps to a stable script | `control/command-sugar` | ADR-008 syntax-mappable contract; in-place `CONTROL_SUGAR_V1` edit under the ADR-038 pre-release exception |

### Consequences

| Decision | Alternatives Considered | Rationale |
|----------|------------------------|-----------|
| DB-side install as an ALTERNATIVE to the CLI (extends ADR-007) | Keep deployment CLI-only; make the CLI mandatory | ADR-007's vision is the DB owning its management surface end-to-end; a SQL-only DBA should not need a Python/uv toolchain. The CLI stays fully supported; parity of end-state means both paths converge |
| Python resolver UDF + Lua orchestrator | A single Lua script (impossible — no BucketFS/network in Lua); vendor the whole `preproc` package into a UDF | ADR-001 forces the capability split; vendoring the package is far heavier than re-implementing the ~3 resolver functions for Phase 1 |
| BucketFS native read is air-gap-safe (refines ADR-045) | Treat any DB-side read as an air-gap violation | ADR-045 rejected a UDF *fetching from GitHub*; it never considered a UDF reading a locally-staged, cluster-replicated bundle — that involves no egress, nothing leaves the DB perimeter |
| HTTPS activated implicitly by an `https:` source, no toggle (further departs from ADR-045) | A default-off admin toggle (`SET_DB_FETCH` / `allow_https_fetch`); never ship HTTPS | A separate toggle is redundant friction — the operator already opts in by writing an `https:` source and by the cluster being open to the host; goal is lowest-friction UX. A no-egress cluster fails the GET and deploys nothing, so BucketFS native read (ADR-050) remains the recommended no-egress default |
| Invoker-rights + `PREPROC_ADMIN` role boundary | "Definer-rights scripts owned by PREPROC_ADMIN" (as first framed) | Exasol has NO definer-rights Lua scripts — ADR-001 is why the view-bridge exists. Verified in `sql/install/10_schemas.sql` + `65_admin_script_grant.sql`: `PREPROC_ADMIN` owns both schemas and holds `CREATE SCRIPT`, exactly what `REFRESH_CORE` needs to CREATE `P_<hash>` scripts. "Delegated admin" = a `PREPROC_ADMIN` grantee, not a DBA |
| `scope` targets rule activation, not install permission | Overload `scope` as an authorization grant | Activation (which users/roles get the transform) maps onto `RULE_ENSURE`; install permission is the separate `PREPROC_ADMIN` role boundary |
| Add verbs to `CONTROL_SUGAR_V1` in place | A separate `INSTALL_SUGAR_V1` module | ADR-038 pre-release exception applies (no active sessions); one unified sugar module is cleaner than a second EXPAND competitor |

## Features

| Feature | Status | Spec |
|---------|--------|------|
| `module/db-resolver` | NEW | `specs/_plans/add-in-db-module-install/module/db-resolver/spec.md` |
| `module/db-install` | NEW | `specs/_plans/add-in-db-module-install/module/db-install/spec.md` |
| `module/db-catalog` | NEW | `specs/_plans/add-in-db-module-install/module/db-catalog/spec.md` |
| `control/command-sugar` | CHANGED | `specs/_plans/add-in-db-module-install/control/command-sugar/spec.md` |
| `install/schema-layout` | CHANGED | `specs/_plans/add-in-db-module-install/install/schema-layout/spec.md` |

Feature placement rationale: the in-DB installer is a DB-side sibling of
`module/cli`'s `add`, so its three new features live under the existing `module/`
domain — co-locating the two deployment paths rather than fragmenting the module
story into a new domain (the same way `refresh` keeps its CLI-triggered and
SQL-command-triggered paths in one domain). The grants/CONNECTION groundwork and
the sugar surface attach as deltas to the features that already own them
(`install/schema-layout`, `control/command-sugar`).

## Requirements

| Requirement | Details |
|-------------|---------|
| End-state parity | An in-DB install and a CLI `preproc module add` MUST leave an identical `PREPROC_RT.<NAME>_V<N>` script + `PREPROC.CONFIG` rule + `PREPROC.MODULES` row |
| Abort-before-write | A `sha256` mismatch, not-found, unreadable source, or unreachable-HTTPS-host / network error MUST abort before any DB write |
| No-egress safety | A `bucketfs:` source performs no network egress; an `https:` source on a no-egress cluster fails with a network error and deploys nothing — no toggle exists to disable HTTPS |
| CLI untouched | `preproc install`, `preproc module …`, `bundle`, and `release.sql` behaviour are unchanged except for additively deploying the new DB objects |
| Admin-only readers | The resolver UDF(s), `MODULE_INSTALL`, and `MODULE_CATALOG` live in `PREPROC` and are NOT granted to PUBLIC |

## Dependencies

- Existing DB surfaces reused unchanged: `PREPROC.RULE_ENSURE`,
  `PREPROC.MODULE_REGISTER`, `PREPROC.REFRESH`, `PREPROC_RT.INSTALLED_MODULES`,
  `PREPROC_RT.MASTER` / `MY_PIPELINE`.
- Exasol capabilities (both verified): a Python3 UDF reads a BucketFS file via the
  cluster-local mount (`/buckets/<service>/<bucket>/…`) with `exa.get_connection()`
  supplying the CONNECTION-named location; `VARCHAR(2000000)` is valid as an EMITS
  column so an artifact body up to 2 MB passes through the UDF into a Lua `pquery`
  (chunked-EMITS fallback for larger artifacts).
- A docker Exasol instance for the integration tests; a BucketFS-staged
  `preproc-lib-*.tar.gz` fixture (built in-test from the module sources, since
  Phase 2 has not produced the real release tarball yet).
- Python stdlib only inside the UDF: `tarfile`, `json`, `hashlib`, `urllib`.

## Migration

None. This plan adds new DB objects (resolver UDF + orchestrator/catalog scripts)
but changes no existing table structure or data — HTTPS activation is implicit in
the source scheme, so there is no settings column to add or back-fill.

## Dead Code Removal

None. This plan is strictly ADDITIVE — the existing `preproc` CLI, its
`deploy_module` path, and `bundle`/`release.sql` are unchanged. The in-DB
installer is an alternative, not a replacement, so no code is obsoleted.

## Implementation Tasks

- [ ] 1.1 Add the resolver Python3 UDF `sql/udf/module_resolver_v1.sql` in `PREPROC` (admin-only): BucketFS native `tarfile` read via the CONNECTION mount, `urllib` HTTPS read that runs whenever the source scheme is `https:` (no toggle; a network failure is signalled, not raised), `registry/index.json` parse + latest-entry select, `sha256` verify, metadata + `VARCHAR(2000000)` artifact EMITS with ordered-chunk fallback, self-deployed metadata-only, fail-loud (no-raise) error signalling [expert]
- [ ] 1.2 Add `sql/lua/module_install.lua` (`PREPROC.MODULE_INSTALL`) — drive the resolver, abort-before-write on any resolver failure (including an unreachable HTTPS host), run the five-step re-home (`OPEN SCHEMA` / `CREATE OR REPLACE` / `RULE_ENSURE` / `MODULE_REGISTER` / `REFRESH`), self-deployed skip, parse `scope` `TYPE:value` → `RULE_ENSURE`, `RETURNS TABLE` result set [expert]
- [ ] 1.3 Add `sql/lua/module_catalog.lua` (`PREPROC.MODULE_CATALOG`) — drive the resolver for index metadata only, LEFT JOIN `PREPROC_RT.INSTALLED_MODULES` for the `installed?` flag (deployed, not retired), `RETURNS TABLE`, read-only
- [ ] 2.1 Wire the new UDF + Lua scripts into `src/preproc/install.py` (deploy) and `src/preproc/bundle.py` (`_script_statements`, `_FULL_SCRIPT_ORDER`, `_RELEASE_SCRIPT_ORDER`); add the admin-only grants for `MODULE_INSTALL` / `MODULE_CATALOG` / resolver (extend the admin-script grant DDL; never PUBLIC)
- [ ] 2.2 Update `tests/test_cli_bundle.py` statement counts and release-bundle assertions for the new UDF + scripts; regenerate `release.sql`
- [ ] 3.1 Add the `PREPROC INSTALL MODULE … FROM … FOR (ROLE|USER) …` and `PREPROC CATALOG MODULES FROM …` verbs to `sql/lua/control_sugar_v1.lua` in place (ADR-038 pre-release exception): parse the quoted source, module name, and `FOR` scope into a `TYPE:value` string; obey the `gsub` two-value-return discipline; case-insensitive keywords
- [ ] 4.1 Author `tests/test_db_resolver.py` (integration; build + stage a BucketFS tarball fixture) [expert]
- [ ] 4.2 Author `tests/test_db_module_install.py` (integration; assert byte-identical end-state parity with a CLI `add`, `sha256`-mismatch abort-before-write, self-deployed register-only, HTTPS-on-no-egress network-error-with-no-partial-deploy, non-admin reject, idempotent re-install) [expert]
- [ ] 4.3 Author `tests/test_db_module_catalog.py` (integration)
- [ ] 4.4 Extend `tests/test_command_sugar.py` with the INSTALL MODULE / CATALOG MODULES expansion scenarios
- [ ] 4.5 Extend `tests/test_schema_layout.py` (resolver/install/catalog admin-only ownership; install DDL creates no CONNECTION)
- [ ] 5.1 Docs: add an "In-database module install" section to `docs/operations.md` (create the BucketFS CONNECTION + grant ACCESS to `PREPROC_ADMIN`, stage the release tarball, `PREPROC INSTALL MODULE` / `EXECUTE SCRIPT MODULE_INSTALL`, `MODULE_CATALOG`, and that an `https:` source is used directly on internet-connected clusters) and an "In-database install path" section to `docs/how-it-works.md` (resolver UDF + orchestrator split, air-gap-safe BucketFS, invoker-rights/`PREPROC_ADMIN` boundary)
- [ ] 5.2 Run `luacheck sql/lua/`, `uv run ruff check .`, `uv run ruff format --check .`, `uv run pytest`, and `speq plan validate add-in-db-module-install`

## Parallelization

| Parallel Group | Tasks |
|----------------|-------|
| Group A (resolver UDF, the foundation) | 1.1 |
| Group B (orchestrator + catalog, need the resolver) | 1.2, 1.3 |
| Group C (CLI wiring + count tests, need A+B) | 2.1, 2.2 |
| Group D (sugar grammar, targets the new scripts) | 3.1 |
| Group E (tests, each needs its subject) | 4.1, 4.2, 4.3, 4.4, 4.5 |
| Group F (docs + final verification, needs all) | 5.1, 5.2 |

Sequential dependencies:
- Group A → Group B (the orchestrator and catalog drive the resolver)
- Group B → Group C (wiring + count tests need the scripts/UDF)
- Group B → Group D (the sugar verbs target `MODULE_INSTALL` / `MODULE_CATALOG`)
- each subject → its Group E test
- Groups C + D + E → Group F

## Verification

### Scenario Coverage

| Scenario | Test Type | Test Location | Test Name |
|----------|-----------|---------------|-----------|
| Resolves a library-deployed module from a BucketFS-staged tarball | Integration | `tests/test_db_resolver.py` | `test_resolver_bucketfs_library_deployed` |
| A sha256 mismatch is a hard failure with no usable artifact | Integration | `tests/test_db_resolver.py` | `test_resolver_sha256_mismatch_fails` |
| A self-deployed entry returns metadata only | Integration | `tests/test_db_resolver.py` | `test_resolver_self_deployed_metadata_only` |
| An HTTPS source is fetched and verified when the scheme is https | Integration | `tests/test_db_resolver.py` | `test_resolver_https_fetches_and_verifies` |
| An HTTPS source on a no-egress cluster fails with a network error | Integration | `tests/test_db_resolver.py` | `test_resolver_https_no_egress_network_error` |
| A module absent from the source index is reported not found | Integration | `tests/test_db_resolver.py` | `test_resolver_module_not_found` |
| The latest version is selected when the index lists several | Integration | `tests/test_db_resolver.py` | `test_resolver_selects_latest_version` |
| An artifact larger than a single EMITS value is chunked in order | Integration | `tests/test_db_resolver.py` | `test_resolver_chunks_large_artifact` |
| The resolver is not executable by a non-admin user | Integration | `tests/test_db_resolver.py` | `test_resolver_admin_only` |
| Installs a library-deployed module from BucketFS, byte-identical to a CLI install | Integration | `tests/test_db_module_install.py` | `test_install_bucketfs_parity_with_cli` |
| A sha256 mismatch aborts before any database write | Integration | `tests/test_db_module_install.py` | `test_install_sha_mismatch_aborts_before_write` |
| A self-deployed module registers rule and provenance without a CREATE | Integration | `tests/test_db_module_install.py` | `test_install_self_deployed_register_only` |
| The scope argument targets rule activation | Integration | `tests/test_db_module_install.py` | `test_install_scope_activates_rule` |
| Re-installing an identical version is idempotent | Integration | `tests/test_db_module_install.py` | `test_install_idempotent_reinstall` |
| A non-admin caller cannot install a module | Integration | `tests/test_db_module_install.py` | `test_install_non_admin_rejected` |
| An HTTPS source installs when the cluster can reach the host | Integration | `tests/test_db_module_install.py` | `test_install_https_when_reachable` |
| An HTTPS source on a no-egress cluster fails with a network error and performs no partial deploy | Integration | `tests/test_db_module_install.py` | `test_install_https_no_egress_no_partial_deploy` |
| The result set reports the install outcome | Integration | `tests/test_db_module_install.py` | `test_install_result_set_reports_outcome` |
| A module absent from the source is rejected before any write | Integration | `tests/test_db_module_install.py` | `test_install_missing_module_rejected` |
| MODULE_INSTALL passes luacheck | Integration | `tests/test_db_module_install.py` | `test_module_install_passes_luacheck` |
| Catalogs a BucketFS source's modules with installed state | Integration | `tests/test_db_module_catalog.py` | `test_catalog_lists_with_installed_state` |
| The catalog is read-only | Integration | `tests/test_db_module_catalog.py` | `test_catalog_read_only` |
| An HTTPS source on a no-egress cluster fails with a network error (catalog) | Integration | `tests/test_db_module_catalog.py` | `test_catalog_https_no_egress_network_error` |
| The installed flag reflects deployed, not retired, provenance | Integration | `tests/test_db_module_catalog.py` | `test_catalog_installed_excludes_retired` |
| A non-admin caller cannot run the catalog | Integration | `tests/test_db_module_catalog.py` | `test_catalog_non_admin_rejected` |
| INSTALL MODULE command expands to a MODULE_INSTALL execute-script call | Integration | `tests/test_command_sugar.py` | `test_sugar_install_module_expands` |
| A FOR USER scope threads a USER-typed scope argument | Integration | `tests/test_command_sugar.py` | `test_sugar_install_module_for_user_scope` |
| CATALOG MODULES command expands to a MODULE_CATALOG execute-script call | Integration | `tests/test_command_sugar.py` | `test_sugar_catalog_modules_expands` |
| A malformed INSTALL MODULE command is declined fail-open | Integration | `tests/test_command_sugar.py` | `test_sugar_install_module_malformed_declined` |
| The resolver UDF and install/catalog scripts are created admin-only in PREPROC | Integration | `tests/test_schema_layout.py` | `test_indb_objects_admin_only` |
| The BucketFS source is an operator-created CONNECTION, not a framework object | Integration | `tests/test_schema_layout.py` | `test_install_ddl_creates_no_connection` |

### Manual Testing

| Feature | Command | Expected Output |
|---------|---------|-----------------|
| `module/db-resolver` | `EXECUTE SCRIPT PREPROC.MODULE_CATALOG('bucketfs:PREPROC_BFS/preproc-lib-0.1.0.tar.gz')` (drives the resolver) | Rows for the staged modules; proves the resolver read the tarball with no egress |
| `module/db-install` | `EXECUTE SCRIPT PREPROC.MODULE_INSTALL('bucketfs:PREPROC_BFS/preproc-lib-0.1.0.tar.gz','cast-shorthand','ROLE:ANALYSTS')` then `SELECT name,version,status FROM PREPROC_RT.INSTALLED_MODULES` | Result row `cast-shorthand \| 1 \| TRANSLATE \| installed`; one `deployed` provenance row |
| `module/db-catalog` | `EXECUTE SCRIPT PREPROC.MODULE_CATALOG('bucketfs:PREPROC_BFS/preproc-lib-0.1.0.tar.gz')` | One row per advertised module with `name`, `version`, `phase`, `deploy_mode`, `installed?` |
| `control/command-sugar` | `ALTER SESSION SET sql_preprocessor_script=PREPROC_RT.MASTER;` then `PREPROC INSTALL MODULE cast-shorthand FROM 'bucketfs:PREPROC_BFS/preproc-lib-0.1.0.tar.gz' FOR ROLE ANALYSTS;` | Module installed exactly as the `EXECUTE SCRIPT` path |
| `install/schema-layout` | `grep -i "create connection" sql/install/*.sql` then, as a non-admin, `EXECUTE SCRIPT PREPROC.MODULE_CATALOG('bucketfs:...')` | No `CREATE CONNECTION` in the install DDL; the non-admin call fails with insufficient privileges |

### Checklist

| Step | Command | Expected |
|------|---------|----------|
| Sync + deps | `uv sync` | Exit 0 |
| Lint (Lua) | `luacheck sql/lua/` | 0 errors/warnings |
| Lint (Python) | `uv run ruff check .` | 0 errors |
| Format check | `uv run ruff format --check .` | No changes |
| Test suite | `uv run pytest` | 0 failures |
| Plan validation | `speq plan validate add-in-db-module-install` | pass |
