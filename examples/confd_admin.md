# `confd_admin` — `CONTROL DATABASE` / `CONTROL BUCKETFS` command reference

A real-world EXPAND add-on (`examples/confd_admin.sql`) that adds two SQL command
surfaces for administering Exasol via **ConfD**. Each recognised command is
rewritten by an EXPAND module into a single
`EXECUTE SCRIPT PREPROC.ADMIN_CONFD_JOB(<job>, <params>)` call, which invokes the
ConfD job over XML-RPC through a generic Python3 gateway UDF and returns the
result.

## What it deploys

| Object | Kind | Role |
|--------|------|------|
| `PREPROC_RT.CONFD_JOB_V1` | Python3 SCALAR UDF | Generic ConfD XML-RPC gateway |
| `PREPROC_RT.CONTROL_DATABASE_V1` | Lua EXPAND module | `CONTROL DATABASE …` grammar |
| `PREPROC_RT.CONTROL_BUCKETFS_V1` | Lua EXPAND module | `CONTROL BUCKETFS …` grammar |
| `PREPROC.ADMIN_CONFD_JOB` | Lua script (`EXECUTE SCRIPT` target) | Calls the UDF, raises on failure, formats the result |
| 2 EXPAND rules in `PREPROC.CONFIG` | — | Scope both grammars to `ROLE DBA` |

## Prerequisites

1. The framework is installed (`PREPROC` / `PREPROC_RT` schemas exist; run
   `release.sql` or `uv run preproc install`).
2. A `CONNECTION` named `CONFD_CONNECTION` points at the ConfD XML-RPC endpoint:
   ```sql
   CREATE CONNECTION CONFD_CONNECTION
       TO 'https://<host>:20003'
       USER '<confd_user>' IDENTIFIED BY '<password>';
   ```
3. Run `examples/confd_admin.sql` whole (the `/` on its own line is the required
   delimiter).
4. The operator **holds the `DBA` role** (`GRANT DBA TO <user>`). The implicit
   superuser `SYS` does **not** appear in the role-grant graph that
   `PREPROC.REFRESH` walks, so the shipped `ROLE DBA` rules will not resolve to
   `SYS`. To drive the commands as `SYS`, register a `USER`-scoped rule instead:
   ```sql
   EXECUTE SCRIPT PREPROC.RULE_ADD('USER','SYS','EXPAND','PREPROC_RT.CONTROL_DATABASE_V1','expand', NULL);
   EXECUTE SCRIPT PREPROC.RULE_ADD('USER','SYS','EXPAND','PREPROC_RT.CONTROL_BUCKETFS_V1','expand', NULL);
   ```
5. Activate the preprocessor for your session:
   ```sql
   ALTER SESSION SET sql_preprocessor_script = PREPROC_RT.MASTER;
   ```

## Output format

Every command returns the **same two columns**, one row per leaf value of the
ConfD job's `result_output`:

| Column | Type | Contents |
|--------|------|----------|
| `FIELD` | `VARCHAR(256)` | Dotted/indexed path into the result (e.g. `nodes.active[1]`, `config.version`) |
| `VALUE_TEXT` | `VARCHAR(2000000)` | The scalar value as text; `null`, or `(empty)` for empty objects/arrays |

```
FIELD                     | VALUE_TEXT
--------------------------+------------------
config.version            | 2025.2.0
name                      | Exasol
nodes.active[1]           | n11
state                     | running
```

Object keys are sorted for stable output; nested objects become dotted paths and
array elements get `[i]` suffixes. The column names are `FIELD` / `VALUE_TEXT`
deliberately — `RESULT`, `VALUE`, `ATTRIBUTE`, and `PATH` are reserved keywords in
Exasol and cannot be used as identifiers here.

### Errors

