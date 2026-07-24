# Feature: In-DB Module Install (PREPROC.MODULE_INSTALL)

Lets a DBA install a preprocessor module entirely from SQL — a single
`EXECUTE SCRIPT PREPROC.MODULE_INSTALL(source, module_name, scope)` — with no
external Python/uv toolchain. It re-homes the CLI's `deploy_module` sequence
(`src/preproc/module/deploy.py`) into the database: it drives the resolver UDF to
read and verify the artifact, then deploys the versioned script, registers the
activation rule, writes provenance, and refreshes — leaving an end-state
BYTE-IDENTICAL to a CLI `preproc module add`. It is an ALTERNATIVE to the CLI,
which stays 100% unchanged; the no-egress CLI + `bundle` + local `--registry`
path remains the default, fully-supported air-gap path.

## Background

* `PREPROC.MODULE_INSTALL` is an unversioned `CREATE OR REPLACE LUA SCRIPT`
  admin entry point in the `PREPROC` schema, alongside `REFRESH`, `STATUS`,
  `RULE_ENSURE`, and `MODULE_REGISTER` (the ADR-008 stable, syntax-mappable
  admin-entry-point precedent applies: simple contract, result set reporting the
  outcome — so the intercepted `PREPROC INSTALL MODULE` statement maps cleanly
  onto it).
* Signature `(source, module_name, scope)`. `source` names a `bucketfs:` or
  `https:` source; `module_name` is the module to install; `scope` is REQUIRED
  and is a single `TYPE:value` where `TYPE` ∈ {`ROLE`, `USER`}. `scope` targets
  RULE ACTIVATION — which users or roles receive the transform — NOT install
  permission; it maps onto `PREPROC.RULE_ENSURE(scope_type, scope_value, ...)`.
* Security posture, accurate to the framework's rights model (ADR-001): Exasol
  has NO definer-rights Lua scripts — invoker rights are why the two-schema
  view-bridge exists. `MODULE_INSTALL` therefore runs with INVOKER rights, and
  the caller MUST hold the `PREPROC_ADMIN` role, which confers ownership of
  `PREPROC` / `PREPROC_RT`, the `CREATE SCRIPT` system privilege, and — once the
  operator grants it — ACCESS on the BucketFS CONNECTION. This is exactly the
  boundary `PREPROC.REFRESH` / `REFRESH_CORE` already require to `CREATE OR
  REPLACE` the `PREPROC_RT.P_<hash>` profile scripts at refresh time, proving a
  Lua admin script can CREATE scripts at runtime under this role. The "delegated
  admin" is a `PREPROC_ADMIN` grantee, NOT a DBA. A caller lacking the role fails
  at the first privileged access. The resolver UDFs are admin-only (in `PREPROC`,
  never PUBLIC), so a non-admin cannot use the DB's readers to reach
  BucketFS/network.
* The install sequence mirrors `deploy_module` (`deploy.py:181-236`) exactly, in
  a fixed order that preserves the CLI's "leave the DB consistent on failure"
  invariant:
  1. Resolve + verify via the resolver UDF. Any failure — `sha256` mismatch,
     module not found, an unreachable HTTPS host / network error, unreadable
     source — MUST abort HERE, BEFORE any DB write (parity with
     `fetch_and_verify_artifact`, `deploy.py:80-95`).
  2. For a library-deployed module: `OPEN SCHEMA PREPROC_RT`; then `CREATE OR
     REPLACE LUA SCRIPT` the resolver-returned artifact text — the version is now
     present.
  3. `EXECUTE SCRIPT PREPROC.RULE_ENSURE(scope_type, scope_value, phase,
     script_name, function, NULL, TRUE)` — idempotent rule registration with the
     refresh suppressed; `RULE_ENSURE` validates the script exists in
     `EXA_ALL_SCRIPTS` before inserting.
  4. `EXECUTE SCRIPT PREPROC.MODULE_REGISTER(name, version, source, ref, sha256,
     script_name, function, phase, deploy_mode, source_url, library_version)` —
     idempotent provenance upsert. `source_url` is the effective source the
     operator/default resolved from (revealing a mutable `latest` tag vs a pinned
     `vX.Y.Z` vs a `bucketfs:` path) and `library_version` is the concrete library
     release the resolved index self-declared — together the audit anchor that
     ties a `latest` install to a fixed release.
  5. A single `EXECUTE SCRIPT PREPROC.REFRESH` — materialises `RESOLUTION` from
     `CONFIG`.
