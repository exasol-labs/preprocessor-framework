-- =============================================================================
-- DEMO 06 - PREPROCESSORS OUTSIDE PREPROC_RT, AND WHY THAT IS A SECURITY CONTROL
-- =============================================================================
-- WHAT THIS SHOWS
--   A rule's `script_name` is an ordinary schema-qualified name. Nothing requires
--   a preprocessor - or a UDF it calls - to live in `PREPROC_RT`. Putting it in a
--   schema whose EXECUTE privilege you control turns "who can run this
--   transformation" into an ordinary Exasol grant.
--
-- WHY IT MATTERS
--   `PREPROC_RT` has EXECUTE and SELECT granted to PUBLIC. That is a hard
--   framework invariant - without it, activating the preprocessor locks every
--   user out. The consequence: ANY script in `PREPROC_RT` is executable by
--   everyone. Fine for `expr::type`. Not fine for a preprocessor that reaches
--   sensitive data or performs a privileged action.
--
-- THREE INDEPENDENT LAYERS, DEMONSTRATED
--   1. The RULE decides who the framework OFFERS a transformation to.
--   2. The EXECUTE grant on the script's schema decides who can actually RUN it.
--      No grant -> the dispatcher's per-rule fail-open skips the module and the
--      raw statement reaches the engine. Denial is by privilege, not by rule.
--   3. The EXPANDED SQL still executes with the INVOKER's rights. A preprocessor
--      is not a privilege-escalation vector, even for a user who can run it.
--
--   Layers 1 and 2 are separate on purpose: the same rule can be live for a whole
--   role while only a subset of that role can execute it.
--
-- PREREQUISITES
--   * Files 01-05 have been run (demo roles and users exist).
--   * Two sessions again - admin here, DEMO_ANA / DEMO_FIN_ANA where marked.
-- =============================================================================

ALTER SESSION SET sql_preprocessor_script = PREPROC_RT.MASTER;


-- -----------------------------------------------------------------------------
-- 1. SOMETHING WORTH PROTECTING
-- -----------------------------------------------------------------------------
-- This is the ONE file in the demo that creates a schema and a table. Everywhere
-- else the sample rows ride inline as VALUES-derived tables, because the
-- transformations are about SQL text. Here they cannot: privileges are the subject,
-- and you cannot GRANT SELECT on a subselect. The table IS the thing being
-- protected.

CREATE SCHEMA IF NOT EXISTS FIN;

CREATE OR REPLACE TABLE FIN.PAYROLL (
    REGION    VARCHAR(20),
    EMPLOYEE  VARCHAR(40),
    SALARY    DECIMAL(10,2)
);

INSERT INTO FIN.PAYROLL VALUES
    ('EMEA', 'A. Klein', 82000),
    ('EMEA', 'B. Rossi', 91500),
    ('APAC', 'C. Tan',   77000);


-- -----------------------------------------------------------------------------
-- 2. A GUARDED SCHEMA FOR THE PREPROCESSOR
-- -----------------------------------------------------------------------------
-- Deliberately NOT `PREPROC_RT`. This schema gets no PUBLIC grant, so by default
-- nobody but its owner can execute what is in it.

CREATE SCHEMA IF NOT EXISTS FIN_GUARD;

--/
CREATE OR REPLACE LUA SCRIPT FIN_GUARD.SHOW_PAYROLL_V1 AS
local function normalise(text)
    local s = text
    while true do
        s = s:gsub("^%s+", "")
        local without = s:gsub("^%-%-[^\n]*\n?", "", 1)
        if without == s then break end
        s = without
    end
    local trimmed = s:gsub("^%s*(.-)%s*$", "%1")
    local stripped = trimmed:gsub(";%s*$", "")
    return stripped
end

-- SHOW PAYROLL SUMMARY -> the aggregate over FIN.PAYROLL.
function payroll(sqltext)
    if string.upper(normalise(sqltext)) ~= "SHOW PAYROLL SUMMARY" then
        return nil
    end
    return "SELECT REGION, COUNT(*) AS HEADCOUNT, SUM(SALARY) AS TOTAL_SALARY"
        .. " FROM FIN.PAYROLL GROUP BY REGION ORDER BY 1"
