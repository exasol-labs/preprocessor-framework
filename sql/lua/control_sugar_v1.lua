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
--   Every mutating command (ADD/DROP/ENABLE/DISABLE RULE, SET MODE,
--   SET SYS_GROUPING) accepts an optional trailing NOREFRESH keyword. The
--   admin scripts take an optional trailing boolean parameter that, when true,
--   skips the in-script auto-refresh (for batch workflows that refresh once at
--   the end). The threading rule is
--   purely textual: if the command ends in NOREFRESH, strip that keyword before
--   parsing the rest, then append `, true` as the final argument of the emitted
--   EXECUTE SCRIPT call. Non-mutating commands (LIST RULES, REFRESH, STATUS,
--   SET SELF_DISABLE) do not take the suffix and never receive the extra arg.
--
-- -----------------------------------------------------------------------------
-- 10. Quoting semantics  —  captured values follow Exasol identifier rules
-- -----------------------------------------------------------------------------
--   PREPROC keywords (PREPROC, ADD, RULE, FOR, PHASE, NOREFRESH, …) are always
--   matched case-insensitively. Captured VALUES are folded to match how Exasol
--   itself resolves the names they stand for, so the sugar behaves like the SQL
--   it resembles:
--     * Object-name identifiers (module name, script name, phase) follow regular
--       vs delimited identifier rules: an UNQUOTED token folds to UPPER CASE
--       (Exasol stores regular identifiers upper-cased), while a "..."-delimited
--       token is taken verbatim (embedded "" unescaped to "). A lowercase
--       registry key must therefore be quoted: INSTALL MODULE "cast-shorthand".
--     * Role/user names (the FOR ROLE|USER value) are ALWAYS upper-cased, even
--       when delimited — Exasol upper-cases users and roles regardless of
--       quoting — so FOR ROLE dba, DBA, and "dba" all target DBA.
--     * The FUNCTION name is a case-SENSITIVE Lua symbol, not a SQL identifier,
--       so it is taken literally (quotes stripped if present, case never
--       changed). The SET MODE value and boolean/rule-id operands are likewise
--       passed through verbatim — they are not identifiers.
--   The tokenizer keeps a "..." or '...' span (including doubled-quote escapes)
--   as a single token, so a quoted identifier or source survives whitespace.
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
    set_sys_grouping = "PREPROC.SET_SYS_GROUPING",
    module_install = "PREPROC.MODULE_INSTALL",
    module_catalog = "PREPROC.MODULE_CATALOG",
}

-- Default module source when INSTALL/CATALOG omit the FROM clause: the public
-- Exasol Labs library, pinned to the mutable `latest` tag so an omitted source
-- always resolves the newest release (a release moves that tag to the release
-- commit). Supplying FROM overrides this for a fork, a pinned `vX.Y.Z` tag, or a
-- bucketfs: source. Kept as a code constant rather than a PREPROC.SETTINGS row,
-- in keeping with ADR-051 (no settings toggle on the fetch path).
local DEFAULT_MODULE_SOURCE =
    "https://raw.githubusercontent.com/exasol-labs/preprocessor-library/latest/registry/index.json"

