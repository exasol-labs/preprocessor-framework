# Feature: In-DB Module Resolver UDFs

Reads and integrity-verifies a module artifact from a source — a BucketFS-staged
release tarball (air-gap-safe) or an HTTPS fetch from GitHub — entirely
inside the database, with no operator CLI and no external Python/uv toolchain,
returning the artifact's runnable text plus its registry metadata to the in-DB
installer. This is the DB-side analogue of the CLI's registry read + artifact
fetch + `sha256` verify (`registry.read_registry` / `fetch_artifact_bytes` /
`verify_entry_artifact`, `deploy.fetch_and_verify_artifact`); it re-implements
those ~3 resolver functions inside the UDF rather than vendoring the `preproc`
package.

## Background

* Exasol Lua scripts have NO filesystem, BucketFS, or network access (mission
  Constraints; ADR-001). Only a UDF can read BucketFS or reach the network, so
  the resolver is a Python3 UDF that lives in the LOCKED admin schema `PREPROC`,
  is owned by `PREPROC_ADMIN`, and is executable by `PREPROC_ADMIN` ONLY — it is
  NEVER granted to PUBLIC. This is a deliberate departure from the PUBLIC UDF
  placement of `PREPROC_RT.DEMO_SQLGLOT_TRANSPILE_V1`, because the resolver
  reaches BucketFS/network and must never be invokable by an ordinary user.
* The source string carries a scheme: `bucketfs:<CONNECTION_NAME>/<path>/preproc-lib-<ver>.tar.gz`
  names an operator-created Exasol CONNECTION object (the framework never creates
  it) and is read natively; a value beginning `https://` is a pinned GitHub raw
  source. The `<CONNECTION_NAME>` is resolved via `exa.get_connection(name)`; no
  connection name is hardcoded.
* The CONNECTION's address MUST be the native Exasol BucketFS form
  `bucketfs:<service>/<bucket>` (the string the Exasol docs prescribe, e.g.
  `CREATE CONNECTION … TO 'bucketfs:bfsdefault/bucket1' IDENTIFIED BY '<readpw>'`) —
  no host, no port. It maps 1:1 to the cluster-local UDF mount
  `/buckets/<service>/<bucket>`; an address already given as an absolute
  `/buckets/…` path is accepted verbatim. Any other address (an `https://…:2581/…`
  URL, a bare host) is a `source_unreadable` error: the database reaches BucketFS
  natively and never needs a host or port, and the service name is taken from the
  address rather than guessed by scanning `/buckets`. Read access is governed by
  Exasol's BucketFS access rights — the granted CONNECTION and its read password —
  not by the resolver.
