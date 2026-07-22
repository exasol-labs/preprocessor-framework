# Decision Log: add-in-db-module-install

Date: 2026-07-21

## Interview

**Q:** What is being built, and what is its relationship to the existing `preproc` CLI?
**A:** An ALTERNATIVE to the existing `preproc` CLI (which stays 100% unchanged):
let a DBA install a preprocessor module entirely from SQL, with no external
Python/uv toolchain. Two source flavors under one entry point — a BucketFS-staged
release tarball (air-gap-safe) and an HTTPS fetch from GitHub for
egress-permitted databases. This is Phase 1 (framework repo only). Phase 2 (the
preprocessor-library repo producing the release tarball + CI packaging) is a
separate later plan and is NOT in scope here.

**Q:** What SQL surface altitude — a bare scripted entry point, or intercept real syntax?
**A:** Also intercept real syntax now. Ship a preprocessor-intercepted management
statement `PREPROC INSTALL MODULE cast-shorthand FROM
'bucketfs:PREPROC_BFS/preproc-lib-0.1.0.tar.gz' FOR ROLE PREPROC_ADMIN;`
rewritten by a dispatch rule into `EXECUTE SCRIPT PREPROC.MODULE_INSTALL(...)`.
This is IN ADDITION to the underlying scripted entry points and a
discoverability/catalog surface with rich result sets.

**Q:** How is the BucketFS connection wired — hardcoded, or named?
**A:** Connection name in the source arg. The operator creates an Exasol
CONNECTION object once; the source string names it, e.g.
`bucketfs:PREPROC_BFS/path/lib.tar.gz`. No hardcoded connection name.

**Q:** Should the HTTPS DB-fetch path be included, and if so how is it defaulted?
**A:** Include it, opt-in / off by default. Ship it, disabled by default, enabled
explicitly only where the customer permits DB egress. A new ADR must amend
ADR-045.

**Q:** (REVISION — later user feedback) Keep the opt-in toggle for HTTPS?
**A:** No. Remove the `SET_DB_FETCH` setter and the `allow_https_fetch` (or
equivalent) setting entirely. The operator already "opts in" by (a) writing an
`https://…` source in the install call and (b) the cluster being open to the
internet; a separate admin toggle is redundant friction and the goal is
lowest-friction UX. The HTTPS resolver runs whenever the `source` uses the
`https:` scheme — no settings check gates it. An `https:` source on a cluster with
no outbound egress fails with a clear network error and performs NO partial deploy
(abort-before-write holds — nothing created, no rule, no provenance). This is a
further, deliberate departure from ADR-045, explicitly accepted; BucketFS native
read (ADR-050) remains the recommended default for no-egress customers.

**Q:** How does the in-DB installer avoid violating the ADR-001 constraint that Lua scripts have no BucketFS/network access?
**A:** Split the work along the capability line: a Python resolver UDF reads +
verifies the artifact (only UDFs can read BucketFS / reach the network), wrapped
by a Lua orchestrator that runs the DDL (only Lua `EXECUTE SCRIPT`s can). The
engine already proves a Lua admin script CREATEs scripts at runtime —
`REFRESH_CORE` issues `CREATE OR REPLACE LUA SCRIPT PREPROC_RT.P_<hash>` on every
refresh (`sql/lua/refresh_core.lua:608`) under the `PREPROC_ADMIN` role.

**Q:** What must the end-state of an in-DB install look like?
**A:** Byte-identical to a CLI install: a `PREPROC_RT.<NAME>_V<N>` script + a
`PREPROC.CONFIG` rule row + a `PREPROC.MODULES` provenance row, so `MASTER` /
`MY_PIPELINE` dispatch is unchanged regardless of which path installed the module.
The orchestrator re-homes `deploy_module`'s five-step sequence
(`src/preproc/module/deploy.py:181-236`); `sha256` verification is mandatory and
aborts before ANY DB write on mismatch; self-deployed modules register rule +
provenance only, with no CREATE.

**Q:** What does the `scope` argument mean?
**A:** `MODULE_INSTALL(source, module_name, scope)`. `scope` is REQUIRED, a single
`TYPE:value` where `TYPE` ∈ {`ROLE`, `USER`}. It targets RULE ACTIVATION (which
users/roles get the transform), NOT install permission. It maps onto
`RULE_ENSURE(scope_type, scope_value, ...)`.

