# top_objects

Adds a `SHOW TOP [n] OBJECTS` command that ranks the database objects the caller
can see by raw size, defaulting to 5 rows.

Deployed script: `PREPROC_RT.TOP_OBJECTS_V1`, function `top_objects`, phase
`EXPAND`. Single-statement artifact, so `module.toml` declares no `[[objects]]`
array — the inventory is derived from `script_name`.

## Use

```sql
SHOW TOP OBJECTS;        -- top 5
SHOW TOP 20 OBJECTS;     -- top 20
```

Returns `SCHEMA_NAME`, `OBJECT_NAME`, `OBJECT_TYPE`, `RAW_BYTES`. Schemas and the
framework's own generated `P_<hash>` profile scripts are excluded.

## Install

```sql
PREPROC INSTALL MODULE top_objects FOR ROLE PREPROC_ADMIN;
```

or with the CLI:

```bash
uv run preproc module add top_objects --scope ROLE:PREPROC_ADMIN
```

## Privileges

The expansion reads `EXA_ALL_OBJECT_SIZES`, a user-scoped dictionary view, and is
compiled under the **calling** user's privileges. Two users running the same
command legitimately see different rows, and the module confers no access of its
own.
