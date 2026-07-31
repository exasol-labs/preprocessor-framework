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

The resolver's single-`CREATE`-statement structural check is generalised to an
object-inventory check, and the ordering between chunk reassembly and
statement splitting is pinned explicitly.

This delta replaces the BucketFS source grammar with a plain BucketFS LOCATION.
A `bucketfs:` source is now `bucketfs:<service>/<bucket>/<path>` — the first
segment is ALWAYS the BucketFS service, the second ALWAYS the bucket, the
remainder the release path — and it NEVER names an Exasol CONNECTION object.
`exa.get_connection` leaves the BucketFS path entirely: credentials are resolved
by the database, not by the resolver. This is a BREAKING grammar change, taken
deliberately while the project is pre-1.0.

## Background

* Exasol Lua scripts have NO filesystem, BucketFS, or network access (mission
  Constraints; ADR-001). Only a UDF can read BucketFS or reach the network, so
  the resolver is a Python3 UDF that lives in the LOCKED admin schema `PREPROC`,
  is owned by `PREPROC_ADMIN`, and is executable by `PREPROC_ADMIN` ONLY — it is
  NEVER granted to PUBLIC. This is a deliberate departure from the PUBLIC UDF
  placement of `PREPROC_RT.DEMO_SQLGLOT_TRANSPILE_V1`, because the resolver
  reaches BucketFS/network and must never be invokable by an ordinary user.
* The source string carries a scheme. A value beginning `bucketfs:` is a
  BucketFS LOCATION and is read natively; a value beginning `https://` is a
  pinned GitHub raw source.
* A BucketFS source has EXACTLY the shape
  `bucketfs:<service>/<bucket>/<release-path>` — for example
  `bucketfs:bfsdefault/default/preproc-lib-0.3.0.tar.gz`. The first path segment
  is ALWAYS the BucketFS service name, the second ALWAYS the bucket, and every
  remaining segment (one or more) is the release path inside that bucket. The
  service and bucket map 1:1 to the cluster-local UDF mount
  `/buckets/<service>/<bucket>`, so the resolver derives the mount path from the
  SOURCE alone and reads the release off it.
* The source string NEVER names an Exasol CONNECTION object, and the resolver
  performs NO `exa.get_connection` call on the BucketFS path. Credentials are
  resolved by the database: the operator creates a CONNECTION
  `TO 'bucketfs:<service>/<bucket>' IDENTIFIED BY '<read-password>'` (the name
  is arbitrary and is never referenced by the framework) and grants it to the
  installing admin; Exasol then mounts that read-protected bucket into the UDF
  container for that caller. The CONNECTION is still an operator prerequisite —
  it is simply not part of the source grammar.
* Because the mount path comes from the source, the resolver has no CONNECTION
  address to interpret: the address-form rules that previously governed a
  BucketFS CONNECTION (native `bucketfs:<service>/<bucket>` only, an absolute
  `/buckets/…` accepted verbatim, an `https://…:2581/…` URL rejected) no longer
  apply to the resolver at all. They remain an operator concern when creating
  the CONNECTION, enforced by Exasol rather than by this UDF.
* A BucketFS source with fewer than three non-empty segments — notably the
  pre-delta two-segment form `bucketfs:PREPROC_BFS/preproc-lib-0.3.0.tar.gz` —
  is MALFORMED and is a `bad_source` error naming the expected
  `bucketfs:<service>/<bucket>/<path>` shape. There is NO heuristic that tries
  to tell a service name from a connection name, and NO fallback to a CONNECTION
  lookup: the two forms are grammatically indistinguishable, so accepting both
  would make the meaning of a source depend on database state. This is a
  deliberate hard break (ADR-065).
* Every segment of a BucketFS source MUST be a plain path segment. A segment
  that is empty, `.`, or `..` is a `bad_source` error, so a source can never
  address a mount outside `/buckets/<service>/<bucket>`.
* The BucketFS reader reads the whole-library release natively through the
  cluster-local mount using Python `tarfile`, reads `registry/index.json` from
  within the release, selects the requested module's latest entry, and reads that
  module's artifact member. BucketFS AUTO-EXTRACTS an uploaded archive and
  presents it on the UDF mount as a DIRECTORY at the extension-less path, so the
  reader probes BOTH `/buckets/<service>/<bucket>/<release-path>` (a raw tarball
  file) AND the same path with its archive extension stripped (the auto-extracted
  directory). A BucketFS read performs NO network egress — the file is
  cluster-replicated and read locally.
* When NEITHER probe finds a release, the resolver reports ONE
  `source_unreadable` error. It cannot distinguish "no such bucket" from "bucket
  not granted to this caller", because an unmounted bucket and an absent file are
  the same observation on the filesystem, so there is no second error kind and no
  separate bucket-existence probe. The message MUST name the tarball path it
  probed, the auto-extracted directory path it probed, AND state that a
  read-protected bucket is only mounted for a caller granted a
  `CONNECTION TO 'bucketfs:<service>/<bucket>' IDENTIFIED BY '<read-password>'`,
  with the service and bucket interpolated from the source — so the operator sees
  the credential prerequisite in the one message they will actually read.