## Design Decisions

### [1] DB-side module install as a first-class ALTERNATIVE deployment path (ADR-049)

- **Decision:** Ship `PREPROC.MODULE_INSTALL(source, module_name, scope)` — driven
  by a Python resolver UDF and a Lua orchestrator that re-homes the CLI's
  five-step `deploy_module` sequence into the DB — as an alternative to
  `preproc module add`. The end-state is byte-identical to a CLI install (script +
  rule + provenance row); `sha256` verify aborts before any write; self-deployed
  is register-only.
- **Alternatives:** Keep deployment CLI-only (ADR-007's original split); make the
  CLI mandatory for all deployment; vendor the whole `preproc` package into a UDF.
- **Rationale:** ADR-007's strategic direction is the framework owning its
  management surface end-to-end, "eventually intercepting and controlling its own
  management syntax from within the preprocessor." ADR-007 kept *deployment* in the
  CLI; this plan EXTENDS that vision to deployment without superseding it — the CLI
  stays fully supported, and byte-identical end-state means both paths converge.
  Re-implementing the ~3 resolver functions inside the UDF is far lighter than
  vendoring the package for Phase 1.
- **Promotes to ADR:** yes

### [2] BucketFS-staged in-DB read is air-gap-safe; refines ADR-045 (ADR-050)

- **Decision:** A UDF reading the cluster-replicated release tarball natively via
  the BucketFS mount (`/buckets/…`, located by an operator-created CONNECTION named
  in the source string) involves NO network egress — nothing leaves the DB
  perimeter. This is established as a NEW air-gap-safe option, not the option
  ADR-045 rejected.
- **Alternatives:** Treat any DB-side read as an air-gap violation and forbid it.
- **Rationale:** ADR-045 rejected "DB-side fetch (a UDF pulling from GitHub)" —
  a network egress. It never considered "the DB reads a locally-staged, replicated
  bundle," which is not egress at all. ADR-045 is marked refined, not reversed.
- **Promotes to ADR:** yes

### [3] HTTPS DB-fetch is activated implicitly by an `https:` source — no toggle; further departs from ADR-045 (ADR-051)

- **Decision:** Ship the HTTPS reader (`urllib` GET of `registry/index.json` + the
  artifact, `sha256`-verified). It runs whenever the `source` uses the `https:`
  scheme — there is NO settings toggle and NO `SET_DB_FETCH` setter / stored flag.
  Activation is implicit: the operator opts in by supplying an `https://` source
  AND the cluster being able to reach the host. On a cluster with no outbound
  egress the GET fails with a clear network error and NO partial deploy occurs
  (abort-before-write holds — nothing created, no rule, no provenance). BucketFS
  native read (ADR-050) remains the recommended default for no-egress customers.
- **Alternatives:** A default-off admin toggle gating HTTPS (`SET_DB_FETCH` +
  `PREPROC.SETTINGS.allow_https_fetch`) — the plan's own earlier design, dropped
  on user feedback; never ship HTTPS at all.
- **Rationale:** A separate toggle is redundant friction: the operator has already
  opted in twice over (by writing an `https:` source and by the cluster being open
  to the host), and the goal is lowest-friction UX. This is a further, DELIBERATE
  departure from ADR-045, explicitly accepted by the user — it goes beyond ADR-050
  (which showed BucketFS reads are not egress) to allow genuine DB-side egress with
  no gate. ADR-045's no-egress guarantee is preserved by physics, not policy: a
  no-egress cluster cannot complete the GET, so it deploys nothing. `sha256`-verify
  and abort-before-write remain mandatory for both schemes.
- **Promotes to ADR:** yes

### [4] In-DB install runs invoker-rights under the PREPROC_ADMIN boundary; resolver UDFs are admin-only (ADR-052)

- **Decision:** `MODULE_INSTALL`, `MODULE_CATALOG`, and the resolver UDF(s) run
  with INVOKER rights and live in the locked `PREPROC` schema owned by
  `PREPROC_ADMIN`; the resolver UDFs are NEVER granted to PUBLIC. The caller MUST
  hold `PREPROC_ADMIN`, which confers ownership of `PREPROC`/`PREPROC_RT`, the
  `CREATE SCRIPT` system privilege, and — once the operator grants it — ACCESS on
  the BucketFS CONNECTION. The "delegated admin" is a `PREPROC_ADMIN` grantee, not
  a DBA.
- **Alternatives:** "Definer-rights scripts owned by PREPROC_ADMIN so a delegated
  admin installs without raw CREATE SCRIPT rights" (as the feature was first
  framed).