end
/

-- Nothing about the rule syntax changes because the schema changed. The framework
-- takes the fully-qualified name you give it.
PREPROC ADD RULE FOR ROLE DEMO_ANALYST PHASE EXPAND
    SCRIPT FIN_GUARD.SHOW_PAYROLL_V1 FUNCTION payroll;

-- Note the scope: ROLE DEMO_ANALYST. BOTH DEMO_ANA and DEMO_FIN_ANA hold it, so
-- both are OFFERED this transformation. Confirm the framework treats them
-- identically - same rule, same chain, same profile hash:
SELECT USER_NAME, PHASE, SCRIPT_NAME, PROFILE_HASH
FROM   PREPROC.RESOLUTION
WHERE  SCRIPT_NAME = 'FIN_GUARD.SHOW_PAYROLL_V1'
ORDER BY USER_NAME;


-- -----------------------------------------------------------------------------
-- 3. GRANT EXECUTE TO ONE AUDIENCE ONLY
-- -----------------------------------------------------------------------------
-- This is layer 2, and it is a plain Exasol grant - no framework concept
-- involved. Only DEMO_FIN_ANA holds DEMO_FIN.

GRANT EXECUTE ON SCHEMA FIN_GUARD TO DEMO_FIN;
GRANT SELECT  ON SCHEMA FIN       TO DEMO_FIN;

-- No refresh needed: grants on the module's schema do not change RESOLUTION.
-- The dispatcher imports the script at statement time under the caller's rights,
-- so a grant or revoke takes effect on the NEXT statement - including in sessions
-- that are already open and already pinned to a profile.


-- =============================================================================
-- >> SESSION B - connect as DEMO_FIN_ANA / Demo1234  (authorised)
-- =============================================================================
--
--   ALTER SESSION SET sql_preprocessor_script = PREPROC_RT.MASTER;
--   SELECT * FROM PREPROC_RT.MY_PIPELINE;     -- the FIN_GUARD rule is there
--   SHOW PAYROLL SUMMARY;                     -- works
--
-- =============================================================================
-- >> SESSION C - connect as DEMO_ANA / Demo1234  (NOT authorised)
-- =============================================================================
--
--   ALTER SESSION SET sql_preprocessor_script = PREPROC_RT.MASTER;
--
--   -- The SAME rule is in this user's pipeline. The framework is offering it.
--   SELECT * FROM PREPROC_RT.MY_PIPELINE;
--
--   -- And it does nothing, because the import fails and the dispatcher's
--   -- per-rule fail-open skips it. The raw text reaches the engine:
--   SHOW PAYROLL SUMMARY;
--   -- syntax error, unexpected IDENTIFIER_PART_
--
--   -- The skip is PER RULE, not per session. This user's other transformations
--   -- are untouched:
--   SELECT ORDER_ID, AMOUNT_TXT::DECIMAL(10,2) FROM DEMO.ORDERS ORDER BY 1;
--
-- =============================================================================
--
-- Read the failure carefully - it is a SYNTAX error, not a privileges error. The
-- unauthorised user learns that `SHOW PAYROLL SUMMARY` is not SQL. They do not
-- learn that a payroll command exists, who has it, or what it queries. Fail-open
-- degrades to "no transformation", and that is also the discreet answer.


-- -----------------------------------------------------------------------------
-- 4. LAYER 3: EXECUTING THE MODULE IS NOT READING THE DATA
-- -----------------------------------------------------------------------------
-- The obvious next question from a security reviewer: "if a preprocessor can
-- rewrite my SQL, can it rewrite it into something I am not allowed to run?"
-- No. The expansion is handed back to the engine and compiled under the CALLER's
-- privileges. Prove it by granting EXECUTE without granting the data:

GRANT EXECUTE ON SCHEMA FIN_GUARD TO DEMO_ANALYST;   -- can now run the module...
                                                     -- ...but still no SELECT on FIN

-- >> In SESSION C (DEMO_ANA), re-run:
--
--   SHOW PAYROLL SUMMARY;
--   -- insufficient privileges: SELECT on table PAYROLL
--
-- The command expanded. The query was refused. Two independent gates, and the
-- preprocessor cannot bypass the second one - it never runs with elevated rights.