* The HTTPS reader performs `urllib` GETs of `registry/index.json` and the
  artifact from the pinned source. It runs whenever the source uses the `https:`
  scheme — there is NO settings toggle gating it. Activation is implicit: the
  operator opts in by writing an `https://` source and by the cluster being open
  to the host. On a cluster with no outbound egress the GET simply fails; the
  resolver signals that network failure and returns no usable artifact, so the
  orchestrator aborts before any DB write. The HTTPS path is UNCHANGED by this
  delta.
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
* Chunking is unchanged in mechanism and ordering is now explicit: the resolver
  emits the artifact text as ordered chunks of at most 2,000,000 characters, and
  the caller reassembles ALL chunks in `chunk_index` order BEFORE any statement
  splitting occurs. Splitting a chunk in isolation is incorrect, because a
  chunk boundary may fall inside a statement or inside a `--/` … `/` block. A
  representative composite artifact of roughly 280 KB fits a single chunk; the
  chunked path is exercised only beyond the 2,000,000-character limit.
* After the `sha256` check, the resolver verifies the artifact's STRUCTURE
  against the index entry's declared object inventory rather than against a
  single `script_name`: it splits the artifact into statements, derives the
  object each statement creates, and compares the derived list to the entry's
  declared `objects` list. A disagreement in either direction — an object the
  artifact creates but the entry does not declare, or an entry-declared object
  no statement creates — is a hard failure with no usable artifact body. The
  artifact MUST still contain exactly one `CREATE OR REPLACE LUA SCRIPT` whose
  target matches the declared `script_name`; that script is the module's entry
  point.
* An index entry from a `schema_version = 1` index carries no `objects` list. It
  is interpreted as a single-statement entry whose inventory is the one object
  named by `script_name`, which preserves the existing check exactly.
* The resolver NEVER raises, so a failure cannot fail the caller mid-write:
  every failure path — including an artifact whose object inventory does not
  match the entry's declaration — returns a structured error indicator and no
  usable artifact body, so the orchestrator aborts BEFORE any DB write.
* The cluster-local mount root `/buckets` is a single named constant in the UDF
  body. Production never varies it; the off-database test harness rebinds it to a
  temporary directory so the whole `<service>/<bucket>/<path>` derivation is
  exercised offline. This replaces the pre-delta harness seam, which was the fake
  `exa.get_connection` the resolver no longer calls.

## Scenarios

### Scenario: Resolves a library-deployed module from a BucketFS-staged tarball

* *GIVEN* a BucketFS bucket `default` on service `bfsdefault` holding `preproc-lib-0.1.0.tar.gz` whose index lists `cast_shorthand` version 1
* *AND* a caller holding `PREPROC_ADMIN` for whom that bucket is mounted at `/buckets/bfsdefault/default`
* *WHEN* the resolver is invoked with source `bucketfs:bfsdefault/default/preproc-lib-0.1.0.tar.gz` and module `cast_shorthand`
* *THEN* the resolver MUST return the artifact text of the `PREPROC_RT.CAST_SHORTHAND_V1` CREATE statement
* *AND* the resolver MUST return the entry metadata (`phase`, `script_name`, `function`, `deploy_mode`, `sha256`, suggested scope, source)
* *AND* the resolver MUST derive the mount path from the source's first two segments alone, performing no network egress

### Scenario: A BucketFS source is the location, never a CONNECTION name

* *GIVEN* a resolvable BucketFS location `bucketfs:bfsdefault/default/preproc-lib-0.1.0.tar.gz`
* *WHEN* the resolver resolves any module from it
* *THEN* the resolver MUST NOT call `exa.get_connection` on the BucketFS path
* *AND* the resolver MUST NOT read any CONNECTION address
* *AND* the resolved artifact and metadata MUST be identical to what the pre-delta connection-named form returned for the same staged release

### Scenario: A two-segment BucketFS source is malformed

* *GIVEN* the pre-delta source form `bucketfs:PREPROC_BFS/preproc-lib-0.1.0.tar.gz`, which carries only a first segment and a release path
* *WHEN* the resolver is invoked with that source
* *THEN* the resolver MUST signal a `bad_source` failure
* *AND* the message MUST name the expected `bucketfs:<service>/<bucket>/<path>` shape
* *AND* the resolver MUST NOT attempt a CONNECTION lookup as a fallback
* *AND* the resolver MUST NOT return artifact text

### Scenario: A BucketFS source segment that escapes the bucket is malformed

