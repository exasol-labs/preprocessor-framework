# Plan: change-bucketfs-source-to-location

## Summary

A BucketFS module source today embeds an Exasol CONNECTION name as its first path segment (`bucketfs:PREPROC_BFS/preproc-lib-0.3.0.tar.gz`), so the same `bucketfs:` prefix means two different things in the two places an operator types it. This plan makes the source the plain BucketFS location `bucketfs:<service>/<bucket>/<path>`, removes `exa.get_connection` from the resolver's BucketFS path entirely, and sweeps the change through the specs, tests, docs, and demos of this repo. The sibling `preprocessor-library` README was updated by hand by the user on 2026-07-31 and is out of scope.

## Design

### Context

The resolver's BucketFS reader (`sql/udf/module_resolver_v1.sql:609`) splits the source on its first `/`, treats the left half as a CONNECTION name, calls `exa.get_connection(name)`, and maps the returned `connection.address` — which must itself be `bucketfs:<service>/<bucket>` — to the UDF mount `/buckets/<service>/<bucket>` via `_mount_base` (line 673). The release path is the right half.

That indirection buys nothing. Every fact it recovers is already known to the operator writing the source string, and it costs three things:

1. **A dual meaning for one prefix.** `bucketfs:bfsdefault/default` in `CREATE CONNECTION … TO` is a *bucket*; `bucketfs:PREPROC_BFS/lib.tar.gz` in a source string is a *CONNECTION plus a path*. The framework's own documentation has to stop and explain this — `docs/air-gap-runbook.md:229-235` has a table headed "**Two meanings of `bucketfs:`**" (the sibling library README carried the same note until the user removed it on 2026-07-31). A persona study (`studies/2026-07-persona-docs/persona-1-airgap-dba.md:72`) recorded it as a comprehension failure.
2. **A hardcoded-name hazard the framework works hard to avoid.** Because the source carries an operator-chosen name, `PREPROC.MODULES.source_url` records provenance against a mutable indirection: `PREPROC_BFS` can be dropped and recreated pointing at a different bucket, and every historical provenance row silently re-targets.
3. **A failure surface that does not correspond to anything.** `connection '<NAME>' is not accessible: F-UDF-CL-SL-PYTHON-1121` and `unsupported BucketFS connection address 'https://…:2581/…'` are both errors about the *shape of an indirection*, not about the release the operator is trying to read.

The premise that makes the indirection removable — asserted by the user, not yet verified in this repo — is that **Exasol mounts a read-protected bucket at `/buckets/<service>/<bucket>` for any session whose user has been granted a CONNECTION pointing at that bucket with the correct read password, without the UDF ever naming that CONNECTION**. The CONNECTION does not disappear; it stops being an address-book entry and becomes purely a credential grant.

This premise is load-bearing and the existing test environment cannot confirm it. `tests/conftest.py:83-85` states outright that "BucketFS reads are public but writes need `EXASOL_BFS_WRITE_PASSWORD`" — the `default` bucket the suite stages into is publicly readable, so it is mounted regardless of any grant. Every integration test in this plan would therefore pass *whether or not the premise holds*. Verifying it demands a deliberately read-protected bucket and a grant/revoke pair, and that verification is Task 0.1, gating everything else.

- **Goals** — one meaning for `bucketfs:`; a source string that is self-describing and audit-stable; no `exa.get_connection` on the BucketFS path; a `source_unreadable` message that names the credential prerequisite the operator most often missed; the premise verified against a live read-protected bucket before the rewrite is trusted.
- **Non-Goals** — no change to the HTTPS reader, the `sha256` check, the object-inventory verification, chunking, the install sequence, atomicity, or the sugar grammar; no backward compatibility with the two-segment form; no second error kind distinguishing "no such bucket" from "not granted"; no separate bucket-existence probe; no change to how an operator creates or grants the CONNECTION.

### Decision

The BucketFS source is parsed as a location and nothing else.

```
bucketfs:bfsdefault/default/preproc-lib-0.3.0.tar.gz
         └────┬───┘ └──┬──┘ └──────────┬───────────┘
           service   bucket        release path (1..N segments)
```

#### Architecture

