-- =============================================================================
-- preprocessor-framework — release.sql   (single-file framework deploy)
-- =============================================================================
-- Deploys the COMPLETE framework into a clean Exasol instance: the PREPROC_ADMIN
-- role, the PREPROC (admin) and PREPROC_RT (runtime) schemas, all tables, the
-- MY_PIPELINE view, schema grants, the MASTER dispatcher, the REFRESH/STATUS
-- engine, the full rule/mode admin control surface, and the PREPROC command
-- sugar (CONTROL_SUGAR_V1) so `PREPROC ...` admin commands work out of the box.
-- A final REFRESH resolves the seeded control-sugar rule into RESOLUTION.
--
-- This file deploys NO demo or example preprocessors. Those live in examples/,
-- one per file, each self-registering — add only the ones you want.
--
-- HOW TO RUN
--   Run the whole file in EXAplus or DbVisualiser against a CLEAN database.
--   Plain SQL statements are terminated by `;`. Each Lua script is wrapped in an
--   EXAplus block delimiter: a `--/` on its own line opens the block and a `/` on
--   its own line closes it (required because the Lua bodies contain semicolons).
--   Re-running on an already-installed database will error on CREATE ROLE /
--   CREATE SCHEMA (Exasol has no IF NOT EXISTS for the role); use
--   `preproc install` for idempotent (re-runnable) installs.
--
-- ACTIVATION (deliberately NOT done here — a DBA decision)
--   ALTER SYSTEM SET sql_preprocessor_script = PREPROC_RT.MASTER;
-- =============================================================================

CREATE ROLE PREPROC_ADMIN;

CREATE SCHEMA IF NOT EXISTS PREPROC;

CREATE SCHEMA IF NOT EXISTS PREPROC_RT;

ALTER SCHEMA PREPROC CHANGE OWNER PREPROC_ADMIN;

CREATE TABLE IF NOT EXISTS PREPROC.CONFIG (
    rule_id          DECIMAL(18, 0)  NOT NULL,
    scope_type       VARCHAR(4)      NOT NULL,
    scope_value      VARCHAR(128)    NOT NULL,
    client_pattern   VARCHAR(256),
    phase            VARCHAR(32)     NOT NULL,
    script_name      VARCHAR(128)    NOT NULL,
    function_name    VARCHAR(128)    NOT NULL,
    enabled          BOOLEAN         NOT NULL,
    version          DECIMAL(9, 0)   NOT NULL
);

CREATE TABLE IF NOT EXISTS PREPROC.RESOLUTION (
    user_name        VARCHAR(128)    NOT NULL,
    client_pattern   VARCHAR(256),
    phase            VARCHAR(32)     NOT NULL,
    script_name      VARCHAR(128)    NOT NULL,
    function_name    VARCHAR(128)    NOT NULL,
    ordinal          DECIMAL(9, 0),
    profile_hash     VARCHAR(64)
);

CREATE TABLE IF NOT EXISTS PREPROC.SETTINGS (
    mode                 VARCHAR(32)  NOT NULL,
    self_disable_allowed BOOLEAN      NOT NULL,
    last_refresh         TIMESTAMP,
    state_fingerprint    VARCHAR(64)
);

CREATE TABLE IF NOT EXISTS PREPROC.PROFILES (
    profile_hash  VARCHAR(64)   NOT NULL,
    manifest      VARCHAR(4000) NOT NULL,
    created_at    TIMESTAMP     NOT NULL,
    orphaned_at   TIMESTAMP
);

INSERT INTO PREPROC.SETTINGS (mode, self_disable_allowed, last_refresh)
SELECT 'convenience', FALSE, NULL
WHERE NOT EXISTS (SELECT 1 FROM PREPROC.SETTINGS);

CREATE OR REPLACE VIEW PREPROC_RT.MY_PIPELINE AS
SELECT
    s.self_disable_allowed,
    s.mode,
    r.phase,
    r.client_pattern,
    r.script_name,
    r.function_name,
    r.ordinal,
    r.profile_hash
FROM PREPROC.SETTINGS s
LEFT JOIN PREPROC.RESOLUTION r
    ON r.user_name = CURRENT_USER
ORDER BY r.phase, r.ordinal;

GRANT EXECUTE ON SCHEMA PREPROC_RT TO PUBLIC;

GRANT SELECT ON SCHEMA PREPROC_RT TO PUBLIC;

GRANT SELECT ANY DICTIONARY TO PREPROC_ADMIN;

INSERT INTO PREPROC.CONFIG
    (rule_id, scope_type, scope_value, client_pattern, phase, script_name, function_name, enabled, version)
SELECT
    (SELECT COALESCE(MAX(rule_id), 0) + 1 FROM PREPROC.CONFIG WHERE rule_id < 9000),
    'ROLE',
    'PREPROC_ADMIN',
    NULL,
    'EXPAND',
    'PREPROC_RT.CONTROL_SUGAR_V1',
    'expand',
    TRUE,
    1
WHERE NOT EXISTS (
    SELECT 1 FROM PREPROC.CONFIG WHERE script_name = 'PREPROC_RT.CONTROL_SUGAR_V1'
);

OPEN SCHEMA PREPROC_RT;

--/
CREATE OR REPLACE LUA SCRIPT PREPROC_RT.MASTER() AS
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
/

--/
CREATE OR REPLACE LUA SCRIPT PREPROC_RT.CONTROL_SUGAR_V1() AS
-- =============================================================================
-- CONTROL_SUGAR_V1 preprocessor module  (PREPROC_RT.CONTROL_SUGAR_V1)
-- =============================================================================
-- Canonical authoring reference for PRODUCTION EXPAND-phase command modules.
--
-- This module is the framework dogfooding its own EXPAND phase: it parses the
-- SQL-like `PREPROC …` keyword grammar and rewrites each recognised command into
-- exactly one `EXECUTE SCRIPT PREPROC.<SCRIPT>(...)` call against the admin
-- control scripts. Where DEMO_PING_V1 is the minimal teaching module, this file
-- is the reference for a real, supported command surface — read it before
-- authoring any production command module and copy its shape.
--
-- -----------------------------------------------------------------------------
-- 1. Return contract  (nil = no match, continue; string = matched, terminal)
-- -----------------------------------------------------------------------------
--   An EXPAND module function returns exactly one of two things:
--     * nil    — "this is not my command." The dispatcher keeps scanning the
--                remaining EXPAND rules in ordinal order. Returning nil is the
--                normal, non-error path for every statement the module does not
--                recognise. THIS MODULE returns nil in two distinct cases:
--                  (a) the statement does not begin with the PREPROC keyword —
--                      it is ordinary SQL and none of our business; and
--                  (b) the statement begins with PREPROC but the subcommand is
--                      unrecognised — we fail OPEN, letting the original text
--                      reach the engine so it raises a loud, debuggable syntax
--                      error rather than silently swallowing a typo'd command.
--     * string — "I matched; here is the rewritten statement." TERMINAL for the
--                EXPAND phase: the dispatcher stops scanning EXPAND rules and
--                threads this string forward (REWRITE rules still run after).
--
-- -----------------------------------------------------------------------------
-- 2. Function signature  —  expand(sqltext)
-- -----------------------------------------------------------------------------
--   The dispatcher calls expand(sqltext), passing the CURRENT SQL text (the
--   statement as it stands after any earlier phase/rule). Match against this
--   argument; do not call sqlparsing.getsqltext() yourself.
--
-- -----------------------------------------------------------------------------
-- 3. Do not suppress errors  (the dispatcher is fail-open)
-- -----------------------------------------------------------------------------
--   The MASTER dispatcher wraps every module call in pcall: a thrown error is
--   caught and treated as "skip this rule, keep scanning." A module must NOT
--   wrap its own logic in defensive pcall/try-catch, which would convert a
--   visible fault into a silent wrong-answer. Let errors propagate.
--
-- -----------------------------------------------------------------------------
-- 4. Versioning discipline  —  why CONTROL_SUGAR_V1, not CONTROL_SUGAR
-- -----------------------------------------------------------------------------
--   Dispatched modules are deployed under a version-suffixed name and are never
--   replaced in place. A live module must never be the target of CREATE OR
--   REPLACE, because an in-flight statement could bind the old body while the
--   new body is being compiled. To change the grammar, author CONTROL_SUGAR_V2
--   as a new file and script, repoint the seeded CONFIG rule, then retire V1
--   once no rule references it. The _V1 suffix encodes that discipline in the
--   deployed name. (Admin control scripts in the PREPROC schema are unversioned
--   by contrast — they are EXECUTE SCRIPT targets, not live-bound dispatch
--   modules, so the live-replace hazard does not apply to them.)
--
-- -----------------------------------------------------------------------------
-- 5. Production vs demo  —  why there is NO DEMO_ prefix
-- -----------------------------------------------------------------------------
--   The DEMO_ prefix marks teaching/reference objects that are safe to leave
--   deployed and unmistakable as non-product. CONTROL_SUGAR_V1 is the opposite:
--   it is a SUPPORTED PRODUCT command surface, registered at install time via a
--   seeded CONFIG rule scoped to PREPROC_ADMIN, and operators are expected to
--   rely on it. It therefore carries no DEMO_ prefix and lives under its real,
--   versioned product name. The absence of the prefix is the signal that this
--   module is part of the shipped framework, not a sample.
--
-- -----------------------------------------------------------------------------
-- 6. Import-loading contract  —  how the dispatcher finds expand
-- -----------------------------------------------------------------------------
--   The dispatcher calls import(script_name, alias) to load this module into a
--   global table, then invokes alias[function_name](text):
--
--     import("PREPROC_RT.CONTROL_SUGAR_V1", "sugar_mod")
--     sugar_mod.expand(sqltext)
--
--   Lua's module table is populated from globals defined in the script body, so
--   the exported function MUST be declared at top level with the plain global
--   function syntax. A local function, or a function returned in a table
--   literal, will NOT be found by the dispatcher. The seeded CONFIG rule
--   references script_name='PREPROC_RT.CONTROL_SUGAR_V1', function_name='expand'.
--
-- -----------------------------------------------------------------------------
-- 7. gsub two-value return trap  —  the single most common authoring mistake
-- -----------------------------------------------------------------------------
--   string.gsub always returns TWO values: the result string and the match
--   count. Passed inline to a function or returned directly, Lua forwards both,
--   causing "function expects one parameter" errors or wrong-argument bugs.
--   Always capture the result into a single local first:
--
--     local trimmed = sqltext:gsub("^%s*(.-)%s*$", "%1")   -- correct
--     return sqltext:gsub("^%s*(.-)%s*$", "%1")            -- WRONG: two values
--
--   Every gsub in this file captures into exactly one local.
--
-- -----------------------------------------------------------------------------
-- 8. Single-statement output requirement
-- -----------------------------------------------------------------------------
--   A matched expansion MUST return exactly one SQL statement as a single
--   string. The framework does not split the returned string on semicolons or
--   newlines; whatever string is returned is handed to the engine as ONE
--   statement. This is why a mutating command cannot emit "mutate; REFRESH" —
--   the auto-refresh lives INSIDE the admin script instead. Each PREPROC command
--   here maps to exactly one EXECUTE SCRIPT call, with no trailing semicolon.
--
-- -----------------------------------------------------------------------------
-- 9. NOREFRESH suffix threading  —  how the opt-out reaches the admin script
-- -----------------------------------------------------------------------------
--   Every mutating command (ADD/DROP/ENABLE/DISABLE RULE, SET MODE) accepts an
--   optional trailing NOREFRESH keyword. The admin scripts take an optional
--   trailing boolean parameter that, when true, skips the in-script auto-refresh
--   (for batch workflows that refresh once at the end). The threading rule is
--   purely textual: if the command ends in NOREFRESH, strip that keyword before
--   parsing the rest, then append `, true` as the final argument of the emitted
--   EXECUTE SCRIPT call. Non-mutating commands (LIST RULES, REFRESH, STATUS,
--   SET SELF_DISABLE) do not take the suffix and never receive the extra arg.
--
--   Keyword case-insensitivity vs value case-preservation: PREPROC keywords are
--   matched case-insensitively, but captured VALUES (role/user names, script
--   names, function names, modes) are data destined for the emitted SQL and MUST
--   preserve their original case. This module therefore upper-cases only the
--   keyword tokens it dispatches on and copies value tokens through verbatim.
-- =============================================================================

