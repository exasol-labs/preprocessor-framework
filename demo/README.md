# Demos

A long-form, runnable walkthrough of the framework. Every statement in every file
here has been executed end to end against Exasol **2026.1**.

## Walkthrough

For someone evaluating the framework hands-on. Run the files in order; each one
explains what it shows statement by statement.

| File | Shows | Needs |
|---|---|---|
| [`01_administration.sql`](01_administration.sql) | The whole control surface as SQL. Activation, rules, status, modes, and a live fail-open proof. | — |
| [`02_module_from_github.sql`](02_module_from_github.sql) | Browse and install a published module over HTTPS. Provenance. | HTTPS egress |
| [`03_module_from_bucketfs.sql`](03_module_from_bucketfs.sql) | The same flow from a BucketFS-staged release. Air-gapped is not a degraded mode. | staged tarball + CONNECTION |
| [`04_users_roles_refresh.sql`](04_users_roles_refresh.sql) | Targeting transformations at roles and users, and the refresh contract. | 2 sessions |
| [`05_own_preprocessor.sql`](05_own_preprocessor.sql) | Write your own preprocessor in ~20 lines. No manifest, no registry, no toolchain. | — |
| [`06_secure_schemas.sql`](06_secure_schemas.sql) | Preprocessors outside `PREPROC_RT`, and three independent privilege layers. | 3 sessions |
| [`99_teardown.sql`](99_teardown.sql) | Removes everything, back to a clean framework install. | — |

## Worked module example

[`top_objects/`](top_objects/) packages a hand-written `SHOW TOP [n] OBJECTS`
preprocessor, like the one in file 05, as a complete library module: `module.toml`
manifest, versioned artifact, and tests. It passes every gate the
[`preprocessor-library`](https://github.com/exasol-labs/preprocessor-library) CI
applies.

## Assumed starting point

- An **empty Exasol database** with the framework deployed — `release.sql` from the
  [latest
  Release](https://github.com/exasol-labs/preprocessor-framework/releases/latest/download/release.sql),
  or `uv run preproc install`.
- Connected as `SYS`, or a user holding `DBA` or `PREPROC_ADMIN`.
- The preprocessor **not** activated. Every file activates it for the session only;
  nothing here touches the system-wide setting.

**Rule ids are hardcoded** in a few places (`PREPROC DROP RULE 3` in file 01) and
are only correct on a clean install. Recreate the container between runs.

Only [`06_secure_schemas.sql`](06_secure_schemas.sql) creates a schema and a table,
because privileges are its subject and you cannot `GRANT SELECT` on a subselect.
Everywhere else sample rows ride inline as `VALUES`-derived tables — nothing to set
up, nothing to clean up.

### SQL client

The walkthrough creates Lua scripts, so your client must understand the EXAplus block
delimiter: a lone `--/` opens a script body, a lone `/` closes it.

> **GUI clients: turn OFF parameterized SQL.** DbVisualiser, DBeaver and other JDBC
> tools treat `:name` as a bind variable and will substitute on the colons *inside*
> Lua bodies, silently corrupting the deployed scripts. DbVisualiser: *SQL Commander
> → Parameterized SQL → off*. DBeaver: *Preferences → Editors → SQL Editor →
> uncheck "Use named parameters"*. EXAplus needs no change.

## Setup for file 03 (BucketFS)

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

## Known rough edges

- **Never put a trailing `--` comment on the same line as a `PREPROC …` command.**
  Most clients send it as part of the statement, and while the command module strips
  *leading* comment lines it does not strip trailing ones — you get a puzzling
  `syntax error`. All comments in these files are on the line above.
  ([framework#8](https://github.com/exasol-labs/preprocessor-framework/issues/8))
- **Never drop a script while an enabled rule still points at it.** A *throwing*
  module fails open cleanly; a *missing* one breaks the framework's admin surface
  (`PREPROC REFRESH` and friends fail with `attempt to call a nil value (field
  'rebuild')`) for every user whose pipeline includes that rule. Ordinary user SQL
  is unaffected. Recovery: `ALTER SESSION SET sql_preprocessor_script = '';`, drop
  the dangling rule, re-activate. Drop rules before scripts and you never see it.
  ([framework#7](https://github.com/exasol-labs/preprocessor-framework/issues/7))
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
