# Feature: In-DB Module Resolver UDFs

Reads and integrity-verifies a module artifact from a source — a BucketFS-staged
release tarball (air-gap-safe) or an HTTPS fetch from GitHub — entirely
inside the database, returning the artifact's runnable text plus its registry
metadata to the in-DB installer.

This delta replaces the resolver's single-`CREATE`-statement structural check
with an object-inventory check, and pins the ordering between chunk reassembly
and statement splitting.

## Background

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
* Chunking is unchanged in mechanism and ordering is now explicit: the resolver
  emits the artifact text as ordered chunks of at most 2,000,000 characters, and
  the caller reassembles ALL chunks in `chunk_index` order BEFORE any statement
  splitting occurs. Splitting a chunk in isolation is incorrect, because a
  chunk boundary may fall inside a statement or inside a `--/` … `/` block. A
  representative composite artifact of roughly 280 KB fits a single chunk; the
  chunked path is exercised only beyond the 2,000,000-character limit.
* The resolver NEVER raises, so a failure cannot fail the caller mid-write:
  every failure path — including an artifact whose object inventory does not
  match the entry's declaration — returns a structured error indicator and no
  usable artifact body, so the orchestrator aborts BEFORE any DB write.

## Scenarios

<!-- DELTA:CHANGED -->
### Scenario: An artifact whose CREATE target differs from the declared script_name is a hard failure

* *GIVEN* a staged tarball whose `cast_shorthand` artifact hashes to the index entry's `sha256` but whose `CREATE OR REPLACE LUA SCRIPT` targets a name other than the declared `script_name`
* *WHEN* the resolver is invoked for `cast_shorthand`
* *THEN* the resolver MUST signal an artifact-invalid failure naming both the found and the declared script name
* *AND* the resolver MUST NOT return usable artifact text
* *AND* the same failure MUST be signalled when the artifact carries no `CREATE OR REPLACE LUA SCRIPT` for the declared `script_name` at all
<!-- /DELTA:CHANGED -->

<!-- DELTA:NEW -->
### Scenario: A composite artifact's object inventory is verified against the index entry

* *GIVEN* a staged tarball whose index entry declares an `objects` inventory of a Lua entry script and two Python UDFs, and whose artifact hashes to the entry's `sha256`
* *WHEN* the resolver is invoked for that module
* *THEN* the resolver MUST split the artifact, derive the created objects, and confirm the derived list equals the declared list
* *AND* on a match the resolver MUST return the full artifact text and the entry metadata
* *AND* the verification MUST run only after the `sha256` check has passed
<!-- /DELTA:NEW -->

<!-- DELTA:NEW -->
### Scenario: An artifact whose derived objects disagree with the entry inventory is a hard failure

* *GIVEN* a staged tarball whose artifact creates an object the index entry's `objects` list does not declare
* *WHEN* the resolver is invoked for that module
* *THEN* the resolver MUST signal an artifact-invalid failure naming the undeclared object
* *AND* the resolver MUST NOT return usable artifact text
* *AND* the same failure MUST be signalled when the entry declares an object the artifact does not create
<!-- /DELTA:NEW -->

<!-- DELTA:NEW -->
### Scenario: A schema-version-1 index entry is verified as a single-statement artifact

* *GIVEN* a staged tarball whose `registry/index.json` declares `schema_version` 1 and whose `cast_shorthand` entry carries no `objects` list
* *WHEN* the resolver is invoked for `cast_shorthand`
* *THEN* the resolver MUST derive the inventory as the single object named by the entry's `script_name`
* *AND* the resolver MUST return the artifact text and metadata unchanged from the pre-delta behaviour
<!-- /DELTA:NEW -->

<!-- DELTA:CHANGED -->
### Scenario: An artifact larger than a single EMITS value is chunked in order

* *GIVEN* a library-deployed composite artifact whose text exceeds the 2,000,000-character single-value limit and whose statements straddle a chunk boundary
* *WHEN* the resolver is invoked for that module
* *THEN* the resolver MUST emit the artifact body as ordered chunks
* *AND* the caller-reassembled text in chunk order MUST equal the artifact bytes decoded as UTF-8
* *AND* statement splitting MUST be performed on the reassembled text only, never on an individual chunk
<!-- /DELTA:CHANGED -->
