-- =============================================================================
-- MASTER preprocessor dispatcher  (PREPROC_RT.MASTER)
-- =============================================================================
-- Role
--   This is the single global SQL preprocessor entry point for the framework.
--   Once activated (a separate DBA action), Exasol invokes this script for
--   EVERY statement on the system. It is therefore the most safety-critical
--   file in the project: an error here fails the user's statement.
--
--   This script enforces the non-negotiable safety contract (control-statement
--   guard + fail-open lookup), dispatches the caller's resolved pipeline, and
--   then performs a one-time convenience-mode self-optimization swap. The guard
--   and the view lookup are the original, separately tested safety base; the
--   dispatch loop and the swap are layered on top without weakening it (ADR-004).
--
-- Single source of truth (this file IS the dispatch contract)
--   This file's body is the ONE canonical implementation of resolve -> dispatch
--   -> swap. It is deployed verbatim as PREPROC_RT.MASTER, and every retirement
--   stub PREPROC_RT.P_<hash> is created by the refresh path from THIS deployed
--   body (read back from EXA_ALL_SCRIPTS at refresh time, see REFRESH_CORE). A
--   retired profile is therefore byte-for-byte identical to MASTER and re-runs
--   the full resolve -> dispatch -> swap for the current statement; there is no
--   second or third copy of the dispatch logic to drift.
--
--   The earlier import("PREPROC_RT.MASTER_CORE") indirection was abandoned: in
--   the Exasol preprocessor context, the per-rule dynamic import() works only at
--   top-level script scope, never from inside a function of an imported module,
--   so a thin import-wrapper silently fail-opened every dispatch. The dispatch
--   logic MUST live inline at top level here.
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
--      the normal no-rules case (SELF_DISABLE_ALLOWED and MODE still populated
--      from SETTINGS). Fail-open applies only to errors, not to the all-NULL row.
--   3. Per-rule fail-open: each module dispatch is wrapped in pcall, so a
--      failing or missing module silently skips that one rule and dispatch
--      continues with the text unchanged. A broken module never blocks the
--      statement (DEMO_PING_V1 is the first shipped module using this path).
--   4. Convenience-mode self-optimization (ADR-004): the swap is a performance
--      optimization LAYERED on the safety contract. It is gated on
--      mode == 'convenience' and wrapped in pcall, so a swap/self-disable
--      failure leaves the session on its current script and never weakens the
--      safety base. self_disable (no-rules) requires BOTH convenience mode and
--      self_disable_allowed == true (two independent axes). The swap is issued
--      via pquery BEFORE setsqltext so the call order is dispatch -> swap ->
--      commit text.
--   5. setsqltext is called only when dispatch actually changed the text, so a
--      no-op pipeline leaves the statement byte-for-byte as submitted.
--
-- Queries issued from inside the preprocessor are NOT themselves preprocessed,
-- so the MY_PIPELINE lookup and the swap ALTER SESSION carry no recursion risk.
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
-- through untouched and MUST NOT trigger a pipeline lookup. This is also what
-- keeps the swap ALTER SESSION below from being intercepted recursively.
local upper = string.upper(sqltext)
if string.find(upper, "^%s*ALTER%s+SESSION")
    or string.find(upper, "^%s*ALTER%s+SYSTEM") then
    return
end

-- --- Pipeline lookup (the only data access) ---------------------------------
-- Single read of the definer-rights view. Wrapped in pcall so that a thrown
-- error is treated identically to a not-ok result: both fail open. Column order
-- mirrors the view: [1]=SELF_DISABLE_ALLOWED, [2]=MODE, [3]=PHASE,
-- [4]=CLIENT_PATTERN, [5]=SCRIPT_NAME, [6]=FUNCTION_NAME, [7]=ORDINAL,
-- [8]=PROFILE_HASH.
local pcall_ok, pquery_ok, result = pcall(pquery, [[
    SELECT SELF_DISABLE_ALLOWED, MODE, PHASE, CLIENT_PATTERN, SCRIPT_NAME, FUNCTION_NAME, ORDINAL, PROFILE_HASH
    FROM PREPROC_RT.MY_PIPELINE
]])

