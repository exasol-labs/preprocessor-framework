-- =============================================================================
-- EXAMPLE preprocessor: DEMO_TRANSLATE_V1  (TRANSLATE phase) + companion UDF
-- =============================================================================
-- Transpiles a foreign-dialect statement to Exasol SQL when it is prefixed with
-- a  --!dialect:<name>  marker (e.g. --!dialect:tsql), using the companion
-- Python3 UDF DEMO_SQLGLOT_TRANSPILE_V1 (sqlglot). Unmarked statements pass
-- through byte-for-byte; a failed transpile fails closed (original text runs).
--
--   Input:  --!dialect:tsql
--           SELECT TOP 5 col FROM t
--   Output: SELECT col FROM t LIMIT 5
--
-- This example deploys TWO objects: the UDF (called by the module) and the
-- TRANSLATE module itself. Prerequisite: the framework is installed
-- (run release.sql) and the preprocessor is active for your session:
--   ALTER SESSION SET sql_preprocessor_script = PREPROC_RT.MASTER;
-- =============================================================================

OPEN SCHEMA PREPROC_RT
/

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

CREATE OR REPLACE LUA SCRIPT PREPROC_RT.DEMO_TRANSLATE_V1() AS
-- =============================================================================
-- DEMO_TRANSLATE_V1 preprocessor module  (PREPROC_RT.DEMO_TRANSLATE_V1)
-- =============================================================================
-- Canonical authoring reference for TRANSLATE-phase modules. Read demo_ping_v1
-- for the full authoring contract; this file applies that contract to a
-- translation rule that calls a companion Python3 UDF.
--
-- TRANSLATE-phase return contract (differs from EXPAND)
--   On TRANSLATE the dispatcher runs every matching rule and threads the
--   returned string forward (run-all threading), not first-match-terminal. The
--   function therefore returns a string on EVERY path: the transpiled text when
--   it translated, or the ORIGINAL text byte-for-byte when it did not. There is
--   no nil "decline" path here; "decline" means returning the input unchanged.
--
-- Trigger: the dialect marker
--   The only statements this module acts on begin -- after optional leading
--   whitespace -- with the marker comment  --!dialect:<name>  (e.g.
--   --!dialect:tsql). A statement without the marker is returned byte-for-byte
--   unchanged and the UDF is never called. Always-transpile was rejected: it is
--   slow (~133 ms/statement) and risks corrupting already-valid Exasol SQL.
--
-- Fail-closed contract
--   TRANSLATE is fail-closed: corrupted or wrong-dialect text must never reach
--   the engine masquerading as a successful translation. The UDF returns the
--   sentinel ERR:<message> instead of raising on any parse/transpile failure.
--   When the UDF returns ERR: (or the pquery itself is not-ok / returns no
--   value), this module returns the ORIGINAL statement text unchanged. It never
--   threads the ERR: sentinel forward as statement text.
--
-- No defensive pcall
--   MASTER wraps this call in pcall (fail-open at the rule level). Reading
--   pquery's ok flag is ordinary control flow, not error suppression; a genuine
--   throw is left to propagate so MASTER's pcall can skip the rule visibly.
--
-- gsub two-value trap
--   string.gsub returns (result, count). The marker strip below captures only
--   the result string into a local before use, never forwarding the count.
-- =============================================================================

-- luacheck: globals translate

-- translate(sqltext) -> transpiled Exasol SQL when the statement carries a
-- leading --!dialect:<name> marker and the UDF succeeds; the original text
-- unchanged otherwise (no marker, or a fail-closed UDF/pquery failure).
function translate(sqltext)
    local dialect = string.match(sqltext, "^%s*%-%-!dialect:(%S+)")
    if dialect == nil then
        return sqltext
    end

    local stripped = string.gsub(sqltext, "^%s*%-%-!dialect:%S+[^\n]*\n?", "")

    local ok, result = pquery(
        "SELECT PREPROC_RT.DEMO_SQLGLOT_TRANSPILE_V1(:d, :t)",
        { d = dialect, t = stripped })

    if not ok or result == nil or result[1] == nil or result[1][1] == null then
        return sqltext
    end

    local transpiled = tostring(result[1][1])
    if string.sub(transpiled, 1, 4) == "ERR:" then
        return sqltext
    end

    return transpiled
end
/


-- -----------------------------------------------------------------------------
-- Register this module with the framework
-- -----------------------------------------------------------------------------
-- A module does nothing until a PREPROC.CONFIG rule points at it and a REFRESH
-- resolves that rule. RULE_ADD does both (it auto-refreshes unless you pass a
-- non-NULL norefresh). Edit the scope below: 'ROLE' + a role name applies to
-- every holder of that role; 'USER' + a user name applies to one user.
--
-- Requires PREPROC_ADMIN (or DBA). Works whether or not the preprocessor is
-- active. The commented PREPROC ... line is the equivalent command-sugar form,
-- usable in convenience mode once the preprocessor is active and you hold
-- PREPROC_ADMIN.

EXECUTE SCRIPT PREPROC.RULE_ADD('ROLE', 'PREPROC_ADMIN', 'TRANSLATE', 'PREPROC_RT.DEMO_TRANSLATE_V1', 'translate', NULL)
/

--   PREPROC ADD RULE ROLE PREPROC_ADMIN TRANSLATE PREPROC_RT.DEMO_TRANSLATE_V1 translate
