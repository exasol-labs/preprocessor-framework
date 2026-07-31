# Decision Log: change-bucketfs-source-to-location

Date: 2026-07-31

> **Note for the recorder:** decisions [1]–[4] are the promoting ones, intended
> as **ADR-064 through ADR-067**, continuing from ADR-063 in
> `specs/decision-log.md`. Nothing has been written into the permanent decision
> log during planning — promote at record time. Decisions [5]–[7] are local
> design and scope choices and do not promote.
>
> **Decision [1] carries an unverified premise.** Task 0.1 gates the whole plan
> on confirming it live. The implementer MUST paste the observed evidence into
> decision [1] before the entry is promoted, and MUST stop and re-open the plan
> if the premise is falsified.

## Interview

**Q:** What is the change, in one sentence?
**A:** A BucketFS module source must stop embedding an Exasol CONNECTION object name as its first path segment. Today it is `bucketfs:PREPROC_BFS/preproc-lib-0.3.0.tar.gz`, where the resolver calls `exa.get_connection('PREPROC_BFS')`, reads `connection.address` (which must itself be `bucketfs:<service>/<bucket>`), and maps that to the UDF mount. The source must instead be the plain BucketFS LOCATION `bucketfs:<service>/<bucket>/<path>`, e.g. `bucketfs:bfsdefault/default/preproc-lib-0.3.0.tar.gz`. The connection object must not be referenced in the URL at all.

**Q:** If the CONNECTION is not named, how do credentials reach the read?
**A:** The database resolves them automatically. The operator still creates a CONNECTION — the name is irrelevant — `TO 'bucketfs:<service>/<bucket>' IDENTIFIED BY '<read-password>'` and is granted it; Exasol then mounts that read-protected bucket into the UDF container, and the resolver simply reads `/buckets/<service>/<bucket>/<path>` off the mount with no `exa.get_connection` call whatsoever.

**Q:** Which surfaces does this apply to?
**A:** All of them, identically: `PREPROC CATALOG MODULES FROM '…'`, `PREPROC INSTALL MODULE … FROM '…'`, `PREPROC.MODULE_CATALOG(...)`, `PREPROC.MODULE_INSTALL(...)`, and `PREPROC.MODULE_RESOLVER_V1(...)`.

**Q:** The old `bucketfs:<CONNECTION>/<path>` and new `bucketfs:<service>/<bucket>/<path>` forms are grammatically ambiguous — both are `bucketfs:X/…`. How should the old form be treated?
**A:** **Hard break — location only.** The first segment is ALWAYS the BucketFS service, the second ALWAYS the bucket, the remainder the release path. A two-segment source such as `bucketfs:PREPROC_BFS/preproc-lib-0.3.0.tar.gz` is now malformed and MUST fail with `bad_source` naming the expected `bucketfs:<service>/<bucket>/<path>` shape. No heuristics, no connection fallback, no `exa.get_connection` in the BucketFS path at all. This is a deliberate breaking change — the project is pre-1.0 at 0.7.1 — and should be recorded as such in the ADR/decision log.

**Q:** When the mount path is absent — usually because no granted CONNECTION carries the bucket's read password, so Exasol never mounts the bucket — what should the resolver report?
**A:** **One error with a credential hint.** Keep the single `source_unreadable` error kind: the resolver cannot distinguish "no such bucket" from "bucket not granted to this caller". But the message MUST name both the tarball path and the auto-extracted directory path it looked for AND state that a read-protected bucket is only mounted for a caller granted a `CONNECTION TO 'bucketfs:<service>/<bucket>' IDENTIFIED BY '<read-password>'`. Roughly:

```
source_unreadable: no BucketFS release at
  /buckets/bfsdefault/default/preproc-lib-0.3.0.tar.gz (tarball) nor
  /buckets/bfsdefault/default/preproc-lib-0.3.0 (auto-extracted).
  A read-protected bucket is only mounted for a caller granted a
  CONNECTION TO 'bucketfs:bfsdefault/default' IDENTIFIED BY '<read-password>'.
```

Do NOT add a separate bucket-existence probe or a second error kind.

**Q:** Should the design's dependence on Exasol's mount behaviour be settled during planning?
**A:** No. Treat as the accepted premise the fact that a read-protected BucketFS bucket is made visible on the UDF container mount `/buckets/<service>/<bucket>` for a session whose user has been granted a CONNECTION object pointing at that bucket with the correct read password, and that the UDF does not have to reference the connection by name. Note it in the decision log as the assumption the design depends on, and add a spec scenario or test note that verifies it against the live instance during implementation rather than trying to settle it now.

**Q:** Does the documentation sweep extend beyond this repo?
**A:** Originally yes — the sibling module library at `/home/nc/Github/preprocessor-library` documented the old syntax in its `README.md`, including a closing paragraph explaining "the two different meanings of `bucketfs:`", and the plan carried a Group F to fix it. **Superseded 2026-07-31:** the user updated that README by hand before implementation started and stated no further changes are needed there. Verified — `README.md:171-172` already carries the location form and the dual-meaning paragraph is gone. Group F was removed from the plan; the sibling repo is out of scope and MUST NOT be edited. See decision [9].

**Q:** How is the read-protected bucket that Task 0.1 needs created?
**A:** (User, 2026-07-31, mid-planning) Via ConfD — create a new bucket that is not publicly readable, rather than relying on whatever buckets the instance already has. See decision [8].

**Q:** What about `specs/_recorded/**` and the untracked `studies/**` notes?
**A:** `specs/_recorded/**` is archived and MUST NOT be edited. `studies/2026-07-persona-docs/*` are dated study notes — check whether they should be updated or left as a dated snapshot. (Planner resolution: left as a dated snapshot with a superseded note; see decision [7].)