* A self-deployed module (the resolver returns a null artifact body) SKIPS step 2
  entirely — its own installer deployed the backing script — and registers rule +
  provenance only (parity with `deploy.py:208-211`).
* End-state parity is the load-bearing contract: after `MODULE_INSTALL` the
  database MUST hold a `PREPROC_RT.<NAME>_V<N>` script + a `PREPROC.CONFIG` rule
  row + a `PREPROC.MODULES` provenance row identical to what the CLI `add`
  produces, so `MASTER` / `MY_PIPELINE` dispatch is byte-identical regardless of
  which path installed the module.
* never-mutate-a-live-version (ADR-046): `MODULE_INSTALL` deploys a new script and
  registers; it never mutates an existing live version. Re-installing an identical
  version is idempotent because `RULE_ENSURE` and `MODULE_REGISTER` are both
  idempotent.
* `MODULE_INSTALL` is `RETURNS TABLE` and reports the outcome as
  `module | version | phase | status | library_version` (`installed` for a fresh
  library-deployed install, `already-current` for an idempotent re-install,
  `registered-only` for a self-deployed module). `library_version` echoes the
  concrete release the resolved index declared (NULL when it carried none), so
  the operator sees which release a `latest` install landed on.
* An `https:` source is fetched over the network whenever the scheme is `https:`
  — there is NO settings toggle. Activation is implicit: the operator opts in by
  writing an `https://` source and by the cluster being open to the host. If the
  cluster has no outbound egress, the resolver's network failure aborts the
  install before any DB write (abort-before-write holds — nothing created, no
  rule, no provenance). BucketFS staging remains the recommended path for
  no-egress customers.

## Scenarios

### Scenario: Installs a library-deployed module from a BucketFS source, end-state byte-identical to a CLI install

* *GIVEN* an installed framework and an operator-created BucketFS CONNECTION staging `preproc-lib-0.1.0.tar.gz` with `cast_shorthand` version 1
* *AND* a caller holding `PREPROC_ADMIN` with ACCESS on the CONNECTION
* *WHEN* the caller runs `EXECUTE SCRIPT PREPROC.MODULE_INSTALL('bucketfs:PREPROC_BFS/preproc-lib-0.1.0.tar.gz', 'cast_shorthand', 'ROLE:ANALYSTS')`
* *THEN* the script MUST create `PREPROC_RT.CAST_SHORTHAND_V1`, register the TRANSLATE rule at `ROLE ANALYSTS` via `PREPROC.RULE_ENSURE`, and write a `PREPROC.MODULES` provenance row for the deployed version
* *AND* a subsequent in-scope statement MUST be transformed by the module
* *AND* the resulting script, rule, and provenance row MUST match the end-state of a CLI `preproc module add cast_shorthand`

### Scenario: A sha256 mismatch aborts before any database write

* *GIVEN* a staged tarball whose `cast_shorthand` artifact hashes to a digest other than the index `sha256`
* *WHEN* the caller runs `MODULE_INSTALL` for `cast_shorthand`
* *THEN* the script MUST fail
* *AND* `PREPROC_RT` MUST NOT contain the module script
* *AND* `PREPROC.CONFIG` and `PREPROC.MODULES` MUST be unchanged

### Scenario: A self-deployed module registers rule and provenance without a CREATE

