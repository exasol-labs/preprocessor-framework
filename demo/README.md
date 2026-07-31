# Demos

Two recording scripts and a long-form walkthrough. Every statement in every file
here has been executed end to end against Exasol **2026.1**.

## The two videos

| | Script | Narration | Length |
|---|---|---|---|
| **1 — The framework** | [`framework_demo.sql`](framework_demo.sql) | [`framework_demo.md`](framework_demo.md) | ~9 min, 4 tabs + one non-DbVisualizer client |
| **2 — The library** | [`library_demo.sql`](library_demo.sql) | [`library_demo.md`](library_demo.md) | ~8 min, 1 tab |

**Demo 1** is how the framework works and how you use it: install, activate, the
config → resolution → dispatch model, writing your own preprocessor, and the core
beat — **three users in three tabs, each able to run a command the others cannot**.
Then **client-scoped rules**: a `HELP` command that exists in DbVisualizer and
nowhere else. Then the refresh contract and fail-open. It closes by installing one
module from the library and handing off to demo 2.

**Demo 2** is the library, in two halves. Consuming: browse the catalog, install
over HTTPS, install from BucketFS on an air-gapped cluster, retarget, and the
retire/reclaim lifecycle. Contributing: package the `SHOW TOP OBJECTS`
preprocessor written in demo 1 into a real module and get it validated — the
worked example is in [`library_demo/top_objects/`](library_demo/top_objects/) and
passes every gate the library's CI applies.

Watch them in order; demo 2 assumes demo 1 was watched, not run.

> **ConfD is deliberately out of both videos.** It is a large enough topic to need
> its own. The material is still here as reference:
> [`08_guarded_module_schema.sql`](08_guarded_module_schema.sql) (re-homing all 15
> `confd_control` objects into a guarded schema, delegating the surface to a
> non-DBA role), and [`examples/confd_admin.md`](../examples/confd_admin.md) for
> the `CONTROL DATABASE` / `CONTROL BUCKETFS` command reference and its setup.

## Long-form walkthrough

For someone evaluating the framework hands-on rather than watching. Same material,
more depth, more explanation per statement.

| File | Shows | Needs |
|---|---|---|
| [`01_administration.sql`](01_administration.sql) | The whole control surface as SQL. Activation, rules, status, modes, and a live fail-open proof. | — |
| [`02_module_from_github.sql`](02_module_from_github.sql) | Browse and install a published module over HTTPS. Provenance. | HTTPS egress |
| [`03_module_from_bucketfs.sql`](03_module_from_bucketfs.sql) | The same flow from a BucketFS-staged release. Air-gapped is not a degraded mode. | staged tarball + CONNECTION |
| [`04_users_roles_refresh.sql`](04_users_roles_refresh.sql) | Targeting transformations at roles and users, and the refresh contract. | 2 sessions |
| [`05_own_preprocessor.sql`](05_own_preprocessor.sql) | Write your own preprocessor in ~20 lines. No manifest, no registry, no toolchain. | — |
| [`06_secure_schemas.sql`](06_secure_schemas.sql) | Preprocessors outside `PREPROC_RT`, and three independent privilege layers. | 3 sessions |
| [`08_guarded_module_schema.sql`](08_guarded_module_schema.sql) | All 15 `confd_control` objects in a guarded schema, delegated to a non-DBA role. | `CONFD_CONNECTION` + a `sed` step |
| [`99_teardown.sql`](99_teardown.sql) | Removes everything, back to a clean framework install. | — |

## Assumed starting point