**Q:** (Planner-raised, during discovery) The existing integration suite stages into the BucketFS `default` bucket. Is that adequate evidence for the premise?
**A:** No — and this is the most consequential thing discovery turned up. `tests/conftest.py:83-85` states that "BucketFS reads are public but writes need `EXASOL_BFS_WRITE_PASSWORD`". The `default` bucket is publicly readable, so it is mounted regardless of any grant, and every integration test in this plan would pass whether or not the premise holds. The premise therefore needs a deliberately read-protected bucket and a grant/revoke pair, run as a gating task rather than treated as covered by a green suite. See decisions [1] and [6].

## Design Decisions

### [1] A BucketFS source is a location; the CONNECTION becomes a credential grant that is never named

- **Decision:** A `bucketfs:` source is `bucketfs:<service>/<bucket>/<release-path>`. The resolver derives the UDF mount `/buckets/<service>/<bucket>` from the source's own first two segments and performs NO `exa.get_connection` call and NO CONNECTION-address interpretation on the BucketFS path. The operator still creates a CONNECTION `TO 'bucketfs:<service>/<bucket>' IDENTIFIED BY '<read-password>'` and is granted it for a read-protected bucket; that grant is what makes Exasol mount the bucket into the UDF container. `_mount_base` is deleted.
- **Premise this rests on (UNVERIFIED at planning time):** Exasol mounts a read-protected bucket at `/buckets/<service>/<bucket>` for any session whose user holds a granted CONNECTION pointing at that bucket with the correct read password, without the UDF naming the CONNECTION. Asserted by the user; accepted for planning. **Task 0.1 verifies it live against a genuinely read-protected bucket, observing BOTH grant → readable and revoke → not readable. Evidence to be pasted here before promotion. If falsified, this decision is void and the plan re-opens.** — **RESOLVED 2026-07-31: PREMISE HELD.** See the Task 0.1 verification bullet below for the evidence.
- **Task 0.0 provisioning evidence (2026-07-31):** The read-protected bucket Task 0.1 needs was created live via this repo's own ConfD add-on, against Exasol Personal instance `18.195.244.225:8563` (`SELECT PARAM_VALUE FROM SYS.EXA_METADATA WHERE PARAM_NAME='databaseProductVersion'` → `2026.1.0`).
  - Installed `examples/confd_admin.sql` whole (split on the four `/`-only delimiter lines, executed via `pyexasol`), then confirmed `PREPROC_RT.CONFD_JOB_V1` (UDF), `PREPROC_RT.CONTROL_DATABASE_V1`, `PREPROC_RT.CONTROL_BUCKETFS_V1`, and `PREPROC.ADMIN_CONFD_JOB` all exist via `SYS.EXA_ALL_SCRIPTS`.
  - Connected as `SYS`, so per § Prerequisites item 4 registered `USER`-scoped rules instead of relying on the shipped `ROLE DBA` ones: `EXECUTE SCRIPT PREPROC.RULE_ADD('USER','SYS','EXPAND','PREPROC_RT.CONTROL_DATABASE_V1','expand', NULL, NULL)` and the `CONTROL_BUCKETFS_V1` equivalent, then `ALTER SESSION SET sql_preprocessor_script = PREPROC_RT.MASTER` in the same session.
  - Confirmed the gateway end-to-end with the read-only commands first: `CONTROL BUCKETFS SHOW SERVICES` → `bfsdefault`; `CONTROL BUCKETFS SHOW SERVICE 'bfsdefault'` → pre-existing buckets `default` (`public = true`) and `test` (`public = false`).
  - Created the bucket with the **sugar form** (not the raw `ADMIN_CONFD_JOB` fallback):
    ```sql
    CONTROL BUCKETFS ADD BUCKET 'preproc-protected' TO SERVICE 'bfsdefault' PRIVATE
        WITH read_password = '<read-pw>', write_password = '<write-pw>';
    ```
    Result: `result_code = 0`, `result_desc = 'Success'`. The read and write passwords were chosen by the implementer (not decoded from any existing bucket) and are redacted here as `<read-pw>` / `<write-pw>`; the live values are held outside the repo. Nothing in the evidence below depends on their literal text.
  - **Confirmed read-protection** via `CONTROL BUCKETFS SHOW SERVICE 'bfsdefault'` afterward:
    ```
    ('buckets.preproc-protected.name', 'preproc-protected')
    ('buckets.preproc-protected.public', 'false')
    ```
  - **Independently corroborated over HTTP** (unauthenticated `curl -k`, self-signed cert): `GET https://18.195.244.225:2581/preproc-protected/` → `HTTP/1.1 403 Forbidden`; `GET https://18.195.244.225:2581/default/` → `HTTP/1.1 200 OK`. Confirms the new bucket is read-protected while the pre-existing `default` bucket stays public-read, exactly as the premise for Task 0.1 requires.
  - `examples/confd_admin.md:125` (`bucket_add`) flipped ◻️ → ✅ with a dated note recording this run, since the sugar worked end-to-end with no fallback needed. `bucket_delete` was NOT exercised (no teardown performed) and stays ◻️.