-- Split a string into tokens on whitespace, preserving original case, but keep a
-- "double-quoted" or 'single-quoted' span as ONE token even when it contains
-- spaces. A doubled quote inside a span ("" or '') is an escaped quote and does
-- NOT close it (SQL delimited-identifier / string-literal escaping). The
-- surrounding quotes are kept in the token so the folding helpers below can tell
-- a delimited identifier from a regular one. Value case is preserved here;
-- Exasol identifier folding happens per position at capture time.
local function tokenize(text)
    local tokens = {}
    local i = 1
    local n = #text
    while i <= n do
        local c = string.sub(text, i, i)
        if string.match(c, "%s") then
            i = i + 1
        elseif c == '"' or c == "'" then
            local j = i + 1
            while j <= n do
                if string.sub(text, j, j) == c then
                    if string.sub(text, j + 1, j + 1) == c then
                        j = j + 2 -- escaped quote: stay inside the span
                    else
                        break -- closing quote
                    end
                else
                    j = j + 1
                end
            end
            local last = (j <= n) and j or n -- include the closing quote, or run to end if unterminated
            tokens[#tokens + 1] = string.sub(text, i, last)
            i = last + 1
        else
            local j = i
            while j <= n do
                local cj = string.sub(text, j, j)
                if string.match(cj, "%s") or cj == '"' or cj == "'" then
                    break
                end
                j = j + 1
            end
            tokens[#tokens + 1] = string.sub(text, i, j - 1)
            i = j
        end
    end
    return tokens
end

-- Return (inner, was_delimited) for a token: if it is a "..."-delimited
-- identifier, strip the surrounding double quotes and unescape any doubled quote
-- ("" -> "); otherwise return the token unchanged with false.
local function undelimit(token)
    if #token >= 2 and string.sub(token, 1, 1) == '"' and string.sub(token, -1) == '"' then
        local inner = string.gsub(string.sub(token, 2, -2), '""', '"')
        return inner, true
    end
    return token, false
end

-- Exasol identifier folding for object-name positions (module name, script name,
-- phase): a regular (unquoted) identifier is stored upper-cased, while a
-- delimited "..." identifier keeps its exact case. So an unquoted token folds to
-- UPPER CASE and a quoted one is taken verbatim.
local function fold_identifier(token)
    local inner, delimited = undelimit(token)
    if delimited then
        return inner
    end
    return string.upper(inner)
end

-- Role/user name folding: Exasol upper-cases role and user names even when they
-- are delimited ("... case-sensitive ... except in users and roles"), so a scope
-- value is ALWAYS upper-cased whether written dba, DBA, or "dba". This is what
-- makes FOR ROLE dba target the DBA role rather than a non-existent "dba".
local function fold_principal(token)
    local inner = undelimit(token)
    return string.upper(inner)
end

-- A Lua function symbol is code, not a SQL identifier: it is case-SENSITIVE and
-- must be taken literally. Strip surrounding double quotes if the caller
-- delimited it (to allow characters a regular identifier disallows) but NEVER
-- change its case.
local function literal_symbol(token)
    local inner = undelimit(token)
    return inner
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
-- Remove leading whitespace and any leading run of full-line `--` comments, so a
-- PREPROC command preceded by explanatory comments is still recognised. SQL
-- clients (and the examples/ files) commonly submit a comment block together with
-- the following statement; the preprocessor sees that raw text, so without this
-- the command's first token would be `--` and the command would be declined and
-- passed through to the engine as a syntax error. Only LEADING comment lines are
-- stripped; the command keyword must follow them.
local function strip_leading_comments(text)
    local s = text
    while true do
        s = s:gsub("^%s+", "")
        local without = s:gsub("^%-%-[^\n]*\n?", "", 1)
        if without == s then
            return s
        end
        s = without
    end
end

function expand(sqltext)
    if type(sqltext) ~= "string" then
        return nil
    end

    -- Drop leading comment lines, then trim surrounding whitespace and a single
    -- optional trailing semicolon.
    local decommented = strip_leading_comments(sqltext)
    local trimmed = decommented:gsub("^%s*(.-)%s*$", "%1")
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
        -- Exasol identifier folding: role/user name and the schema-qualified
        -- script name follow SQL identifier rules (unquoted -> UPPER, "..." ->
        -- verbatim; role/user always UPPER); phase is an enum folded the same
        -- way; the FUNCTION is a case-sensitive Lua symbol taken literally.
        local scope_value = fold_principal(word(5))
        local phase = fold_identifier(word(7))
        local script_name = fold_identifier(word(9))
        local function_name = literal_symbol(word(11))
        local args = {
            quote(scope_type),
            quote(scope_value),
            quote(phase),
            quote(script_name),
            quote(function_name),
        }
        -- Always pass the trailing norefresh slot: the target admin scripts have
        -- a fixed parameter list and Exasol requires an exact argument count, so
        -- omitting it ("got N expected N+1") makes the expanded EXECUTE SCRIPT
        -- fail. NOREFRESH -> true; otherwise NULL (the script then auto-refreshes).
        args[#args + 1] = norefresh and "true" or "NULL"
        return execute_script(CONTROL.rule_add, args)
    end

    if verb == "DROP" and upper_word(2) == "RULE" then
        -- PREPROC DROP RULE <rule_id>
        if subcommand_count ~= 3 then
            return nil
        end
        local rule_id = word(3)
        local args = { rule_id }
        -- Always pass the trailing norefresh slot: the target admin scripts have
        -- a fixed parameter list and Exasol requires an exact argument count, so
        -- omitting it ("got N expected N+1") makes the expanded EXECUTE SCRIPT
        -- fail. NOREFRESH -> true; otherwise NULL (the script then auto-refreshes).
        args[#args + 1] = norefresh and "true" or "NULL"
        return execute_script(CONTROL.rule_drop, args)
    end

    if verb == "DISABLE" and upper_word(2) == "RULE" then
        if subcommand_count ~= 3 then
            return nil
        end
        local args = { word(3) }
        -- Always pass the trailing norefresh slot: the target admin scripts have
        -- a fixed parameter list and Exasol requires an exact argument count, so
        -- omitting it ("got N expected N+1") makes the expanded EXECUTE SCRIPT
        -- fail. NOREFRESH -> true; otherwise NULL (the script then auto-refreshes).
        args[#args + 1] = norefresh and "true" or "NULL"
        return execute_script(CONTROL.rule_disable, args)
    end

    if verb == "ENABLE" and upper_word(2) == "RULE" then
        if subcommand_count ~= 3 then
            return nil
        end
        local args = { word(3) }
        -- Always pass the trailing norefresh slot: the target admin scripts have
        -- a fixed parameter list and Exasol requires an exact argument count, so
        -- omitting it ("got N expected N+1") makes the expanded EXECUTE SCRIPT
        -- fail. NOREFRESH -> true; otherwise NULL (the script then auto-refreshes).
        args[#args + 1] = norefresh and "true" or "NULL"
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
        -- Always pass the trailing norefresh slot: the target admin scripts have
        -- a fixed parameter list and Exasol requires an exact argument count, so
        -- omitting it ("got N expected N+1") makes the expanded EXECUTE SCRIPT
        -- fail. NOREFRESH -> true; otherwise NULL (the script then auto-refreshes).
        args[#args + 1] = norefresh and "true" or "NULL"
        return execute_script(CONTROL.set_mode, args)
    end

    if verb == "SET" and upper_word(2) == "SELF_DISABLE" then
        -- PREPROC SET SELF_DISABLE <bool>  (non-mutating-of-rules; no NOREFRESH)
        if subcommand_count ~= 3 or norefresh then
            return nil
        end
        return execute_script(CONTROL.set_self_disable, { word(3) })
    end

    if verb == "SET" and upper_word(2) == "SYS_GROUPING" then
        -- PREPROC SET SYS_GROUPING <bool> [NOREFRESH]  (mutating; accepts NOREFRESH)
        if subcommand_count ~= 3 then
            return nil
        end
        local args = { word(3) }
        -- Always pass the trailing norefresh slot: the target admin scripts have
        -- a fixed parameter list and Exasol requires an exact argument count, so
        -- omitting it ("got N expected N+1") makes the expanded EXECUTE SCRIPT
        -- fail. NOREFRESH -> true; otherwise NULL (the script then auto-refreshes).
        args[#args + 1] = norefresh and "true" or "NULL"
        return execute_script(CONTROL.set_sys_grouping, args)
    end

    if verb == "INSTALL" and upper_word(2) == "MODULE" then
        -- With FROM:    PREPROC INSTALL MODULE <name> FROM '<source>' FOR (ROLE|USER) <value>  (8 words)
        -- Without FROM: PREPROC INSTALL MODULE <name> FOR (ROLE|USER) <value>                  (6 words)
        -- When FROM is omitted the source defaults to DEFAULT_MODULE_SOURCE; the
        -- rest of the command (FOR ROLE|USER <value>) is unchanged. The FOR clause
        -- sits at a different token index in each form, so resolve it first.
        local source_literal, for_index
        if subcommand_count == 8 and upper_word(4) == "FROM" then
            -- The FROM source is single-quoted verbatim in the command text; carry
            -- that token through unchanged so a bucketfs:/https: source survives
            -- intact. Require the well-formed quoted shape or fail open.
            source_literal = word(5)
            if not source_literal:match("^'.*'$") then
                return nil
            end
            for_index = 6
        elseif subcommand_count == 6 then
            source_literal = quote(DEFAULT_MODULE_SOURCE)
            for_index = 4
        else
            return nil
        end
        if upper_word(for_index) ~= "FOR" then
            return nil
        end
        local scope_type = upper_word(for_index + 1)
        if scope_type ~= "ROLE" and scope_type ~= "USER" then
            return nil
        end
        -- The module name is an object-name identifier (unquoted -> UPPER,
        -- "..." -> verbatim), so a lowercase registry key like cast-shorthand
        -- must be quoted: INSTALL MODULE "cast-shorthand". The scope value is a
        -- role/user name, always upper-cased.
        local module_name = fold_identifier(word(3))
        local scope_value = fold_principal(word(for_index + 2))
        local args = {
            source_literal,
            quote(module_name),
            quote(scope_type .. ":" .. scope_value),
        }
        return execute_script(CONTROL.module_install, args)
    end

    if verb == "CATALOG" and upper_word(2) == "MODULES" then
        -- With FROM:    PREPROC CATALOG MODULES FROM '<source>'  (4 words)
        -- Without FROM: PREPROC CATALOG MODULES                 (2 words; source defaults)
        local source_literal
        if subcommand_count == 4 and upper_word(3) == "FROM" then
            source_literal = word(4)
            if not source_literal:match("^'.*'$") then
                return nil
            end
        elseif subcommand_count == 2 then
            source_literal = quote(DEFAULT_MODULE_SOURCE)
        else
            return nil
        end
        return execute_script(CONTROL.module_catalog, { source_literal })
    end

    return nil
end