* *GIVEN* a BucketFS source whose release path contains an empty, `.`, or `..` segment (for example `bucketfs:bfsdefault/default/../other/lib.tar.gz`)
* *WHEN* the resolver is invoked with that source
* *THEN* the resolver MUST signal a `bad_source` failure
* *AND* the resolver MUST NOT open any path outside `/buckets/<service>/<bucket>`

### Scenario: An unreadable BucketFS location names both probed paths and the credential prerequisite

* *GIVEN* a well-formed source `bucketfs:bfsdefault/default/preproc-lib-0.1.0.tar.gz` for which no release is present on the UDF mount — whether because the bucket holds no such file or because the bucket is read-protected and not mounted for this caller
* *WHEN* the resolver is invoked with that source
* *THEN* the resolver MUST signal exactly one `source_unreadable` failure — there MUST NOT be a second error kind distinguishing the two causes, and the resolver MUST NOT perform a separate bucket-existence probe
* *AND* the message MUST name BOTH probed paths — `/buckets/bfsdefault/default/preproc-lib-0.1.0.tar.gz` as the tarball and `/buckets/bfsdefault/default/preproc-lib-0.1.0` as the auto-extracted directory
* *AND* the message MUST state that a read-protected bucket is only mounted for a caller granted a `CONNECTION TO 'bucketfs:bfsdefault/default' IDENTIFIED BY '<read-password>'`, with the service and bucket taken from the source
* *AND* the resolver MUST NOT return artifact text

### Scenario: A read-protected bucket is readable through a granted CONNECTION the source never names

* *GIVEN* a READ-PROTECTED BucketFS bucket holding a release, and a live Exasol instance
* *AND* an operator-created CONNECTION `TO 'bucketfs:<service>/<bucket>' IDENTIFIED BY '<read-password>'` whose name appears nowhere in the source string
* *WHEN* the caller is granted ACCESS on that CONNECTION and invokes the resolver with `bucketfs:<service>/<bucket>/<release-path>`
* *THEN* the resolver MUST resolve the module, proving Exasol mounts the read-protected bucket for a grantee without the UDF naming the CONNECTION
* *AND* WHEN the grant is revoked and the resolver is invoked again with the same source, the resolver MUST signal `source_unreadable`
* *AND* this scenario MUST be verified against a read-protected bucket, NOT the publicly readable `default` bucket, because a public bucket is mounted regardless of any grant and would satisfy the first outcome vacuously

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
* *AND* the same failure MUST be signalled when the artifact carries no `CREATE OR REPLACE LUA SCRIPT` for the declared `script_name` at all

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
* *AND* a library MUST NOT carry two names differing only by case

### Scenario: An artifact larger than a single EMITS value is chunked in order

* *GIVEN* a library-deployed composite artifact whose text exceeds the 2,000,000-character single-value limit and whose statements straddle a chunk boundary
* *WHEN* the resolver is invoked for that module
* *THEN* the resolver MUST emit the artifact body as ordered chunks
* *AND* the caller-reassembled text in chunk order MUST equal the artifact bytes decoded as UTF-8
* *AND* statement splitting MUST be performed on the reassembled text only, never on an individual chunk

### Scenario: The resolver is not executable by a non-admin user

* *GIVEN* a database user who does not hold `PREPROC_ADMIN`
* *WHEN* that user attempts to execute the resolver UDF
* *THEN* the database MUST reject the execution with an insufficient-privileges error
* *AND* the resolver UDF MUST NOT be granted to PUBLIC

### Scenario: A composite artifact's object inventory is verified against the index entry

* *GIVEN* a staged tarball whose index entry declares an `objects` inventory of a Lua entry script and two Python UDFs, and whose artifact hashes to the entry's `sha256`
* *WHEN* the resolver is invoked for that module
* *THEN* the resolver MUST split the artifact, derive the created objects, and confirm the derived list equals the declared list
* *AND* on a match the resolver MUST return the full artifact text and the entry metadata
* *AND* the verification MUST run only after the `sha256` check has passed

### Scenario: An artifact whose derived objects disagree with the entry inventory is a hard failure

* *GIVEN* a staged tarball whose artifact creates an object the index entry's `objects` list does not declare
* *WHEN* the resolver is invoked for that module
* *THEN* the resolver MUST signal an artifact-invalid failure naming the undeclared object
* *AND* the resolver MUST NOT return usable artifact text
* *AND* the same failure MUST be signalled when the entry declares an object the artifact does not create

### Scenario: A schema-version-1 index entry is verified as a single-statement artifact

* *GIVEN* a staged tarball whose `registry/index.json` declares `schema_version` 1 and whose `cast_shorthand` entry carries no `objects` list
* *WHEN* the resolver is invoked for `cast_shorthand`
* *THEN* the resolver MUST derive the inventory as the single object named by the entry's `script_name`
* *AND* the resolver MUST return the artifact text and metadata unchanged from the pre-delta behaviour
