# Feature: Two-Schema Database Layout

Establishes the foundational database objects the preprocessor framework
dispatches from. This delta extends the layout for the in-database module
installer: the locked admin schema `PREPROC` gains the admin-only resolver
UDF(s), `PREPROC.MODULE_INSTALL`, and `PREPROC.MODULE_CATALOG` — owned by
`PREPROC_ADMIN` and NEVER granted to PUBLIC. The BucketFS source used by the
installer is an operator-created CONNECTION object; the framework never creates
it.

## Background

* The DDL source of truth lives in numbered files under `sql/install/` and is
  applied in filename order. All object creation is idempotent: `IF NOT EXISTS` /
  `CREATE OR REPLACE`, and column additions on already-installed databases ship as
  guarded `ALTER TABLE ... ADD COLUMN` statements that tolerate "already exists"
  via the `--@tolerate-exists` applier contract.
* The resolver Python3 UDF(s), `PREPROC.MODULE_INSTALL`, and
  `PREPROC.MODULE_CATALOG` are objects in the locked `PREPROC` admin schema owned
  by `PREPROC_ADMIN`. Unlike the runtime schema, `PREPROC` is not granted to
  PUBLIC (ADR-002), so these objects are admin-only — the resolver, which reaches
  BucketFS/network, is never invokable by an ordinary user.
* The BucketFS source is an operator responsibility: the operator creates the
  Exasol CONNECTION object and grants ACCESS on it to `PREPROC_ADMIN`; the
  framework install DDL creates no CONNECTION. The source string names that
  CONNECTION (`bucketfs:<CONNECTION_NAME>/<path>`), so no connection name is
  hardcoded.

## Scenarios

<!-- DELTA:NEW -->
### Scenario: The resolver UDF and install/catalog scripts are created admin-only in PREPROC

* *GIVEN* schema `PREPROC` exists with its admin tables
* *WHEN* the install DDL and UDF/Lua deployment are applied
* *THEN* the system MUST create the resolver Python3 UDF(s), the Lua script `PREPROC.MODULE_INSTALL`, and the Lua script `PREPROC.MODULE_CATALOG`, all owned by `PREPROC_ADMIN`
* *AND* none of them MUST be executable by PUBLIC
* *AND* re-deploying them with `CREATE OR REPLACE` MUST succeed without error
<!-- /DELTA:NEW -->

<!-- DELTA:NEW -->
### Scenario: The BucketFS source is an operator-created CONNECTION, not a framework object

* *GIVEN* a DBA preparing an air-gapped in-database install from a BucketFS-staged release tarball
* *WHEN* the framework install DDL is applied
* *THEN* the install DDL MUST NOT create any BucketFS CONNECTION object
* *AND* the in-database installer MUST resolve the BucketFS location from a CONNECTION named in the source string (`bucketfs:<CONNECTION_NAME>/<path>`)
* *AND* using that CONNECTION MUST require the operator to have created it and granted ACCESS on it to `PREPROC_ADMIN`
<!-- /DELTA:NEW -->
