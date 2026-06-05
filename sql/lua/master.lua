-- =============================================================================
-- MASTER preprocessor dispatcher  (PREPROC_RT.MASTER)
-- =============================================================================
-- Role
--   This is the single global SQL preprocessor entry point for the framework.
--   Once activated (a separate DBA action), Exasol invokes this script for
--   EVERY statement on the system. It is therefore the most safety-critical
--   file in the project: an error here fails the user's statement.
--
--   This slice is a SAFE PASS-THROUGH SKELETON. It already enforces the
--   non-negotiable safety contract (control-statement guard + fail-open) but
--   contains NO dispatch logic yet. Real per-phase dispatch lands in a later
--   slice at the extension point marked below; the statement is always
--   returned unchanged here.
--
-- Invoker-rights constraint (why the lookup goes through a view)
--   Exasol Lua scripts run with INVOKER rights: this script can only read what
--   the calling user is granted. It therefore MUST NOT query the PREPROC.*
--   admin tables directly (callers have no grants on them). The only sanctioned
--   data path is the definer-rights view PREPROC_RT.MY_PIPELINE, which resolves
--   the caller's pipeline under the view owner's privileges, scoped to
--   CURRENT_USER. Never widen this to a direct admin-table read.
--
-- Safety contract encoded here
--   1. Guard: ALTER SESSION / ALTER SYSTEM are returned untouched WITHOUT any
--      pipeline lookup (case-insensitive, leading whitespace tolerated).
--   2. Fail open: any lookup failure (thrown error OR not-ok pquery) returns
--      the statement unchanged. A broken framework must never block or corrupt
--      user SQL. Note: MY_PIPELINE uses a LEFT JOIN so it ALWAYS returns at
--      least one row for every user; an all-NULL phase/script/function row is
--      the normal no-rules case (SELF_DISABLE_ALLOWED still populated from
--      SETTINGS). Fail-open applies only to errors, not to the all-NULL row.
--   3. Byte-for-byte pass-through: this slice does not call setsqltext at all,
--      so the original statement text is left exactly as submitted.
--
-- Queries issued from inside the preprocessor are NOT themselves preprocessed,
-- so the MY_PIPELINE lookup carries no recursion risk.
-- =============================================================================

local sqltext = sqlparsing.getsqltext()

-- Defensive fail-open: if the engine hands us anything other than a string,
-- do nothing rather than risk erroring on string operations below.
if type(sqltext) ~= "string" then
    return
end

-- --- Guard: ALTER SESSION / ALTER SYSTEM ------------------------------------
-- Uppercase once, then anchor-match tolerating leading whitespace and any run
-- of whitespace between the keywords. These control statements must pass
-- through untouched and MUST NOT trigger a pipeline lookup.
local upper = string.upper(sqltext)
if string.find(upper, "^%s*ALTER%s+SESSION")
    or string.find(upper, "^%s*ALTER%s+SYSTEM") then
    return
end

-- --- Pipeline lookup (the only data access) ---------------------------------
-- Single read of the definer-rights view. Wrapped in pcall so that a thrown
-- error is treated identically to a not-ok result: both fail open.
local pcall_ok, pquery_ok, result = pcall(pquery, [[
    SELECT SELF_DISABLE_ALLOWED, PHASE, SCRIPT_NAME, FUNCTION_NAME
    FROM PREPROC_RT.MY_PIPELINE
]])

-- Fail open on lookup errors: pcall caught a throw or pquery reported not-ok.
-- result[1] == nil is defense-in-depth; unreachable while MY_PIPELINE keeps its
-- LEFT JOIN shape (the view always delivers at least one row, even when the user
-- has no rules — that row carries a null phase/script/function sentinel).
if not pcall_ok or not pquery_ok or result == nil or result[1] == nil then
    return
end

-- --- Dispatch extension point (later slice) ---------------------------------
-- `result` always holds at least one row (MY_PIPELINE LEFT JOINs SETTINGS to
-- RESOLUTION, so every user gets a row even with zero rules).
-- result[i][1]=SELF_DISABLE_ALLOWED, [2]=PHASE, [3]=SCRIPT_NAME,
-- [4]=FUNCTION_NAME (1-based; SQL NULL is the `null` sentinel, not Lua nil).
-- When PHASE/SCRIPT_NAME/FUNCTION_NAME are all `null` the user has no rules;
-- that row is the no-rules sentinel and must be treated as the self-disable
-- decision input, NOT indexed as a rule to execute. Later slices implementing
-- real dispatch must handle this: check result[1][2] == null (i.e. PHASE is
-- null) before iterating rule rows. Self-disable / profile swap / per-phase
-- dispatch hooks land here in later slices.
--
-- AUTHORING TRAP when this point starts rewriting text: string.gsub returns
-- TWO values (string, count). Passing it straight into setsqltext errors with
-- "expects one parameter". Always bind to a local first, e.g.:
--     local rewritten = string.gsub(sqltext, pat, repl)
--     sqlparsing.setsqltext(rewritten)
--
-- Pass-through slice: return without calling setsqltext, leaving the statement
-- byte-for-byte unchanged.
return
