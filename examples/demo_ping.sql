-- =============================================================================
-- EXAMPLE preprocessor: DEMO_PING_V1  (EXPAND phase)
-- =============================================================================
-- The canonical "hello world" command module: the statement `PING` expands to
-- `SELECT 'PONG'`; every other statement passes through untouched. Read the
-- in-body comments for the full EXPAND authoring contract.
--
-- Prerequisite: the framework is installed (run release.sql) and, to see it
-- work, the preprocessor is active for your session:
--   ALTER SESSION SET sql_preprocessor_script = PREPROC_RT.MASTER;
-- =============================================================================

OPEN SCHEMA PREPROC_RT;

--/
CREATE OR REPLACE LUA SCRIPT PREPROC_RT.DEMO_PING_V1() AS
-- =============================================================================
-- DEMO_PING_V1 preprocessor module  (PREPROC_RT.DEMO_PING_V1)
-- =============================================================================
-- Canonical authoring reference for EXPAND-phase command modules.
--
-- This file is intentionally the simplest possible module that exercises the
-- full EXPAND contract end-to-end. Module authors should read it before writing
-- a new command and copy its shape. Everything below the comment block is the
-- minimum a real module needs; the comment block IS the authoring contract.
--
-- -----------------------------------------------------------------------------
-- 1. Return contract  (nil = no match, continue; string = matched, terminal)
-- -----------------------------------------------------------------------------
--   An EXPAND module function returns exactly one of two things:
--     * nil    — "this is not my command." The dispatcher keeps scanning the
--                remaining EXPAND rules in ordinal order. Returning nil is NOT
--                an error; it is the normal path for every statement the module
--                does not recognise.
--     * string — "I matched; here is the rewritten statement." This is
--                TERMINAL for the EXPAND phase: the dispatcher stops scanning
--                EXPAND rules and threads this string forward. (REWRITE-phase
--                rules still run afterwards on the expanded text.)
--   Return the original text only if you genuinely mean "I matched and chose
--   not to change it" — that still terminates EXPAND. To decline, return nil.
--
-- -----------------------------------------------------------------------------
-- 2. Function signature  —  fn(sqltext)
-- -----------------------------------------------------------------------------
--   The dispatcher calls your function as fn(sqltext), passing the CURRENT SQL
--   text (the statement as it stands after any earlier phase/rule). Match
--   against this argument; do not call sqlparsing.getsqltext() yourself.
--
-- -----------------------------------------------------------------------------
-- 3. Do not suppress errors  (the dispatcher is fail-open)
-- -----------------------------------------------------------------------------
--   The MASTER dispatcher wraps every module call in pcall. A thrown error is
--   caught and treated as "skip this rule, keep scanning" (fail-open) — it
--   never blocks the user's statement. Therefore a module must NOT wrap its own
--   logic in defensive pcall/try-catch to hide failures: doing so converts a
--   visible, debuggable fault into a silent wrong-answer. Let errors propagate.
--
-- -----------------------------------------------------------------------------
-- 4. Versioning discipline  —  why DEMO_PING_V1, not DEMO_PING
-- -----------------------------------------------------------------------------
--   Modules are deployed under a version-suffixed name and are never replaced
--   in place. A live module must never be the target of CREATE OR REPLACE,
--   because an in-flight statement could bind the old body while the new body
--   is being compiled. To change behaviour, author DEMO_PING_V2 as a new file
--   and script, repoint the rule, then retire V1 once no rule references it.
--   The _V1 suffix encodes that discipline directly in the deployed name.
--
-- -----------------------------------------------------------------------------
-- 5. DEMO_ prefix  —  permanently safe to leave deployed
-- -----------------------------------------------------------------------------
--   The DEMO_ prefix marks this as a demonstration/reference module, not a
--   product command. It can be installed on every instance and left in place
--   without ever being mistaken for a real, supported command. Product
--   commands carry no DEMO_ prefix.
--
-- -----------------------------------------------------------------------------
-- 6. Import-loading contract  —  how the dispatcher finds your function
-- -----------------------------------------------------------------------------
--   The dispatcher calls import(script_name, alias) to load your module into a
--   global table, then invokes alias[function_name](text). For this file that
--   means:
--
--     import("PREPROC_RT.DEMO_PING_V1", "ping_mod")
--     ping_mod.ping(sqltext)
--
--   Lua's module table is populated from globals defined in the script body.
--   Module authors MUST declare the exported function at top level using the
--   plain global function syntax so that import() can find it in the module's
--   namespace:
--
--     function ping(sqltext) ... end      -- correct: visible in global table
--
--   A local function, a function stored only in a local variable, or a
--   function inside a module table literal (return { ping = ... }) will NOT be
--   found by the dispatcher. The global declaration is the contract.
--
-- -----------------------------------------------------------------------------
-- 7. gsub two-value return trap  —  the single most common authoring mistake
-- -----------------------------------------------------------------------------
--   string.gsub always returns two values: the result string and the match
--   count. When the result is passed to a function or stored inline, Lua
--   silently forwards both values, causing "function expects one parameter"
--   errors or subtle wrong-argument bugs. Always capture the result explicitly:
--
--     local s = text:gsub("^%s+", "")        -- correct: captures only string
--     local s, _ = text:gsub("^%s+", "")     -- also correct: discards count
--
--   Never pass text:gsub(...) directly as a function argument:
--
--     setsqltext(text:gsub("^%s+", ""))       -- WRONG: passes count too
--     return text:gsub("^%s+", "")            -- WRONG: returns two values
--
--   The ping function body in this file uses the correct single-capture form.
--
-- -----------------------------------------------------------------------------
-- 8. Single-statement output requirement
-- -----------------------------------------------------------------------------
--   A matched expansion MUST return exactly one SQL statement as a single
--   string. The framework does not split the returned string on semicolons or
--   newlines; whatever string the module returns is handed to the engine as one
--   statement. Returning multiple statements concatenated in one string will
--   cause a syntax error at the engine.
--
-- -----------------------------------------------------------------------------
-- 9. Engine-syntax-error consequence of non-expansion (fail-open behaviour)
-- -----------------------------------------------------------------------------
--   The EXPAND dispatcher is fail-open: if no module claims the statement, the
--   original text is passed to the engine unchanged. When an operator configures
--   a custom command keyword (e.g. PING) in their EXPAND rules but no module
--   matches it, the engine receives "PING" verbatim, which is not valid SQL, and
--   returns a syntax error.
--
--   This is expected and intentional — it is not a framework bug. The syntax
--   error is a direct consequence of the operator configuring a command rule
--   without a matching module deployment. The fix is to deploy the correct
--   module, not to change the fail-open contract.
-- =============================================================================

-- luacheck: globals ping

-- ping(sqltext) -> "SELECT 'PONG'" when the statement is exactly PING, else nil.
-- Trims surrounding whitespace and compares case-insensitively; any other
-- statement returns nil so the dispatcher continues to the next EXPAND rule.
function ping(sqltext)
    local trimmed = sqltext:gsub("^%s*(.-)%s*$", "%1")
    if string.upper(trimmed) == "PING" then
        return "SELECT 'PONG'"
    end
    return nil
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

EXECUTE SCRIPT PREPROC.RULE_ADD('ROLE', 'PREPROC_ADMIN', 'EXPAND', 'PREPROC_RT.DEMO_PING_V1', 'ping', NULL);

--   PREPROC ADD RULE ROLE PREPROC_ADMIN EXPAND PREPROC_RT.DEMO_PING_V1 ping