- An **empty Exasol database** with the framework deployed — `release.sql` from the
  [latest
  Release](https://github.com/exasol-labs/preprocessor-framework/releases/latest/download/release.sql),
  or `uv run preproc install`.
- Connected as `SYS`, or a user holding `DBA` or `PREPROC_ADMIN`.
- The preprocessor **not** activated. Every file activates it for the session only;
  nothing here touches the system-wide setting.

**Rule ids are hardcoded** in a few places (`PREPROC DROP RULE 7` in demo 1,
`DROP RULE 4` in demo 2) and are only correct on a clean install. Recreate the
container between takes.

Only [`06_secure_schemas.sql`](06_secure_schemas.sql) creates a schema and a table,
because privileges are its subject and you cannot `GRANT SELECT` on a subselect.
Everywhere else sample rows ride inline as `VALUES`-derived tables — nothing to set
up, nothing to clean up.

### SQL client

Both demos create Lua scripts, so your client must understand the EXAplus block
delimiter: a lone `--/` opens a script body, a lone `/` closes it.

> **GUI clients: turn OFF parameterized SQL.** DbVisualiser, DBeaver and other JDBC
> tools treat `:name` as a bind variable and will substitute on the colons *inside*
> Lua bodies, silently corrupting the deployed scripts. DbVisualiser: *SQL Commander
> → Parameterized SQL → off*. DBeaver: *Preferences → Editors → SQL Editor →
> uncheck "Use named parameters"*. EXAplus needs no change.

## Setup for the BucketFS beats

1. Get a library release tarball — from the
   [`preprocessor-library`](https://github.com/exasol-labs/preprocessor-library)
   releases, or build one from a checkout:

   ```bash
   cd preprocessor-library && python3 scripts/build_release.py
   ```

2. Upload it. BucketFS auto-extracts on arrival; you still reference it by the name
   you uploaded.

   ```bash
   curl -k -X PUT --user w:<write-password> \
        -T dist/preproc-lib-0.3.0.tar.gz \
        https://<host>:2581/default/preproc-lib-0.3.0.tar.gz
   ```

3. Fill in the read password in the `CREATE CONNECTION PREPROC_BFS` statement. Use
   the native form (`bucketfs:bfsdefault/default`), not an `https://…:2581/…` URL —
   the native form is read off the cluster-local mount with no egress, which is the
   whole point.

### ConfD setup (file 08 only)

`confd_control` needs a ConfD user; the database's own `SYS` password will not work.
From a container/node shell:

```text
confd_client user_create username: confd_gateway userid: 600 password: <password> \
    login_enabled: True group: exausers encode_passwd: True
confd_client user_modify username: confd_gateway \
    additional_groups: '[exaadm, exadbadm, exastoradm, exabfsadm]'
```

Then fill in `CREATE CONNECTION CONFD_CONNECTION`. **Port** is 20003 on the first
active node normally; a Docker single-node image exposes it on **443** — check
`XMLRPCPort` in `/exa/etc/EXAConf`.

## Throwaway Docker instance

Everything here was verified this way.

```bash
docker run -d --name preproc-demo --privileged \
    -p 127.0.0.1:9562:8563 -p 127.0.0.1:2582:2581 \
    exasol/docker-db:latest-2026.1
```

BucketFS credentials for the auto-created `default` bucket are base64-encoded in the
container config:

```bash
docker exec preproc-demo sh -c "grep -A4 'Bucket : default' /exa/etc/EXAConf"
# then: echo -n '<ReadPasswd>' | base64 -d
```

ConfD's XML-RPC port on this image is **443**, and the UDF must reach the node by its
`PrivateNet` IP (e.g. `172.17.0.3`), not `127.0.0.1` — it connects from inside the
cluster.

## Known rough edges

- **Never put a trailing `--` comment on the same line as a `PREPROC …` command.**
  Most clients send it as part of the statement, and while the command module strips
  *leading* comment lines it does not strip trailing ones — you get a puzzling
  `syntax error`. All comments in these files are on the line above.
  ([framework#8](https://github.com/exasol-labs/preprocessor-framework/issues/8))
- **`confd_control` strips no comments at all**, so a `CONTROL …` statement with a
  comment block glued in front of it is declined. Run the `CONTROL …` line on its
  own. ([confd-via-sql#2](https://github.com/exasol-labs/confd-via-sql/issues/2))
- **Never drop a script while an enabled rule still points at it.** A *throwing*
  module fails open cleanly; a *missing* one breaks the framework's admin surface
  (`PREPROC REFRESH` and friends fail with `attempt to call a nil value (field
  'rebuild')`) for every user whose pipeline includes that rule. Ordinary user SQL
  is unaffected. Recovery: `ALTER SESSION SET sql_preprocessor_script = '';`, drop
  the dangling rule, re-activate. Drop rules before scripts and you never see it.
  ([framework#7](https://github.com/exasol-labs/preprocessor-framework/issues/7))
- **Only one `CONTROL …` read per statement.** Two in one statement fails with
  `syntax error, unexpected CONTROL_`.
- `cast_shorthand` v1 does not rewrite a **string-literal** operand
  (`'42'::DECIMAL` returns unchanged, failing closed). Demo with a column or a
  numeric operand.

## Reference

- [`../README.md`](../README.md) — what the framework is
- [`../docs/operations.md`](../docs/operations.md) — install, control surface,
  refresh, modes, module management
- [`../docs/how-it-works.md`](../docs/how-it-works.md) — dispatch architecture,
  phase semantics, profile lifecycle
- [`../docs/module-authoring.md`](../docs/module-authoring.md) — the module contract
- [`../examples/`](../examples/) — single-file example modules
