# Air-Gap Runbook

[← README](../README.md) | [Operations](operations.md) | [How it works](how-it-works.md)

**One ordered procedure for deploying the framework and installing modules on a
cluster with no outbound internet.** Everything here is also documented in
[operations.md](operations.md) alongside the online paths; this page exists so you
do not have to assemble the sequence from four documents.

**Verified end to end on Exasol 2026.1.0** (clean instance, framework 0.7.1,
library 0.3.0). Every command and every error message below is real output from
that run.

- **Nothing on the cluster side needs Python, `uv`, or any toolchain.** A SQL
  client is enough.
- **The database performs no network I/O.** It reads the library off its own
  cluster-local BucketFS mount.
- Total time, once you have the two files: **about 15 minutes.**

---

## 0. Which air-gap path?

There are two. This runbook covers the first, which is the one to pick unless you
have a reason not to.

| | **BucketFS staging** (this page) | **Offline bundle** |
|---|---|---|
| You stage | a release tarball in a bucket | a single generated `.sql` file |
| Cluster side needs | a SQL client | a SQL client |
| Connected side needs | nothing (download the tarball) | `uv` + the framework checkout |
| Installs are | repeatable — the cluster keeps installing from the bucket | one-shot per generated file |
| Provenance recorded | full (`source_url`, `library_version`, `sha256`, `objects`) | partial |
| Best for | a cluster that will install more than one module, ever | a single hand-carried module |

