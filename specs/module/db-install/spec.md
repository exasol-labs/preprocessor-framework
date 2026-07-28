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

Step 2 is generalised from a single `pquery(artifact_text)` to executing the
artifact's 1..N statements in file order, and the atomicity construction is
stated explicitly. DDL cannot be rolled back, so all-or-nothing is BUILT rather
than inherited: pre-flight validation before the first DDL, compensating
cleanup of the objects already created when a later statement fails, idempotent
re-run, and rule registration last so a partially installed module is inert.

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
* The install sequence mirrors `deploy_module` in a fixed order that preserves
  the CLI's "leave the DB consistent on failure" invariant:
  1. Resolve + verify via the resolver UDF. Any failure — `sha256` mismatch,
     an artifact whose object inventory disagrees with the index entry's
     declared inventory, module not found, an unreachable HTTPS host / network
     error, unreadable source — MUST abort HERE, BEFORE any DB write.
  2. **Pre-flight.** Reassemble the artifact, split it into statements, and
     validate the full statement list AND the declared object inventory BEFORE
     executing the first statement. A statement the splitter cannot classify, a
     statement creating an object absent from the declared inventory, or a
     declared object no statement creates MUST abort here, still before any DB
     write. This mirrors the existing "ANY resolver row with status = ERROR
     aborts HERE, BEFORE any DB write" pattern and is the primary all-or-nothing
     mechanism.
  3. For a library-deployed module: `OPEN SCHEMA PREPROC_RT`; then execute the
     artifact's statements in file order.
  4. `EXECUTE SCRIPT PREPROC.RULE_ENSURE(...)` — idempotent rule registration
     with the refresh suppressed.
  5. `EXECUTE SCRIPT PREPROC.MODULE_REGISTER(...)` — idempotent provenance
     upsert, now additionally recording the deployed object inventory.
  6. A single `EXECUTE SCRIPT PREPROC.REFRESH`.
* A self-deployed module (the resolver returns a null artifact body) SKIPS step 2
  entirely — its own installer deployed the backing script — and registers rule +
  provenance only (parity with `deploy.py:208-211`).
* **Atomicity is constructed, not inherited** (ADR-059). DML inside a Lua script
  IS transactional and rollback-able — `REFRESH_CORE` issues an explicit
  `COMMIT` as SQL through `pquery` regardless of the client's autocommit
  setting. DDL is NOT: `CREATE OR REPLACE` and `DROP SCRIPT` implicitly commit
  preceding DML and cannot themselves be rolled back, as the framework's own
  `refresh_core.lua` and `module_reclaim.lua` document and work around. A
  composite artifact is mostly DDL, so a failure at statement k cannot be rolled
  back to undo statements 1..k-1. All-or-nothing is therefore built from four
  mechanisms:
  - **Pre-flight validation** of all N statements and the inventory before the
    first DDL, so the overwhelming majority of failures never reach a write.
  - **Compensating cleanup**: on a failure at statement k, drop the objects
    created by statements 1..k-1, walking the declared inventory in reverse. The
    inventory is the second reason it is required. Cleanup is best-effort and
    MUST NOT mask the original error.
  - **Idempotent re-run**: every artifact statement uses `CREATE OR REPLACE`, so
    re-running an install that failed part-way converges rather than colliding,
    including when compensating cleanup itself was incomplete.
  - **Rule registration last**: the rule is DML and is rollback-able, and
    `RULE_ENSURE` already validates the script exists before inserting. A module
    whose statements deployed but whose rule was never registered is INERT — the
    grammar never activates — rather than half-active. The framework already
    orders it this way; this is now a stated guarantee.
* Compensating cleanup drops objects by their declared type and fully-qualified
  name, so it reaches objects outside `PREPROC_RT` (ADR-057). It runs only over
  objects the failed install itself created in this invocation.
