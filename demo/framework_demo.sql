-- ===========================================================================
-- DEMO 1 of 2 - THE FRAMEWORK
-- Narration, timings and the multi-tab choreography: framework_demo.md
--
-- Creates no schema and no table. Needs a clean `release.sql` install and a
-- DBA/SYS connection. Three extra client tabs, opened where marked.
-- ===========================================================================


-- [1] ONE SLOT, AND WHAT THE INSTALL GAVE YOU ------------------ 0:00

-- Two schemas: PREPROC is locked, PREPROC_RT is PUBLIC-executable.
SELECT SCHEMA_NAME, SCHEMA_OWNER FROM EXA_ALL_SCHEMAS
WHERE  SCHEMA_NAME IN ('PREPROC', 'PREPROC_RT') ORDER BY 1;

-- The dispatcher, the refresh engine, and the admin surface - nothing else yet.
SELECT SCRIPT_SCHEMA, SCRIPT_NAME FROM EXA_ALL_SCRIPTS
WHERE  SCRIPT_SCHEMA IN ('PREPROC', 'PREPROC_RT')
  AND  SCRIPT_NAME NOT LIKE 'P\_%' ESCAPE '\'
ORDER BY 1, 2;

-- Nothing is intercepting SQL yet. This is a plain syntax error right now:
--   PREPROC STATUS;

-- One statement, reversible, no restart. (ALTER SYSTEM for the whole cluster.)
ALTER SESSION SET sql_preprocessor_script = PREPROC_RT.MASTER;
ALTER SYSTEM SET sql_preprocessor_script = PREPROC_RT.MASTER;


PREPROC STATUS;

PREPROC LIST RULES;


-- [2] HOW IT WORKS --------------------------------------------- 1:15

-- Rules live in one table. This is the only thing an operator edits.
SELECT RULE_ID, SCOPE_TYPE, SCOPE_VALUE, PHASE, SCRIPT_NAME, FUNCTION_NAME
FROM   PREPROC.CONFIG ORDER BY RULE_ID;

-- REFRESH flattens rules x the live role-grant graph into a per-user table.
-- Dispatch reads ONLY this - never the rule table - which is why a rule change
-- costs nothing per statement.
SELECT USER_NAME, PHASE, SCRIPT_NAME, ORDINAL, PROFILE_HASH
FROM   PREPROC.RESOLUTION ORDER BY USER_NAME, PHASE, ORDINAL;

-- And every user can read their OWN slice of it, and only their own.
SELECT * FROM PREPROC_RT.MY_PIPELINE;


-- [3] WRITE YOUR OWN PREPROCESSOR ------------------------------ 2:15

-- `SHOW TOP 3 OBJECTS` is not Exasol syntax. Twenty lines from now, it will be.

--/
CREATE OR REPLACE LUA SCRIPT PREPROC_RT.DEMO_TOP_OBJECTS_V1 AS
function top_objects(sqltext)
    local trimmed = sqltext:gsub("^%s*(.-)%s*$", "%1")
    local stripped = trimmed:gsub(";%s*$", "")
    local n = string.upper(stripped):match("^SHOW%s+TOP%s+(%d*)%s*OBJECTS$")
    if n == nil then
        return nil                      -- not my command; keep scanning
    end
    if n == "" then n = "5" end
    return "SELECT ROOT_NAME AS SCHEMA_NAME, OBJECT_NAME, OBJECT_TYPE,"
        .. " RAW_OBJECT_SIZE AS RAW_BYTES FROM EXA_ALL_OBJECT_SIZES"
        .. " WHERE OBJECT_TYPE <> 'SCHEMA' AND OBJECT_NAME NOT LIKE 'P!_%' ESCAPE '!'"
        .. " ORDER BY RAW_OBJECT_SIZE DESC LIMIT " .. n
end
/

PREPROC ADD RULE FOR ROLE DBA PHASE EXPAND
    SCRIPT PREPROC_RT.DEMO_TOP_OBJECTS_V1 FUNCTION top_objects;

SHOW TOP 3 OBJECTS;

-- Statements it declines are untouched:
SELECT 1 AS UNAFFECTED;


-- [4] DIFFERENT USERS, DIFFERENT RULES ------------------------- 3:30

-- Ordinary Exasol roles and users. No parallel principal model.
CREATE ROLE DEMO_ANALYST;
CREATE ROLE DEMO_ETL;

CREATE USER USER_A IDENTIFIED BY "Demo1234";
CREATE USER USER_B IDENTIFIED BY "Demo1234";
CREATE USER USER_C IDENTIFIED BY "Demo1234";
GRANT CREATE SESSION TO USER_A, USER_B, USER_C;

