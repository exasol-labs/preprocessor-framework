# Examples

Ready-to-run preprocessor modules for the framework — **one preprocessor per
file**. Each file is self-contained: it creates the module script(s) and ends
with the command that registers the module with the framework (an
`EXECUTE SCRIPT PREPROC.RULE_ADD(...)` call, with the equivalent `PREPROC ...`
command-sugar form shown in a trailing comment). Every example passes `NULL`
for `RULE_ADD`'s `client_pattern` argument — none of these rules is
client-scoped, so they apply to every client.

## Prerequisites

1. The framework is installed — `uv run preproc install`, or deploy the
   generated `release.sql` (`uv run preproc bundle --release`, or the
   `release-sql` CI artifact) — first.
2. To see a module act on your statements, activate the preprocessor for your
   session: `ALTER SESSION SET sql_preprocessor_script = PREPROC_RT.MASTER;`

Run an example in EXAplus or DbVisualiser; the `/` on its own line is the
statement delimiter (required because Lua/Python bodies contain semicolons).

## Files

| File | Phase | What it adds |
|------|-------|--------------|
| `demo_ping.sql` | EXPAND | `PING` → `SELECT 'PONG'`. The canonical authoring reference — read its in-body comments for the full EXPAND module contract. |
| `demo_translate.sql` | TRANSLATE | Transpiles a `--!dialect:<name>`-marked statement to Exasol SQL via the companion `sqlglot` Python3 UDF. Deploys two objects (UDF + module). |
| `demo_audit.sql` | REWRITE | Appends an inert `/* PREPROC_AUDIT user=<CURRENT_USER> */` comment. Demonstration only — writes to no table. |

## Ergonomics modules have moved

The `cast_shorthand` (`expr::type` → `CAST(expr AS type)`) and
`trailing_comma` (removes optional trailing commas) modules used to live here
as `ergonomics_cast.sql` / `ergonomics_trailing_comma.sql`. They have moved to
the community module library, [`exasol-labs/preprocessor-library`](https://github.com/exasol-labs/preprocessor-library),
as the first two modules proving out the module ecosystem. Install them from SQL,
with no toolchain at all:

```sql
PREPROC INSTALL MODULE cast_shorthand FOR ROLE ANALYSTS;
PREPROC INSTALL MODULE trailing_comma FOR ROLE ANALYSTS;
```

or with the CLI, which adds `list` / `search` / `update` / `remove` / `sync` /
`bundle` / `validate`:

```
uv run preproc module add cast_shorthand
uv run preproc module add trailing_comma
```

(Module names are `snake_case` identifiers — the old hyphenated spellings
`cast-shorthand` / `trailing-comma` no longer resolve.)

See [`docs/module-authoring.md`](../docs/module-authoring.md) for the module
contract and [`docs/operations.md` § Module management](../docs/operations.md#module-management)
for the CLI reference.

## Registration scope

Each example registers its rule against a role (`PREPROC_ADMIN`). A user must
**hold** that role for the rule to resolve to them (`PREPROC.REFRESH` walks the
role-grant graph). Edit the trailing `RULE_ADD`
call to change the scope — e.g. `'USER', 'ALICE'` to bind the module to a single
user instead of a role. The 6th argument, `client_pattern`, narrows the rule
further to callers whose reported client name contains that substring; leave
it `NULL` (as every example does) to apply to all clients regardless of which
tool connected.