* End-state parity remains the load-bearing contract: after `MODULE_INSTALL` the
  database MUST hold every object of the module's inventory + a `PREPROC.CONFIG`
  rule row + a `PREPROC.MODULES` provenance row identical to what the CLI `add`
  produces. The CLI and in-DB paths MUST use statement splitters that agree
  (the shared conformance corpus), because a split divergence would break this
  byte-identical guarantee.
* never-mutate-a-live-version (ADR-046): `MODULE_INSTALL` deploys new objects and
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
* *AND* the resulting objects, rule, and provenance row MUST match the end-state of a CLI `preproc module add cast_shorthand`, the single-statement artifact having taken the same split-then-execute path as a composite one

### Scenario: A sha256 mismatch aborts before any database write

* *GIVEN* a staged tarball whose `cast_shorthand` artifact hashes to a digest other than the index `sha256`
* *WHEN* the caller runs `MODULE_INSTALL` for `cast_shorthand`
* *THEN* the script MUST fail
* *AND* no object of the module's declared inventory MUST exist
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

### Scenario: A composite module installs every declared object in file order

* *GIVEN* a staged source whose module declares an `[[objects]]` inventory of a Lua entry script, two Python UDFs, and one table, carried in one multi-statement artifact
* *WHEN* a caller holding `PREPROC_ADMIN` runs `MODULE_INSTALL` for that module
* *THEN* the script MUST execute the artifact's statements in file order
* *AND* every object named in the declared inventory MUST exist in the database afterwards
* *AND* the activation rule MUST be registered only after the last artifact statement has succeeded

### Scenario: Pre-flight validation of every statement precedes the first DDL

* *GIVEN* a staged composite module whose artifact creates an object absent from its declared `[[objects]]` inventory
* *WHEN* the caller runs `MODULE_INSTALL` for that module
* *THEN* the script MUST fail naming the undeclared object
* *AND* the script MUST NOT have executed any statement of the artifact
* *AND* no object of the inventory MUST exist, and `PREPROC.CONFIG` and `PREPROC.MODULES` MUST be unchanged

### Scenario: A failure at statement k drops the objects created by statements 1 through k-1

* *GIVEN* a staged composite module whose fourth statement is invalid DDL that passes pre-flight splitting but fails at execution
* *WHEN* the caller runs `MODULE_INSTALL` for that module
* *THEN* the script MUST fail reporting the error from the fourth statement
* *AND* the script MUST drop the objects created by the first three statements, walking the declared inventory in reverse
* *AND* no object of the module's declared inventory MUST remain in the database
* *AND* the compensating cleanup MUST NOT replace or mask the original statement error

### Scenario: A partially installed composite module is inert rather than half-active

* *GIVEN* a composite install that failed at a mid-artifact statement and whose compensating cleanup could not drop every created object
* *WHEN* an in-scope user submits a statement the module would have transformed
* *THEN* `PREPROC.CONFIG` MUST contain no rule for the module
* *AND* the user's `PREPROC_RT.MY_PIPELINE` MUST NOT include the module
* *AND* the statement MUST pass through untransformed
* *AND* `PREPROC.MODULES` MUST contain no provenance row for the failed version

### Scenario: Re-running an install that failed part-way converges

* *GIVEN* a composite install that failed at a mid-artifact statement, leaving some objects created
* *AND* a corrected artifact at the same module version
* *WHEN* the caller runs `MODULE_INSTALL` again for that module
* *THEN* the install MUST succeed
* *AND* every object of the declared inventory MUST exist
* *AND* re-executing a statement whose object already exists MUST NOT fail, because every artifact statement uses `CREATE OR REPLACE`

### Scenario: A composite module's objects outside PREPROC_RT are created and cleaned up

* *GIVEN* a staged composite module declaring an object in a schema other than `PREPROC_RT`, which the caller can write
* *WHEN* the caller runs `MODULE_INSTALL` and a later statement fails
* *THEN* the compensating cleanup MUST drop the object in the non-`PREPROC_RT` schema
* *AND* cleanup MUST resolve each object by its declared type and fully-qualified name