-- Fail open on lookup errors: pcall caught a throw or pquery reported not-ok.
-- result[1] == nil is defense-in-depth; unreachable while MY_PIPELINE keeps its
-- LEFT JOIN shape (the view always delivers at least one row, even when the user
-- has no rules — that row carries a null phase/script/function sentinel).
if not pcall_ok or not pquery_ok or result == nil or result[1] == nil then
    return
end

-- --- No-rules sentinel ------------------------------------------------------
-- The LEFT JOIN always yields at least one row. When the user has no resolved
-- rules, that single row carries a null PHASE (SELF_DISABLE_ALLOWED and MODE are
-- still populated from SETTINGS). A null PHASE in the first row is the no-rules
-- signal: nothing to dispatch, pass the statement through byte-for-byte.
local mode = result[1][2]
local is_convenience = (mode ~= null and tostring(mode) == "convenience")

if result[1][3] == null then
    -- --- Convenience-mode self-disable (no rules) ---------------------------
    -- Clearing the session preprocessor removes the per-statement MY_PIPELINE
    -- lookup for all subsequent statements (~0 ms/stmt). It requires BOTH
    -- convenience mode (system posture) and self_disable_allowed == true
    -- (per-deployment safety toggle). The ALTER SESSION is wrapped in pcall: any
    -- failure leaves the session on its current script (fail-open). The
    -- statement still passes through unchanged either way.
    local self_disable_allowed = result[1][1]
    if is_convenience
        and self_disable_allowed ~= null
        and tostring(self_disable_allowed) == "true" then
        pcall(pquery, "ALTER SESSION SET sql_preprocessor_script = ''")
    end
    return
end

-- --- Connecting client (for client_pattern matching) ------------------------
-- Resolve the client application identifier once via EXA_ALL_SESSIONS (there
-- is no CURRENT_CLIENT function). A non-null CLIENT_PATTERN on a rule is
-- matched against this. The lookup is best-effort: on any failure we fall back
-- to an empty string, which still lets null-pattern (apply-always) rules run
-- while non-null patterns simply will not match.
local current_client = ""
local cc_pcall_ok, cc_ok, cc_res = pcall(pquery, [[
    SELECT CLIENT FROM EXA_ALL_SESSIONS WHERE SESSION_ID = CURRENT_SESSION
]])
if cc_pcall_ok and cc_ok and cc_res ~= nil
    and cc_res[1] ~= nil and cc_res[1][1] ~= null then
    current_client = tostring(cc_res[1][1])
end

-- --- Dispatch ---------------------------------------------------------------
-- Apply phases in fixed semantic order TRANSLATE → EXPAND → REWRITE. The view's
-- ORDER BY phase is lexical, so it cannot be trusted to sequence phases; only
-- the within-phase ordinal order it provides is meaningful. We therefore walk
-- the phase list explicitly and, for each phase, scan the view rows in their
-- delivered (ordinal-ascending) order, dispatching the rows for that phase.
local phase_order = { "TRANSLATE", "EXPAND", "REWRITE" }

-- A rule's pattern matches when it is null (apply always) or the connecting
-- client contains the pattern as a literal substring (plain string.find, not
-- a Lua pattern match).
local function client_matches(client_pattern)
    if client_pattern == null then
        return true
    end
    return string.find(current_client, tostring(client_pattern), 1, true) ~= nil
end