```
BEFORE                                    AFTER
──────                                    ─────
source 'bucketfs:PREPROC_BFS/lib.tgz'     source 'bucketfs:bfsdefault/default/lib.tgz'
  │ partition('/')                          │ split into >= 3 non-empty segments
  ▼                                         ▼
conn_name='PREPROC_BFS'                   service='bfsdefault'
member_root='lib.tgz'                     bucket='default'
  │                                       release_path='lib.tgz'
  ▼  exa.get_connection(conn_name)          │
connection.address                          │  (no exa call at all)
  │  _mount_base(address)                   │  MOUNT_ROOT + '/' + service + '/' + bucket
  ▼                                         ▼
'/buckets/bfsdefault/default'             '/buckets/bfsdefault/default'
  │                                         │
  ├─ isfile(base+'/lib.tgz')  → tarball ────┤   (probe pair unchanged)
  └─ isdir(base+'/lib')       → extracted ──┘

DELETED: _mount_base(), the exa.get_connection call, and every error they owned
         (connection-not-accessible, empty/unsupported/malformed CONNECTION address)
ADDED:   MOUNT_ROOT constant; >= 3-segment + no-traversal grammar check (bad_source);
         credential hint appended to the existing source_unreadable message
```

Nothing downstream of the reader changes. `_tarball_reader`, `_directory_reader`, `_strip_archive_extension`, `_parse_index`, `_select_entry`, `_verify_inventory`, the chunked EMITS, and the whole `https:` branch are untouched.

#### Patterns

| Pattern | Where | Why |
|---------|-------|-----|
| Hard break, no compatibility shim | `_bucketfs_reader` grammar | `bucketfs:X/y` is grammatically identical in both forms. Any heuristic (probe as a service, fall back to a CONNECTION) would make a source's meaning depend on database state, so an operator's typo could silently resolve a *different* release. A `bad_source` naming the expected shape is the only non-surprising behaviour. |
| One error kind, richer message | `source_unreadable` | The filesystem cannot distinguish an unmounted bucket from an absent file, so a second error kind would be a guess presented as a fact. The credential hint carries the diagnostic value a second kind would have carried, without claiming to know which cause applies. |
| Named mount-root constant as the test seam | `MOUNT_ROOT = '/buckets'` | Removing `exa.get_connection` removes the seam the offline harness used to point the resolver at a `tmp_path` tree. A module-level constant the harness rebinds in the exec'd namespace restores it, exercises the full `<service>/<bucket>/<path>` derivation offline, and mirrors the existing `namespace["exa"] = …` injection in `run_resolver_body`. |
| Reject traversal segments | source grammar | The mount base now comes from operator-supplied text rather than from a CONNECTION address, so an empty, `.`, or `..` segment is rejected as `bad_source` and a source can never address a path outside its own bucket. |
| Source passed through verbatim | sugar, `MODULE_INSTALL`, `MODULE_CATALOG` | Source validity is the resolver's single contract. No other layer parses the string, so the grammar has exactly one implementation and exactly one error site. |
| Premise verified before rewrite | Task 0.1 | The design rests on an unverified claim about Exasol's mount behaviour, and the standard test bucket is public, so the suite cannot falsify it. Verification is a gate, not a checkbox. |

### Consequences