- **Rationale:** The definer-rights framing is inconsistent with the verified repo
  reality: Exasol has NO definer-rights Lua scripts — ADR-001 is precisely why the
  two-schema definer-rights-VIEW bridge exists. `sql/install/10_schemas.sql` and
  `65_admin_script_grant.sql` show `PREPROC_ADMIN` already owns both schemas and
  holds `CREATE SCRIPT` — exactly the boundary `REFRESH_CORE` uses to `CREATE`
  `P_<hash>` scripts at refresh time. The correct model reuses that same boundary:
  invoker rights + the `PREPROC_ADMIN` role. Making the resolver admin-only closes
  the hole where a non-admin could otherwise invoke the DB's BucketFS/network
  reader. This satisfies the original INTENT (install without DBA / without
  system-wide CREATE ANY SCRIPT) while being accurate to Exasol.
- **Promotes to ADR:** yes

### [5] Intercepted `PREPROC INSTALL MODULE` maps onto MODULE_INSTALL per ADR-008 (ADR-053)

- **Decision:** Add `PREPROC INSTALL MODULE <name> FROM '<source>' FOR (ROLE|USER)
  <x>` (plus `PREPROC CATALOG MODULES FROM …`) to
  the live `CONTROL_SUGAR_V1` module in place, each rewriting into exactly one
  `EXECUTE SCRIPT PREPROC.<SCRIPT>(...)`. The `FOR (ROLE|USER) x` clause becomes the
  `scope` `TYPE:value` argument (activation target). The scripted entry points
  remain the stable, syntax-mappable contract (ADR-008); the sugar is additive.
- **Alternatives:** A separate `INSTALL_SUGAR_V1` EXPAND module; require operators
  to call `EXECUTE SCRIPT` directly with no sugar.
- **Rationale:** ADR-008 keeps the admin entry points syntax-mappable so a future
  intercepted statement maps cleanly; this is that future slice for install. The
  in-place `CONTROL_SUGAR_V1` edit reuses the ADR-038 pre-release exception (no
  active sessions can bind a stale body); a second EXPAND module would just compete
  in the same phase for no benefit.
- **Promotes to ADR:** yes

### [6] Feature placement under the existing `module/` domain

- **Decision:** The three new features (`db-resolver`, `db-install`, `db-catalog`)
  live under `module/`, beside `module/cli`; the grants/CONNECTION and sugar
  surfaces attach as deltas to `install/schema-layout` and
  `control/command-sugar`.
- **Alternatives:** A new top-level domain (e.g. `indb/`) for the in-DB installer.
- **Rationale:** The in-DB installer is a DB-side sibling of `module/cli`'s `add`;
  co-locating both deployment paths under `module/` keeps the module story together
  and avoids new-domain churn, mirroring how `refresh` keeps its CLI-triggered and
  SQL-command-triggered paths in one domain.
- **Promotes to ADR:** no

### [7] Artifact transported through EMITS with a chunked fallback

- **Decision:** The resolver returns the artifact body as a single
  `VARCHAR(2000000)` EMITS value; an artifact exceeding that limit is emitted as
  ordered chunks the orchestrator reassembles.
- **Alternatives:** Assume every artifact fits 2 MB and fail otherwise; stage the
  artifact to a temp table.
- **Rationale:** Verified: `VARCHAR(2000000)` is Exasol's max and valid as an EMITS
  column; real module artifacts are a few KB, so the single-value path is the norm
  and the chunked fallback is defensive robustness only.
- **Promotes to ADR:** no

## Review Findings

<!-- Populated by speq-implement after code review. -->