-- Dispatch one rule, threading the current text through its module function.
-- Exasol Lua loads sibling scripts via import(name, alias) — the alias becomes
-- a global holding the module table (there is no require, and import's return
-- value is nil). Wrapped in pcall: a missing module (import throws) or a
-- throwing/garbage function silently leaves the text unchanged. Returns the
-- (possibly new) text. import() works here because this dispatch runs at
-- top-level script scope (never from inside an imported module's function).
local function dispatch_rule(script_name, function_name, text)
    local dispatch_ok, new_text = pcall(function()
        import(script_name, "preproc_module")
        return preproc_module[function_name](text)
    end)
    if dispatch_ok and type(new_text) == "string" then
        return new_text
    end
    return text
end

-- Dispatch one EXPAND rule under first-match-terminal semantics. Same import
-- pattern as dispatch_rule, but the return contract differs: a string is the
-- expansion (the module owns this statement), and nil means "not my command —
-- keep looking". Crucially, nil is the answer for EVERY non-match cause: the
-- module returned nil, the module returned a non-string (treated as no match,
-- never threaded into setsqltext), OR pcall failed (missing/throwing module —
-- fail-open). dispatch_rule falls back to the original text on failure; here we
-- must NOT, because that text is indistinguishable from a real expansion and
-- would terminate the EXPAND scan on a broken module.
local function dispatch_expand_rule(script_name, function_name, text)
    local dispatch_ok, result_text = pcall(function()
        import(script_name, "preproc_module")
        return preproc_module[function_name](text)
    end)
    if dispatch_ok and type(result_text) == "string" then
        return result_text
    end
    return nil
end

for _, phase in ipairs(phase_order) do
    for _, row in ipairs(result) do
        local row_phase = row[3]
        local script_name = row[5]
        local function_name = row[6]
        -- Only execute rows that belong to this phase and carry a real module
        -- target; the all-null sentinel and any partial row are skipped.
        if row_phase == phase
            and script_name ~= null and function_name ~= null
            and client_matches(row[4]) then
            if phase == "EXPAND" then
                -- First-match-terminal: the first EXPAND module to recognise the
                -- statement (non-nil string) owns the expansion; remaining EXPAND
                -- rules are skipped. A nil means "not my command" (or a broken
                -- module, fail-open) — keep scanning. break leaves only the inner
                -- row loop, so REWRITE still runs on the resulting text.
                local expanded = dispatch_expand_rule(
                    tostring(script_name), tostring(function_name), sqltext)
                if expanded ~= nil then
                    sqltext = expanded
                    break
                end
            else
                -- TRANSLATE and REWRITE: every matching rule runs, threading the
                -- text through; no terminal semantics.
                sqltext = dispatch_rule(tostring(script_name), tostring(function_name), sqltext)
            end
        end
    end
end

-- --- Convenience-mode profile swap (has rules) ------------------------------
-- After dispatching the current statement, repoint a convenience-mode session
-- to its generated profile script PREPROC_RT.P_<hash>, which bakes this user's
-- rule chain in statically and pays no per-statement MY_PIPELINE lookup
-- (~0.5 ms/stmt). profile_hash is null when codegen produced no profile; in that
-- case there is nothing to swap to and the session stays on its current script.
-- The swap is wrapped in pcall: a failure (e.g. the profile script was
-- reclaimed) leaves the session on its current script (fail-open). It is issued
-- BEFORE setsqltext so the order is dispatch → swap → commit text.
local profile_hash = result[1][8]
if is_convenience and profile_hash ~= null then
    pcall(pquery, "ALTER SESSION SET sql_preprocessor_script = PREPROC_RT.P_"
        .. tostring(profile_hash))
end

-- --- Commit rewritten text --------------------------------------------------
-- setsqltext is called only when dispatch changed the text; an unchanged result
-- leaves the statement byte-for-byte as submitted. sqltext is already a string
-- (dispatch_rule guarantees it), so no gsub is needed — avoiding the two-value
-- gsub return trap that would error "expects one parameter" against setsqltext.
if sqltext ~= sqlparsing.getsqltext() then
    sqlparsing.setsqltext(sqltext)
end
