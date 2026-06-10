# Examples

Ready-to-run preprocessor modules for the framework — **one preprocessor per
file**. Each file is self-contained: it creates the module script(s) and ends
with the command that registers the module with the framework (an
`EXECUTE SCRIPT PREPROC.RULE_ADD(...)` call, with the equivalent `PREPROC ...`
command-sugar form shown in a trailing comment).

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
| `confd_admin.sql` | EXPAND | A real-world add-on: the `CONTROL DATABASE …` and `CONTROL BUCKETFS …` SQL command surfaces, routed through a generic Exasol ConfD gateway UDF. Deploys four objects and self-registers two `ROLE DBA` rules. Requires a `CONFD_CONNECTION` object (see the file header). Vendored from the companion confd-via-sql project. **Full command reference: [`confd_admin.md`](confd_admin.md).** |
| `ergonomics_cast.sql` | TRANSLATE | PostgreSQL-style `expr::type` cast shorthand → `CAST(expr AS type)`. Pure-Lua; skips strings/identifiers/comments; passes any unrecognised operand through unchanged. |
| `ergonomics_trailing_comma.sql` | TRANSLATE | Removes optional trailing commas before `)` or a list-terminating keyword (`SELECT a, b, FROM t`). Pure-Lua; additive — statements without trailing commas are untouched. |

## Registration scope

Each example registers its rule against a role (`PREPROC_ADMIN` for the demos,
`DBA` for confd). A user must **hold** that role for the rule to resolve to them
(`PREPROC.REFRESH` walks the role-grant graph). Edit the trailing `RULE_ADD`
call to change the scope — e.g. `'USER', 'ALICE'` to bind the module to a single
user instead of a role.
