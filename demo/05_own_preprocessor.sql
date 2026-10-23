-- =============================================================================
-- DEMO 05 - WRITE YOUR OWN PREPROCESSOR (no registry, no manifest, no toolchain)
-- =============================================================================
-- WHAT THIS SHOWS
--   You do not have to publish a module to use the framework. A preprocessor is
--   a Lua function `fn(text) -> nil | string` and a rule pointing at it. That is
--   the whole contract. Everything else in the module ecosystem - manifests,
--   sha256, provenance, the registry - exists to DISTRIBUTE preprocessors, not
--   to run them.
--
-- THE PUNCHLINE
--   We are about to add a SQL command Exasol does not have, in about twenty
--   lines of Lua, with two statements, in the middle of a live session. Nothing
--   is registered anywhere. `INSTALLED_MODULES` will stay empty for it and the
--   dispatcher will not care.
--
-- PREREQUISITES
--   * Files 01-04 have been run.
--   * Run in EXAplus, DbVisualiser, or any client that understands the `/`
--     block delimiter - the script body below contains semicolons.
--     IMPORTANT for GUI clients: turn OFF parameterized SQL / named parameters,
--     or the client will substitute on the `:` characters inside the body and
--     silently corrupt the deployed script.
-- =============================================================================

ALTER SESSION SET sql_preprocessor_script = PREPROC_RT.MASTER;


-- -----------------------------------------------------------------------------
-- 1. THE COMMAND WE WANT
-- -----------------------------------------------------------------------------
-- Every DBA has typed the "which tables are eating my memory" query from memory
-- one time too many. We want to type this instead:
--
--     SHOW TOP TABLES
--     SHOW TOP TABLES 5
--
-- Right now it is a syntax error. Try it:
--
--   SHOW TOP TABLES;
--   -- syntax error, unexpected IDENTIFIER_PART_


-- -----------------------------------------------------------------------------
-- 2. THE PREPROCESSOR - TWENTY LINES
-- -----------------------------------------------------------------------------
-- The whole EXPAND contract, in one function:
--
--   * Return `nil` to say "not my command" - the dispatcher keeps scanning the
--     remaining EXPAND rules, and an unclaimed statement reaches the engine
--     verbatim. Declining is the normal path, not an error path.
--   * Return a string to claim the statement. That is TERMINAL for the EXPAND
--     phase, and it must be exactly ONE SQL statement.
--
-- Three authoring rules that account for most first-attempt bugs:
--
--   a) The exported function must be a plain top-level GLOBAL. The dispatcher
--      loads the script with `import(name, alias)` and reads the globals table -
--      a `local function` is invisible to it. (Note `normalise` below IS local:
--      helpers should be, so only the entry point is exported.)
--   b) `string.gsub` returns TWO values. Never return or forward a gsub call
--      directly; capture it into one local first. Every gsub below does.
--   c) Do NOT wrap your body in `pcall`. The dispatcher's pcall is the one error
--      boundary; a defensive pcall inside a module converts a visible fault into
--      a silent wrong answer.

--/
CREATE OR REPLACE LUA SCRIPT PREPROC_RT.DEMO_TOP_TABLES_V1 AS
-- Trim, drop any leading full-line comments, and drop one trailing semicolon.
-- The leading-comment strip matters in practice: SQL clients routinely submit a
-- comment block together with the statement that follows it, and the
-- preprocessor sees that raw text. The framework's own command module does this
-- for the same reason.
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

-- SHOW TOP TABLES [n]  ->  the object-size query, defaulting to 10 rows.
function top_tables(sqltext)
    local n = string.upper(normalise(sqltext)):match("^SHOW%s+TOP%s+TABLES%s*(%d*)$")
    if n == nil then
        return nil                      -- not my command; keep scanning
    end
    if n == "" then
        n = "10"
    end
    return "SELECT ROOT_NAME AS SCHEMA_NAME, OBJECT_NAME AS TABLE_NAME,"
        .. " RAW_OBJECT_SIZE AS RAW_BYTES, MEM_OBJECT_SIZE AS MEM_BYTES"
        .. " FROM EXA_ALL_OBJECT_SIZES WHERE OBJECT_TYPE = 'TABLE'"
        .. " ORDER BY RAW_OBJECT_SIZE DESC LIMIT " .. n
end
/


-- -----------------------------------------------------------------------------
-- 3. BIND IT - ONE RULE
-- -----------------------------------------------------------------------------
-- Same command you used for the library modules in files 02 and 03. The
-- dispatcher cannot tell the difference and does not look.
--
-- (`FUNCTION top_tables` is a case-SENSITIVE Lua symbol, unlike the identifiers
-- around it. Write it exactly as declared.)

PREPROC ADD RULE FOR ROLE DBA PHASE EXPAND
    SCRIPT PREPROC_RT.DEMO_TOP_TABLES_V1 FUNCTION top_tables;

-- New syntax, live, in this session:
SHOW TOP TABLES;
SHOW TOP TABLES 3;

-- Statements it declines are untouched - no interference, no measurable cost:
SELECT COUNT(*) FROM VALUES (1), (2), (3) AS T(N);


-- -----------------------------------------------------------------------------
-- 4. IT IS NOT A MODULE, AND NOTHING MINDS
-- -----------------------------------------------------------------------------
-- No manifest, no sha256, no registry entry, no provenance row. Compare:

SELECT NAME, VERSION, SCRIPT_NAME, DEPLOY_MODE, STATUS
FROM   PREPROC_RT.INSTALLED_MODULES        -- only the two installed modules
ORDER BY NAME;

-- ...but the rule is a first-class rule, indistinguishable from an installed
-- module's rule:
PREPROC LIST RULES;

-- Two ways to read this. For a platform team: prototype in the session, promote
-- to a published module only when someone else needs it. For a customer with an
-- in-house SQL dialect: their transformations never have to leave their cluster
-- to be first-class citizens of the framework.


-- -----------------------------------------------------------------------------
-- 5. WHAT PROMOTING IT WOULD COST
-- -----------------------------------------------------------------------------
-- If you later want this distributable, the delta is a `module.toml` beside the
-- .sql file:
--
--   name          = "top_tables"
--   description   = "Adds SHOW TOP TABLES [n]."
--   phase         = "EXPAND"
--   script_name   = "PREPROC_RT.TOP_TABLES_V1"
--   function      = "top_tables"
--   version       = 1
--   min_framework = "0.7.0"
--   deploy_mode   = "library-deployed"
--   sha256        = "<digest of the .sql>"
--   [suggested_scope]
--   type  = "ROLE"
--   value = "DBA"
--
-- The Lua does not change. The function contract is identical whether the
-- preprocessor is a twenty-line convenience or the front door to a whole
-- subsystem. `demo/top_objects/` is a command like this one packaged
-- as a complete module: manifest, artifact, and tests.
--
-- One discipline to adopt now rather than later: the `_V1` suffix. Never
-- `CREATE OR REPLACE` a module that is live in someone's session. Ship
-- `_V2` alongside it, repoint the rule, refresh, and retire `_V1` once nothing
-- references it. (We used `CREATE OR REPLACE` above because nothing was bound
-- yet - the first deploy is the only safe time for it.)
--
-- Full contract: docs/module-authoring.md.


-- =============================================================================
-- WHERE WE ARE
--   A custom SQL command, authored and live, with no packaging ceremony at all.
--
-- NEXT: 06_secure_schemas.sql - that script did not have to live in PREPROC_RT,
--       and where you put it is a security control.
-- =============================================================================