For the offline-bundle alternative, see
[operations.md § Online vs. air-gap flows](operations.md#online-vs-air-gap-flows).

## 1. Know your two credentials

The single most common source of lost time on this path. BucketFS has **two
separate passwords** and you need both, for different steps:

| Credential | Used for | Used by | Step |
|---|---|---|---|
| **Write** password | uploading the tarball into the bucket | `curl` / your BucketFS client, over HTTPS to port **2581** | [4](#4-stage-the-library-in-bucketfs) |
| **Read** password | the `CONNECTION` credential that makes the bucket's mount visible to the database | the database, via the **native** `bucketfs:` address — no host, no port | [5](#5-create-the-connection) |

They are not interchangeable. Putting the write password in the `CONNECTION`, or
the upload URL in its address, are the two failure modes worth memorising.

On a standard deployment, get them from `EXAConf` on a node (they are
base64-encoded):

```bash
grep -A4 'Bucket : default' /exa/etc/EXAConf
#     ReadPasswd  = RFdlMkczbjA1VGE4T3BicHJhYU82RmlPalBzQk1DNlk=
#     WritePasswd = dDUzYmlkYnJEc1RnMFRYTTdSN1FRSHZEeGdoQ0taUDc=

echo -n '<ReadPasswd>'  | base64 -d
echo -n '<WritePasswd>' | base64 -d
```

You also need the **BucketFS service name** — the section header in the same
file, usually `bfsdefault`. You will use it twice, in the identical
`bucketfs:<service>/<bucket>` form both times: once in the CONNECTION address
(step 5) and again as the first of the two `bucketfs:<service>/<bucket>/…`
segments in the module source string itself (step 6):

```bash
grep 'BucketFS :' /exa/etc/EXAConf        # → [BucketFS : bfsdefault]
```

## 2. Collect two files (connected side)

Both are plain downloads from GitHub Releases. Nothing needs to be built.

| File | From | What it is |
|---|---|---|
| `release.sql` | [framework releases](https://github.com/exasol-labs/preprocessor-framework/releases/latest/download/release.sql) | the entire framework as one SQL file |
| `preproc-lib-<ver>.tar.gz` | [library releases](https://github.com/exasol-labs/preprocessor-library/releases) | the module catalog + every artifact |

If your policy requires building the tarball yourself rather than trusting a
release asset, it needs only Python's standard library — no framework, no `uv`:

```bash
git clone https://github.com/exasol-labs/preprocessor-library
cd preprocessor-library
python3 scripts/build_release.py      # → dist/preproc-lib-<VERSION>.tar.gz
```

The build re-verifies every artifact's `sha256` against the index as it packs and
is **byte-reproducible** — two runs produce identical bytes, so an independent
rebuild is a valid integrity check.

**Record the digests now**, on the connected side, so you can prove on the other
side that what arrived is what you fetched:

```bash
sha256sum release.sql preproc-lib-0.3.0.tar.gz
```

## 3. Deploy the framework

Transfer both files across, then run `release.sql` in your SQL client as a DBA.

> ### ⚠️ Turn off parameterized SQL first
>
> `release.sql` contains Lua and Python bodies full of literal colons
> (`'bucketfs:'`, `def …:`). A JDBC GUI client with named-parameter substitution
> **on** will substitute on those colons and silently corrupt the deployed
> scripts. You will not see an error until later, and it will look like
> `object <NAME> not found` or `SyntaxError: expected ':'`.
>
> - **DbVisualiser:** *SQL Commander → Parameterized SQL → off*
> - **DBeaver:** *Preferences → Editors → SQL Editor → uncheck "Use named parameters"*
> - **EXAplus:** no change needed.
>
> If you have already run a corrupted deploy, fix the client setting and re-run
> `release.sql` on a clean database — it is clean-install only.

`release.sql` is 40 statements. Plain statements end with `;`; each script body is
wrapped between a lone `--/` line and a lone `/` line, because the bodies contain
semicolons. Confirm it landed:

```sql
SELECT SCHEMA_NAME FROM EXA_ALL_SCHEMAS WHERE SCHEMA_NAME LIKE 'PREPROC%';
-- PREPROC, PREPROC_RT

-- The control plane. Excludes the generated P_<hash> profile scripts, whose
-- number varies with how many distinct rule chains resolve — do not count those.
SELECT SCRIPT_SCHEMA, SCRIPT_NAME FROM EXA_ALL_SCRIPTS
WHERE  SCRIPT_SCHEMA LIKE 'PREPROC%'
  AND  SCRIPT_NAME NOT LIKE 'P\_%' ESCAPE '\'
ORDER BY 1, 2;
-- 20 rows on a clean install: PREPROC_RT.MASTER, PREPROC_RT.CONTROL_SUGAR_V1,
-- and the 18 PREPROC.* admin scripts (RULE_*, SET_*, REFRESH*, STATUS,
-- MODULE_*, and the MODULE_RESOLVER_V1 UDF).

SELECT VIEW_NAME FROM EXA_ALL_VIEWS WHERE VIEW_SCHEMA = 'PREPROC_RT';
-- MY_PIPELINE, INSTALLED_MODULES

SELECT rule_id, scope_type, scope_value, script_name FROM PREPROC.CONFIG ORDER BY rule_id;
-- 1  ROLE  PREPROC_ADMIN  PREPROC_RT.CONTROL_SUGAR_V1
-- 2  ROLE  DBA            PREPROC_RT.CONTROL_SUGAR_V1
```

`PREPROC.MODULE_RESOLVER_V1` is the Python UDF that will read your bucket in step
6. If it is missing, the deploy did not complete.

Those two seeded rules are the `PREPROC …` command surface. The `DBA`-scoped one
is why **`SYS` works out of the box** — Exasol will not let you grant
`PREPROC_ADMIN` to `SYS`, so the installer seeds `DBA` as well.

Activate for your own session first. This never touches the system-wide setting:

```sql
ALTER SESSION SET sql_preprocessor_script = PREPROC_RT.MASTER;
PREPROC STATUS;        -- CURRENT, plus the last refresh timestamp
```

> **Never put a trailing `--` comment on a `PREPROC …` line.** Most clients send
> it as part of the statement; the sugar strips only *leading* comment lines, so
> the command is declined and you get a raw `syntax error, unexpected
> IDENTIFIER_PART_`. Comments go on the line above.

System-wide activation (`ALTER SYSTEM SET sql_preprocessor_script = PREPROC_RT.MASTER;`)
is a separate, deliberate decision — leave it until after you have verified in
step 6.

## 4. Stage the library in BucketFS

Upload with the **write** password, over HTTPS, to the BucketFS write service
(port **2581**):

```bash
curl -k -X PUT --user w:<WRITE-PASSWORD> \
     -T preproc-lib-0.3.0.tar.gz \
     https://<bucketfs-host>:2581/<bucket>/preproc-lib-0.3.0.tar.gz
# → HTTP 200
```

Confirm it landed (read password, same service):

```bash
curl -k --user r:<READ-PASSWORD> https://<bucketfs-host>:2581/<bucket>/
# → preproc-lib-0.3.0.tar.gz
```

Two things to expect here:

- **BucketFS auto-extracts the archive on arrival**, presenting it as a directory
  on the UDF mount. You still reference it by the name you uploaded — the resolver
  finds either form. No extra step, no different syntax.
- **Allow a few seconds** before the next step. Extraction is not instantaneous;
  an install attempted immediately after the PUT can miss it.
- **You cannot inspect `/buckets/…` from a node shell.** That mount exists only
  inside the UDF container. Verify through `MODULE_CATALOG` in step 6, not with
  `ls`.

## 5. Create the CONNECTION

The framework never creates this — it is an operator responsibility, done once per
cluster. The name is arbitrary; `PREPROC_BFS` is only a convention, and — unlike
the old two-segment source form — it is never referenced by a module source
string. The CONNECTION's only job now is the `GRANT ACCESS` that makes the
bucket's mount readable to `PREPROC_ADMIN`.

```sql
CREATE OR REPLACE CONNECTION PREPROC_BFS
    TO 'bucketfs:bfsdefault/<bucket>'
    IDENTIFIED BY '<READ-PASSWORD>';

GRANT ACCESS ON CONNECTION PREPROC_BFS TO PREPROC_ADMIN;
```

> ### ⚠️ Use the native address — not the upload URL
>
> The address is `bucketfs:<service>/<bucket>` — **no host, no port, no
> `https://`**. The database reads BucketFS off its cluster-local mount
> (`/buckets/<service>/<bucket>/…`), which is precisely what makes this path
> egress-free.
>
> The `https://<host>:2581/…` URL from step 4 is the **upload endpoint** and is
> *not* interchangeable. Get this wrong and there is no dedicated error for it:
> the bucket is simply never mounted for the granted role — indistinguishable
> from a missing grant or a wrong read password (see step 1) — and step 6 fails
> with `source_unreadable` (below).
>
> Note there is no `USER` clause — just `IDENTIFIED BY` with the **read**
> password.

**`bucketfs:` always names a location — a service and a bucket, nothing more.**
The CONNECTION address above and the module source string in step 6 use the
identical `bucketfs:<service>/<bucket>` grammar because both point at the same
mount; no SQL this framework consumes ever carries a CONNECTION name. What
gates *who* may install from this bucket is the `GRANT ACCESS ON CONNECTION`
above; the resolver then reads the mount directly by its
`bucketfs:<service>/<bucket>/<path>` location, never through the CONNECTION
object.

## 6. Catalog, then install

**Always catalog first.** It is read-only — it touches no config, no resolution,
no provenance — and it proves the resolver can actually read your bucket before
you attempt any write:

```sql
PREPROC CATALOG MODULES FROM 'bucketfs:bfsdefault/<bucket>/preproc-lib-0.3.0.tar.gz';
```

```
NAME            VERSION  PHASE      DEPLOY_MODE       INSTALLED  OBJECT_COUNT  OBJECTS
cast_shorthand  1        TRANSLATE  library-deployed  FALSE      1             LUA SCRIPT|PREPROC_RT.CAST_SHORTHAND_V1
trailing_comma  2        TRANSLATE  library-deployed  FALSE      1             LUA SCRIPT|PREPROC_RT.ERGONOMICS_TRAILING_COMMA_V2
```

`OBJECT_COUNT` and `OBJECTS` are your pre-install review: an install is DDL run
under `PREPROC_ADMIN`, and a composite module can create many objects across
several schemas. Read that inventory before you approve it.

Then install. One statement per module — the examples in step 7 assume you
installed both of these:

```sql
PREPROC INSTALL MODULE cast_shorthand
  FROM 'bucketfs:bfsdefault/<bucket>/preproc-lib-0.3.0.tar.gz' FOR ROLE ANALYSTS;

PREPROC INSTALL MODULE trailing_comma
  FROM 'bucketfs:bfsdefault/<bucket>/preproc-lib-0.3.0.tar.gz' FOR ROLE ANALYSTS;
```

```
module          VERSION  PHASE      STATUS     LIBRARY_VERSION
cast_shorthand  1        TRANSLATE  installed  0.3.0

module          VERSION  PHASE      STATUS     LIBRARY_VERSION
trailing_comma  2        TRANSLATE  installed  0.3.0
```

`STATUS` is `installed`, `already-current` (an identical version was already
there — a safe idempotent no-op), or `registered-only` (a `self-deployed`
module: rule and provenance written, no script created).

Scope targets **activation** — who receives the transform — not permission to
install. Installing is always the `PREPROC_ADMIN` boundary. Remember a user must
*hold* the role you name.

> `min_framework` in the index is **advisory — nothing enforces it.** A module
> requiring a newer framework than you run will install and report `installed`,
> then fail at use. Check each module's `min_framework` against your framework
> version yourself.

## 7. Verify — the evidence for your change record

**a. Provenance.** One row per installed module version, and the audit anchor for
what you just did:

```sql
SELECT name, version, status, library_version, source_url, sha256
FROM PREPROC_RT.INSTALLED_MODULES ORDER BY name;
```

```
cast_shorthand  1  deployed  0.3.1  bucketfs:bfsdefault/default/preproc-lib-0.3.1.tar.gz  1ff0c225ae0d…
trailing_comma  2  deployed  0.3.1  bucketfs:bfsdefault/default/preproc-lib-0.3.1.tar.gz  44f3bd0fb721…
```

Real output, captured 2026-07-31 against a single-node Exasol Personal instance
(2026.1.0) from a fresh `release.sql` install: `PREPROC INSTALL MODULE
cast_shorthand FROM 'bucketfs:bfsdefault/default/preproc-lib-0.3.1.tar.gz' FOR
ROLE ANALYSTS;` then the same for `trailing_comma`, against the release tarball
already staged in the cluster's public `default` bucket (0.3.1, not 0.3.0 — the
bucket had no 0.3.0 object and staging one was not possible in this run; see the
Task 7.2 decision-log entry). The full digests: `cast_shorthand` sha256
`1ff0c225ae0d0988283ab9f16de394af7546fe870ce9b6c9493ed9a531339ada`;
`trailing_comma` sha256
`44f3bd0fb721107530b7cbc81e7765b97196ceafbe64fd700583c6d6800a8354`.

`source_url` is where it came from, `library_version` is the concrete release the
index self-declared, `sha256` is the digest the database **verified before writing
anything**, and `objects` (not shown) is the full deployed inventory — your
rollback list.

**b. What will actually run, for a given user:**

```sql
SELECT * FROM PREPROC_RT.MY_PIPELINE;
```

Any user can run this, no admin rights needed. Each row is one transformation
that will apply to their statements, in order. **No rows means nothing applies to
them** — the normal answer for most users, and the first thing to check when a
module "isn't working" (usually: they don't hold the role).

**c. Prove a transform actually fires:**

```sql
SELECT 123::varchar(10) FROM dual;   -- → 123   (cast_shorthand rewrote it)
SELECT 1, 2, FROM dual;              -- → 1, 2  (trailing_comma rewrote it)
```

Only now consider `ALTER SYSTEM SET sql_preprocessor_script = PREPROC_RT.MASTER;`.

## 8. Rollback

Remove a module in the order the framework itself enforces — **rules before
scripts.** Verified sequence:

```sql
-- 1. find and drop the rule(s) naming the module's script
PREPROC LIST RULES;
EXECUTE SCRIPT PREPROC.RULE_DROP(<rule_id>, TRUE);   -- TRUE = defer the refresh
EXECUTE SCRIPT PREPROC.REFRESH;

-- 2. mark the version retired in provenance
EXECUTE SCRIPT PREPROC.MODULE_RETIRE('trailing_comma', 2);
--  → trailing_comma | 2 | retired | 2026-07-29 18:08:54.692

-- 3. later, reclaim: the guarded physical DROP of every object in its inventory
EXECUTE SCRIPT PREPROC.MODULE_RECLAIM('trailing_comma', 2);
--  → trailing_comma | 2 | dropped=TRUE | reclaimed=TRUE | reason=reclaimed
```

`MODULE_RECLAIM` returns `(name, version, dropped, reclaimed, reason)` and
**refuses rather than raising** when it is not safe — so a wrong call is harmless
and tells you why. Both guards, observed:

```sql
-- skipped step 2 (still deployed)
EXECUTE SCRIPT PREPROC.MODULE_RECLAIM('cast_shorthand', 1);
--  → cast_shorthand | 1 | FALSE | FALSE | not_retired

-- retired, but its activation rule is still enabled
EXECUTE SCRIPT PREPROC.MODULE_RECLAIM('cast_shorthand', 1);
--  → cast_shorthand | 1 | FALSE | FALSE | blocked_by_rule
```

Full set of reasons: `reclaimed`, `not_retired`, `blocked_by_rule`,
`blocked_by_profile` (a live profile manifest still references it), `not_found`,
`drop_failed`.

Reclaim is deliberately a **separate, later** call, never automatic: a long-lived
session could still be pinned to what you are dropping.

To deactivate the framework entirely:

```sql
ALTER SYSTEM SET sql_preprocessor_script = NULL;   -- system-wide
ALTER SESSION SET sql_preprocessor_script = '';    -- just your session
```

> **Never drop a script while an enabled rule still names it.** A module that
> *throws* fails open cleanly; a module that is *missing* does not — the failed
> `import` also breaks the one the admin scripts use, and every `PREPROC REFRESH`
> / `ADD RULE` / `INSTALL MODULE` starts failing with `attempt to call a nil value
> (field 'rebuild')` for affected users. Recovery is in
> [operations.md § Troubleshooting](operations.md#troubleshooting). Following the
> order above, you will never see it — which is exactly what `MODULE_RECLAIM`'s
> guards enforce.

## 9. Updating to a new library release

The bucket is a staging area, so an update is just another upload. Both releases
can coexist — keep the old tarball until you have cut over.

```bash
curl -k -X PUT --user w:<WRITE-PASSWORD> \
     -T preproc-lib-0.4.0.tar.gz \
     https://<host>:2581/<bucket>/preproc-lib-0.4.0.tar.gz
```

```sql
-- the CONNECTION and its grant are unchanged; only the path in the source
-- string moves, since the service and bucket are the same staging area
PREPROC CATALOG MODULES FROM 'bucketfs:bfsdefault/<bucket>/preproc-lib-0.4.0.tar.gz';
PREPROC INSTALL MODULE cast_shorthand
  FROM 'bucketfs:bfsdefault/<bucket>/preproc-lib-0.4.0.tar.gz' FOR ROLE ANALYSTS;
```

A module whose version increased deploys as `_V<N+1>` alongside the live `_V<N>`,
the rule repoints, and the old version becomes `retired` — reclaimable later. A
module whose version did not change reports `already-current` and does nothing.

Note the source string is **pinned to a filename**, so there is no moving-tag
ambiguity on this path: an air-gapped install is reproducible by construction.

## 10. Staging your own modules

Nothing here is specific to the public library. A registry is just a
`registry/index.json` plus the artifacts it points at, so your own internal
catalog stages identically — same CONNECTION, same `bucketfs:<service>/<bucket>`
grammar:

```
own-lib-2.0.0.tar.gz
├── registry/index.json               # your index, your own library_version
└── modules/my_guard/my_guard_v1.sql  # your artifact, at the path the index names
```

```sql
PREPROC CATALOG MODULES FROM 'bucketfs:bfsdefault/<bucket>/own-lib-2.0.0.tar.gz';
PREPROC INSTALL MODULE my_guard
  FROM 'bucketfs:bfsdefault/<bucket>/own-lib-2.0.0.tar.gz' FOR ROLE DBA;
```

Your index declares its own `library_version`, and installs record it in
provenance exactly as the public library's do. Verified end to end with a
customer-authored two-object module (a `TABLE` plus a `LUA SCRIPT`).

See [README § Bring your own preprocessor](../README.md#bring-your-own-preprocessor)
and [module-authoring.md](module-authoring.md) for the manifest and artifact
contract.

## 11. Prove you are actually air-gapped

Do not assume. A cluster believed to have no egress often does — and if it does,
an operator can bypass your staging process with a one-line `https:` source.

```sql
PREPROC CATALOG MODULES
  FROM 'https://raw.githubusercontent.com/exasol-labs/preprocessor-library/latest/registry/index.json';
```

- **A network error** → confirmed no egress. On a no-egress cluster an `https:`
  source fails and installs nothing; no partial deploy is left behind.
- **A module listing** → your cluster **has** internet, and
  `PREPROC INSTALL MODULE <name>` with no `FROM` clause will silently pull from
  the public library over HTTPS, resolving a **mutable `latest` tag**.

There is currently **no setting to forbid `https:` sources** — supplying one *is*
the opt-in. If your policy requires that installs only ever come from your
staged bucket, that is enforced today by the `PREPROC_ADMIN` grant boundary and by
audit (`source_url` in `INSTALLED_MODULES` records the effective source of every
install), not by a preventive control.

## Troubleshooting

Real messages from the verified run. All resolver failures **abort before any
write** — no script, no rule, no provenance row.

| Symptom | Cause | Fix |
|---|---|---|
| `[source_unreadable] no BucketFS release at /buckets/<service>/<bucket>/<path> (tarball) nor …/<path-sans-extension> (auto-extracted). A read-protected bucket is only mounted for a caller granted a CONNECTION TO 'bucketfs:<service>/<bucket>' IDENTIFIED BY '<read-password>'.` | Filename in the source string ≠ what you uploaded; extraction has not finished; or the CONNECTION grant is missing, revoked, or was created with the wrong **read** password | Check `curl … /<bucket>/`, match the name exactly, wait a few seconds; confirm `GRANT ACCESS ON CONNECTION … TO PREPROC_ADMIN` and the read password — step 5 |
| `[bad_source] malformed bucketfs source (expected bucketfs:<service>/<bucket>/<path>): 'bucketfs:PREPROC_BFS/preproc-lib-0.1.0.tar.gz'` | Source uses the retired two-segment `bucketfs:<CONNECTION>/<path>` form, or otherwise has fewer than three segments — there is no CONNECTION fallback | Use the three-segment location, e.g. `bucketfs:bfsdefault/<bucket>/preproc-lib-0.1.0.tar.gz` |
| `[bad_source] malformed bucketfs source: segment '..' is empty or a path traversal, and every segment of bucketfs:<service>/<bucket>/<path> must be a plain name: '…'` | An empty, `.`, or `..` segment in the source | Every segment must be a plain name — fix the source string, do not point outside the bucket |
| `[not_found] module '<name>' is not listed in the source index` | Typo, or the module is not in that release | `PREPROC CATALOG MODULES FROM …` to see what is there |
| `[sha256_mismatch] sha256 mismatch for module '<name>': index declares …` | The artifact does not match its declared digest — tampering or a corrupted transfer | Re-fetch the tarball; compare digests against the connected side. **Nothing was installed** |
| `syntax error, unexpected IDENTIFIER_PART_` on a `PREPROC …` command | One of: preprocessor not activated for this session; you hold neither `PREPROC_ADMIN` nor `DBA`; a trailing `--` comment on the line | Check in that order; `SELECT * FROM PREPROC_RT.MY_PIPELINE;` — empty means no rules resolve to you |
| `object <NAME> not found` / `SyntaxError: expected ':'` after deploying | The SQL client substituted named parameters inside script bodies | Disable parameterized SQL, re-run `release.sql` on a clean database — step 3 |
| Every admin command fails with `attempt to call a nil value (field 'rebuild')` | An enabled rule points at a script that no longer exists | `ALTER SESSION SET sql_preprocessor_script = '';` then drop the dangling rule — [operations.md § Troubleshooting](operations.md#troubleshooting) |
| Module installed, but nothing is transformed | The user does not hold the scoped role, or refresh has not run since a grant change | `SELECT * FROM PREPROC_RT.MY_PIPELINE;` as that user; `PREPROC REFRESH` |

One nuance worth knowing: **`MODULE_CATALOG` does not verify `sha256`.** A
tampered tarball still lists normally; only `MODULE_INSTALL` verifies and refuses.
A successful catalog proves the source is *readable*, not that it is *intact*.

## Reference

- [operations.md](operations.md) — full control surface, module management, CLI
- [how-it-works.md](how-it-works.md) — why a UDF reads and a Lua script writes; the air-gap capability split
- [module-authoring.md](module-authoring.md) — the module contract
- [demo/03_module_from_bucketfs.sql](../demo/03_module_from_bucketfs.sql) — this path as a narrated, runnable demo