- **Task 0.1 premise verification (2026-07-31): PREMISE HELD.** Both required observations were made live against Exasol Personal instance `18.195.244.225:8563`, product version `2026.1.0` (`SELECT PARAM_VALUE FROM SYS.EXA_METADATA WHERE PARAM_NAME='databaseProductVersion'` → `2026.1.0`), against the read-protected bucket `bfsdefault/preproc-protected` provisioned by Task 0.0. All SQL was executed through `pyexasol` (never a GUI client).

  **The identity that produced the evidence — and why it could not be `SYS`.** The negative control is **not observable as `SYS`**: `SYS` sees a read-protected bucket mounted even when the CONNECTION to it is *granted to nobody*. Verified directly (control A below, including a 2026-07-31 correction — the mount does require the CONNECTION object to exist; `SYS` bypasses only the grant). The whole verification was therefore run as a dedicated non-superuser, `T01_PROBE_USER`, created with only `CREATE SESSION`, `CREATE SCHEMA`, and `CREATE SCRIPT`:

  ```sql
  CREATE USER T01_PROBE_USER IDENTIFIED BY "Task01probe123";
  GRANT CREATE SESSION TO T01_PROBE_USER;
  GRANT CREATE SCHEMA  TO T01_PROBE_USER;
  GRANT CREATE SCRIPT  TO T01_PROBE_USER;
  ```

  The probe script was created **by `T01_PROBE_USER` in its own schema** (`T01_USER`), so script owner and session user are the same identity and neither can be the source of a false positive.

  **The release staged into the protected bucket.** Built with this repo's own `tests/_dbfixtures.build_library_tarball(..., standard_library_specs())` (modules `cast_shorthand`, `trailing_comma`) and `PUT` over HTTPS to port 2581 with `Authorization: Basic base64("w:<write-pw>")`. Read-protection re-confirmed on the staged object itself:

  ```
  built preproc-lib-0.1.0.tar.gz: 822 bytes  sha256=fa1ff4c89f8427fa5790a4cec1bf284a5a7bfc3d39ea78afdbc1c6847662b12e
  PUT https://<host>:2581/preproc-protected/preproc-lib-0.1.0.tar.gz -> HTTP 200
  GET (no auth)          -> HTTPError: HTTP Error 403: Forbidden
  GET (r:wrong-password) -> HTTPError: HTTP Error 403: Forbidden
  GET (r:<read-pw>)    -> HTTP 200 (822 bytes) sha256=fa1ff4c89f8427fa5790a4cec1bf284a5a7bfc3d39ea78afdbc1c6847662b12e
  round-trip identical: True
  ```

  **The probe UDF — the primary evidence, and the code shape the rewritten resolver will have.** It contains **no `exa` reference whatsoever** (verified against the deployed `SYS.EXA_ALL_SCRIPTS.SCRIPT_TEXT`: `get_connection` occurrences = 0; the only two `exa` substrings are inside the literal strings `'0_no_exa_call'` and `'this script never references exa'`):

  ```sql
  CREATE OR REPLACE PYTHON3 SCALAR SCRIPT T01_USER.PROBE(
      p_service VARCHAR(200), p_bucket VARCHAR(200), p_release VARCHAR(400))
  RETURNS VARCHAR(2000000) AS
  import hashlib
  import json
  import os

  MOUNT_ROOT = '/buckets'


  def _listing(path):
      try:
          return sorted(os.listdir(path))
      except Exception as exc:
          return '%s: %s' % (type(exc).__name__, exc)


  def _read(path):
      try:
          with open(path, 'rb') as handle:
              blob = handle.read()
          return 'OK %d bytes sha256=%s' % (len(blob), hashlib.sha256(blob).hexdigest())
      except Exception as exc:
          return '%s: %s' % (type(exc).__name__, exc)


  def run(ctx):
      service_dir = MOUNT_ROOT + '/' + ctx.p_service
      bucket_dir = service_dir + '/' + ctx.p_bucket
      tarball = bucket_dir + '/' + ctx.p_release
      extracted = tarball
      for ext in ('.tar.gz', '.tgz', '.tar'):
          if extracted.endswith(ext):
              extracted = extracted[:-len(ext)]
              break

      report = {}
      report['0_no_exa_call'] = 'this script never references exa'
      report['1_mount_root'] = MOUNT_ROOT
      report['1_mount_root_isdir'] = os.path.isdir(MOUNT_ROOT)
      report['1_mount_root_listing'] = _listing(MOUNT_ROOT)
      report['2_service_dir'] = service_dir
      report['2_service_dir_isdir'] = os.path.isdir(service_dir)
      report['2_service_dir_listing'] = _listing(service_dir)
      report['3_bucket_dir'] = bucket_dir
      report['3_bucket_dir_isdir'] = os.path.isdir(bucket_dir)
      report['3_bucket_dir_listing'] = _listing(bucket_dir)
      report['4_tarball_path'] = tarball
      report['4_tarball_isfile'] = os.path.isfile(tarball)
      report['4_tarball_read'] = _read(tarball)
      report['5_extracted_path'] = extracted
      report['5_extracted_isdir'] = os.path.isdir(extracted)
      report['5_extracted_listing'] = _listing(extracted)
      report['5_extracted_index_read'] = _read(os.path.join(extracted, 'registry', 'index.json'))
      try:
          with open('/proc/mounts') as handle:
              report['6_proc_mounts_bucket_lines'] = [
                  line.strip() for line in handle if 'bucket' in line]
      except Exception as exc:
          report['6_proc_mounts_bucket_lines'] = '%s: %s' % (type(exc).__name__, exc)
      return json.dumps(report, indent=2, sort_keys=True)
  ```

  **The CONNECTION — named `ANY_NAME`, deliberately unrelated to the source string:**

  ```sql
  CREATE CONNECTION ANY_NAME TO 'bucketfs:bfsdefault/preproc-protected' IDENTIFIED BY '<read-pw>';
  GRANT ACCESS ON CONNECTION ANY_NAME TO T01_PROBE_USER;
  ```

  **OBSERVATION 1 — granted → readable.** Fresh session as `T01_PROBE_USER`, own script, `ACCESS` on `ANY_NAME` held. Verbatim:

  ```
  # NEW session 1872222981618991104  CURRENT_USER=T01_PROBE_USER  script=T01_USER.PROBE (owner=T01_PROBE_USER)
  SQL> SELECT T01_USER.PROBE('bfsdefault', 'preproc-protected', 'preproc-lib-0.1.0.tar.gz') FROM DUAL
  {
    "0_no_exa_call": "this script never references exa",
    "1_mount_root": "/buckets",
    "1_mount_root_isdir": true,
    "1_mount_root_listing": [
      "bfsdefault",
      "zmqvmcontainer_conn_12868858582561849312"
    ],
    "2_service_dir": "/buckets/bfsdefault",
    "2_service_dir_isdir": true,
    "2_service_dir_listing": [
      "default",
      "preproc-protected"
    ],
    "3_bucket_dir": "/buckets/bfsdefault/preproc-protected",
    "3_bucket_dir_isdir": true,
    "3_bucket_dir_listing": [
      "preproc-lib-0.1.0"
    ],
    "4_tarball_isfile": false,
    "4_tarball_path": "/buckets/bfsdefault/preproc-protected/preproc-lib-0.1.0.tar.gz",
    "4_tarball_read": "FileNotFoundError: [Errno 2] No such file or directory: '/buckets/bfsdefault/preproc-protected/preproc-lib-0.1.0.tar.gz'",
    "5_extracted_index_read": "OK 1607 bytes sha256=a3ebcb6d1b5c510f2ab74936406d71e6cfd791a14fe6777b18fa7daa2c3eefaa",
    "5_extracted_isdir": true,
    "5_extracted_listing": [
      "modules",
      "registry"
    ],
    "5_extracted_path": "/buckets/bfsdefault/preproc-protected/preproc-lib-0.1.0",
    "6_proc_mounts_bucket_lines": [
      "none /buckets tmpfs rw,nosuid,nodev,relatime,uid=1000,gid=1000,inode64 0 0",
      "/dev/root /buckets/bfsdefault/default ext4 ro,nosuid,nodev,relatime,discard,errors=remount-ro 0 0",
      "/dev/root /buckets/bfsdefault/preproc-protected ext4 ro,nosuid,nodev,relatime,discard,errors=remount-ro 0 0"
    ]
  }
  ```

  **OBSERVATION 2 — revoked → NOT readable.** `REVOKE ACCESS ON CONNECTION ANY_NAME FROM T01_PROBE_USER`, then a **new** session, same script, same arguments. Verbatim:

  ```
  SQL> REVOKE ACCESS ON CONNECTION ANY_NAME FROM T01_PROBE_USER
  # NEW session 1872223013083807744  CURRENT_USER=T01_PROBE_USER  script=T01_USER.PROBE (owner=T01_PROBE_USER)
  SQL> SELECT T01_USER.PROBE('bfsdefault', 'preproc-protected', 'preproc-lib-0.1.0.tar.gz') FROM DUAL
  {
    "0_no_exa_call": "this script never references exa",
    "1_mount_root": "/buckets",
    "1_mount_root_isdir": true,
    "1_mount_root_listing": [
      "bfsdefault",
      "zmqvmcontainer_conn_12867764256637677059"
    ],
    "2_service_dir": "/buckets/bfsdefault",
    "2_service_dir_isdir": true,
    "2_service_dir_listing": [
      "default"
    ],
    "3_bucket_dir": "/buckets/bfsdefault/preproc-protected",
    "3_bucket_dir_isdir": false,
    "3_bucket_dir_listing": "FileNotFoundError: [Errno 2] No such file or directory: '/buckets/bfsdefault/preproc-protected'",
    "4_tarball_isfile": false,
    "4_tarball_path": "/buckets/bfsdefault/preproc-protected/preproc-lib-0.1.0.tar.gz",
    "4_tarball_read": "FileNotFoundError: [Errno 2] No such file or directory: '/buckets/bfsdefault/preproc-protected/preproc-lib-0.1.0.tar.gz'",
    "5_extracted_index_read": "FileNotFoundError: [Errno 2] No such file or directory: '/buckets/bfsdefault/preproc-protected/preproc-lib-0.1.0/registry/index.json'",
    "5_extracted_isdir": false,
    "5_extracted_listing": "FileNotFoundError: [Errno 2] No such file or directory: '/buckets/bfsdefault/preproc-protected/preproc-lib-0.1.0'",
    "5_extracted_path": "/buckets/bfsdefault/preproc-protected/preproc-lib-0.1.0",
    "6_proc_mounts_bucket_lines": [
      "none /buckets tmpfs rw,nosuid,nodev,relatime,uid=1000,gid=1000,inode64 0 0",
      "/dev/root /buckets/bfsdefault/default ext4 ro,nosuid,nodev,relatime,discard,errors=remount-ro 0 0"
    ]
  }
  ```

  `/proc/mounts` makes the mechanism explicit and unambiguous: `/buckets` is a `tmpfs`, and each accessible bucket is an individual read-only bind mount. The grant adds `/buckets/bfsdefault/preproc-protected`; the revoke removes it. This is a mount-level fact, not a permission-bit fact — which is why the unreadable case surfaces as `FileNotFoundError`, never `PermissionError`.

  **Supplementary controls** (condensed one-line verdicts as printed; `in_proc_mounts` = whether `preproc-protected` appears in `/proc/mounts`):

  ```
  A  SYS, SYS-owned script, NO grant to anyone   -> bucket_isdir=True  in_proc_mounts=True   index_read=OK 1607 bytes
  B  T01_PROBE_USER (no grant), SYS-OWNED script -> bucket_isdir=False in_proc_mounts=False  index_read=FileNotFoundError
  C1 session opened WHILE GRANTED                -> bucket_isdir=True  in_proc_mounts=True   index_read=OK 1607 bytes
  C2 SAME session, after REVOKE                  -> bucket_isdir=False in_proc_mounts=False  index_read=FileNotFoundError
  C3 NEW  session, after REVOKE                  -> bucket_isdir=False in_proc_mounts=False  index_read=FileNotFoundError
  D1 grant held, CONNECTION has WRONG password   -> bucket_isdir=False in_proc_mounts=False  index_read=FileNotFoundError
  E1 correct password, granted via a ROLE        -> bucket_isdir=True  in_proc_mounts=True   index_read=OK 1607 bytes
  E2 role's ACCESS revoked                       -> bucket_isdir=False in_proc_mounts=False  index_read=FileNotFoundError
  F1 session opened while REVOKED                -> bucket_isdir=False in_proc_mounts=False  index_read=FileNotFoundError
  F2 SAME session, after GRANT                   -> bucket_isdir=True  in_proc_mounts=True   index_read=OK 1607 bytes
  ```

  What each control establishes:
  - **A — `SYS` cannot produce this evidence.** `SYS` read the protected bucket with **no GRANT to any identity**. An "it is readable" result obtained as `SYS` is unfalsifiable and worthless here. Any future test of this behaviour MUST run as a non-superuser.

    **Correction, 2026-07-31 (Task 6.2).** As first written this bullet said `SYS` read the bucket with "no CONNECTION to it in existence at all". That overstates `SYS`'s reach and is wrong. Re-probed directly while fixing Task 3.4's test: as `SYS`, with **no** CONNECTION object addressing the bucket, `/buckets/bfsdefault/preproc-protected` is `[Errno 2] No such file or directory` for 90s continuously; once a CONNECTION carrying the correct read password exists — **granted to nobody** — the mount appears and lists content. So the mount is conditioned on the CONNECTION *object existing*, and what `SYS` bypasses is only the *grant*. The premise (R11) is unaffected: a read-protected bucket is still mounted only for a caller granted a CONNECTION to it, and the negative control below is still valid, because the revoke removed a grant while the CONNECTION continued to exist. The practical consequence is for test authors: a staging step that uploads and then polls for the mount **must create the CONNECTION first**, or it waits forever. Anyone re-deriving this evidence from the original wording would wrongly conclude the platform had changed.
  - **B — mount visibility follows the SESSION USER, not the script owner.** A `SYS`-owned script invoked by an ungranted non-superuser saw nothing. This is the security property the design needs: `PREPROC.MODULE_RESOLVER_V1`, though owned by a privileged identity, cannot hand an ungranted caller a bucket the caller was not granted. Removing `exa.get_connection` does not widen access.
  - **C/F — visibility is re-evaluated per statement, in BOTH directions, with no reconnect.** A revoke took effect inside an already-open session (C2) and a grant took effect inside an already-open session (F2). Task 3.4 therefore does **not** need to reconnect between its granted and revoked assertions.
  - **D — a wrong read password behaves exactly like no grant: the bucket is simply never mounted.** There is no "mounted but unreadable" state, so `source_unreadable` legitimately covers wrong-password as well as not-granted. This is why the Task 1.2 hint must say *the correct read password*, not merely *a CONNECTION*.
  - **E — a role-mediated grant works identically**, matching the framework's existing `GRANT ACCESS ... TO PREPROC_ADMIN` pattern in `tests/conftest.py`.

  **Resolver-level corroboration (supplementary, disclosed).** To confirm the premise holds for the exact *code shape* Group A will produce — not merely for a bare `open()` — a **locally patched** copy of the resolver body (the Task 1.1 location-grammar `_bucketfs_reader`, `MOUNT_ROOT = '/buckets'`, no `exa`) was deployed as `T01_USER.MODULE_RESOLVER_PROBE`, owned by and called as `T01_PROBE_USER`. The patch was applied to an **in-memory** copy of `sql/udf/module_resolver_v1.sql` — that file was NOT edited, and `PREPROC.MODULE_RESOLVER_V1` on the instance was NOT touched (re-verified afterwards: its `SCRIPT_TEXT` still contains `get_connection`, i.e. still the legacy body Group A will rewrite). The probe script was dropped immediately after the run. Verbatim:

  ```
  # patched body: 'exa.' occurrences = 0, 'get_connection' = 0 (both must be 0)
  # source under test: bucketfs:bfsdefault/preproc-protected/preproc-lib-0.1.0.tar.gz
  # CONNECTION ANY_NAME address is 'bucketfs:bfsdefault/preproc-protected' -- the name ANY_NAME appears NOWHERE in the source string

  SQL(SYS)> GRANT ACCESS ON CONNECTION ANY_NAME TO T01_PROBE_USER
  R1 GRANTED  session 1872223176302723072:
     {'status': 'OK', 'error_kind': None, 'error_message': None, 'name': 'cast_shorthand', 'version': 1, 'sha256': '78d223f736a2073abaa819c54586c408a151451383df109bfcd17bbd4a274d9d'}

  SQL(SYS)> REVOKE ACCESS ON CONNECTION ANY_NAME FROM T01_PROBE_USER
  R2 REVOKED  session 1872223177045377024:
     {'status': 'ERROR', 'error_kind': 'source_unreadable', 'error_message': "no BucketFS release at /buckets/bfsdefault/preproc-protected/preproc-lib-0.1.0.tar.gz (tarball) nor /buckets/bfsdefault/preproc-protected/preproc-lib-0.1.0 (auto-extracted). A read-protected bucket is only mounted for a caller granted a CONNECTION TO 'bucketfs:bfsdefault/preproc-protected' IDENTIFIED BY '<read-password>'.", 'name': None, 'version': None, 'sha256': None}

  R3 legacy two-segment form (the old bucketfs:<CONNECTION>/<path>), same session:
     {'status': 'ERROR', 'error_kind': 'bad_source', 'error_message': "malformed bucketfs source (expected bucketfs:<service>/<bucket>/<path>): 'bucketfs:ANY_NAME/preproc-lib-0.1.0.tar.gz'", 'name': None, 'version': None, 'sha256': None}
  ```

  This resolves the full chain — location parse → mount derivation → auto-extracted-directory reader → `registry/index.json` → artifact read → `sha256` verify — with zero `exa` involvement, and shows decisions [2] and [3] producing their intended messages verbatim.

  **Findings that constrain later tasks:**
  - **Task 1.2:** the unreadable case is `FileNotFoundError: [Errno 2] No such file or directory` on the *bucket directory itself*, and `os.path.isdir('/buckets/<service>')` is **`True`** even when the bucket is unmounted (the service directory is always visible and lists only the caller's accessible buckets). A service-level existence probe would therefore distinguish nothing useful — confirming decision [3]'s rejection of it on live evidence rather than reasoning alone. The one-kind-plus-credential-hint message is correct, and the hint must mention the *correct read password* (control D).
  - **BucketFS auto-extracted the `.tar.gz`.** On the mount the tarball **file does not exist**; only the extension-less directory `preproc-lib-0.1.0/` does (`4_tarball_isfile: false`, `5_extracted_isdir: true`), even though the HTTPS bucket listing still shows `preproc-lib-0.1.0.tar.gz`. The resolver's directory branch is the live path for a `.tar.gz` release; the tarball branch must be retained for buckets that do not auto-extract but will not be the one exercised here.
  - **Task 3.4 constraints:** (a) the grant/revoke test MUST run as a non-superuser — as `SYS` it cannot fail, per control A; (b) no reconnect is needed between grant and revoke assertions, per C2/F2; (c) `SYS.EXA_DBA_CONNECTION_PRIVS`, `SYS.EXA_ROLE_CONNECTION_PRIVS`, and `SYS.EXA_USER_CONNECTION_PRIVS` are **all empty on this instance even while a grant is functionally live**, so the test must not assert grant state from those views — an `exa.get_connection` call inside a throwaway script was used here as the functional oracle instead (`ACCESS GRANTED: address='bucketfs:bfsdefault/preproc-protected'` while granted; `NO ACCESS: ImportError: F-UDF-CL-SL-PYTHON-1121: get_connection for connection name ANY_NAME failed: insufficient privileges for using connection ANY_NAME in script ACCESSCHK` after revoke).
  - **Left on the instance:** the bucket `bfsdefault/preproc-protected` and its `preproc-lib-0.1.0.tar.gz` (Tasks 3.1/3.4 consume them). Everything else created for this task — user `T01_PROBE_USER`, schemas `T01_USER` and `T01_SYS`, role `T01_BFS_READER`, connection `ANY_NAME`, and both probe scripts — was dropped and the absence re-verified.
- **Task 7.1/7.2 final-validation evidence (2026-07-31), same instance (`18.195.244.225:8563`, `2026.1.0`):**
  - A fresh `release.sql` (`uv run preproc bundle --release`) was deployed from scratch: `DROP SCHEMA PREPROC CASCADE`, `DROP SCHEMA PREPROC_RT CASCADE`, `DROP ROLE PREPROC_ADMIN`, then all 40 generated statements executed via `pyexasol` (never a GUI client), 40/40 `OK`. Immediately after, `SELECT function_name FROM PREPROC.CONFIG` returned exactly the two shipped seeds (`ROLE PREPROC_ADMIN` and `ROLE DBA`, both → `PREPROC_RT.CONTROL_SUGAR_V1.expand`) — the Task 0.0 collateral (the stray `CONTROL_DATABASE_V1`/`CONTROL_BUCKETFS_V1` EXPAND rules and the `DEMO_*` rows) was gone, confirming a fresh install is the fix `tests/test_command_sugar.py::test_client_clause_lands_in_v1_in_place` needed.
  - **Live-suite side effect worth recording:** running the full `uv run pytest` (480 passed, 43 skipped, 0 failed) against that same fresh install left `PREPROC.CONFIG` **completely empty** (0 rows, including the two shipped seeds) by the time the run finished — some test in the suite deletes rows it does not fully own, or a teardown over-matches. This is a live-suite hygiene finding, not a regression in this plan's resolver change, and is out of this task's edit scope (`tests/` is off-limits here); a second fresh `release.sql` install was run before the manual-testing probes below so they exercise a known-clean state rather than the suite's leftover.
  - **Hard-break probe** (`sql/udf/module_resolver_v1.sql` freshly deployed via the same `deploy_resolver_udf` helper the test suite uses; deployed `SCRIPT_TEXT` confirmed 0 occurrences of `get_connection`, 0 of `exa.`, 4 of `MOUNT_ROOT`): `SELECT PREPROC.MODULE_RESOLVER_V1('bucketfs:PREPROC_BFS/preproc-lib-0.3.0.tar.gz','cast_shorthand')` → `('ERROR', 'bad_source', "malformed bucketfs source (expected bucketfs:<service>/<bucket>/<path>): 'bucketfs:PREPROC_BFS/preproc-lib-0.3.0.tar.gz'", …)`. No `get_connection` error, no hang.
  - **Message-contract probe:** `SELECT PREPROC.MODULE_RESOLVER_V1('bucketfs:bfsdefault/default/no-such-release.tar.gz','cast_shorthand')` → `('ERROR', 'source_unreadable', "no BucketFS release at /buckets/bfsdefault/default/no-such-release.tar.gz (tarball) nor /buckets/bfsdefault/default/no-such-release (auto-extracted). A read-protected bucket is only mounted for a caller granted a CONNECTION TO 'bucketfs:bfsdefault/default' IDENTIFIED BY '<read-password>'.", …)`. All three required elements present.
  - **Sugar + provenance, live:** `PREPROC CATALOG MODULES FROM 'bucketfs:bfsdefault/default/preproc-lib-0.3.1.tar.gz'` listed `cast_shorthand` and `trailing_comma`; `PREPROC INSTALL MODULE cast_shorthand FROM '…' FOR ROLE ANALYSTS` → `installed`, and likewise for `trailing_comma`. `SELECT source_url FROM PREPROC_RT.INSTALLED_MODULES` returned the location form for both (`bucketfs:bfsdefault/default/preproc-lib-0.3.1.tar.gz`), fed into `docs/air-gap-runbook.md`'s provenance block, replacing the `TASK-7.2-PROVENANCE` placeholder.
  - **Version substitution, disclosed:** the plan's manual-testing table and the runbook's prior placeholder both name `preproc-lib-0.3.0.tar.gz`. The cluster's public `default` bucket (the only bucket with `EXASOL_BFS_WRITE_PASSWORD` unavailable in this run) had no `0.3.0` object staged, only a pre-existing `preproc-lib-0.3.1.tar.gz` (same `cast_shorthand`/`trailing_comma` sha256 as the documented `0.3.0` samples, i.e. an unchanged module pair between those two library releases). Every live probe and the demo runs below therefore used `0.3.1` verbatim rather than `0.3.0`; this is a version substitution, not a masked failure, and is called out at every site it touches.
  - **Demo runs:** `demo/03_module_from_bucketfs.sql` and `demo/library_demo.sql` beats 1–5 ran against this same fresh install (adjusting only the version literal as above); beat 6 (publishing a module) and the file-02 prerequisite this file assumes (`cast_shorthand` already installed via HTTPS) were not exercised — see the Task 7.2 report for the full per-step walk.
  - **Left on the instance after Task 7.2:** two installed modules (`cast_shorthand` v1, `trailing_comma` v2) and their `PREPROC.CONFIG` rules from the demo/probe runs, plus the two shipped sugar seeds. The `preproc-protected` bucket is untouched (still empty, as Task 6.2 left it). No user, role, or connection was created beyond what the demos themselves document (`PREPROC_BFS`, dropped at the end of the `03_module_from_bucketfs.sql` walk-through — see the report for whether teardown ran).
- **Alternatives:** Keep the CONNECTION-named form — rejected: it gives `bucketfs:` two meanings in the two places an operator types it, which the framework's own docs must stop and explain (`docs/air-gap-runbook.md:229-235`) and which a persona study recorded as a comprehension failure (`studies/2026-07-persona-docs/persona-1-airgap-dba.md:72`). Support both forms — rejected as impossible to do safely; see decision [2]. Introduce a distinct `bucketfsloc:` scheme for the new form — rejected: unambiguous, but it leaves two schemes to document forever and lets the worse form keep the better name.
- **Rationale:** The indirection recovers nothing the operator writing the source did not already know, and it costs three things. It creates the dual meaning above. It makes `PREPROC.MODULES.source_url` provenance depend on a mutable indirection — a CONNECTION can be dropped and recreated against a different bucket, silently re-targeting every historical row — which is exactly the class of hazard the framework's audit design exists to avoid. And it produces failure messages (`connection '<NAME>' is not accessible`, `unsupported BucketFS connection address 'https://…:2581/…'`) that describe the shape of an indirection rather than anything about the release the operator wanted.
- **Promotes to ADR:** yes

### [2] The old two-segment form is a hard break, not a compatibility case

- **Decision:** A `bucketfs:` source with fewer than three non-empty segments is a `bad_source` error naming the expected `bucketfs:<service>/<bucket>/<path>` shape. No heuristic probe, no CONNECTION fallback, no deprecation window. Segments that are empty, `.`, or `..` are likewise `bad_source`, so a source can never address a path outside its own bucket.
- **Alternatives:** Probe `/buckets/<seg1>` and fall back to a CONNECTION lookup when it is absent — rejected: it makes a source's meaning depend on database state, so a bucket coincidentally named like a CONNECTION resolves a different release than intended, silently. A deprecation window emitting a warning row — rejected: it requires keeping the ambiguous dual parse for the whole window, which is the thing being retired. Accept the old form as an alias — rejected for the same reason: the two forms are grammatically identical, so no parser can tell them apart.
- **Rationale:** Silent resolution of the wrong release is the worst outcome available on this path — the `sha256` check would pass, the inventory check would pass, and a legitimate-but-wrong module would install. A loud `bad_source` costs one edit per source string. The project is pre-1.0 at 0.7.1 with no external installed base, so the break is affordable exactly now and will not be later.
- **Promotes to ADR:** yes

### [3] One `source_unreadable` kind, carrying the credential prerequisite in its message

- **Decision:** When neither the tarball probe nor the auto-extracted-directory probe finds a release, the resolver reports exactly one `source_unreadable` error. Its message names the tarball path probed, the auto-extracted directory path probed, and states that a read-protected bucket is only mounted for a caller granted a `CONNECTION TO 'bucketfs:<service>/<bucket>' IDENTIFIED BY '<read-password>'`, with service and bucket interpolated from the source. No second error kind, no separate bucket-existence probe.
- **Alternatives:** A distinct `bucket_not_granted` kind — rejected: the resolver observes one thing, that the path is not there, and cannot tell an unmounted bucket from an absent file. Probe `os.path.isdir('/buckets/<service>')` first to distinguish them — rejected: it separates *some* cases but not the important one, because a mounted service holding an unmounted read-protected bucket is indistinguishable from a typo'd bucket name; the result is a second error kind that misattributes causes, which is worse than one honest kind.
- **Rationale:** The diagnostic value a second kind would have carried is exactly the credential hint, and the hint can be delivered without claiming to know which cause applies. Naming both probed paths also makes the auto-extract behaviour visible — the single most surprising thing about BucketFS on this path — in the one message the operator will actually read. This continues the framework's established preference for a loud, complete error over a confidently wrong classification.
- **Promotes to ADR:** yes

### [4] The offline resolver harness moves from a faked `exa` to a rebindable mount-root constant

- **Decision:** The cluster-local mount root is a module-level `MOUNT_ROOT = '/buckets'` constant in the UDF body. Production never varies it. `tests/_dbfixtures.run_resolver_body` gains a `mount_root` parameter that rebinds `namespace["MOUNT_ROOT"]` after the `exec`, replacing the `exa` parameter and `make_fake_exa`, both of which become dead once the resolver stops calling `exa`.
- **Alternatives:** Monkeypatch `os.path.isfile`/`isdir` — rejected: it couples every test to the reader's exact call sequence, so an internal refactor breaks tests that should not care about it. Fake the whole `os` module in the exec'd namespace — same objection, larger. Make the BucketFS tests live-only — rejected: it would push ~35 currently hermetic tests onto a cluster. Keep no seam and test only the parse — rejected: the parse and the mount derivation are the change.
- **Rationale:** The old `exa` seam had a defect worth not reproducing: because the fake CONNECTION address was an absolute filesystem path, the offline suite only ever exercised `_mount_base`'s `address.startswith('/')` branch, so the `bucketfs:<service>/<bucket>` → `/buckets/<service>/<bucket>` mapping had NO offline coverage at all. The mount-root constant reverses that — the harness supplies only the root, so the service/bucket/path derivation under test runs for real. One production line, no production behaviour change, and it mirrors the existing `namespace["exa"] = …` injection so the harness pattern is unchanged in shape.
- **Promotes to ADR:** yes

### [5] No layer but the resolver parses a source string

- **Decision:** `PREPROC.MODULE_INSTALL`, `PREPROC.MODULE_CATALOG`, and the `CONTROL_SUGAR_V1` module verbs pass the source through verbatim and neither parse, validate, nor rewrite it. `sql/lua/control_sugar_v1.lua` therefore needs no code change at all — only a comment — because `DEFAULT_MODULE_SOURCE` is an `https:` URL and the `FROM` token is an opaque single-quoted span the tokenizer keeps whole, so extra `/` separators cannot affect parsing.
- **Alternatives:** Validate the source shape in the sugar so a malformed one is declined fail-open before reaching the resolver — rejected: it would duplicate the grammar in a second language, and the sugar's fail-open contract means a decline sends the raw text to the engine for a generic syntax error, which is strictly less informative than the resolver's `bad_source` naming the expected shape.
- **Rationale:** One grammar, one implementation, one error site. This is the same anti-duplication position the framework took on the statement splitter (ADR-059's shared conformance corpus) and on the `client_pattern` validator (ADR-063).
- **Promotes to ADR:** no

### [6] Premise verification is a gating task against a read-protected bucket, not a green suite

- **Decision:** Task 0.1 blocks every other task. It requires a genuinely read-protected bucket, a grant → resolves observation AND a revoke → `source_unreadable` observation, and the evidence pasted into decision [1]. A spec scenario records the requirement, and the corresponding integration test skips unless a protected bucket is configured.
- **Alternatives:** Trust the assertion and let the integration suite stand as evidence — rejected on a discovery finding: `tests/conftest.py:83-85` documents that the `default` bucket the suite stages into is publicly readable, so it is mounted regardless of any grant and the suite would go green whether or not the premise holds. Treating that as confirmation would be a false positive on the single fact the design depends on.
- **Rationale:** The whole plan removes the mechanism (`exa.get_connection`) by which access is currently obtained, on the strength of a claim about how Exasol grants access without it. If the claim is wrong, the framework loses the ability to read a read-protected bucket at all — a regression that a public-bucket test suite cannot detect. Verification has to come first and has to use the case that can falsify it.
- **Promotes to ADR:** no

### [7] Dated study notes are annotated, not rewritten

- **Decision:** `studies/2026-07-persona-docs/FINDINGS.md` and `persona-1-airgap-dba.md` keep their text; a short dated "superseded by change-bucketfs-source-to-location" note is added where the dual meaning of `bucketfs:` is recorded as a finding. Existing `PREPROC.MODULES.source_url` rows likewise keep the old form, with no data migration.
- **Alternatives:** Rewrite the studies to the new syntax — rejected: they are dated records of what a persona encountered on a specific date, and rewriting them destroys the evidence that the confusion was real and was acted on. Backfill provenance rows to the location form — rejected: it would be a fabrication, since the old rows name an indirection whose target may since have changed and whose historical meaning cannot be recovered.
- **Rationale:** Same instinct in both cases — a historical record that is edited to match the present is no longer a record. The annotation carries the reader forward without erasing what was found. This matches the precedent set for `studies/2026-06-persona-ux/*` in the `add-client-scoped-rules` plan.
- **Promotes to ADR:** no

### [8] The read-protected test bucket is provisioned through ConfD, by this repo's own add-on

- **Decision:** (User, 2026-07-31) Task 0.0 creates a dedicated PRIVATE bucket via ConfD before Task 0.1 runs — `CONTROL BUCKETFS ADD BUCKET '<bucket>' TO SERVICE '<svc>' PRIVATE WITH read_password = '…', write_password = '…'`, the `bucket_add` job, through `examples/confd_admin.sql`. `PRIVATE` is what emits `"public":false` (`examples/confd_admin.sql:790-792`) and therefore what makes the bucket capable of falsifying the premise. The framework's own test suite CONSUMES that bucket through `EXASOL_BFS_PROTECTED_*` env vars and skips when they are unset; it does not create one, so ConfD and a `CONFD_CONNECTION` never become test dependencies.
- **Alternatives:** Reuse an existing non-default bucket if the instance happens to have one — rejected: its protection status would be incidental and unverified, and the premise check is worthless against a bucket that turns out to be public. Have the pytest fixture provision the bucket via ConfD on demand — rejected: it would make the framework's test suite depend on a ConfD endpoint, credentials, and an example add-on that is not part of the framework, to serve one skippable test.
- **Rationale:** The gate needs a bucket whose protection is a known fact, not an assumption, and ConfD is the supported way to state that fact at creation time. A side benefit: `bucket_add` is currently marked ◻️ Documented-not-verified in `examples/confd_admin.md:125` precisely because it mutates cluster state and was never run; this task is that run, so the marker can be promoted to ✅ with a date.
- **Promotes to ADR:** no

### [9] The sibling library README is out of scope

- **Decision:** (User, 2026-07-31) Group F is removed. `/home/nc/Github/preprocessor-library/README.md` was updated by hand by the user before implementation started and needs no further change; implementation MUST NOT edit that repo.
- **Alternatives:** Keep Group F as a verify-only task — rejected: the verification was done once at planning time (location form present at `README.md:171-172`, dual-meaning paragraph absent) and re-checking a repo outside this plan's control adds a failure mode without adding safety.
- **Rationale:** The sibling README still matters as a wording cross-check — the two repos should tell the same single-meaning story — but that is a review consideration for the primary repo's docs, not a task against someone else's working tree.
- **Promotes to ADR:** no

## Review Findings

<!-- Populated by speq-implement after code review. -->
