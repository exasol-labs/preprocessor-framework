# Feature: In-DB Module Install (PREPROC.MODULE_INSTALL)

Lets a DBA install a preprocessor module entirely from SQL — a single
`EXECUTE SCRIPT PREPROC.MODULE_INSTALL(source, module_name, scope)` — with no
external Python/uv toolchain. It re-homes the CLI's `deploy_module` sequence
(`src/preproc/module/deploy.py`) into the database: it drives the resolver UDF to
read and verify the artifact, then deploys the versioned script, registers the
activation rule, writes provenance, and refreshes — leaving an end-state
BYTE-IDENTICAL to a CLI `preproc module add`.

This delta generalises step 2 from a single `pquery(artifact_text)` to executing
the artifact's 1..N statements in file order, and states the atomicity
construction explicitly. DDL cannot be rolled back, so all-or-nothing is BUILT
rather than inherited: pre-flight validation before the first DDL, compensating
cleanup of the objects already created when a later statement fails, idempotent
re-run, and rule registration last so a partially installed module is inert.

## Background

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
    orders it this way; this delta makes it a stated guarantee.
* Compensating cleanup drops objects by their declared type and fully-qualified
  name, so it reaches objects outside `PREPROC_RT` (ADR-057). It runs only over
  objects the failed install itself created in this invocation.
* End-state parity remains the load-bearing contract: after `MODULE_INSTALL` the
  database MUST hold every object of the module's inventory + a `PREPROC.CONFIG`
  rule row + a `PREPROC.MODULES` provenance row identical to what the CLI `add`
  produces. The CLI and in-DB paths MUST use statement splitters that agree
  (the shared conformance corpus), because a split divergence would break this
  byte-identical guarantee.

## Scenarios

<!-- DELTA:CHANGED -->
### Scenario: Installs a library-deployed module from a BucketFS source, end-state byte-identical to a CLI install

* *GIVEN* an installed framework and an operator-created BucketFS CONNECTION staging `preproc-lib-0.1.0.tar.gz` with `cast_shorthand` version 1
* *AND* a caller holding `PREPROC_ADMIN` with ACCESS on the CONNECTION
* *WHEN* the caller runs `EXECUTE SCRIPT PREPROC.MODULE_INSTALL('bucketfs:PREPROC_BFS/preproc-lib-0.1.0.tar.gz', 'cast_shorthand', 'ROLE:ANALYSTS')`
* *THEN* the script MUST create `PREPROC_RT.CAST_SHORTHAND_V1`, register the TRANSLATE rule at `ROLE ANALYSTS` via `PREPROC.RULE_ENSURE`, and write a `PREPROC.MODULES` provenance row for the deployed version
* *AND* a subsequent in-scope statement MUST be transformed by the module
* *AND* the resulting objects, rule, and provenance row MUST match the end-state of a CLI `preproc module add cast_shorthand`, the single-statement artifact having taken the same split-then-execute path as a composite one
<!-- /DELTA:CHANGED -->

<!-- DELTA:NEW -->
### Scenario: A composite module installs every declared object in file order

* *GIVEN* a staged source whose module declares an `[[objects]]` inventory of a Lua entry script, two Python UDFs, and one table, carried in one multi-statement artifact
* *WHEN* a caller holding `PREPROC_ADMIN` runs `MODULE_INSTALL` for that module
* *THEN* the script MUST execute the artifact's statements in file order
* *AND* every object named in the declared inventory MUST exist in the database afterwards
* *AND* the activation rule MUST be registered only after the last artifact statement has succeeded
<!-- /DELTA:NEW -->

<!-- DELTA:NEW -->
### Scenario: Pre-flight validation of every statement precedes the first DDL

* *GIVEN* a staged composite module whose artifact creates an object absent from its declared `[[objects]]` inventory
* *WHEN* the caller runs `MODULE_INSTALL` for that module
* *THEN* the script MUST fail naming the undeclared object
* *AND* the script MUST NOT have executed any statement of the artifact
* *AND* no object of the inventory MUST exist, and `PREPROC.CONFIG` and `PREPROC.MODULES` MUST be unchanged
<!-- /DELTA:NEW -->

<!-- DELTA:NEW -->
### Scenario: A failure at statement k drops the objects created by statements 1 through k-1

* *GIVEN* a staged composite module whose fourth statement is invalid DDL that passes pre-flight splitting but fails at execution
* *WHEN* the caller runs `MODULE_INSTALL` for that module
* *THEN* the script MUST fail reporting the error from the fourth statement
* *AND* the script MUST drop the objects created by the first three statements, walking the declared inventory in reverse
* *AND* no object of the module's declared inventory MUST remain in the database
* *AND* the compensating cleanup MUST NOT replace or mask the original statement error
<!-- /DELTA:NEW -->

<!-- DELTA:NEW -->
### Scenario: A partially installed composite module is inert rather than half-active

* *GIVEN* a composite install that failed at a mid-artifact statement and whose compensating cleanup could not drop every created object
* *WHEN* an in-scope user submits a statement the module would have transformed
* *THEN* `PREPROC.CONFIG` MUST contain no rule for the module
* *AND* the user's `PREPROC_RT.MY_PIPELINE` MUST NOT include the module
* *AND* the statement MUST pass through untransformed
* *AND* `PREPROC.MODULES` MUST contain no provenance row for the failed version
<!-- /DELTA:NEW -->

<!-- DELTA:NEW -->
### Scenario: Re-running an install that failed part-way converges

* *GIVEN* a composite install that failed at a mid-artifact statement, leaving some objects created
* *AND* a corrected artifact at the same module version
* *WHEN* the caller runs `MODULE_INSTALL` again for that module
* *THEN* the install MUST succeed
* *AND* every object of the declared inventory MUST exist
* *AND* re-executing a statement whose object already exists MUST NOT fail, because every artifact statement uses `CREATE OR REPLACE`
<!-- /DELTA:NEW -->

<!-- DELTA:NEW -->
### Scenario: A composite module's objects outside PREPROC_RT are created and cleaned up

* *GIVEN* a staged composite module declaring an object in a schema other than `PREPROC_RT`, which the caller can write
* *WHEN* the caller runs `MODULE_INSTALL` and a later statement fails
* *THEN* the compensating cleanup MUST drop the object in the non-`PREPROC_RT` schema
* *AND* cleanup MUST resolve each object by its declared type and fully-qualified name
<!-- /DELTA:NEW -->

<!-- DELTA:CHANGED -->
### Scenario: A sha256 mismatch aborts before any database write

* *GIVEN* a staged tarball whose `cast_shorthand` artifact hashes to a digest other than the index `sha256`
* *WHEN* the caller runs `MODULE_INSTALL` for `cast_shorthand`
* *THEN* the script MUST fail
* *AND* no object of the module's declared inventory MUST exist
* *AND* `PREPROC.CONFIG` and `PREPROC.MODULES` MUST be unchanged
<!-- /DELTA:CHANGED -->