* *GIVEN* a source whose index lists a `self-deployed` module whose backing script is already present
* *WHEN* the caller runs `MODULE_INSTALL` for that module
* *THEN* the script MUST register the rule and write a provenance row
* *AND* the script MUST NOT create or replace the backing script
* *AND* the result set status MUST report `registered-only`

### Scenario: The scope argument targets rule activation

* *GIVEN* an installed framework and a staged `cast_shorthand`
* *WHEN* the caller runs `MODULE_INSTALL('bucketfs:...', 'cast_shorthand', 'USER:BOB')`
* *THEN* the registered rule MUST be scoped to `USER` `BOB`
* *AND* after the refresh `BOB`'s `PREPROC_RT.MY_PIPELINE` MUST include the module
* *AND* a user outside the scope MUST NOT receive the module in their pipeline

### Scenario: Re-installing an identical version is idempotent

* *GIVEN* `cast_shorthand` version 1 already installed at `ROLE ANALYSTS` via `MODULE_INSTALL`
* *WHEN* the caller runs `MODULE_INSTALL` again with the same source, module, and scope
* *THEN* `PREPROC.CONFIG` MUST still contain exactly one matching rule
* *AND* `PREPROC.MODULES` MUST still contain exactly one row for `('cast_shorthand', 1)`
* *AND* the result set status SHOULD report `already-current`

### Scenario: A non-admin caller cannot install a module

* *GIVEN* a database user who does not hold `PREPROC_ADMIN`
* *WHEN* that user attempts `EXECUTE SCRIPT PREPROC.MODULE_INSTALL(...)`
* *THEN* the database MUST reject the execution with an insufficient-privileges error
* *AND* no module script, `PREPROC.CONFIG` rule, or `PREPROC.MODULES` row MUST be created

### Scenario: An HTTPS source installs when the cluster can reach the host

* *GIVEN* an egress-permitted database and a pinned GitHub raw source, reachable from the cluster, listing `cast_shorthand` version 1 with a matching `sha256`
* *WHEN* the caller runs `MODULE_INSTALL` with the `https://` source
* *THEN* the script MUST resolve and verify the artifact over HTTPS with no settings toggle consulted
* *AND* the resulting script, rule, and provenance row MUST match the BucketFS-path end-state for the same module

### Scenario: An HTTPS source on a no-egress cluster fails with a network error and performs no partial deploy

* *GIVEN* an `https://` source whose host the cluster cannot reach (no outbound egress)
* *WHEN* the caller runs `MODULE_INSTALL` with that source
* *THEN* the script MUST fail with a network error naming the unreachable source
* *AND* the script MUST NOT create a script, `PREPROC.CONFIG` rule, or `PREPROC.MODULES` row

### Scenario: The result set reports the install outcome

* *GIVEN* a successful library-deployed install
* *WHEN* `MODULE_INSTALL` returns
* *THEN* the script MUST return a row carrying `module`, `version`, `phase`, `status`, and `library_version`
* *AND* `library_version` MUST echo the resolved index's release (NULL when the index declared none)

### Scenario: A `latest` install records the resolved release and effective source for audit

* *GIVEN* a source whose index declares a `library_version` (e.g. an index served at the mutable `latest` tag)
* *WHEN* `MODULE_INSTALL` writes the provenance row
* *THEN* the `PREPROC.MODULES` row MUST record `source_url` = the effective source resolved from and `library_version` = the index's declared release
* *AND* the recorded pair MUST let a `latest` install be audited back to the concrete release even though the tag keeps moving

### Scenario: A module absent from the source is rejected before any write

* *GIVEN* a source whose registry does not list the requested module
* *WHEN* the caller runs `MODULE_INSTALL` for that module
* *THEN* the script MUST fail naming the module
* *AND* `PREPROC.CONFIG` and `PREPROC.MODULES` MUST be unchanged

### Scenario: MODULE_INSTALL passes luacheck

* *GIVEN* the `MODULE_INSTALL` Lua source
* *WHEN* luacheck is run against `sql/lua/`
* *THEN* luacheck MUST report zero warnings and zero errors for that file
