-- =============================================================================
-- ERGONOMICS_TRAILING_COMMA_V1 preprocessor module
-- (PREPROC_RT.ERGONOMICS_TRAILING_COMMA_V1)
-- =============================================================================
-- TRANSLATE-phase module that removes trailing commas from SQL statements
-- before the Exasol engine compiles them.  A trailing comma is a comma that
-- is followed (ignoring whitespace and comments) by either:
--   a) a closing parenthesis ')'  (structural rule — always safe)
--   b) a list-terminating SQL keyword from the recognised set (keyword rule)
--   c) end-of-statement (end of input string)
--
-- Recognised keywords (case-insensitive, word-boundary-guarded):
--   FROM, WHERE, GROUP, ORDER, HAVING, LIMIT, UNION, INTERSECT, EXCEPT,
--   INTO, SET, ON, RETURNING
--
-- A statement with no trailing commas is returned byte-for-byte unchanged.
--
-- TRANSLATE contract:
--   Returns a string on every path.  Returns the original sqltext unchanged
--   when no trailing commas are found.  No defensive pcall — MASTER's
--   dispatch-level pcall handles errors.
--
-- Single-pass scanner:
--   Walks the text left-to-right.  Single-quoted strings ('...' with ''
--   escape), double-quoted identifiers ("..." with "" escape), line comments
--   (-- … EOL), and block comments (/* … */) are appended verbatim to the
--   output buffer and are never inspected for commas.
--
-- Removal rule:
--   Only the comma byte is deleted.  All surrounding whitespace and comments
--   are preserved verbatim (e.g. ',  )' becomes '  )'; '/* x */ ,' followed
--   by ')' becomes '/* x */ )').
--
-- gsub two-value trap:
--   No gsub return values are forwarded; all results are captured into a
--   local before use.
-- =============================================================================

-- luacheck: globals trailing_comma

-- trailing_comma(sqltext) -> SQL text with trailing commas removed.
-- Returns the original text byte-for-byte when no trailing commas are found.
function trailing_comma(sqltext)

    -- ------------------------------------------------------------------
    -- skip_ws_comments: advance j past whitespace and comment tokens in s.
    -- Returns the new position (first non-ws, non-comment position).
    -- ------------------------------------------------------------------
    local function skip_ws_comments(s, j)
        local n = #s
        while j <= n do
            local c = s:sub(j, j)
            if c == ' ' or c == '\t' or c == '\n' or c == '\r' then
                j = j + 1
            elseif c == '-' and j + 1 <= n and s:sub(j + 1, j + 1) == '-' then
                j = j + 2
                while j <= n and s:sub(j, j) ~= '\n' do
                    j = j + 1
                end
                if j <= n then j = j + 1 end
            elseif c == '/' and j + 1 <= n and s:sub(j + 1, j + 1) == '*' then
                j = j + 2
                while j <= n do
                    if s:sub(j, j) == '*' and j + 1 <= n and s:sub(j + 1, j + 1) == '/' then
                        j = j + 2
                        break
                    end
                    j = j + 1
                end
            else
                break
            end
        end
        return j
    end

    -- ------------------------------------------------------------------
    -- is_trailing: returns true when the comma at comma_pos is a trailing
    -- comma — i.e. after skipping whitespace and comments, the next token
    -- is ')', ';', end-of-string, or a recognised list-terminating keyword
    -- with a word-boundary guard.
    -- ------------------------------------------------------------------
    local function is_trailing(s, comma_pos)
        local n = #s
        local j = skip_ws_comments(s, comma_pos + 1)

        -- structural: ), ; or end-of-string
        if j > n then return true end
        local c = s:sub(j, j)
        if c == ')' or c == ';' then return true end

        -- keyword-terminated (case-insensitive, word-boundary-guarded)
        -- listed longest-first to avoid a shorter prefix matching first
        local keywords = {
            "INTERSECT", "RETURNING", "EXCEPT", "HAVING",
            "UNION", "ORDER", "LIMIT", "GROUP", "WHERE",
            "INTO", "FROM", "SET", "ON",
        }
        for _, kw in ipairs(keywords) do
            local klen = #kw
            if j + klen - 1 <= n then
                if s:sub(j, j + klen - 1):upper() == kw then
                    -- word-boundary check after keyword
                    local after = j + klen
                    if after > n then return true end
                    local ac = s:sub(after, after)
                    if ac == ' ' or ac == '\t' or ac == '\n' or ac == '\r'
                       or ac == '(' or ac == ',' or ac == ';'
                       or (ac == '-' and after + 1 <= n
                           and s:sub(after + 1, after + 1) == '-')
                       or (ac == '/' and after + 1 <= n
                           and s:sub(after + 1, after + 1) == '*') then
                        return true
                    end
                end
            end
        end
        return false
    end

    -- ------------------------------------------------------------------
    -- Main scanner loop
    -- ------------------------------------------------------------------
    local s = sqltext
    local n = #s
    local buf = ""
    local i = 1
    local changed = false

    while i <= n do
        local c = s:sub(i, i)

        if c == "'" then
            -- single-quoted string: '' is an escaped quote, not end-of-string
            local j = i + 1
            while j <= n do
                if s:sub(j, j) == "'" then
                    if j + 1 <= n and s:sub(j + 1, j + 1) == "'" then
                        j = j + 2
                    else
                        j = j + 1
                        break
                    end
                else
                    j = j + 1
                end
            end
            buf = buf .. s:sub(i, j - 1)
            i = j

        elseif c == '"' then
            -- double-quoted identifier: "" is an escaped quote
            local j = i + 1
            while j <= n do
                if s:sub(j, j) == '"' then
                    if j + 1 <= n and s:sub(j + 1, j + 1) == '"' then
                        j = j + 2
                    else
                        j = j + 1
                        break
                    end
                else
                    j = j + 1
                end
            end
            buf = buf .. s:sub(i, j - 1)
            i = j

        elseif c == '-' and i + 1 <= n and s:sub(i + 1, i + 1) == '-' then
            -- line comment: scan to end of line
            local j = i + 2
            while j <= n and s:sub(j, j) ~= '\n' do
                j = j + 1
            end
            if j <= n then j = j + 1 end
            buf = buf .. s:sub(i, j - 1)
            i = j

        elseif c == '/' and i + 1 <= n and s:sub(i + 1, i + 1) == '*' then
            -- block comment: scan to */
            local j = i + 2
            while j <= n do
                if s:sub(j, j) == '*' and j + 1 <= n and s:sub(j + 1, j + 1) == '/' then
                    j = j + 2
                    break
                end
                j = j + 1
            end
            buf = buf .. s:sub(i, j - 1)
            i = j

        elseif c == ',' then
            -- comma: emit only if it is NOT a trailing comma
            if is_trailing(s, i) then
                changed = true
                -- skip the comma; everything after it is emitted normally
            else
                buf = buf .. ','
            end
            i = i + 1

        else
            buf = buf .. c
            i = i + 1
        end
    end

    if not changed then
        return sqltext
    end
    return buf
end
