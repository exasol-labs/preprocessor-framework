-- =============================================================================
-- DEMO_SQLGLOT_TRANSPILE_V1  (PREPROC_RT.DEMO_SQLGLOT_TRANSPILE_V1)
-- =============================================================================
-- Companion scalar UDF for the TRANSLATE-phase demo module DEMO_TRANSLATE_V1.
--
-- Role
--   AST-based dialect transpilation. Given a source dialect and a statement
--   body, it transpiles the body to Exasol SQL using sqlglot (>=27.x, shipped
--   in Exasol's default Python3 UDF container -- no BucketFS vendoring).
--
-- Return contract  (the ERR: sentinel)
--   The UDF NEVER raises: it returns a value on every path so a parse/transpile
--   failure can never fail the user's calling statement. On success it returns
--   the transpiled Exasol SQL string. On any failure it returns the sentinel
--   ERR:<message>. The calling Lua module (DEMO_TRANSLATE_V1) checks for the
--   exact ERR: prefix and fails closed (returns the original text unchanged)
--   when it sees it, so the sentinel is the load-bearing contract between the
--   UDF and the module.
--
-- Empty-result guard
--   sqlglot.transpile returns a list. An empty list (no statement produced) is
--   treated as a no-op: the original txt is returned rather than indexing [0].
--
-- Versioning / prefix discipline
--   _V1 suffix: never the target of CREATE OR REPLACE on a live body once a rule
--   binds it; author a _V2 to change behaviour. DEMO_ prefix: a demonstration
--   object, safe to leave deployed, never mistaken for a product command.
-- =============================================================================
CREATE OR REPLACE PYTHON3 SCALAR SCRIPT PREPROC_RT.DEMO_SQLGLOT_TRANSPILE_V1(dialect VARCHAR(64), txt VARCHAR(2000)) RETURNS VARCHAR(2000) AS
import sqlglot
def run(ctx):
    try:
        result = sqlglot.transpile(ctx.txt, read=ctx.dialect, write='exasol')
        return result[0] if result else ctx.txt
    except Exception as e:
        return 'ERR:' + str(e)
/