-- luacheck: globals expand

local CONTROL = {
    rule_add = "PREPROC.RULE_ADD",
    rule_drop = "PREPROC.RULE_DROP",
    rule_enable = "PREPROC.RULE_ENABLE",
    rule_disable = "PREPROC.RULE_DISABLE",
    rule_list = "PREPROC.RULE_LIST",
    refresh = "PREPROC.REFRESH",
    status = "PREPROC.STATUS",
    set_mode = "PREPROC.SET_MODE",
    set_self_disable = "PREPROC.SET_SELF_DISABLE",
}

-- Split a string into whitespace-delimited tokens, preserving original case.
local function tokenize(text)
    local tokens = {}
    for word in string.gmatch(text, "%S+") do
        tokens[#tokens + 1] = word
    end
    return tokens
end

-- Single-quote a value for use as a string literal in the emitted EXECUTE SCRIPT
-- call, escaping any embedded single quote by doubling it (SQL standard).
local function quote(value)
    local escaped = value:gsub("'", "''")
    return "'" .. escaped .. "'"
end

-- Build "EXECUTE SCRIPT <script>" with an optional parenthesised argument list.
-- args is a list of already-formatted argument strings; an empty/nil list emits
-- the bare parameterless form (PREPROC.RULE_LIST, PREPROC.REFRESH, ...).
local function execute_script(script, args)
    if not args or #args == 0 then
        return "EXECUTE SCRIPT " .. script
    end
    return "EXECUTE SCRIPT " .. script .. "(" .. table.concat(args, ", ") .. ")"
end

-- expand(sqltext) -> "EXECUTE SCRIPT PREPROC.<...>" for a recognised PREPROC
-- command, else nil. nil covers both non-PREPROC statements and unrecognised
-- PREPROC subcommands (fail-open: the original text reaches the engine).
function expand(sqltext)
    if type(sqltext) ~= "string" then
        return nil
    end

    -- Trim surrounding whitespace and a single optional trailing semicolon.
    local trimmed = sqltext:gsub("^%s*(.-)%s*$", "%1")
    local without_semicolon = trimmed:gsub(";%s*$", "")
    local clean = without_semicolon:gsub("^%s*(.-)%s*$", "%1")

    local tokens = tokenize(clean)
    if #tokens == 0 then
        return nil
    end
    if string.upper(tokens[1]) ~= "PREPROC" then
        return nil
    end

    -- Detect and strip a trailing NOREFRESH keyword before subcommand parsing.
    local norefresh = false
    if #tokens > 1 and string.upper(tokens[#tokens]) == "NOREFRESH" then
        norefresh = true
        tokens[#tokens] = nil
    end

    -- words after the leading PREPROC keyword
    local function word(index)
        return tokens[index + 1]
    end
    local function upper_word(index)
        local w = tokens[index + 1]
        return w and string.upper(w) or nil
    end
    local subcommand_count = #tokens - 1

    local verb = upper_word(1)

    if verb == "ADD" and upper_word(2) == "RULE" then
        -- PREPROC ADD RULE FOR (ROLE|USER) <v> PHASE <p> SCRIPT <s> FUNCTION <f>
        if subcommand_count ~= 11 then
            return nil
        end
        if upper_word(3) ~= "FOR" then
            return nil
        end
        local scope_type = upper_word(4)
        if scope_type ~= "ROLE" and scope_type ~= "USER" then
            return nil
        end
        if upper_word(6) ~= "PHASE"
            or upper_word(8) ~= "SCRIPT"
            or upper_word(10) ~= "FUNCTION" then
            return nil
        end
        local scope_value = word(5)
        local phase = word(7)
        local script_name = word(9)
        local function_name = word(11)
        local args = {
            quote(scope_type),
            quote(scope_value),
            quote(phase),
            quote(script_name),
            quote(function_name),
        }
        if norefresh then
            args[#args + 1] = "true"
        end
        return execute_script(CONTROL.rule_add, args)
    end

    if verb == "DROP" and upper_word(2) == "RULE" then
        -- PREPROC DROP RULE <rule_id>
        if subcommand_count ~= 3 then
            return nil
        end
        local rule_id = word(3)
        local args = { rule_id }
        if norefresh then
            args[#args + 1] = "true"
        end
        return execute_script(CONTROL.rule_drop, args)
    end

    if verb == "DISABLE" and upper_word(2) == "RULE" then
        if subcommand_count ~= 3 then
            return nil
        end
        local args = { word(3) }
        if norefresh then
            args[#args + 1] = "true"
        end
        return execute_script(CONTROL.rule_disable, args)
    end

    if verb == "ENABLE" and upper_word(2) == "RULE" then
        if subcommand_count ~= 3 then
            return nil
        end
        local args = { word(3) }
        if norefresh then
            args[#args + 1] = "true"
        end
        return execute_script(CONTROL.rule_enable, args)
    end

    if verb == "LIST" and upper_word(2) == "RULES" then
        -- PREPROC LIST RULES  (non-mutating; no NOREFRESH suffix accepted)
        if subcommand_count ~= 2 or norefresh then
            return nil
        end
        return execute_script(CONTROL.rule_list, nil)
    end

    if verb == "REFRESH" then
        if subcommand_count ~= 1 or norefresh then
            return nil
        end
        return execute_script(CONTROL.refresh, nil)
    end

    if verb == "STATUS" then
        if subcommand_count ~= 1 or norefresh then
            return nil
        end
        return execute_script(CONTROL.status, nil)
    end

    if verb == "SET" and upper_word(2) == "MODE" then
        -- PREPROC SET MODE <mode>  (mutating; accepts NOREFRESH)
        if subcommand_count ~= 3 then
            return nil
        end
        local args = { quote(word(3)) }
        if norefresh then
            args[#args + 1] = "true"
        end
        return execute_script(CONTROL.set_mode, args)
    end

    if verb == "SET" and upper_word(2) == "SELF_DISABLE" then
        -- PREPROC SET SELF_DISABLE <bool>  (non-mutating-of-rules; no NOREFRESH)
        if subcommand_count ~= 3 or norefresh then
            return nil
        end
        return execute_script(CONTROL.set_self_disable, { word(3) })
    end

    return nil
end
/

OPEN SCHEMA PREPROC;

--/
CREATE OR REPLACE LUA SCRIPT PREPROC.REFRESH_CORE() AS
-- =============================================================================
-- REFRESH_CORE shared resolution rebuild  (PREPROC.REFRESH_CORE)
-- =============================================================================
-- Role
--   The single source of truth for the resolution rebuild. Materialises the
--   scoped rules in PREPROC.CONFIG (ROLE- and USER-scoped) into the flat,
--   per-user PREPROC.RESOLUTION table that PREPROC_RT.MY_PIPELINE reads, then
--   stamps PREPROC.SETTINGS and commits once. Transitive nested-role expansion
--   is performed in native Exasol SQL (CONNECT BY); this module only
--   orchestrates: it issues the SQL, owns the transaction, and commits.
--
--   Factored out of refresh.lua so that PREPROC.REFRESH and every mutating
--   admin script (RULE_ADD/DROP/ENABLE/DISABLE, SET_MODE) run the IDENTICAL
--   rebuild instead of duplicating the SQL or nesting EXECUTE SCRIPT
--   PREPROC.REFRESH (fragile under Exasol). Callers import this module and
--   invoke rebuild(); they own their own result reporting (exit()).
--
-- Import-loading contract
--   This script is deployed as a plain importable LUA SCRIPT (no RETURNS TABLE).
--   A caller loads it and runs the rebuild via:
--
--     import("PREPROC.REFRESH_CORE", "rc")
--     local rows_inserted = rc.rebuild()
--
--   import() executes this body in the CALLER'S context, so the Exasol globals
--   pquery / error / null are bound. The exported function MUST be declared at
--   top level with plain global function syntax (function rebuild() ... end) so
--   that import() finds it in the module table; a local function would be
--   invisible to the caller.
--
-- Privilege model (invoker rights)
--   Exasol Lua scripts run with INVOKER rights. Reading EXA_DBA_ROLE_PRIVS /
--   EXA_DBA_USERS requires SELECT ANY DICTIONARY, granted to PREPROC_ADMIN. The
--   fingerprint read happens FIRST, so a non-admin caller lacking the privilege
--   fails before any write, leaving PREPROC.RESOLUTION unchanged.
--
-- Resolution model (additive UNION, SQL-privilege style)
--   A user's resolved pipeline is the UNION (superset) of every enabled
--   ROLE-derived rule (via the transitive role closure) and every enabled
--   USER-scoped rule that applies to that user. There is no override or
--   exclusion. Identical (phase, script, function) rows reaching a user by
--   multiple paths are deduplicated, the lowest contributing rule_id winning
--   the ordinal. Ordering within a phase is pure rule_id ascending; scope type
--   is ignored.
--
-- Profile hash manifest (content-addressed profile scripts)
--   After RESOLUTION is reinserted, every row of a user's chain is stamped with a
--   profile_hash: HASH_MD5 over the user's ordered manifest. The manifest is the
--   per-user LISTAGG of 'phase|ordinal|script_name|function_name|client_pattern'
--   joined with '||', ordered by (phase, ordinal). It is deterministic and
--   order-sensitive: two users with identical rules in a different ordinal order,
--   or differing client_patterns, hash to distinct profiles. The same manifest is
--   stored in PREPROC.PROFILES and the hash names the generated PREPROC_RT.P_<hash>
--   profile script, so a session pinned to a hash always finds a matching script.
--
-- Atomicity (the invariant this module exists to centralise)
--   The fingerprint is captured FIRST (so STATUS, recomputing the identical SQL
--   afterwards, reports "current"). Then the rebuild -- DELETE all of
--   RESOLUTION, reinsert resolved rows, stamp each row's profile_hash, generate
--   the per-hash profile scripts, upsert PREPROC.PROFILES, retire then reclaim
--   stale profiles, stamp last_refresh + fingerprint -- runs as one transaction
--   committed once at the end. No reader can observe an emptied RESOLUTION
--   paired with an old last_refresh, nor a populated RESOLUTION paired with a
--   stale stamp, nor a profile_hash that references a not-yet-created profile
--   script.
--
-- Profile lifecycle (generate -> retire -> reclaim, in fixed order)
--   A profile script outlives the chain that created it: a session may be
--   pinned (via convenience-mode self-swap) to PREPROC_RT.P_<hash> long after
--   the rules that produced <hash> are gone. The rebuild therefore manages the
--   full lifecycle, in this strict phase order, all before the single COMMIT:
--
--     GENERATE  CREATE OR REPLACE the real baked body for every CURRENT chain,
--               then clear orphaned_at for any re-adopted hash (a hash that had
--               been retired but is referenced by a current RESOLUTION row again
--               -- its real body is restored above and it must no longer be
--               treated as orphaned). After this, PROFILES is upserted for the
--               current hashes.
--     RETIRE    For each PROFILES hash NOT referenced by any current RESOLUTION
--               row AND not already retired (orphaned_at IS NULL), CREATE OR
--               REPLACE the script with the retirement STUB body (PREPROC_RT.
--               MASTER's own body, read from the catalog) and stamp orphaned_at
--               = CURRENT_TIMESTAMP. A pinned session that next executes a
--               statement re-runs the full MASTER resolve -> dispatch -> swap and
--               re-points itself -- no untransformed gap. Already-retired hashes
--               are excluded by the orphaned_at filter so their original
--               orphaned_at is never re-stamped.
--     RECLAIM   For each retired hash that is provably unpinned, DROP SCRIPT
--               PREPROC_RT.P_<hash> and delete its PROFILES row.
--
--   Never-same-refresh-drop (the hard safety invariant)
--     A profile is NEVER dropped on the refresh that orphans it. RETIRE only
--     stamps; RECLAIM only drops hashes retired on a PRIOR refresh. This is
--     enforced by construction: reclaim_cutoff is captured as CURRENT_TIMESTAMP
--     at the TOP of rebuild() (before any retire), and RECLAIM considers only
--     hashes whose orphaned_at < reclaim_cutoff -- strictly before this refresh
--     began. A hash retired in THIS rebuild has orphaned_at >= reclaim_cutoff
--     and is excluded regardless of session visibility, so it cannot be dropped
--     in the same refresh that orphaned it.
--
--   Login-time reclaim criterion (provably unpinned)
--     A retired hash is reclaimable only when no active session could still be
--     pinned to it. A session pinned to P_<hash> must have logged in BEFORE the
--     hash was retired (you cannot pin to a script that was already a stub at
--     login -- the self-swap targets the current target, not a stub). So the
--     criterion is: NO active EXA_ALL_SESSIONS row has LOGIN_TIME < orphaned_at.
--     Zero active sessions older than orphaned_at => reclaimable (the NOT EXISTS
--     is vacuously true).
--
--   DDL implicit-commit reasoning (why reclaim cannot strand a reader)
--     Each CREATE OR REPLACE / DROP SCRIPT implicitly commits preceding DML, so
--     the single-COMMIT story is nuanced. The invariant that MUST hold is: no
--     COMMITTED RESOLUTION row references a hash whose script was dropped.
--     RECLAIM drops only hashes ABSENT from the current RESOLUTION (orphaned on a
--     prior refresh, not re-adopted), and the current RESOLUTION was already
--     INSERTed above, so a reader resolving the current RESOLUTION never names a
--     reclaimed hash. The DROP's implicit commit of the pending PROFILES delete
--     is therefore safe: the committed RESOLUTION and the dropped script are
--     disjoint by hash.
--
-- Return contract
--   rebuild() returns rows_inserted (a number), the authoritative count of rows
--   materialised into RESOLUTION. It does NOT call exit(): result reporting (and
--   reading last_refresh back) belongs to each caller, because the result shape
--   differs per caller (REFRESH returns rows_inserted + last_refresh; an admin
--   script returns its own outcome). On any failure rebuild() raises via error()
--   so the caller's transaction is left uncommitted up to that point.
-- =============================================================================

-- luacheck: globals rebuild

-- State fingerprint over the resolution inputs (the user universe and the full
-- role-grant graph). Shared verbatim with PREPROC.STATUS so writer and reader
-- compute it identically: an MD5 over the '|'-joined, sorted identifiers.
local FINGERPRINT_SQL = [[
    SELECT HASH_MD5(
        LISTAGG(ident, '|') WITHIN GROUP (ORDER BY ident)
    ) AS fp
    FROM (
        SELECT USER_NAME AS ident FROM EXA_DBA_USERS
        UNION ALL
        SELECT GRANTED_ROLE || ':' || GRANTEE AS ident FROM EXA_DBA_ROLE_PRIVS
    ) t
]]

-- Transitive role-to-user closure JOIN enabled CONFIG, UNIONed with USER-scoped
-- rules, deduplicated to one row per (user, client_pattern, phase, script,
-- function) keeping the lowest contributing rule_id, with a per-(user, phase)
-- ordinal in rule_id-ascending order.
--
-- Closure semantics: in EXA_DBA_ROLE_PRIVS, GRANTED_ROLE is granted to GRANTEE.
-- Walking PRIOR GRANTEE = GRANTED_ROLE descends the grant chain to arbitrary
-- nesting depth. There is deliberately NO START WITH: Exasol forbids IN
-- subqueries outside WHERE clauses, so every row roots a chain and the full
-- (origin_role, transitive_grantee) closure is produced; the JOIN against
-- enabled ROLE-scoped CONFIG then keeps only the origins that matter. Joining
-- EXA_DBA_USERS on USER_NAME = GRANTEE keeps only the users at the leaves
-- (other roles in the chain are intermediate, not output). CONNECT BY NOCYCLE
-- is defensive: a role-grant cycle would otherwise abort the whole refresh.
local RESOLUTION_SQL = [[
    WITH scoped_rules AS (
        SELECT
            closure.grantee      AS user_name,
            cfg.client_pattern   AS client_pattern,
            cfg.phase            AS phase,
            cfg.script_name      AS script_name,
            cfg.function_name    AS function_name,
            cfg.rule_id          AS rule_id
        FROM (
            SELECT CONNECT_BY_ROOT GRANTED_ROLE AS origin_role,
                   GRANTEE                      AS grantee
            FROM EXA_DBA_ROLE_PRIVS
            CONNECT BY NOCYCLE PRIOR GRANTEE = GRANTED_ROLE
        ) closure
        JOIN PREPROC.CONFIG cfg
            ON cfg.scope_type = 'ROLE'
           AND cfg.enabled = TRUE
           AND cfg.scope_value = closure.origin_role
        JOIN EXA_DBA_USERS u
            ON u.user_name = closure.grantee
        UNION ALL
        SELECT
            cfg.scope_value      AS user_name,
            cfg.client_pattern   AS client_pattern,
            cfg.phase            AS phase,
            cfg.script_name      AS script_name,
            cfg.function_name    AS function_name,
            cfg.rule_id          AS rule_id
        FROM PREPROC.CONFIG cfg
        WHERE cfg.scope_type = 'USER' AND cfg.enabled = TRUE
    ),
    deduped AS (
        SELECT
            user_name,
            client_pattern,
            phase,
            script_name,
            function_name,
            MIN(rule_id) AS min_rule_id
        FROM scoped_rules
        GROUP BY user_name, client_pattern, phase, script_name, function_name
    )
    SELECT
        user_name,
        client_pattern,
        phase,
        script_name,
        function_name,
        ROW_NUMBER() OVER (
            PARTITION BY user_name, phase
            ORDER BY min_rule_id ASC
        ) AS ordinal
    FROM deduped
]]

-- Per-user manifest fragment, reused for both the profile_hash UPDATE and the
-- PROFILES upsert so the stored manifest and the hash describe the identical
-- ordered chain. ORDER BY (phase, ordinal) makes the manifest deterministic and
-- order-sensitive; COALESCE(client_pattern, '') keeps a NULL pattern stable.
local MANIFEST_EXPR = [[
    LISTAGG(
        phase || '|' || CAST(ordinal AS VARCHAR(20)) || '|' ||
        script_name || '|' || function_name || '|' ||
        COALESCE(client_pattern, ''), '||'
    ) WITHIN GROUP (ORDER BY phase, ordinal)
]]

-- Stamp every RESOLUTION row of a user with HASH_MD5 over that user's manifest.
-- Exasol rejects a correlated scalar subquery in an UPDATE ... SET that reads the
-- target table, so the per-user hash is computed once in a USING-derived table
-- and joined back via MERGE on user_name. The aggregate yields one hash per user,
-- so all rows in a chain carry the same profile_hash; users with no rows are
-- untouched (no empty-chain hash).
local HASH_UPDATE_SQL = [[
    MERGE INTO PREPROC.RESOLUTION r
    USING (
        SELECT user_name, HASH_MD5(]] .. MANIFEST_EXPR .. [[) AS h
        FROM PREPROC.RESOLUTION
        GROUP BY user_name
    ) s
    ON r.user_name = s.user_name
    WHEN MATCHED THEN UPDATE SET r.profile_hash = s.h
]]

-- One (profile_hash, manifest) row per distinct chain. Computed per user then
-- DISTINCT-collapsed so two users sharing a chain yield a single profile.
local PROFILE_ROWS_SQL = [[
    SELECT DISTINCT profile_hash, manifest
    FROM (
        SELECT profile_hash, ]] .. MANIFEST_EXPR .. [[ AS manifest
        FROM PREPROC.RESOLUTION
        WHERE profile_hash IS NOT NULL
        GROUP BY profile_hash, user_name
    )
]]

-- The distinct rule rows of one chain, in dispatch order. Selected per hash and
-- serialised into the baked rule table of that hash's profile script. DISTINCT
-- collapses the (per-user) RESOLUTION rows of a shared chain to the single set
-- of rules that defines the chain; ORDER BY (phase, ordinal) makes the baked
-- table deterministic and yields ordinal-ascending order within each phase, on
-- which the generated dispatch loop relies.
local PROFILE_RULES_SQL = [[
    SELECT DISTINCT phase, ordinal, script_name, function_name, client_pattern
    FROM PREPROC.RESOLUTION
    WHERE profile_hash = :hash
    ORDER BY phase, ordinal
]]

-- Profile script body, in two halves bracketing the baked rule table. The body
-- is a STATIC mirror of the MASTER dispatcher (guard, EXA_ALL_SESSIONS client
-- lookup, client_matches, TRANSLATE->EXPAND->REWRITE loop with per-rule pcall
-- fail-open and EXPAND first-match-terminal) but reads its rules from the baked
-- `rules` literal instead of querying PREPROC_RT.MY_PIPELINE -- that absent
-- pquery per statement is the whole point of the zero-overhead session path.
--
-- Stored as level-1 long strings (`[=[ ... ]=]`) so the embedded level-0
-- (`[[ ... ]]`) SQL literal needs no escaping. The first line is the luacheck
-- globals declaration so the generated body validates standalone; the baked
-- rule table is spliced between the two halves.
--
-- client_matches here compares against Lua `nil` (the baked table stores nil for
-- a NULL pattern), NOT the Exasol `null` sentinel MASTER tests, because the rule
-- data is a static Lua table, not a live view row.
local PROFILE_BODY_PREFIX = [=[-- luacheck: globals sqlparsing pquery null import
local sqltext = sqlparsing.getsqltext()
if type(sqltext) ~= "string" then return end
local upper = string.upper(sqltext)
if string.find(upper, "^%s*ALTER%s+SESSION")
    or string.find(upper, "^%s*ALTER%s+SYSTEM") then
    return
end
local rules = {]=]

local PROFILE_BODY_SUFFIX = [=[}
local current_client = ""
local cc_pcall_ok, cc_ok, cc_res = pcall(pquery, [[
    SELECT CLIENT FROM EXA_ALL_SESSIONS WHERE SESSION_ID = CURRENT_SESSION
]])
if cc_pcall_ok and cc_ok and cc_res ~= nil
    and cc_res[1] ~= nil and cc_res[1][1] ~= null then
    current_client = tostring(cc_res[1][1])
end
local function client_matches(cp)
    if cp == nil then return true end
    return string.find(current_client, cp, 1, true) ~= nil
end
local function dispatch_rule(sn, fn, text)
    local ok, t = pcall(function()
        import(sn, "preproc_module")
        return preproc_module[fn](text)
    end)
    if ok and type(t) == "string" then return t end
    return text
end
local function dispatch_expand_rule(sn, fn, text)
    local ok, t = pcall(function()
        import(sn, "preproc_module")
        return preproc_module[fn](text)
    end)
    if ok and type(t) == "string" then return t end
    return nil
end
local phase_order = {"TRANSLATE", "EXPAND", "REWRITE"}
for _, phase in ipairs(phase_order) do
    for _, rule in ipairs(rules) do
        if rule.phase == phase and client_matches(rule.client_pattern) then
            if phase == "EXPAND" then
                local expanded = dispatch_expand_rule(rule.script_name, rule.function_name, sqltext)
                if expanded ~= nil then
                    sqltext = expanded
                    break
                end
            else
                sqltext = dispatch_rule(rule.script_name, rule.function_name, sqltext)
            end
        end
    end
end
if sqltext ~= sqlparsing.getsqltext() then
    sqlparsing.setsqltext(sqltext)
end
]=]

-- The deployed body of PREPROC_RT.MASTER, read back from the catalog. The
-- retirement stub IS this body verbatim, so a retired profile is byte-for-byte
-- identical to MASTER: a session still pinned to P_<hash> re-runs the full
-- resolve -> dispatch -> swap for its NEXT statement, correctly transforming
-- that statement under the CURRENT rules and re-pointing itself via ALTER SESSION
-- to the correct current target. Sourcing the stub from the single deployed
-- MASTER (rather than a copy baked here) keeps ONE source of truth and makes
-- "stub == MASTER" true by construction; a change to the dispatch contract in
-- master.lua propagates to every stub created or refreshed afterwards. The
-- import("MASTER_CORE") wrapper was abandoned: dynamic import() works only at
-- top-level preprocessor scope, so the dispatch logic lives inline in MASTER and
-- the stub must carry that same inline body.
--
-- SCRIPT_TEXT is the full CREATE statement: its single first line is
-- `CREATE LUA SCRIPT "MASTER" () RETURNS ROWCOUNT AS`, and the script body
-- (which is what a CREATE OR REPLACE ... AS expects) is everything after the
-- first newline. The empty parameter list guarantees the header is one line, so
-- splitting on the first newline is a robust strip.
local MASTER_TEXT_SQL = [[
    SELECT SCRIPT_TEXT FROM EXA_ALL_SCRIPTS
    WHERE SCRIPT_SCHEMA = 'PREPROC_RT' AND SCRIPT_NAME = 'MASTER'
]]

-- master_stub_body() -> the MASTER script body to deploy as a retirement stub.
--   Reads PREPROC_RT.MASTER's catalog text and strips the CREATE header,
--   leaving the body. Raises if MASTER is absent or its text is unreadable, so a
--   refresh that cannot source a faithful stub fails loudly (uncommitted) rather
--   than installing a broken or empty stub.
--
--   The header ends with the keyword AS as the last token on its line; the body
--   starts on the next line. Anchoring the strip on that ` AS\n` (rather than the
--   first newline) tolerates a header that some Exasol version might render
--   across more than one line -- a first-newline strip would leave a dangling
--   `RETURNS ROWCOUNT AS` fragment and produce a syntactically broken stub. As a
--   final guard, the stripped body MUST NOT still begin with a CREATE/header
--   remnant, so a layout this anchor did not anticipate fails loudly here instead
--   of deploying a broken stub.
local function master_stub_body()
    local ok, res = pquery(MASTER_TEXT_SQL)
    if not ok then
        error("PREPROC.REFRESH_CORE: reading MASTER script text failed: "
            .. tostring(res.error_message))
    end
    if res[1] == nil or res[1][1] == null then
        error("PREPROC.REFRESH_CORE: PREPROC_RT.MASTER text not found; "
            .. "cannot source retirement stub body")
    end
    local master_text = tostring(res[1][1])
    -- Find the first ` AS` that is the last token on its line (the CREATE header
    -- terminator); the body is everything after that line's newline.
    local _, as_newline = string.find(master_text, "%sAS%s-\n")
    if as_newline == nil then
        error("PREPROC.REFRESH_CORE: MASTER script text has no 'AS' header terminator")
    end
    local body = string.sub(master_text, as_newline + 1)
    if string.find(body, "^%s*CREATE")
        or string.find(string.upper(body), "^%s*RETURNS") then
        error("PREPROC.REFRESH_CORE: stripped MASTER body still carries a CREATE header remnant")
    end
    return body
end

-- build_profile_body(rule_rows) -> Lua source for one P_<hash> script.
--   Serialises the chain's rule rows into the baked `rules` table and splices it
--   between the dispatch-template halves. Each row becomes a Lua table element
--   {phase=..,ordinal=..,script_name=..,function_name=..,client_pattern=..}.
--   string.format("%q") double-quotes and escapes every string field, so an odd
--   script/function name (operator-supplied) cannot break out of the literal; a
--   NULL client_pattern (Exasol `null`) is emitted as the Lua keyword nil so the
--   generated client_matches treats it as apply-always.
local function build_profile_body(rule_rows)
    local rule_parts = {}
    for _, r in ipairs(rule_rows) do
        local cp
        if r[5] == null then
            cp = "nil"
        else
            cp = string.format("%q", tostring(r[5]))
        end
        table.insert(rule_parts, string.format(
            "{phase=%q,ordinal=%d,script_name=%q,function_name=%q,client_pattern=%s}",
            tostring(r[1]), tonumber(r[2]), tostring(r[3]), tostring(r[4]), cp
        ))
    end
    return PROFILE_BODY_PREFIX
        .. table.concat(rule_parts, ",\n")
        .. PROFILE_BODY_SUFFIX
end

-- rebuild() -> rows_inserted
--   Performs the atomic resolution rebuild and returns the authoritative count
--   of rows materialised into PREPROC.RESOLUTION. Raises via error() on any
--   failure; never calls exit() (the caller owns result reporting). The step
--   ordering below IS the atomicity contract and the profile lifecycle contract
--   and must not be reordered:
--
--     fingerprint -> reclaim_cutoff -> DELETE/INSERT RESOLUTION -> hash UPDATE
--       -> GENERATE (real bodies + clear re-adopted orphaned_at)
--       -> PROFILES upsert -> RETIRE -> RECLAIM -> stamp SETTINGS -> COMMIT
--
--   Profile codegen (hash stamp, CREATE SCRIPT, PROFILES upsert) runs after the
--   RESOLUTION insert and before the stamp + COMMIT, so no committed RESOLUTION
--   row ever references a profile script that does not yet exist. RETIRE runs
--   after GENERATE + upsert so a re-adopted hash is already restored to its real
--   body and is excluded from retirement. RECLAIM runs last (before the stamp)
--   so it sees the final orphaned_at set, and only drops hashes orphaned on a
--   prior refresh (orphaned_at < reclaim_cutoff) -- see the module header for the
--   never-same-refresh-drop and login-time reclaim reasoning.
function rebuild()
    -- --- Step 1: capture the fingerprint of the current input state ----------
    -- Run before the rebuild so the stamped fingerprint describes exactly the
    -- dictionary state the resolution was built from. A failure here (e.g. a
    -- non-admin caller lacking SELECT ANY DICTIONARY) surfaces before any write.
    local fp_ok, fp_res = pquery(FINGERPRINT_SQL)
    if not fp_ok then
        error("PREPROC.REFRESH_CORE: fingerprint computation failed: " .. tostring(fp_res.error_message))
    end
    local fingerprint = fp_res[1][1]
    if fingerprint == null then
        fingerprint = ""
    end

    -- --- Step 1b: capture the reclaim cutoff (BEFORE any write or retire) -----
    -- The instant this refresh began. RECLAIM (Step 8) only drops hashes whose
    -- orphaned_at < this cutoff -- i.e. hashes retired on a PRIOR refresh. Any
    -- hash retired by THIS refresh's RETIRE phase is stamped with a later
    -- CURRENT_TIMESTAMP and is therefore excluded, making never-same-refresh-drop
    -- true by construction, independent of which sessions EXA_ALL_SESSIONS shows.
    local cutoff_ok, cutoff_res = pquery("SELECT CURRENT_TIMESTAMP AS c")
    if not cutoff_ok then
        error("PREPROC.REFRESH_CORE: capturing reclaim cutoff failed: " .. tostring(cutoff_res.error_message))
    end
    local reclaim_cutoff = cutoff_res[1][1]

    -- --- Step 2: atomic rebuild (single transaction, committed once) ---------
    local del_ok, del_res = pquery("DELETE FROM PREPROC.RESOLUTION")
    if not del_ok then
        error("PREPROC.REFRESH_CORE: clearing RESOLUTION failed: " .. tostring(del_res.error_message))
    end

    local ins_ok, ins_res = pquery(
        [[INSERT INTO PREPROC.RESOLUTION
            (user_name, client_pattern, phase, script_name, function_name, ordinal)
        ]] .. RESOLUTION_SQL
    )
    if not ins_ok then
        error("PREPROC.REFRESH_CORE: inserting resolved rows failed: " .. tostring(ins_res.error_message))
    end
    local row_count = ins_res.rows_inserted

    -- Count back the materialised rows as the reported outcome. rows_inserted is
    -- the authoritative count; the COUNT(*) is defence-in-depth should the driver
    -- not populate rows_inserted for an INSERT...SELECT.
    if row_count == nil then
        local cnt_ok, cnt_res = pquery("SELECT COUNT(*) AS c FROM PREPROC.RESOLUTION")
        if not cnt_ok then
            error("PREPROC.REFRESH_CORE: counting resolved rows failed: " .. tostring(cnt_res.error_message))
        end
        row_count = cnt_res[1][1]
    end

    -- --- Step 3: stamp each RESOLUTION row with its chain's profile_hash ------
    -- Runs after the INSERT and before any codegen so the freshly materialised
    -- rows carry the content-addressed hash that names their profile script.
    local hash_ok, hash_res = pquery(HASH_UPDATE_SQL)
    if not hash_ok then
        error("PREPROC.REFRESH_CORE: stamping profile hashes failed: " .. tostring(hash_res.error_message))
    end

    -- --- Step 4: gather the distinct (hash, manifest) profiles to generate ----
    local prof_ok, prof_res = pquery(PROFILE_ROWS_SQL)
    if not prof_ok then
        error("PREPROC.REFRESH_CORE: collecting profile chains failed: " .. tostring(prof_res.error_message))
    end

    -- --- Step 5: GENERATE the per-hash profile scripts ------------------------
    -- CREATE OR REPLACE is idempotent: an unchanged chain re-creates the same
    -- P_<hash> with the same body, so a re-refresh never drops a pinned script.
    -- The hash is HASH_MD5 hex from the dictionary (not user input), so it is a
    -- safe identifier suffix. The body bakes the chain's rules into a static Lua
    -- table dispatched by an inline mirror of MASTER, so a session pinned to
    -- P_<hash> runs no per-statement MY_PIPELINE lookup.
    for _, prof_row in ipairs(prof_res) do
        local hash = tostring(prof_row[1])

        -- Step 5a: read the chain's rules in dispatch (phase, ordinal) order.
        local rules_ok, rules_res = pquery(PROFILE_RULES_SQL, {hash = hash})
        if not rules_ok then
            error("PREPROC.REFRESH_CORE: reading rules for P_" .. hash
                .. " failed: " .. tostring(rules_res.error_message))
        end

        -- Step 5b/5c: serialise the rules into the baked table and splice it
        -- into the dispatch template to form the full script body.
        local script_body = build_profile_body(rules_res)

        local create_ok, create_res = pquery(
            "CREATE OR REPLACE LUA SCRIPT PREPROC_RT.P_" .. hash .. " AS\n" .. script_body
        )
        if not create_ok then
            error("PREPROC.REFRESH_CORE: creating profile script P_" .. hash
                .. " failed: " .. tostring(create_res.error_message))
        end
    end

    -- --- Step 5d: clear orphaned_at for re-adopted hashes --------------------
    -- A hash that was retired on a prior refresh (orphaned_at non-NULL) but is
    -- referenced by a current RESOLUTION row again has just had its REAL baked
    -- body restored by the GENERATE loop above. It is no longer orphaned, so
    -- clear orphaned_at back to NULL. This is set-based over every re-adopted
    -- hash and runs BEFORE RETIRE, so RETIRE (which targets hashes NOT in the
    -- current RESOLUTION) cannot re-retire it, and RECLAIM cannot drop it.
    local readopt_ok, readopt_res = pquery([[
        UPDATE PREPROC.PROFILES SET orphaned_at = NULL
        WHERE orphaned_at IS NOT NULL
          AND profile_hash IN (
              SELECT DISTINCT profile_hash FROM PREPROC.RESOLUTION
              WHERE profile_hash IS NOT NULL
          )
    ]])
    if not readopt_ok then
        error("PREPROC.REFRESH_CORE: clearing re-adopted orphaned_at failed: "
            .. tostring(readopt_res.error_message))
    end

    -- --- Step 6: upsert PREPROC.PROFILES for each current chain ---------------
    -- MERGE inserts a row for a newly-seen hash and is a no-op for one already
    -- recorded (re-adopt clears orphaned_at in Group 6, not here).
    for _, prof_row in ipairs(prof_res) do
        local hash = tostring(prof_row[1])
        local manifest = prof_row[2]
        local merge_ok, merge_res = pquery(
            [[MERGE INTO PREPROC.PROFILES p
                USING (SELECT :hash AS profile_hash, :manifest AS manifest,
                              CURRENT_TIMESTAMP AS created_at) s
                ON p.profile_hash = s.profile_hash
                WHEN NOT MATCHED THEN
                    INSERT (profile_hash, manifest, created_at)
                    VALUES (s.profile_hash, s.manifest, s.created_at)]],
            {hash = hash, manifest = manifest}
        )
        if not merge_ok then
            error("PREPROC.REFRESH_CORE: upserting PROFILES for P_" .. hash
                .. " failed: " .. tostring(merge_res.error_message))
        end
    end

    -- --- Step 7: RETIRE profiles whose chains are gone -----------------------
    -- A PROFILES hash NOT referenced by any current RESOLUTION row, and not
    -- already retired (orphaned_at IS NULL), is orphaned by this refresh. Rather
    -- than drop it (a session may still be pinned to it), replace its body with
    -- the MASTER stub body (MASTER's own inline body, read from the catalog) and
    -- stamp orphaned_at = CURRENT_TIMESTAMP. The
    -- WHERE clause excludes already-retired hashes, so their original orphaned_at
    -- is never re-stamped. The NOT IN subquery filters profile_hash IS NOT NULL
    -- so a stray NULL cannot make NOT IN vacuously empty.
    local retire_ok, retire_res = pquery([[
        SELECT profile_hash FROM PREPROC.PROFILES
        WHERE orphaned_at IS NULL
          AND profile_hash NOT IN (
              SELECT DISTINCT profile_hash FROM PREPROC.RESOLUTION
              WHERE profile_hash IS NOT NULL
          )
    ]])
    if not retire_ok then
        error("PREPROC.REFRESH_CORE: selecting profiles to retire failed: "
            .. tostring(retire_res.error_message))
    end
    -- Source the stub body once (it is MASTER's body, identical for every stub)
    -- and only when there is at least one hash to retire, so a refresh that
    -- retires nothing never reads the catalog.
    local stub_body = nil
    if retire_res[1] ~= nil then
        stub_body = master_stub_body()
    end
    for _, retire_row in ipairs(retire_res) do
        local hash = tostring(retire_row[1])

        local stub_ok, stub_res = pquery(
            "CREATE OR REPLACE LUA SCRIPT PREPROC_RT.P_" .. hash .. " AS\n" .. stub_body
        )
        if not stub_ok then
            error("PREPROC.REFRESH_CORE: retiring profile script P_" .. hash
                .. " failed: " .. tostring(stub_res.error_message))
        end

        local stamp_ok, stamp_res = pquery(
            "UPDATE PREPROC.PROFILES SET orphaned_at = CURRENT_TIMESTAMP WHERE profile_hash = :hash",
            {hash = hash}
        )
        if not stamp_ok then
            error("PREPROC.REFRESH_CORE: stamping orphaned_at for P_" .. hash
                .. " failed: " .. tostring(stamp_res.error_message))
        end
    end

    -- --- Step 8: RECLAIM provably-unpinned retired profiles ------------------
    -- A retired hash is reclaimable when it was retired on a PRIOR refresh
    -- (orphaned_at < reclaim_cutoff -- excludes anything RETIRE just stamped,
    -- enforcing never-same-refresh-drop) AND no active session could still be
    -- pinned to it (no EXA_ALL_SESSIONS row logged in before it was retired). A
    -- pinned session must have logged in before the orphaning, so LOGIN_TIME <
    -- orphaned_at is exactly the could-be-pinned test; zero qualifying sessions
    -- makes the NOT EXISTS vacuously true => reclaimable.
    local reclaim_ok, reclaim_res = pquery([[
        SELECT p.profile_hash FROM PREPROC.PROFILES p
        WHERE p.orphaned_at IS NOT NULL
          AND p.orphaned_at < :cutoff
          AND NOT EXISTS (
              SELECT 1 FROM EXA_ALL_SESSIONS s
              WHERE s.LOGIN_TIME < p.orphaned_at
          )
    ]], {cutoff = reclaim_cutoff})
    if not reclaim_ok then
        error("PREPROC.REFRESH_CORE: selecting profiles to reclaim failed: "
            .. tostring(reclaim_res.error_message))
    end
    for _, reclaim_row in ipairs(reclaim_res) do
        local hash = tostring(reclaim_row[1])

        -- DROP the script first, then delete the PROFILES row ONLY once the
        -- script is confirmed gone. The PROFILES row is the sole record that a
        -- retired hash still needs reclaiming; deleting it before the DROP truly
        -- succeeded would strand the script forever (orphaned, untracked, never
        -- re-examined). A DROP failure because the script is already absent is
        -- the normal idempotent case and still reclaims; a DROP failure with the
        -- script still present (e.g. a transient lock) leaves the PROFILES row
        -- and its orphaned_at intact, so the hash is retried on the next refresh.
        -- Both the DROP and the delete target a hash absent from the current
        -- (already-inserted) RESOLUTION, so no committed reader can name a
        -- reclaimed hash regardless of the DROP's implicit commit.
        pquery("DROP SCRIPT PREPROC_RT.P_" .. hash)

        local still_ok, still_res = pquery(
            [[SELECT COUNT(*) AS c FROM EXA_ALL_SCRIPTS
              WHERE SCRIPT_SCHEMA = 'PREPROC_RT' AND UPPER(SCRIPT_NAME) = UPPER(:n)]],
            {n = "P_" .. hash}
        )
        if not still_ok then
            error("PREPROC.REFRESH_CORE: verifying drop of P_" .. hash
                .. " failed: " .. tostring(still_res.error_message))
        end
        if still_res[1][1] == 0 then
            local del_prof_ok, del_prof_res = pquery(
                "DELETE FROM PREPROC.PROFILES WHERE profile_hash = :hash",
                {hash = hash}
            )
            if not del_prof_ok then
                error("PREPROC.REFRESH_CORE: deleting PROFILES row for P_" .. hash
                    .. " failed: " .. tostring(del_prof_res.error_message))
            end
        end
    end

    -- --- Step 9: stamp SETTINGS, then the single COMMIT ----------------------
    local stamp_ok, stamp_res = pquery(
        "UPDATE PREPROC.SETTINGS SET last_refresh = CURRENT_TIMESTAMP, state_fingerprint = :fp",
        {fp = fingerprint}
    )
    if not stamp_ok then
        error("PREPROC.REFRESH_CORE: stamping SETTINGS failed: " .. tostring(stamp_res.error_message))
    end

    -- Exasol Lua scripts have no commit() global; COMMIT is issued as SQL.
    local commit_ok, commit_res = pquery("COMMIT")
    if not commit_ok then
        error("PREPROC.REFRESH_CORE: commit failed: " .. tostring(commit_res.error_message))
    end

    return row_count
end
/

--/
CREATE OR REPLACE LUA SCRIPT PREPROC.REFRESH() RETURNS TABLE AS
-- =============================================================================
-- REFRESH preprocessor resolver  (PREPROC.REFRESH)
-- =============================================================================
-- Role
--   Materialises the scoped rules in PREPROC.CONFIG (ROLE- and USER-scoped)
--   into the flat, per-user PREPROC.RESOLUTION table that PREPROC_RT.MY_PIPELINE
--   reads. The rebuild itself -- fingerprint, transitive role expansion, delete,
--   reinsert, stamp, commit -- lives in the shared PREPROC.REFRESH_CORE module
--   so that this script and every mutating admin script run the IDENTICAL logic.
--   This script is a thin RETURNS TABLE wrapper: it delegates the rebuild to
--   REFRESH_CORE, then reports the outcome.
--
-- Privilege model (invoker rights)
--   Exasol Lua scripts run with INVOKER rights. Reading the dictionary tables
--   EXA_DBA_ROLE_PRIVS / EXA_DBA_USERS requires SELECT ANY DICTIONARY, granted
--   to PREPROC_ADMIN. This script is therefore executable only by holders of
--   PREPROC_ADMIN (or EXECUTE ANY SCRIPT / DBA) -- never PUBLIC. A non-admin
--   caller fails at the dictionary read (inside REFRESH_CORE) with an
--   insufficient-privileges error, before any write, leaving PREPROC.RESOLUTION
--   unchanged.
--
-- Atomicity
--   Owned by REFRESH_CORE.rebuild(): the fingerprint is captured FIRST (so
--   STATUS, recomputing the identical SQL afterwards, reports "current"). Then
--   the rebuild -- DELETE all of RESOLUTION, reinsert resolved rows, stamp
--   last_refresh + fingerprint -- runs as one transaction committed once. No
--   reader can observe an emptied RESOLUTION paired with an old last_refresh,
--   nor a populated RESOLUTION paired with a stale stamp.
--
-- Result contract
--   The script is created RETURNS TABLE, so it returns its outcome row through
--   exit(result_table, column_spec) -- a two-dimensional array plus a column
--   type string. (output() only emits WITH-OUTPUT log lines and does NOT
--   populate a RETURNS TABLE result set; exit is the documented mechanism.)
--   The single returned row is (rows_inserted DECIMAL(9,0), last_refresh
--   VARCHAR) so any SQL client can observe the outcome. Result reporting stays
--   in this wrapper (not in REFRESH_CORE) because the result shape is specific
--   to PREPROC.REFRESH.
-- =============================================================================

-- rc is the alias table populated at runtime by import("PREPROC.REFRESH_CORE",
-- "rc"); luacheck cannot see import's binding, so declare it here (same pattern
-- as master.lua's preproc_module global).
-- luacheck: globals rc

-- --- Step 1+2: atomic rebuild via the shared module --------------------------
-- import() executes REFRESH_CORE's body in this script's context, binding its
-- exported rebuild() into the rc table. rebuild() performs the fingerprint
-- capture, delete/insert, stamp and single COMMIT, raising on any failure, and
-- returns the authoritative rows_inserted count. It does NOT call exit() --
-- result reporting is this wrapper's responsibility.
import("PREPROC.REFRESH_CORE", "rc")
local row_count = rc.rebuild()

-- --- Step 3: report the outcome ----------------------------------------------
-- After commit the stamped last_refresh is the persisted value; read it back so
-- any SQL client sees the exact timestamp recorded. Returned as one row via
-- exit() because the script is RETURNS TABLE.
local last_refresh = ""
local ts_ok, ts_res = pquery("SELECT last_refresh FROM PREPROC.SETTINGS")
if ts_ok and ts_res[1] ~= nil and ts_res[1][1] ~= null then
    last_refresh = tostring(ts_res[1][1])
end

exit(
    {{row_count, last_refresh}},
    "rows_inserted DECIMAL(9,0), last_refresh VARCHAR(64)"
)
/

--/
CREATE OR REPLACE LUA SCRIPT PREPROC.STATUS() RETURNS TABLE AS
-- =============================================================================
-- STATUS preprocessor staleness probe  (PREPROC.STATUS)
-- =============================================================================
-- Role
--   Read-only drift detector for the materialised resolution. Recomputes the
--   state fingerprint over the live dictionary (user universe + role-grant
--   graph) and compares it against the value PREPROC.REFRESH stamped into
--   PREPROC.SETTINGS, returning a current/stale verdict plus the recorded
--   last_refresh. A delegated periodic refresh (cron/CI) uses this as its
--   "do I need to run" signal. This script NEVER writes -- not to
--   PREPROC.RESOLUTION, not to PREPROC.SETTINGS.
--
-- Privilege model (invoker rights)
--   Identical to PREPROC.REFRESH. Exasol Lua scripts run with INVOKER rights;
--   the fingerprint recomputation reads EXA_DBA_USERS / EXA_DBA_ROLE_PRIVS,
--   which require SELECT ANY DICTIONARY -- granted to PREPROC_ADMIN, never
--   PUBLIC. A non-admin caller fails at the dictionary read with an
--   insufficient-privileges error before any verdict is returned.
--
-- Staleness model
--   - last_refresh IS NULL  -> never refreshed -> stale, verdict NEVER_REFRESHED.
--   - recomputed fingerprint != stored fingerprint -> stale, verdict STALE.
--   - otherwise -> current, verdict CURRENT.
--   The fingerprint is normalised to "" when null the same way PREPROC.REFRESH
--   normalises it before stamping, so an empty user/grant universe (HASH_MD5 of
--   a NULL LISTAGG) compares equal writer-to-reader instead of spuriously stale.
--
-- Result contract
--   Created RETURNS TABLE, so the single outcome row is returned through
--   exit(result_table, column_spec) -- a two-dimensional array plus a column
--   type string -- exactly as PREPROC.REFRESH does.
-- =============================================================================

-- State fingerprint over the resolution inputs (the user universe and the full
-- role-grant graph). Shared verbatim with PREPROC.REFRESH so writer and reader
-- compute it identically: an MD5 over the '|'-joined, sorted identifiers.
local FINGERPRINT_SQL = [[
    SELECT HASH_MD5(
        LISTAGG(ident, '|') WITHIN GROUP (ORDER BY ident)
    ) AS fp
    FROM (
        SELECT USER_NAME AS ident FROM EXA_DBA_USERS
        UNION ALL
        SELECT GRANTED_ROLE || ':' || GRANTEE AS ident FROM EXA_DBA_ROLE_PRIVS
    ) t
]]

-- --- Step 1: read the recorded refresh state --------------------------------
local set_ok, set_res = pquery("SELECT last_refresh, state_fingerprint FROM PREPROC.SETTINGS")
if not set_ok then
    error("PREPROC.STATUS: reading SETTINGS failed: " .. tostring(set_res.error_message))
end

local last_refresh = set_res[1][1]
local stored_fp = set_res[1][2]

-- --- Step 2: never refreshed short-circuit -----------------------------------
-- A NULL last_refresh means PREPROC.REFRESH has never stamped the state; the
-- resolution is by definition stale. Return before touching the dictionary so
-- the verdict is reported even on a brand-new install.
if last_refresh == null then
    exit(
        {{true, "NEVER_REFRESHED", null}},
        "is_stale BOOLEAN, verdict VARCHAR(32), last_refresh VARCHAR(64)"
    )
end

-- --- Step 3: recompute the current fingerprint -------------------------------
-- A failure here (e.g. a non-admin caller lacking SELECT ANY DICTIONARY)
-- surfaces as an error before any verdict is returned.
local fp_ok, fp_res = pquery(FINGERPRINT_SQL)
if not fp_ok then
    error("PREPROC.STATUS: fingerprint computation failed: " .. tostring(fp_res.error_message))
end
local current_fp = fp_res[1][1]

-- --- Step 4: compare and decide ----------------------------------------------
-- Normalise both sides to "" when null, mirroring PREPROC.REFRESH's stamp-time
-- coalescing, so an empty universe compares equal rather than spuriously stale.
if current_fp == null then
    current_fp = ""
end
if stored_fp == null then
    stored_fp = ""
end

local is_stale = current_fp ~= stored_fp
local verdict
if is_stale then
    verdict = "STALE"
else
    verdict = "CURRENT"
end

-- --- Step 5: report the verdict ----------------------------------------------
exit(
    {{is_stale, verdict, tostring(last_refresh)}},
    "is_stale BOOLEAN, verdict VARCHAR(32), last_refresh VARCHAR(64)"
)
/

--/
CREATE OR REPLACE LUA SCRIPT PREPROC.RULE_ADD(scope_type, scope_value, phase, script_name, function_name, norefresh) RETURNS TABLE AS
-- =============================================================================
-- RULE_ADD preprocessor rule installer  (PREPROC.RULE_ADD)
-- =============================================================================
-- Role
--   The supported way to add one scoped preprocessor rule to PREPROC.CONFIG.
--   Direct INSERT into CONFIG is unsupported: this script owns rule_id
--   allocation, input validation, and the auto-refresh that materialises the
--   new rule into PREPROC.RESOLUTION via the shared REFRESH_CORE module. It
--   validates every input BEFORE writing, so a rejected call leaves CONFIG
--   exactly as it was (no row, no committed change).
--
-- Parameters (declared in the CREATE OR REPLACE LUA SCRIPT header, deployed by
-- install.py / bundle.py — not in this body)
--   scope_type    VARCHAR(4)    'ROLE' or 'USER'
--   scope_value   VARCHAR(128)  the role or user name the rule scopes to
--   phase         VARCHAR(32)   'TRANSLATE', 'EXPAND', or 'REWRITE'
--   script_name   VARCHAR(128)  fully-qualified SCHEMA.NAME of the rule module
--   function_name VARCHAR(128)  entry function within that module
--   norefresh     BOOLEAN       TRUE to skip the auto-refresh (caller refreshes)
--   The parameters are directly accessible as Lua variables of the same name.
--
-- rule_id allocation (operator space, below the reserved demo range)
--   rule_id is allocated as MAX(rule_id) + 1 over the existing operator-space
--   rows (rule_id < 9000). The 9000-9999 band is reserved for demo/seed rules
--   and is NEVER allocated here. If operator space is exhausted (the candidate
--   would reach 9000) the script errors rather than colliding with the reserved
--   band. An empty CONFIG yields rule_id 1.
--
-- Validation order (the invariant that keeps a rejected call side-effect free)
--   phase, then scope_type, then script existence — all BEFORE any DML. Each
--   raises via error() naming the offending value. No COMMIT has occurred at
--   that point, so CONFIG is unchanged on any rejection.
--
-- Atomicity / refresh
--   On success the single INSERT is followed by rc.rebuild() (REFRESH_CORE),
--   which stamps SETTINGS and COMMITs once — so the new rule and the rebuilt
--   RESOLUTION become visible together. With norefresh = TRUE the rule is still
--   committed (an explicit COMMIT) so it persists, but RESOLUTION is left for a
--   later refresh; this is the documented "batch several edits, refresh once"
--   path.
--
-- Result contract
--   Created RETURNS TABLE: the single returned row is the allocated rule_id,
--   emitted via exit(result_table, column_spec).
-- =============================================================================

-- rc is bound at runtime by import("PREPROC.REFRESH_CORE", "rc"); luacheck
-- cannot see import's binding, so declare it as a global here.
-- luacheck: globals rc
-- The CREATE OR REPLACE LUA SCRIPT header (added by install.py / bundle.py)
-- declares the typed parameters, which Exasol injects as read-only globals.
-- luacheck cannot see that header, so declare them as read-only globals here.
-- luacheck: read globals scope_type scope_value phase script_name function_name norefresh

local RESERVED_DEMO_FLOOR = 9000

local VALID_PHASES = {TRANSLATE = true, EXPAND = true, REWRITE = true}
local VALID_SCOPE_TYPES = {ROLE = true, USER = true}

-- --- Step 1: validate phase ---------------------------------------------------
if not VALID_PHASES[phase] then
    error(
        "PREPROC.RULE_ADD: invalid phase: " .. tostring(phase)
            .. " (expected TRANSLATE, EXPAND, or REWRITE)"
    )
end

-- --- Step 2: validate scope_type ----------------------------------------------
if not VALID_SCOPE_TYPES[scope_type] then
    error(
        "PREPROC.RULE_ADD: invalid scope_type: " .. tostring(scope_type)
            .. " (expected ROLE or USER)"
    )
end

-- --- Step 3: validate the referenced script exists ----------------------------
-- Read the dictionary under invoker rights; the fully-qualified SCHEMA.NAME must
-- name a deployed script. Rejecting here keeps a typo'd rule out of CONFIG.
local chk_ok, chk_res = pquery(
    "SELECT COUNT(*) AS c FROM EXA_ALL_SCRIPTS"
        .. " WHERE SCRIPT_SCHEMA || '.' || SCRIPT_NAME = :sn",
    {sn = script_name}
)
if not chk_ok then
    error(
        "PREPROC.RULE_ADD: script existence check failed: "
            .. tostring(chk_res.error_message)
    )
end
if chk_res[1][1] == 0 then
    error("PREPROC.RULE_ADD: script not found: " .. tostring(script_name))
end

-- --- Step 4: allocate a non-colliding rule_id in operator space ---------------
-- MAX over rule_id < 9000 keeps the candidate in operator space; an empty table
-- yields 1. Guard the upper boundary so allocation never crosses into the
-- reserved 9000-9999 demo band.
local id_ok, id_res = pquery(
    "SELECT COALESCE(MAX(rule_id), 0) + 1 AS next_id"
        .. " FROM PREPROC.CONFIG WHERE rule_id < :floor",
    {floor = RESERVED_DEMO_FLOOR}
)
if not id_ok then
    error(
        "PREPROC.RULE_ADD: rule_id allocation failed: "
            .. tostring(id_res.error_message)
    )
end
local rule_id = id_res[1][1]
if rule_id >= RESERVED_DEMO_FLOOR then
    error(
        "PREPROC.RULE_ADD: operator rule_id space exhausted below "
            .. tostring(RESERVED_DEMO_FLOOR)
    )
end

-- --- Step 5: insert the enabled rule ------------------------------------------
-- enabled = TRUE and version = 1 for a freshly added rule; client_pattern is
-- NULL (not part of this control surface). Named binding throughout.
local ins_ok, ins_res = pquery(
    [[INSERT INTO PREPROC.CONFIG
        (rule_id, scope_type, scope_value, client_pattern,
         phase, script_name, function_name, enabled, version)
      VALUES
        (:rule_id, :scope_type, :scope_value, NULL,
         :phase, :script_name, :function_name, TRUE, 1)]],
    {
        rule_id = rule_id,
        scope_type = scope_type,
        scope_value = scope_value,
        phase = phase,
        script_name = script_name,
        function_name = function_name,
    }
)
if not ins_ok then
    error("PREPROC.RULE_ADD: insert failed: " .. tostring(ins_res.error_message))
end

-- --- Step 6: refresh (or commit-only under norefresh) -------------------------
-- rc.rebuild() commits the INSERT together with the rebuilt RESOLUTION. Under
-- norefresh the INSERT must still be committed on its own so the rule persists.
if norefresh == true then
    local commit_ok, commit_res = pquery("COMMIT")
    if not commit_ok then
        error(
            "PREPROC.RULE_ADD: commit failed: " .. tostring(commit_res.error_message)
        )
    end
else
    import("PREPROC.REFRESH_CORE", "rc")
    rc.rebuild()
end

-- --- Step 7: report the allocated rule_id -------------------------------------
exit({{rule_id}}, "rule_id DECIMAL(18,0)")
/

--/
CREATE OR REPLACE LUA SCRIPT PREPROC.RULE_DROP(rule_id, norefresh) RETURNS TABLE AS
-- =============================================================================
-- RULE_DROP preprocessor rule remover  (PREPROC.RULE_DROP)
-- =============================================================================
-- Role
--   The supported way to remove one rule from PREPROC.CONFIG by rule_id and
--   re-materialise RESOLUTION so the rule stops applying. Dropping an unknown
--   rule_id is a no-op (0 rows removed), not an error: the script is idempotent
--   so a repeated or speculative drop is safe.
--
-- Parameters (declared in the CREATE header added by install.py / bundle.py)
--   rule_id    DECIMAL(18,0)  the rule to remove
--   norefresh  BOOLEAN        TRUE to skip the auto-refresh (caller refreshes)
--
-- Refresh / atomicity
--   On any outcome the DELETE is followed by rc.rebuild() (REFRESH_CORE), which
--   COMMITs the delete together with the rebuilt RESOLUTION. Under norefresh the
--   DELETE is committed on its own so it persists; RESOLUTION is left for a
--   later refresh. A 0-row delete still refreshes by default — harmless and
--   keeps the contract uniform.
--
-- Result contract
--   Created RETURNS TABLE: the single returned row is rows_removed.
-- =============================================================================

-- luacheck: globals rc
-- luacheck: read globals rule_id norefresh

-- --- Step 1: delete the rule --------------------------------------------------
local del_ok, del_res = pquery(
    "DELETE FROM PREPROC.CONFIG WHERE rule_id = :rule_id",
    {rule_id = rule_id}
)
if not del_ok then
    error("PREPROC.RULE_DROP: delete failed: " .. tostring(del_res.error_message))
end
local rows_removed = del_res.rows_affected
if rows_removed == nil then
    rows_removed = 0
end

-- --- Step 2: refresh (or commit-only under norefresh) -------------------------
if norefresh == true then
    local commit_ok, commit_res = pquery("COMMIT")
    if not commit_ok then
        error(
            "PREPROC.RULE_DROP: commit failed: " .. tostring(commit_res.error_message)
        )
    end
else
    import("PREPROC.REFRESH_CORE", "rc")
    rc.rebuild()
end

-- --- Step 3: report how many rows were removed --------------------------------
exit({{rows_removed}}, "rows_removed DECIMAL(9,0)")
/

--/
CREATE OR REPLACE LUA SCRIPT PREPROC.RULE_ENABLE(rule_id, norefresh) RETURNS TABLE AS
-- =============================================================================
-- RULE_ENABLE preprocessor rule activator  (PREPROC.RULE_ENABLE)
-- =============================================================================
-- Role
--   Sets enabled = TRUE on the CONFIG row with the given rule_id and
--   re-materialises RESOLUTION so the rule starts applying. Enabling an unknown
--   or already-enabled rule_id updates 0 or 1 rows respectively and is never an
--   error — the operation is idempotent.
--
-- Parameters (declared in the CREATE header added by install.py / bundle.py)
--   rule_id    DECIMAL(18,0)  the rule to enable
--   norefresh  BOOLEAN        TRUE to skip the auto-refresh (caller refreshes)
--
-- Refresh / atomicity
--   The UPDATE is followed by rc.rebuild() (REFRESH_CORE), which COMMITs the
--   flip together with the rebuilt RESOLUTION. Under norefresh the UPDATE is
--   committed on its own; RESOLUTION is left for a later refresh.
--
-- Result contract
--   Created RETURNS TABLE: the single returned row is rows_updated.
-- =============================================================================

-- luacheck: globals rc
-- luacheck: read globals rule_id norefresh

-- --- Step 1: flip enabled to TRUE ---------------------------------------------
local upd_ok, upd_res = pquery(
    "UPDATE PREPROC.CONFIG SET enabled = TRUE WHERE rule_id = :rule_id",
    {rule_id = rule_id}
)
if not upd_ok then
    error("PREPROC.RULE_ENABLE: update failed: " .. tostring(upd_res.error_message))
end
local rows_updated = upd_res.rows_affected
if rows_updated == nil then
    rows_updated = 0
end

-- --- Step 2: refresh (or commit-only under norefresh) -------------------------
if norefresh == true then
    local commit_ok, commit_res = pquery("COMMIT")
    if not commit_ok then
        error(
            "PREPROC.RULE_ENABLE: commit failed: " .. tostring(commit_res.error_message)
        )
    end
else
    import("PREPROC.REFRESH_CORE", "rc")
    rc.rebuild()
end

-- --- Step 3: report how many rows were updated --------------------------------
exit({{rows_updated}}, "rows_updated DECIMAL(9,0)")
/

--/
CREATE OR REPLACE LUA SCRIPT PREPROC.RULE_DISABLE(rule_id, norefresh) RETURNS TABLE AS
-- =============================================================================
-- RULE_DISABLE preprocessor rule deactivator  (PREPROC.RULE_DISABLE)
-- =============================================================================
-- Role
--   Sets enabled = FALSE on the CONFIG row with the given rule_id and
--   re-materialises RESOLUTION so the rule stops applying while the row is
--   retained (re-enableable). Disabling an unknown or already-disabled rule_id
--   updates 0 or 1 rows respectively and is never an error — idempotent.
--
-- Parameters (declared in the CREATE header added by install.py / bundle.py)
--   rule_id    DECIMAL(18,0)  the rule to disable
--   norefresh  BOOLEAN        TRUE to skip the auto-refresh (caller refreshes)
--
-- Refresh / atomicity
--   The UPDATE is followed by rc.rebuild() (REFRESH_CORE), which COMMITs the
--   flip together with the rebuilt RESOLUTION. Under norefresh the UPDATE is
--   committed on its own; RESOLUTION is left for a later refresh.
--
-- Result contract
--   Created RETURNS TABLE: the single returned row is rows_updated.
-- =============================================================================

-- luacheck: globals rc
-- luacheck: read globals rule_id norefresh

-- --- Step 1: flip enabled to FALSE --------------------------------------------
local upd_ok, upd_res = pquery(
    "UPDATE PREPROC.CONFIG SET enabled = FALSE WHERE rule_id = :rule_id",
    {rule_id = rule_id}
)
if not upd_ok then
    error("PREPROC.RULE_DISABLE: update failed: " .. tostring(upd_res.error_message))
end
local rows_updated = upd_res.rows_affected
if rows_updated == nil then
    rows_updated = 0
end

-- --- Step 2: refresh (or commit-only under norefresh) -------------------------
if norefresh == true then
    local commit_ok, commit_res = pquery("COMMIT")
    if not commit_ok then
        error(
            "PREPROC.RULE_DISABLE: commit failed: " .. tostring(commit_res.error_message)
        )
    end
else
    import("PREPROC.REFRESH_CORE", "rc")
    rc.rebuild()
end

-- --- Step 3: report how many rows were updated --------------------------------
exit({{rows_updated}}, "rows_updated DECIMAL(9,0)")
/

--/
CREATE OR REPLACE LUA SCRIPT PREPROC.RULE_LIST() RETURNS TABLE AS
-- =============================================================================
-- RULE_LIST preprocessor rule inventory  (PREPROC.RULE_LIST)
-- =============================================================================
-- Role
--   Read-only listing of every rule in PREPROC.CONFIG, ordered by rule_id
--   ascending, so an operator can see what is installed (and which rules are
--   enabled) before editing. NEVER writes and NEVER refreshes: it has no
--   resolution impact.
--
-- No parameters. Created RETURNS TABLE.
--
-- Result contract
--   One row per CONFIG row with columns
--     rule_id, scope_type, scope_value, phase, script_name, function_name,
--     enabled
--   emitted via exit(result_table, column_spec). The rows are returned in
--   rule_id-ascending order. An empty CONFIG returns an empty result set (no
--   error).
-- =============================================================================

-- --- Step 1: read all rules ordered by rule_id --------------------------------
local sel_ok, sel_res = pquery(
    [[SELECT rule_id, scope_type, scope_value, phase,
             script_name, function_name, enabled
        FROM PREPROC.CONFIG
       ORDER BY rule_id ASC]]
)
if not sel_ok then
    error("PREPROC.RULE_LIST: select failed: " .. tostring(sel_res.error_message))
end

-- --- Step 2: marshal the rows into the result table ---------------------------
-- pquery yields a 1-based array of row arrays; copy each column into the result
-- in the declared order so the RETURNS TABLE shape is explicit and stable.
local rows = {}
for i = 1, #sel_res do
    local r = sel_res[i]
    rows[i] = {r[1], r[2], r[3], r[4], r[5], r[6], r[7]}
end

-- --- Step 3: return the listing -----------------------------------------------
exit(
    rows,
    "rule_id DECIMAL(18,0), scope_type VARCHAR(4), scope_value VARCHAR(128),"
        .. " phase VARCHAR(32), script_name VARCHAR(128),"
        .. " function_name VARCHAR(128), enabled BOOLEAN"
)
/

--/
CREATE OR REPLACE LUA SCRIPT PREPROC.SET_MODE(mode, norefresh) RETURNS TABLE AS
-- =============================================================================
-- SET_MODE preprocessor mode setter  (PREPROC.SET_MODE)
-- =============================================================================
-- Role
--   Sets the single PREPROC.SETTINGS.mode value. The mode governs the operator
--   surface posture (the settings_seed seeds 'convenience'); the recognised
--   values are 'convenience' and 'strict'. An unrecognised value is rejected
--   BEFORE any UPDATE, so a bad call leaves SETTINGS exactly as it was.
--
-- Parameters (declared in the CREATE header added by install.py / bundle.py)
--   mode       VARCHAR(32)  'convenience' or 'strict'
--   norefresh  BOOLEAN      TRUE to skip the auto-refresh (caller refreshes)
--
-- Validation order (the invariant that keeps a rejected call side-effect free)
--   The mode value is validated first and raises via error() naming the bad
--   value. No UPDATE runs on rejection, so SETTINGS is unchanged.
--
-- Refresh / atomicity
--   On success the UPDATE is followed by rc.rebuild() (REFRESH_CORE), which
--   COMMITs the change with the rebuilt RESOLUTION. Under norefresh the UPDATE
--   is committed on its own.
--
-- Result contract
--   Created RETURNS TABLE: the single returned row is the new mode.
-- =============================================================================

-- luacheck: globals rc
-- luacheck: read globals mode norefresh

local VALID_MODES = {convenience = true, strict = true}

-- --- Step 1: validate the mode value ------------------------------------------
if not VALID_MODES[mode] then
    error(
        "PREPROC.SET_MODE: invalid mode: " .. tostring(mode)
            .. " (expected convenience or strict)"
    )
end

-- --- Step 2: update the single SETTINGS row -----------------------------------
local upd_ok, upd_res = pquery(
    "UPDATE PREPROC.SETTINGS SET mode = :mode",
    {mode = mode}
)
if not upd_ok then
    error("PREPROC.SET_MODE: update failed: " .. tostring(upd_res.error_message))
end

-- --- Step 3: refresh (or commit-only under norefresh) -------------------------
if norefresh == true then
    local commit_ok, commit_res = pquery("COMMIT")
    if not commit_ok then
        error(
            "PREPROC.SET_MODE: commit failed: " .. tostring(commit_res.error_message)
        )
    end
else
    import("PREPROC.REFRESH_CORE", "rc")
    rc.rebuild()
end

-- --- Step 4: report the new mode ----------------------------------------------
exit({{mode}}, "mode VARCHAR(32)")
/

--/
CREATE OR REPLACE LUA SCRIPT PREPROC.SET_SELF_DISABLE(value) RETURNS TABLE AS
-- =============================================================================
-- SET_SELF_DISABLE preprocessor self-disable toggle  (PREPROC.SET_SELF_DISABLE)
-- =============================================================================
-- Role
--   Sets the single PREPROC.SETTINGS.self_disable_allowed flag. This is a pure
--   settings change with NO resolution impact, so the script deliberately does
--   NOT refresh. It updates ONLY self_disable_allowed: mode, last_refresh, and
--   state_fingerprint are untouched, so the stamped refresh state is preserved
--   and STATUS does not spuriously flip to stale.
--
-- Parameters (declared in the CREATE header added by install.py / bundle.py)
--   value  BOOLEAN  the new self_disable_allowed flag
--
-- Result contract
--   Created RETURNS TABLE: the single returned row is the new flag value.
-- =============================================================================

-- luacheck: read globals value

-- --- Step 1: update only the self_disable_allowed column ----------------------
-- A single-column UPDATE leaves mode, last_refresh, and state_fingerprint as
-- they were; committed on its own since there is no refresh to own the commit.
local upd_ok, upd_res = pquery(
    "UPDATE PREPROC.SETTINGS SET self_disable_allowed = :value",
    {value = value}
)
if not upd_ok then
    error(
        "PREPROC.SET_SELF_DISABLE: update failed: " .. tostring(upd_res.error_message)
    )
end

local commit_ok, commit_res = pquery("COMMIT")
if not commit_ok then
    error(
        "PREPROC.SET_SELF_DISABLE: commit failed: " .. tostring(commit_res.error_message)
    )
end

-- --- Step 2: report the new value ---------------------------------------------
exit({{value}}, "self_disable_allowed BOOLEAN")
/

EXECUTE SCRIPT PREPROC.REFRESH;