* The BucketFS reader reads the whole-library release tarball natively through
  the cluster-local BucketFS mount (`/buckets/<service>/<bucket>/...`, mapped
  directly from the CONNECTION's `bucketfs:<service>/<bucket>` address) using
  Python `tarfile`, reads `registry/index.json` from
  within the tarball, selects the requested module's latest entry, and reads that
  module's artifact member. A BucketFS read performs NO network egress — the file
  is cluster-replicated and read locally.
* The HTTPS reader performs `urllib` GETs of `registry/index.json` and the
  artifact from the pinned source. It runs whenever the source uses the `https:`
  scheme — there is NO settings toggle gating it. Activation is implicit: the
  operator opts in by writing an `https://` source and by the cluster being open
  to the host. On a cluster with no outbound egress the GET simply fails; the
  resolver signals that network failure and returns no usable artifact, so the
  orchestrator aborts before any DB write.
* `sha256` verification is MANDATORY for a library-deployed module: the resolver
  computes the digest over the artifact bytes and compares it to the index
  entry's declared `sha256`, exactly as `manifest.verify_artifact_sha256` does. A
  mismatch is a hard failure — the resolver MUST NOT return usable artifact text.
* A self-deployed entry (null `artifact_path` / `sha256`) carries no artifact to
  fetch: the resolver returns the entry's metadata only, with a null artifact
  body, so the installer registers rule + provenance without a CREATE (parity
  with `deploy.py`'s self-deployed skip).
* The resolver returns, for the resolved entry: `name`, `version`, `phase`,
  `script_name`, `function`, `deploy_mode`, `sha256`, `suggested_scope_type`,
  `suggested_scope_value`, `source_repo`, `source_ref`, `library_version`, and the
  artifact text as a `VARCHAR(2000000)` value. `library_version` is the index's
  optional top-level release stamp (NULL when the index carried none), echoed on
  every row so a `latest` install can be audited back to a concrete release. An artifact up to 2,000,000 characters fits a single
  EMITS value; a larger artifact MUST be emitted as ordered chunks the caller
  reassembles in order (chunked-EMITS fallback — verified: EMITS supports
  `VARCHAR(2000000)`).
* After the `sha256` check, the resolver verifies the artifact's STRUCTURE: it
  MUST be exactly one `CREATE OR REPLACE LUA SCRIPT` statement whose target name
  matches the index entry's declared `script_name` (structural parity with
  `manifest.verify_artifact_script_name`). A mismatch — zero or multiple CREATE
  statements, or a name that differs from `script_name` — is a hard failure with
  no usable artifact body.
* The resolver NEVER raises so a failure cannot fail the caller mid-write:
  every failure path (source unreadable, module not found, `sha256` mismatch,
  an artifact whose structure does not match its declared `script_name`, an
  unreachable HTTPS host / network error) returns a structured error indicator
  and no usable artifact body, so the orchestrator aborts BEFORE any DB write (the
  ERR-sentinel contract pattern of `DEMO_SQLGLOT_TRANSPILE_V1`).

## Scenarios

### Scenario: Resolves a library-deployed module from a BucketFS-staged tarball

* *GIVEN* an operator-created CONNECTION naming a BucketFS location that holds `preproc-lib-0.1.0.tar.gz` whose index lists `cast_shorthand` version 1
* *AND* a caller holding `PREPROC_ADMIN` with ACCESS on that CONNECTION
* *WHEN* the resolver is invoked with source `bucketfs:PREPROC_BFS/preproc-lib-0.1.0.tar.gz` and module `cast_shorthand`
* *THEN* the resolver MUST return the artifact text of the `PREPROC_RT.CAST_SHORTHAND_V1` CREATE statement
* *AND* the resolver MUST return the entry metadata (`phase`, `script_name`, `function`, `deploy_mode`, `sha256`, suggested scope, source)
* *AND* the resolver MUST perform no network egress

### Scenario: A sha256 mismatch is a hard failure with no usable artifact

* *GIVEN* a staged tarball whose `cast_shorthand` artifact bytes hash to a digest other than the index entry's `sha256`
* *WHEN* the resolver is invoked for `cast_shorthand`
* *THEN* the resolver MUST signal a `sha256`-mismatch failure
* *AND* the resolver MUST NOT return usable artifact text

### Scenario: An artifact whose CREATE target differs from the declared script_name is a hard failure

* *GIVEN* a staged tarball whose `cast_shorthand` artifact hashes to the index entry's `sha256` but whose `CREATE OR REPLACE LUA SCRIPT` targets a name other than the declared `script_name`
* *WHEN* the resolver is invoked for `cast_shorthand`
* *THEN* the resolver MUST signal an artifact-invalid failure naming both the found and the declared script name
* *AND* the resolver MUST NOT return usable artifact text

### Scenario: A self-deployed entry returns metadata only

* *GIVEN* a source whose index lists a module with `deploy_mode = "self-deployed"` and null `artifact_path` and `sha256`
* *WHEN* the resolver is invoked for that module
* *THEN* the resolver MUST return the entry metadata with a null artifact body
* *AND* the resolver MUST NOT attempt to read an artifact member or verify a `sha256`

### Scenario: An HTTPS source is fetched and verified when the scheme is https

* *GIVEN* a pinned GitHub raw source, reachable from the cluster, whose index lists `cast_shorthand` version 1 with a matching `sha256`
* *WHEN* the resolver is invoked with the `https://` source and module `cast_shorthand`
* *THEN* the resolver MUST fetch `registry/index.json` and the artifact over HTTPS with no settings toggle consulted
* *AND* the resolver MUST verify the artifact `sha256` and, on a match, return the artifact text and metadata

### Scenario: An HTTPS source on a no-egress cluster fails with a network error

* *GIVEN* an `https://` source whose host the cluster cannot reach (no outbound egress)
* *WHEN* the resolver is invoked with that source
* *THEN* the resolver MUST signal a network failure naming the unreachable source
* *AND* the resolver MUST NOT return usable artifact text

### Scenario: A module absent from the source index is reported not found

* *GIVEN* a source whose `registry/index.json` does not list the requested module
* *WHEN* the resolver is invoked for that module
* *THEN* the resolver MUST signal a not-found failure naming the module
* *AND* the resolver MUST NOT return artifact text

### Scenario: The latest version is selected when the index lists several

* *GIVEN* a source whose index lists `cast_shorthand` versions 1 and 2
* *WHEN* the resolver is invoked for `cast_shorthand` with no version pin
* *THEN* the resolver MUST select the highest version (version 2)
* *AND* the returned `script_name` MUST carry the `_V2` suffix

### Scenario: A module name is matched case-insensitively

* *GIVEN* a source whose index lists `cast_shorthand`
* *WHEN* the resolver is invoked with the name in any case (for example `CAST_SHORTHAND`, as the sugar emits after folding an unquoted name)
* *THEN* the resolver MUST resolve the same entry — module-name matching is case-insensitive
* *AND* a library is expected to carry no two names differing only by case

### Scenario: An artifact larger than a single EMITS value is chunked in order

* *GIVEN* a library-deployed artifact whose text exceeds the 2,000,000-character single-value limit
* *WHEN* the resolver is invoked for that module
* *THEN* the resolver MUST emit the artifact body as ordered chunks
* *AND* the caller-reassembled text in chunk order MUST equal the artifact bytes decoded as UTF-8

### Scenario: The resolver is not executable by a non-admin user

* *GIVEN* a database user who does not hold `PREPROC_ADMIN`
* *WHEN* that user attempts to execute the resolver UDF
* *THEN* the database MUST reject the execution with an insufficient-privileges error
* *AND* the resolver UDF MUST NOT be granted to PUBLIC