GRANT DEMO_ANALYST TO USER_A;
GRANT DEMO_ETL     TO USER_B;
-- USER_C deliberately gets nothing.

-- A second module with its OWN command. An ETL engineer does not care which
-- objects are biggest; they care which changed last -- so they get different
-- syntax, not a different answer to the same words.

--/
CREATE OR REPLACE LUA SCRIPT PREPROC_RT.DEMO_NEWEST_OBJECTS_V1 AS
function newest_objects(sqltext)
    local trimmed = sqltext:gsub("^%s*(.-)%s*$", "%1")
    local stripped = trimmed:gsub(";%s*$", "")
    local n = string.upper(stripped):match("^SHOW%s+NEWEST%s+(%d*)%s*OBJECTS$")
    if n == nil then
        return nil
    end
    if n == "" then n = "5" end
    return "SELECT ROOT_NAME AS SCHEMA_NAME, OBJECT_NAME, OBJECT_TYPE,"
        .. " LAST_COMMIT FROM EXA_ALL_OBJECT_SIZES"
        .. " WHERE OBJECT_TYPE <> 'SCHEMA' AND OBJECT_NAME NOT LIKE 'P!_%' ESCAPE '!'"
        .. " ORDER BY LAST_COMMIT DESC LIMIT " .. n
end
/

-- Two commands, two audiences. Each role gets the vocabulary it needs.
PREPROC ADD RULE FOR ROLE DEMO_ANALYST PHASE EXPAND
    SCRIPT PREPROC_RT.DEMO_TOP_OBJECTS_V1 FUNCTION top_objects;

PREPROC ADD RULE FOR ROLE DEMO_ETL PHASE EXPAND
    SCRIPT PREPROC_RT.DEMO_NEWEST_OBJECTS_V1 FUNCTION newest_objects;

-- Three users, three different resolved pipelines - and USER_C has none.
SELECT USER_NAME, PHASE, SCRIPT_NAME, PROFILE_HASH
FROM   PREPROC.RESOLUTION
WHERE  USER_NAME LIKE 'USER\_%' ESCAPE '\'
ORDER BY USER_NAME;


-- === TAB 2 - USER_A / Demo1234 =============================== 4:15
--
--   ALTER SESSION SET sql_preprocessor_script = PREPROC_RT.MASTER;
--   SELECT PHASE, SCRIPT_NAME FROM PREPROC_RT.MY_PIPELINE;
--   SHOW TOP 3 OBJECTS;                 -- biggest objects they can see
--
-- === TAB 3 - USER_B / Demo1234 =============================== 4:45
--
--   ALTER SESSION SET sql_preprocessor_script = PREPROC_RT.MASTER;
--   SELECT PHASE, SCRIPT_NAME FROM PREPROC_RT.MY_PIPELINE;
--   SHOW TOP 3 OBJECTS;                 -- most recently changed objects
--
-- === TAB 4 - USER_C / Demo1234 =============================== 5:15
--
--   ALTER SESSION SET sql_preprocessor_script = PREPROC_RT.MASTER;
--   SELECT * FROM PREPROC_RT.MY_PIPELINE;      -- one all-NULL row: no rules
--   SHOW TOP 3 OBJECTS;                          -- syntax error: not their syntax
--
-- =============================================================


-- [5] ...AND FROM WHICH CLIENT ---------------------------------- 5:30

-- A rule can also be narrowed to the client APPLICATION. Some conveniences only
-- make sense when a human is typing; you do not want them firing inside a
-- scheduled ETL job.
--
-- First, find out what your client actually calls itself. Run this in each tool
-- you care about, and use a substring of what you see.
SELECT CLIENT, DRIVER FROM EXA_ALL_SESSIONS WHERE SESSION_ID = CURRENT_SESSION;

-- A `HELP` command is the perfect example: obviously a human-at-a-keyboard thing,
-- and meaningless to a batch job.

--/
CREATE OR REPLACE LUA SCRIPT PREPROC_RT.DEMO_HELP_V1 AS
function help_menu(sqltext)
    local trimmed = sqltext:gsub("^%s*(.-)%s*$", "%1")
    local stripped = trimmed:gsub(";%s*$", "")
    if string.upper(stripped) ~= "HELP" then
        return nil
    end
    return "SELECT * FROM VALUES"
        .. " ('HELP', 'this list'),"
        .. " ('SHOW TOP [n] OBJECTS', 'biggest objects you can see'),"
        .. " ('SHOW NEWEST [n] OBJECTS', 'most recently changed objects')"
        .. " AS T(COMMAND, DESCRIPTION)"