`ADMIN_CONFD_JOB` **raises** (it does not return a row) when:
- the gateway UDF fails (the `ERR:<message>` sentinel), or
- ConfD returns a non-zero `result_code` (the envelope's `result_desc` is shown).

A failed job therefore surfaces as a loud script error, never as an empty or
misleading result set.

## Command reference

**Legend** — Status as exercised against a live single-node deployment on
2026-06-10:
- ✅ **Verified** — run end-to-end through the preprocessor against real ConfD; returned data.
- ◻️ **Documented** — grammar implemented and the rewrite is the same code path as the verified commands, but **not executed here** because it mutates cluster/database state. Treat as untested on your deployment until you run it.

### `CONTROL DATABASE …`

The database name is never typed — jobs that need it carry a `"db_name":""`
marker and the UDF substitutes the current database.

| Command | ConfD job | Kind | Status |
|---------|-----------|------|--------|
| `CONTROL DATABASE SHOW INFO` | `db_info` | read | ✅ |
| `CONTROL DATABASE SHOW BACKUPS` | `db_backup_list` | read | ✅ |
| `CONTROL DATABASE SHOW BACKUP PROGRESS` | `db_backup_progress` | read | ✅ |
| `CONTROL DATABASE ABORT BACKUP` | `db_backup_abort` | mutating | ◻️ |
| `CONTROL DATABASE DELETE UNUSABLE BACKUPS` | `db_backup_delete_unusable` | mutating | ◻️ |
| `CONTROL DATABASE DELETE BACKUP '<id>' [, '<id>' …]` | `db_backups_delete` | mutating | ◻️ |
| `CONTROL DATABASE START BACKUP TO VOLUME <vol> [WITH LEVEL <n>] [AND EXPIRATION '<t>']` | `db_backup_start` | mutating | ◻️ |
| `CONTROL DATABASE ADD BACKUP SCHEDULE '<name>' TO VOLUME <vol> [WITH LEVEL <n>] [AT '<cron>'] [AND EXPIRATION '<t>'] [ENABLED\|DISABLED]` | `db_backup_add_schedule` | mutating | ◻️ |
| `CONTROL DATABASE MODIFY BACKUP SCHEDULE '<name>' [AT '<cron>'] [WITH EXPIRATION '<t>'] [ENABLED\|DISABLED]` | `db_backup_modify_schedule` | mutating | ◻️ |
| `CONTROL DATABASE MODIFY BACKUP EXPIRATION ON VOLUME <vol> FOR '<files>' TO '<YYYY-MM-DD hh:mm:ss>'` | `db_backup_change_expiration` | mutating | ◻️ |

Notes:
- Volume names are bare identifiers (`TO VOLUME <vol>`); string arguments
  (schedule names, backup ids, file prefixes, times, expirations, cron specs) are
  single-quoted.
- `LEVEL` defaults to `0` (full backup).
- `<cron>` is a 5-field crontab string `'min hour day month weekday'`
  (e.g. `'0 2 * * 0'` = 02:00 every Sunday).
- `db_backup_change_expiration` is **not** database-scoped, so it carries no
  `db_name`.

### `CONTROL BUCKETFS …`

BucketFS jobs are not database-scoped (no `db_name`). Service/bucket names are
single-quoted.

| Command | ConfD job | Kind | Status |
|---------|-----------|------|--------|
| `CONTROL BUCKETFS SHOW SERVICES` | `bucketfs_list` | read | ✅ |
| `CONTROL BUCKETFS SHOW SERVICE '<name>'` | `bucketfs_info` | read | ✅ |
| `CONTROL BUCKETFS DELETE SERVICE '<name>'` | `bucketfs_delete` | mutating | ◻️ |
| `CONTROL BUCKETFS ADD SERVICE '<name>' WITH <options>` | `bucketfs_add` | mutating | ◻️ |
| `CONTROL BUCKETFS MODIFY SERVICE '<name>' WITH <options>` | `bucketfs_modify` | mutating | ◻️ |
| `CONTROL BUCKETFS ADD BUCKET '<bucket>' TO SERVICE '<svc>' [PUBLIC\|PRIVATE] [WITH <options>]` | `bucket_add` | mutating | ◻️ |
| `CONTROL BUCKETFS MODIFY BUCKET '<bucket>' IN SERVICE '<svc>' [PUBLIC\|PRIVATE] [WITH <options>]` | `bucket_modify` | mutating | ◻️ |
| `CONTROL BUCKETFS DELETE BUCKET '<bucket>' FROM SERVICE '<svc>'` | `bucket_delete` | mutating | ◻️ |

`WITH key = value [, …]` options map 1:1 to the documented ConfD parameter names:
- **service**: `http_port`, `https_port`, `owner` (`'user group'`), `sync_period`,
  `sync_key`, `path`, `mode`, `bucketvolume`
- **bucket**: `read_password`, `write_password`

Ports and `sync_period` are emitted as JSON numbers when integral; `owner` is
split on whitespace into a `[user, group]` array. On `ADD BUCKET`, ConfD requires
the `PUBLIC`/`PRIVATE` flag.

## Examples (verified output)

```sql
CONTROL DATABASE SHOW INFO;
--  FIELD                          | VALUE_TEXT
--  name                           | Exasol
--  state                          | running
--  nodes.active[1]                | n11
--  config.version                 | 2025.2.0
--  …  (44 rows)

CONTROL DATABASE SHOW BACKUP PROGRESS;
--  Comment                        | Backup has been successfully finished
--  Progress                       | 100
--  …  (7 rows)

CONTROL BUCKETFS SHOW SERVICES;
--  [1]                            | bfsdefault

CONTROL BUCKETFS SHOW SERVICE 'bfsdefault';
--  http_port                      | 0
--  https_port                     | 2581
--  mode                           | rsync
--  buckets.default.public         | true
--  …  (16 rows)
```

## Calling a job directly (bypassing the grammar)

Any ConfD job can be invoked without the SQL sugar — useful for jobs not covered
by the grammar or for debugging:

```sql
EXECUTE SCRIPT PREPROC.ADMIN_CONFD_JOB('db_info', '{"db_name":""}');
```

The `params_json` is the literal ConfD job parameter object. Pass `"db_name":""`
to have the UDF fill in the current database; omit it for cluster-scoped jobs.