REVOKE EXECUTE ON SCHEMA FIN_GUARD FROM DEMO_ANALYST;

-- >> In SESSION C again: straight back to the syntax error, no reconnect.


-- -----------------------------------------------------------------------------
-- 5. FINER GRAIN: GRANT ONE SCRIPT, NOT THE SCHEMA
-- -----------------------------------------------------------------------------
-- Schema-level EXECUTE is the blunt instrument. A guarded schema can hold several
-- preprocessors with different audiences, granted one at a time.

GRANT EXECUTE ON FIN_GUARD.SHOW_PAYROLL_V1 TO DEMO_ANALYST;
REVOKE EXECUTE ON FIN_GUARD.SHOW_PAYROLL_V1 FROM DEMO_ANALYST;

-- Audit what you have handed out. Schema-level grants show up as
-- OBJECT_TYPE = 'SCHEMA'; per-script grants carry OBJECT_SCHEMA = 'FIN_GUARD'.
-- Contrast the PREPROC_RT rows: EXECUTE and SELECT to PUBLIC, exactly as the
-- framework invariant requires - and exactly why a sensitive module does not
-- belong there.
SELECT OBJECT_SCHEMA, OBJECT_NAME, OBJECT_TYPE, PRIVILEGE, GRANTEE
FROM   EXA_DBA_OBJ_PRIVS
WHERE  OBJECT_NAME IN ('FIN_GUARD', 'FIN', 'PREPROC_RT', 'PREPROC')
   OR  OBJECT_SCHEMA = 'FIN_GUARD'
ORDER BY 2, 4, 5;


-- -----------------------------------------------------------------------------
-- 6. THE SAME PATTERN APPLIES TO UDFs - AND THE FRAMEWORK USES IT ITSELF
-- -----------------------------------------------------------------------------
-- A preprocessor is Lua and cannot touch the filesystem, BucketFS, or the
-- network. Anything that does needs a UDF, and a UDF is where real capability
-- lives. So the same placement question is sharper for UDFs than for modules.
--
-- Look at how the framework splits its own:
--
--   PREPROC_RT (PUBLIC EXECUTE)   the dispatcher, the modules, the view - the
--                                 things every user MUST be able to run
--   PREPROC    (no PUBLIC grant)  the admin scripts, and the resolver UDF that
--                                 reads BucketFS and the network
--
-- The capability that could matter - fetching a module artifact from a bucket or
-- over HTTPS - is in the locked schema, reachable only through the admin surface.
-- Composite modules can declare objects in either schema, and the catalog shows
-- you exactly which before you install:

SELECT NAME, OBJECTS FROM PREPROC_RT.INSTALLED_MODULES ORDER BY NAME;


-- -----------------------------------------------------------------------------
-- 7. BREAK GLASS: A GUARANTEED RAW-SQL ACCOUNT
-- -----------------------------------------------------------------------------
-- Last question a security reviewer asks: "what if a rewrite is the incident?"
-- `SYS` can be isolated from the ENTIRE role-grant closure - DBA, PUBLIC, nested
-- roles alike - so it always sees untransformed SQL no matter what rules exist.
--
--   EXECUTE SCRIPT PREPROC.SET_SYS_GROUPING(FALSE, NULL);   -- isolate SYS
--   SELECT group_sys_with_dba FROM PREPROC.SETTINGS;
--   EXECUTE SCRIPT PREPROC.SET_SYS_GROUPING(TRUE, NULL);    -- back to the default
--
-- CAUTION IF YOU RUN THIS AS SYS: isolating SYS also removes SYS's own
-- `PREPROC ...` command sugar (it arrives through the DBA-scoped rule). Use the
-- SCRIPTED form above to switch it back - that always works, with or without an
-- active preprocessor. This is the practical reason the sugar and the scripts are
-- two views of one surface.


-- =============================================================================
-- WHERE WE ARE
--   Transformation membership (the rule), execution rights (the grant), and data
--   rights (the query) are three separate controls. All three are ordinary Exasol
--   privileges, auditable with the dictionary views you already use.
--
-- NEXT: 99_teardown.sql - remove everything this walkthrough created.
-- =============================================================================