| Decision | Alternatives Considered | Rationale |
|----------|------------------------|-----------|
| Source is the plain location; the CONNECTION is never named | Keep the CONNECTION-named form; support both forms; add a `bucketfsloc:` scheme for the new form | Keeping it preserves the dual meaning of `bucketfs:` that the docs and a persona study both flag as the comprehension failure of this path. Supporting both is impossible to do safely — the forms are grammatically identical. A new scheme would be unambiguous but leaves two schemes to document forever and makes the old, worse form the one that keeps the good name. |
| Hard break: a two-segment source is `bad_source` | Probe `/buckets/<seg1>` and fall back to a CONNECTION lookup; deprecation window with a warning row | Probing makes a source's meaning depend on database state: a bucket coincidentally named like a connection resolves the wrong release, silently. A deprecation window needs the ambiguous dual parse it exists to retire. The project is pre-1.0 (0.7.1) with no external installed base, so a loud break costs one edit per source string and buys an unambiguous grammar. |
| One `source_unreadable` with a credential hint | A second `bucket_not_granted` error kind; a bucket-existence probe before the file probe | The resolver observes one thing — the path is not there. `os.path.isdir('/buckets/<service>')` would distinguish *some* cases but not the important one (a mounted service with an unmounted read-protected bucket looks identical to a typo'd bucket name), so a second kind would misattribute causes. Naming both probed paths and the grant prerequisite gives the operator the same actionable information with no false precision. |
| `_mount_base` deleted outright | Keep it to parse the SOURCE's `bucketfs:` prefix | Its remaining job is a two-line split the new parser already does, and its other behaviours are actively wrong now: accepting an absolute `/buckets/…` verbatim would let a source bypass the bucket boundary, and its address-shape errors describe a CONNECTION the resolver no longer reads. |
| `MOUNT_ROOT` constant as the offline test seam | Monkeypatch `os.path.isfile`/`isdir`; fake the `os` module in the exec'd namespace; make the offline tests live-only | Monkeypatching `os` couples every test to the reader's exact call sequence, so a refactor breaks tests that should not care. Dropping offline coverage would leave ~35 hermetic tests dependent on a live cluster. A named constant is one production line, changes no production behaviour, and — unlike the `exa` seam it replaces — exercises the real `<service>/<bucket>` derivation instead of bypassing it. |
| Verify the premise first, against a read-protected bucket | Trust the assertion and let the integration suite stand as evidence | The suite stages into a publicly readable bucket, so it would pass either way. Treating a green suite as confirmation would be a false positive on the one fact the whole design depends on. |
| Traversal segments rejected | Leave path handling as-is (the pre-delta form had the same hole) | Cheap to add while the parser is being rewritten anyway, and the justification is stronger now: the mount base is derived from operator text rather than from a CONNECTION address the operator could not choose freely. |

Further consequences:

* `exa` is referenced NOWHERE in the resolver body after this change (only at `sql/udf/module_resolver_v1.sql:24` in a header comment and `:615` in the deleted call). `make_fake_exa` in `tests/_dbfixtures.py` and the `exa=` parameter of `run_resolver_body` become dead and are removed.
* Three resolver error messages disappear. `docs/air-gap-runbook.md:470-473` documents all three in its troubleshooting table; two rows are deleted and one (`malformed bucketfs source`) is rewritten for the new grammar.
* Provenance recorded before this change keeps the old-form `source_url`. Those strings are no longer resolvable, which is correct — they name an indirection that may since have been re-pointed. No data migration; the rows stay as the historical record they are.
* `sql/lua/control_sugar_v1.lua` needs NO code change. `DEFAULT_MODULE_SOURCE` is an `https:` URL, and the `FROM` token is carried through as an opaque quoted span, so extra `/` separators cannot affect parsing. Only its comment at line 603 and the spec/test examples move.
* The `studies/2026-07-persona-docs/*` write-ups are dated research records and are NOT rewritten. `FINDINGS.md:56-57` and `persona-1-airgap-dba.md:72` recorded the dual meaning as a defect; a short dated "superseded by this change" note is added so a future reader sees the finding was acted on, and nothing else is touched.

## Features

| Feature | Status | Spec |
|---------|--------|------|
| `module/db-resolver` | CHANGED | `specs/_plans/change-bucketfs-source-to-location/module/db-resolver/spec.md` |
| `module/db-install` | CHANGED | `specs/_plans/change-bucketfs-source-to-location/module/db-install/spec.md` |
| `module/db-catalog` | CHANGED | `specs/_plans/change-bucketfs-source-to-location/module/db-catalog/spec.md` |
| `control/module-sugar` | CHANGED | `specs/_plans/change-bucketfs-source-to-location/control/module-sugar/spec.md` |
| `install/schema-layout` | CHANGED | `specs/_plans/change-bucketfs-source-to-location/install/schema-layout/spec.md` |

No new features, none removed. No delta for `module/cli`, `module/contract`, `module/registry`, `module/lifecycle`, or `module/db-catalog`'s inventory scenarios: the CLI reads a local `--registry` path and never parses a `bucketfs:` source, the module contract and registry index format are untouched, and the inventory/atomicity contracts are orthogonal to the source grammar.

## Requirements

| Requirement | Details |
|-------------|---------|
| R1 | A `bucketfs:` source MUST be parsed as `bucketfs:<service>/<bucket>/<release-path>`, where the release path is one or more segments. |
| R2 | The resolver MUST NOT call `exa.get_connection` on the BucketFS path, and MUST NOT read any CONNECTION address. |
| R3 | A `bucketfs:` source with fewer than three non-empty segments MUST be a `bad_source` error naming the expected shape. No CONNECTION fallback, no heuristic. |
| R4 | A `bucketfs:` source containing an empty, `.`, or `..` segment MUST be a `bad_source` error, and no path outside `/buckets/<service>/<bucket>` MUST be opened. |
| R5 | The tarball / auto-extracted-directory probe pair MUST be preserved exactly, derived from the source-supplied mount base. |
| R6 | A failed probe MUST report exactly one `source_unreadable` error whose message names the tarball path, the auto-extracted directory path, AND the `CONNECTION TO 'bucketfs:<service>/<bucket>' IDENTIFIED BY '<read-password>'` grant prerequisite with service and bucket interpolated from the source. |
| R7 | No second error kind and no bucket-existence probe MUST be introduced. |
| R8 | The `https:` reader, `sha256` verification, inventory verification, chunking, and the emit-row shape MUST be unchanged. |
| R9 | `MODULE_INSTALL`, `MODULE_CATALOG`, and the sugar MUST pass the source through verbatim and MUST NOT parse or validate it. |
| R10 | The offline resolver suite MUST exercise the full `<service>/<bucket>/<path>` derivation via a rebindable mount-root constant, not by bypassing it. |
| R11 | The premise — a read-protected bucket is mounted for a caller granted a CONNECTION to it, with no CONNECTION named by the UDF — MUST be verified against a live read-protected bucket, with grant and revoke both observed, BEFORE the rewrite is trusted. |
| R12 | Every operator-facing source literal in this repo MUST use the location form, and every passage explaining the dual meaning of `bucketfs:` MUST be deleted and replaced with the single-meaning story. |
| R13 | Documentation MUST continue to instruct the operator to create and grant the CONNECTION; only its role changes, from a name the source carries to a credential grant. |

## Dependencies

A live Exasol instance with a **read-protected** BucketFS bucket the operator can create, upload to, grant, and revoke. See `MEMORY.md` → "Exasol Personal test env recipe" for re-deriving the live DB + BucketFS-write environment (the DB IP is dynamic and the container shell needs a PTY wrap). The publicly readable `default` bucket the suite normally uses is NOT sufficient for Task 0.1.

The protected bucket is created through **ConfD**, using this repo's own `examples/confd_admin.sql` add-on (Task 0.0): `CONTROL BUCKETFS ADD BUCKET '<bucket>' TO SERVICE '<svc>' PRIVATE WITH read_password = '…', write_password = '…'` → ConfD job `bucket_add`, where `PRIVATE` emits `"public":false`. This needs a `CONFD_CONNECTION` and the add-on installed (`examples/confd_admin.md` § Prerequisites). It also converts the premise check into a live exercise of a command currently marked ◻️ Documented-not-verified, so the marker can be promoted.

Sequenced internally: Task 0.0 (provision) → Task 0.1 (premise) gates everything. The resolver rewrite gates the test-harness rewrite, the live suites, and the docs.

## Migration

Every BucketFS source literal moves from `bucketfs:<CONNECTION>/<path>` to `bucketfs:<service>/<bucket>/<path>`. Twenty-four operator-facing literals across two repos, plus code, spec, and test sites.

### Code (primary repo)

| Site | Change |
|------|--------|
| `sql/udf/module_resolver_v1.sql:18-39` | Header `Sources` block: rewrite the `bucketfs:` entry for the location grammar; delete the `exa.get_connection` sentence at line 24. |
| `sql/udf/module_resolver_v1.sql:609-628` | `_bucketfs_reader`: new grammar parse, mount base from the source, no `exa` call, credential hint in the `source_unreadable` message. |
| `sql/udf/module_resolver_v1.sql:673-697` | `_mount_base`: deleted. |
| `sql/udf/module_resolver_v1.sql` (module scope) | Add `MOUNT_ROOT = '/buckets'` beside `_ARCHIVE_EXTENSIONS`. |
| `sql/lua/module_install.lua:25` | `source` parameter comment. |
| `sql/lua/module_install.lua:17` | "ACCESS on the BucketFS CONNECTION" — reword as the credential grant that mounts the bucket. |
| `sql/lua/module_install.lua:858` | `source_url` provenance comment. |
| `sql/lua/module_catalog.lua:16` | `bucketfs:CONN/path` → `bucketfs:<service>/<bucket>/<path>`. |
| `sql/lua/module_register.lua:41` | `bucketfs:` path comment. |
| `sql/lua/control_sugar_v1.lua:603` | Comment only; no code change. |
| `sql/install/22_modules_table.sql:26` | `source` column comment. |

Verify-only, expected to need no change: `src/preproc/bundle.py:239` (a note about GUI clients substituting on the literal colon in `'bucketfs:'` — the scheme token is unchanged); the whole `src/preproc/module/` CLI (reads a local `--registry` path, never a `bucketfs:` source).

### Tests (primary repo)

| Site | Change |
|------|--------|
| `tests/_dbfixtures.py:526-536` | `BucketFsConfig`: `bucketfs_address` stays (the CONNECTION address is unchanged); add `bucketfs_source(path)` returning `bucketfs:{service}/{bucket}/{path}` as the single choke point for the new form. |
| `tests/_dbfixtures.py:406-419` | `make_fake_exa`: delete (dead once the resolver stops calling `exa`). |
| `tests/_dbfixtures.py:428-456` | `run_resolver_body`: drop the `exa` parameter and the `namespace["exa"]` line; add `mount_root` injecting `namespace["MOUNT_ROOT"]`. |
| `tests/conftest.py:108` | Source built from `config.bucketfs_source(_RELEASE_BUCKET_PATH)`. The `CREATE CONNECTION` + `GRANT ACCESS` at 104-106 STAY — they are the credential prerequisite, and keeping them proves the source no longer references them. |
| `tests/test_db_resolver.py:127,205,253,438` | Source literals in three fixtures and `test_resolver_admin_only`. |
| `tests/test_db_resolver.py` | New: two-segment source → `bad_source`; `source_unreadable` message content. |
| `tests/test_db_module_install.py:407,1123` | `_stage` helper and `test_install_non_admin_rejected`. |
| `tests/test_db_module_catalog.py:174,225,441` | Two fixtures and `test_catalog_non_admin_rejected`. |
| `tests/test_module_resolver_udf.py:48-66` | `_stage_local`: build the tree at `tmp_path/<service>/<bucket>/`, return `(source, mount_root, built)`. |
| `tests/test_module_resolver_udf.py` (~35 sites) | `exa=exa` → `mount_root=mount_root` at every `run_resolver_body` call. |
| `tests/test_module_resolver_udf.py:313-316,348-350` | `test_missing_connection_is_signalled` and the `list_all` variant: retarget from a missing connection to an unmounted location. |
| `tests/test_command_sugar.py:801-928` | Source literals in four pass-through tests; behaviour unchanged. |

### Documentation (primary repo)

| Site | Change |
|------|--------|
| `docs/air-gap-runbook.md:229-235` | **Delete** the "Two meanings of `bucketfs:`" heading and table; replace with the single-meaning story. |
| `docs/air-gap-runbook.md:248,267,270,304,305,403,405,428,430` | Source literals, including the two verified `source_url` provenance outputs at 304-305. |
| `docs/air-gap-runbook.md:47,201,237,402,419` | Prose whose logic shifts: the CONNECTION is a credential grant, its name is never referenced, and a version bump changes only the source string's path. |
| `docs/air-gap-runbook.md:64,68` | The BucketFS service name is now needed for BOTH the CONNECTION address and the source string — strengthen. |
| `docs/air-gap-runbook.md:470-473` | Troubleshooting: delete the two CONNECTION-address rows, rewrite the `malformed bucketfs source` row, and add a row for the new `bad_source`. |
| `docs/operations.md:811-813` | **Delete** "…referenced by the `bucketfs:<CONNECTION_NAME>/<path>` source string described below." |
| `docs/operations.md:828,840,843,887,890` | Source literals. |
| `docs/operations.md:789-809` | Keep the CONNECTION prerequisite; reframe as a credential grant. |
| `docs/how-it-works.md:333-335` | "CONNECTION-mounted file" → the mount the grant makes visible; the resolver reads it by location. |
| `README.md:234` | `bucketfs:MY_BFS/own-lib-2.0.0.tar.gz` → location form. |
| `demo/03_module_from_bucketfs.sql:34-36,49-52,62,88` | Two source literals plus the "never hardcodes its name" framing. |
| `demo/07_confd_finale.sql:59,91` | Source literals. |
| `demo/library_demo.sql:63,67` | Source literals; keep the `CREATE CONNECTION` at 57-60. |
| `demo/library_demo.md:29` | Beat-3 narration. |
| `demo/framework_demo.sql:274-275` | Already carries the new form (uncommitted); add the missing trailing semicolons. |
| `studies/2026-07-persona-docs/FINDINGS.md`, `persona-1-airgap-dba.md` | Dated superseded-by note only; the historical record is NOT rewritten. |

Verify-only, expected to need no change: every `CREATE … CONNECTION … TO 'bucketfs:<service>/<bucket>' IDENTIFIED BY …` + `GRANT ACCESS` block (`docs/air-gap-runbook.md:204-208`, `docs/operations.md:797-799`, `demo/03_module_from_bucketfs.sql:43-47`, `demo/library_demo.sql:57-60`, `demo/README.md:102-105`) and `demo/99_teardown.sql:103`.

### Sibling repo — `/home/nc/Github/preprocessor-library` (OUT OF SCOPE)

The user updated `README.md` by hand on 2026-07-31, before implementation started. Verified: step 4 already reads `PREPROC CATALOG MODULES FROM 'bucketfs:bfsdefault/<bucket>/preproc-lib-<ver>.tar.gz'` (`README.md:171-172`), step 3 still creates and grants the CONNECTION (`README.md:164`), and the "two different meanings of `bucketfs:`" paragraph is gone. **Do not edit that repo.** Its README is, however, a useful cross-check on the wording this plan lands in the primary repo's docs — the two should tell the same single-meaning story.

No data migration. Existing `PREPROC.MODULES.source_url` values keep the old form as a historical record (see Consequences).

## Implementation Tasks

See `tasks.md`.

## Parallelization

| Parallel Group | Tasks |
|----------------|-------|
| Group 0 | ConfD bucket provisioning (0.0) → premise verification (0.1) — strictly sequential |
| Group A | Resolver UDF rewrite (1.1–1.3) |
| Group B | Offline test harness + offline resolver suite (2.1–2.3) |
| Group C | Live fixtures + DB-backed suites (3.1–3.4) |
| Group D | Lua/SQL/CLI comments (4.1) |
| Group E | Primary-repo docs and demos (5.1–5.5) |
| Group G | Full-suite + demo re-validation (7.1–7.2) |

Sequential dependencies:

- Group 0 → everything. If the premise is FALSE, STOP and re-plan; do not proceed to Group A.
- Group A → Group B, Group C (both consume the new grammar and the new error text)
- Group A → Group E (docs quote the error messages verbatim)
- Everything → Group G

Group D runs concurrently with anything after Group A.

## Dead Code Removal

| Type | Location | Reason |
|------|----------|--------|
| Function | `sql/udf/module_resolver_v1.sql:673-697` `_mount_base` | The resolver no longer reads a CONNECTION address; its absolute-`/buckets/` acceptance would now be a bucket-boundary bypass. |
| Call + error path | `sql/udf/module_resolver_v1.sql:614-617` `exa.get_connection` and the `connection %r is not accessible` error | No CONNECTION lookup on the BucketFS path. |
| Comment | `sql/udf/module_resolver_v1.sql:24` | The `exa.get_connection(name)` sentence in the header `Sources` block. |
| Function | `tests/_dbfixtures.py:406-419` `make_fake_exa` | Dead once the resolver body references `exa` nowhere. |
| Parameter | `tests/_dbfixtures.py:432` `run_resolver_body(exa=…)` and the `namespace["exa"]` assignment | Replaced by the `mount_root` seam. |
| Prose | `docs/air-gap-runbook.md:229-235` | The "Two meanings of `bucketfs:`" heading and table — the confusion this plan eliminates. |
| Prose | `docs/operations.md:811-813` | The canonical statement of the CONNECTION-named source contract. |
| Table rows | `docs/air-gap-runbook.md:472-473` | Troubleshooting rows for two errors that no longer exist. |

`BucketFsConfig.bucketfs_address` is NOT removed — the CONNECTION address is unchanged and the test still creates the CONNECTION.

## Verification

### Scenario Coverage

Resolver-grammar scenarios are covered offline where they are pure parsing over a faked mount (`tests/test_module_resolver_udf.py` execs the UDF body against a `tmp_path` tree — hermetic, no cluster), and by integration tests where the behaviour is a database or BucketFS fact. Every scenario has a test; the premise scenario is the one whose evidence is a live run against a read-protected bucket, recorded in the decision log.

| Scenario | Test Type | Test Location | Test Name |
|----------|-----------|---------------|-----------|
| Resolves a library-deployed module from a BucketFS-staged tarball (CHANGED) | Integration | `tests/test_db_resolver.py` | `test_resolver_reads_bucketfs_release` (update source form) |
| Resolves a library-deployed module from a BucketFS-staged tarball (CHANGED) | Unit | `tests/test_module_resolver_udf.py` | `test_resolves_extracted_release` / `test_resolves_tarball_release` (update to `mount_root`) |
| A BucketFS source is the location, never a CONNECTION name | Unit | `tests/test_module_resolver_udf.py` | `test_bucketfs_path_makes_no_connection_lookup` |
| A two-segment BucketFS source is malformed | Unit | `tests/test_module_resolver_udf.py` | `test_two_segment_source_is_bad_source` |
| A two-segment BucketFS source is malformed | Integration | `tests/test_db_resolver.py` | `test_resolver_rejects_legacy_connection_form` |
| A BucketFS source segment that escapes the bucket is malformed | Unit | `tests/test_module_resolver_udf.py` | `test_traversal_segment_is_bad_source` |
| An unreadable BucketFS location names both probed paths and the credential prerequisite | Unit | `tests/test_module_resolver_udf.py` | `test_unreadable_location_message_names_paths_and_grant` |
| An unreadable BucketFS location names both probed paths and the credential prerequisite | Integration | `tests/test_db_resolver.py` | `test_resolver_unreadable_location_reports_credential_hint` |
| A read-protected bucket is readable through a granted CONNECTION the source never names | Integration | `tests/test_db_resolver.py` | `test_read_protected_bucket_resolves_only_while_granted` (skips unless `EXASOL_BFS_PROTECTED_BUCKET` is configured) |
| Installs a library-deployed module from a BucketFS source, end-state byte-identical (CHANGED) | Integration | `tests/test_db_module_install.py` | `test_install_from_bucketfs_matches_cli_end_state` (update source form) |
| A malformed BucketFS source aborts before any database write | Integration | `tests/test_db_module_install.py` | `test_install_malformed_bucketfs_source_aborts_before_write` |
| An unreadable BucketFS location surfaces the credential prerequisite to the installer | Integration | `tests/test_db_module_install.py` | `test_install_unreadable_location_reports_credential_hint` |
| Catalogs a BucketFS source's modules with installed state (CHANGED) | Integration | `tests/test_db_module_catalog.py` | `test_catalog_lists_bucketfs_modules` (update source form) |
| A malformed BucketFS source is reported without a write | Integration | `tests/test_db_module_catalog.py` | `test_catalog_malformed_bucketfs_source_reports_without_write` |
| INSTALL MODULE command expands to a MODULE_INSTALL execute-script call (CHANGED) | Integration | `tests/test_command_sugar.py` | `test_install_module_expands_to_module_install` (update literals) |
| A FOR USER scope threads a USER-typed scope argument (CHANGED) | Integration | `tests/test_command_sugar.py` | `test_install_module_for_user_threads_user_scope` (update literals) |
| CATALOG MODULES command expands to a MODULE_CATALOG execute-script call (CHANGED) | Integration | `tests/test_command_sugar.py` | `test_catalog_modules_expands_to_module_catalog` (update literals) |
| The source token stays opaque however many path segments it carries | Integration | `tests/test_command_sugar.py` | `test_module_source_token_carried_verbatim_at_any_depth` |
| The BucketFS CONNECTION is a credential grant the framework never creates and never names | Integration | `tests/test_schema_layout.py` | `test_install_creates_no_bucketfs_connection` (extend: assert no framework artefact names a CONNECTION on the source path) |

Unchanged scenarios in all five features keep their existing tests; only the source literals inside their fixtures move.

### Manual Testing

| Feature | Command | Expected Output |
|---------|---------|-----------------|
| `module/db-resolver` (provisioning, Task 0.0) | `CONTROL BUCKETFS ADD BUCKET '<bucket>' TO SERVICE '<svc>' PRIVATE WITH read_password = '<read-pw>', write_password = '<write-pw>';` then `CONTROL BUCKETFS SHOW SERVICE '<svc>';` | The `bucket_add` job succeeds and the service listing shows `buckets.<bucket>.public \| false`. A `true` there means the bucket cannot falsify the premise — recreate it. |
| `module/db-resolver` (premise, Task 0.1) | Using the bucket from 0.0, upload `preproc-lib-<ver>.tar.gz`, `CREATE CONNECTION ANY_NAME TO 'bucketfs:<service>/<bucket>' IDENTIFIED BY '<read-pw>'`, `GRANT ACCESS … TO PREPROC_ADMIN`, then `SELECT PREPROC.MODULE_RESOLVER_V1('bucketfs:<service>/<bucket>/preproc-lib-<ver>.tar.gz','cast_shorthand')` | `status = OK`. Proves the bucket is mounted for the grantee with no CONNECTION named in the source. |
| `module/db-resolver` (premise, negative control) | `REVOKE ACCESS ON CONNECTION ANY_NAME FROM PREPROC_ADMIN;` then reconnect and re-run the same `SELECT` | `status = ERROR`, `error_kind = source_unreadable`. If it still returns OK the bucket is not actually read-protected — fix the bucket, not the test. |
| `module/db-resolver` (hard break) | `SELECT PREPROC.MODULE_RESOLVER_V1('bucketfs:PREPROC_BFS/preproc-lib-0.3.0.tar.gz','cast_shorthand')` | `error_kind = bad_source`, message naming `bucketfs:<service>/<bucket>/<path>`. No `get_connection` error, no hang. |
| `module/db-resolver` (message contract) | `SELECT PREPROC.MODULE_RESOLVER_V1('bucketfs:bfsdefault/default/no-such-release.tar.gz','cast_shorthand')` | `error_kind = source_unreadable` naming `/buckets/bfsdefault/default/no-such-release.tar.gz`, `/buckets/bfsdefault/default/no-such-release`, and the `CONNECTION TO 'bucketfs:bfsdefault/default' IDENTIFIED BY '<read-password>'` prerequisite. |
| `control/module-sugar` | `PREPROC CATALOG MODULES FROM 'bucketfs:bfsdefault/default/preproc-lib-0.3.0.tar.gz';` | Catalog rows for the staged release. The extra `/` does not disturb the sugar. |
| `module/db-install` | `PREPROC INSTALL MODULE cast_shorthand FROM 'bucketfs:bfsdefault/default/preproc-lib-0.3.0.tar.gz' FOR ROLE ANALYSTS;` | One row `installed`. `SELECT source_url FROM PREPROC.MODULES` shows the location form. |
| `module/db-install` (provenance) | Same install, then compare `source_url` to the runbook's documented output | Matches the rewritten `docs/air-gap-runbook.md:304-305` sample exactly. |
| docs sweep | `grep -rn "bucketfs:[A-Z_]*/" README.md docs/ demo/ specs/ sql/ src/ tests/` in the primary repo | No hits outside `specs/_recorded/**` and `studies/**`. |

### Checklist

| Step | Command | Expected |
|------|---------|----------|
| Environment | `uv sync` | Exit 0 |
| Test | `uv run pytest` | 0 failures |
| Test (offline only, no cluster) | `uv run pytest tests/test_module_resolver_udf.py` | 0 failures, no skips |
| Lint (Python) | `uv run ruff check .` | 0 errors |
| Format | `uv run ruff format --check .` | No changes |
| Lint (Lua) | `luacheck sql/lua/` | 0 warnings |
| Coverage | `uv run pytest --cov=preproc --cov-report=term-missing` | No regression |
| Demo | `demo/03_module_from_bucketfs.sql` and `demo/library_demo.sql` run clean from a fresh `release.sql` install | Catalog and install succeed on the location form |

Contract checks to confirm before calling the plan done:

* [ ] Task 0.1's live evidence — grant → OK, revoke → `source_unreadable` — is pasted into `decision-log.md` under Design Decision [1], with the date and instance.
* [ ] `grep -n "get_connection" sql/udf/module_resolver_v1.sql` returns nothing.
* [ ] `grep -n "exa\." sql/udf/module_resolver_v1.sql` returns nothing.
* [ ] `_mount_base` no longer exists.
* [ ] `MOUNT_ROOT` is defined once and read only by the BucketFS reader.
* [ ] A two-segment source yields `bad_source` and never touches the filesystem.
* [ ] The `source_unreadable` message carries all three elements: tarball path, extracted-directory path, grant prerequisite.
* [ ] Exactly one `source_unreadable` kind; no bucket-existence probe was added.
* [ ] `make_fake_exa` is gone and no test imports it.
* [ ] The offline suite runs with no cluster and exercises the `<service>/<bucket>` derivation (not an absolute-path bypass).
* [ ] `tests/conftest.py` still creates and grants the CONNECTION, and the source string does not name it.
* [ ] The sugar's `.lua` body is unchanged apart from a comment.
* [ ] The dual-meaning passage in `docs/air-gap-runbook.md` is gone (the sibling README's was already removed by the user).
* [ ] The primary repo's docs still instruct the operator to create and grant the CONNECTION, and tell the same single-meaning story as the already-updated sibling README.
* [ ] `studies/**` carries a dated superseded note and is otherwise untouched.