end
/

-- The pattern is a literal SUBSTRING of the client name -- not a wildcard, not a
-- regular expression. 'DbVisualizer' matches 'DbVisualizer 24.1.5'. Note the two
-- literal captures in this one command: `help_menu` stays lowercase (a Lua
-- symbol) and `DbVisualizer` keeps its casing (a self-reported client name) --
-- neither is folded the way an object name or a role would be.
PREPROC ADD RULE FOR ROLE DBA PHASE EXPAND
    SCRIPT PREPROC_RT.DEMO_HELP_V1 FUNCTION help_menu CLIENT 'DbVisualizer';

-- One rule now carries a pattern; every other rule has NULL, meaning "always".
PREPROC LIST RULES;

-- In THIS tab (DbVisualizer), the command exists:
HELP;

-- =============================================================================
-- >> A NON-DbVisualizer CLIENT - DBeaver, EXAplus, anything else --- 6:00
-- =============================================================================
--
--   ALTER SESSION SET sql_preprocessor_script = PREPROC_RT.MASTER;
--
--   -- The rule IS in this session's pipeline. Look at CLIENT_PATTERN.
--   SELECT PHASE, SCRIPT_NAME, CLIENT_PATTERN FROM PREPROC_RT.MY_PIPELINE;
--
--   -- And the command still does not exist here:
--   HELP;
--   -- syntax error
--
-- =============================================================================
--
-- That contrast is the interesting part, and it is worth pausing on: the rule
-- resolved to BOTH sessions. Resolution is client-agnostic -- it is built from
-- rules and role grants, neither of which knows anything about clients. The client
-- match happens at DISPATCH time, per statement, inside MASTER.
--
-- Which also means: a client_pattern is SCOPING, not security. The client name is
-- whatever the connecting tool chooses to report. Use it to keep conveniences out
-- of batch jobs -- never to keep a transformation away from someone who should not
-- have it. That is what roles and schema grants are for.
--
-- Cost note: MASTER looks the client up only when at least one resolved rule
-- carries a pattern. Sessions whose rules are all NULL-pattern never pay for it.


-- [6] THE REFRESH CONTRACT ------------------------------------- 6:30

-- Rule changes auto-refresh. GRANT changes cannot - the framework never sees them.
GRANT DEMO_ETL TO USER_C;

-- The framework says so, honestly:
PREPROC STATUS;

-- USER_C still resolves nothing:
SELECT USER_NAME, SCRIPT_NAME FROM PREPROC.RESOLUTION
WHERE  USER_NAME = 'USER_C';

PREPROC REFRESH;

PREPROC STATUS;

SELECT USER_NAME, SCRIPT_NAME FROM PREPROC.RESOLUTION
WHERE  USER_NAME = 'USER_C';

-- === TAB 4 - USER_C again, WITHOUT reconnecting ============== 6:15
--
--   SHOW NEWEST 3 OBJECTS;     -- now works, without reconnecting
--
-- =============================================================


-- [7] IT CANNOT TAKE THE DATABASE DOWN ----------------------- 7:30

--/
CREATE OR REPLACE LUA SCRIPT PREPROC_RT.DEMO_BROKEN_V1 AS
function expand(sqltext)
    error("this module is broken")
end
/

PREPROC ADD RULE FOR ROLE DBA PHASE EXPAND
    SCRIPT PREPROC_RT.DEMO_BROKEN_V1 FUNCTION expand;

SELECT 'the database still answers' AS PROOF;

SHOW TOP 3 OBJECTS;

-- Rule first, then the script - never the other way round.
PREPROC DROP RULE 7;

DROP SCRIPT PREPROC_RT.DEMO_BROKEN_V1;


-- [8] YOU DO NOT HAVE TO WRITE THEM YOURSELF ----------------- 8:00

PREPROC CATALOG MODULES;

-- Install one, and hand it straight to an audience that already exists.
PREPROC INSTALL MODULE cast_shorthand FOR ROLE DEMO_ANALYST;

-- === TAB 2 - USER_A, still connected ========================= 7:45
--
--   SELECT PHASE, SCRIPT_NAME FROM PREPROC_RT.MY_PIPELINE;   -- two rules now
--   SELECT ORDER_ID, AMOUNT_TXT::DECIMAL(10,2) AS AMOUNT
--   FROM   VALUES (1,'1299.50'), (2,'87.25') AS T(ORDER_ID, AMOUNT_TXT);
--
-- =============================================================


-- [9] CLOSE -------------------------------------------------- 8:45

PREPROC LIST RULES;
