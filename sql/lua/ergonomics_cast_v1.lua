-- =============================================================================
-- ERGONOMICS_CAST_V1 preprocessor module  (PREPROC_RT.ERGONOMICS_CAST_V1)
-- =============================================================================
-- TRANSLATE-phase module that rewrites PostgreSQL-style cast shorthand
-- expr::type  →  CAST(expr AS type)  before the Exasol engine compiles the
-- statement.  Unmodified statements (no :: in code regions) are returned
-- byte-for-byte unchanged.
--
-- TRANSLATE contract:
--   Returns a string on every path.  Returns the original sqltext unchanged
--   when there are no :: tokens in code, or when fail-closed (see below).
--   No defensive pcall — MASTER's dispatch-level pcall handles errors.
--
-- Single-pass scanner:
--   Walks the text left-to-right.  Single-quoted strings ('...' with ''
--   escape), double-quoted identifiers ("..."), line comments (-- … EOL),
--   and block comments (/* … */) are appended verbatim to the output buffer
--   and are never rewritten.  Only :: that appear in bare SQL code are
--   candidates for rewriting.
--
-- Fail-closed:
--   If the backwards operand scan cannot identify a supported operand kind
--   (identifier, dotted identifier, paren group, function call, numeric/string
--   literal, or a prior CAST result), the ORIGINAL text is returned unchanged.
--   Partially-rewritten SQL is never emitted.
--
-- Chaining:
--   a::int::text is rewritten left-to-right, naturally inside-out, because
--   after rewriting the first :: the result CAST(a AS int) becomes the operand
--   for the next :: without any extra pass.
--
-- gsub two-value trap:
--   No gsub calls return values are forwarded; all results are captured into
--   a local before use.
--
-- Operand subset (ADR, plan.md):
--   unqualified identifier, dotted/qualified identifier, parenthesised
--   expression (...), function call f(...), string literal '...', numeric
--   literal, chained CAST result CAST(...).  Any other left operand causes
--   fail-closed return.
-- =============================================================================

-- luacheck: globals cast_shorthand

-- cast_shorthand(sqltext) -> SQL text with expr::type rewritten to
-- CAST(expr AS type).  Returns the original text byte-for-byte when no
-- code-region :: exists, or fail-closed when an operand is unrecognised.
function cast_shorthand(sqltext)

    -- ------------------------------------------------------------------
    -- scan_type: scan the type name (and optional param list) starting
    -- at position j in string s.  Returns (type_text, next_pos) or nil.
    -- ------------------------------------------------------------------
    local function scan_type(s, j)
        local n = #s
        -- skip optional leading whitespace
        while j <= n do
            local b = s:sub(j, j)
            if b == ' ' or b == '\t' or b == '\n' or b == '\r' then
                j = j + 1
            else
                break
            end
        end
        if j > n then return nil, nil end
        if not s:sub(j, j):match("[A-Za-z_]") then return nil, nil end
        local ts = j
        while j <= n and s:sub(j, j):match("[A-Za-z0-9_%.]") do
            j = j + 1
        end
        local tname = s:sub(ts, j - 1)
        -- optional balanced parameter list
        if j <= n and s:sub(j, j) == '(' then
            local ps = j
            local depth = 0
            while j <= n do
                local c = s:sub(j, j)
                if c == '(' then
                    depth = depth + 1
                elseif c == ')' then
                    depth = depth - 1
                    if depth == 0 then
                        j = j + 1
                        break
                    end
                end
                j = j + 1
            end
            if depth ~= 0 then return nil, nil end
            tname = tname .. s:sub(ps, j - 1)
        end
        return tname, j
    end

    -- ------------------------------------------------------------------
    -- scan_backward_operand: find the left operand at the end of the
    -- code_tail string.  Returns (operand_text, remainder) or nil, nil.
    -- The remainder is code_tail with the operand (and any trailing
    -- whitespace before the ::) stripped from its end.
    -- ------------------------------------------------------------------
    local function scan_backward_operand(ct)
        local n = #ct
        if n == 0 then return nil, nil end

        -- skip trailing whitespace (whitespace between operand and ::)
        local ws = n
        while ws >= 1 do
            local b = ct:sub(ws, ws)
            if b == ' ' or b == '\t' or b == '\n' or b == '\r' then
                ws = ws - 1
            else
                break
            end
        end
        if ws == 0 then return nil, nil end

        local op_end = ws
        local op_start = nil
        local last = ct:sub(op_end, op_end)

        if last == ')' then
            -- balanced paren group, optionally preceded by identifier (function call)
            local depth = 0
            local j = op_end
            while j >= 1 do
                local c = ct:sub(j, j)
                if c == ')' then
                    depth = depth + 1
                elseif c == '(' then
                    depth = depth - 1
                    if depth == 0 then
                        op_start = j
                        break
                    end
                end
                j = j - 1
            end
            if op_start == nil then return nil, nil end
            -- optional identifier immediately before '(' — may itself be a
            -- dotted/qualified name (e.g. schema.func(...)), so scan dots too
            local before = op_start - 1
            if before >= 1 and ct:sub(before, before):match("[A-Za-z0-9_]") then
                while before >= 1 and ct:sub(before, before):match("[A-Za-z0-9_%.]") do
                    before = before - 1
                end
                op_start = before + 1
                -- strip any leading dots accidentally consumed (e.g. ).foo())
                while op_start <= op_end and ct:sub(op_start, op_start) == '.' do
                    op_start = op_start + 1
                end
            end

        elseif last == "'" then
            -- single-quoted string literal: scan backward for opening '
            -- '' pairs are an escaped quote and do not end the string
            local j = op_end - 1
            op_start = nil
            while j >= 1 do
                local c = ct:sub(j, j)
                if c == "'" then
                    if j >= 2 and ct:sub(j - 1, j - 1) == "'" then
                        j = j - 2  -- skip the '' escape pair
                    else
                        op_start = j
                        break
                    end
                else
                    j = j - 1
                end
            end
            if op_start == nil then return nil, nil end

        elseif last:match("[A-Za-z0-9_]") then
            -- identifier (possibly dotted: a.b.c) or numeric literal
            local j = op_end
            while j >= 1 and ct:sub(j, j):match("[A-Za-z0-9_%.]") do
                j = j - 1
            end
            op_start = j + 1
            -- strip any leading dots (e.g. if we scanned past a dot at start)
            while op_start <= op_end and ct:sub(op_start, op_start) == '.' do
                op_start = op_start + 1
            end
            if op_start > op_end then return nil, nil end

        else
            return nil, nil
        end

        local operand = ct:sub(op_start, op_end)
        local remainder = ct:sub(1, op_start - 1)
        return operand, remainder
    end

    -- ------------------------------------------------------------------
    -- Main scanner loop
    -- ------------------------------------------------------------------
    -- Output is accumulated into TABLES joined once with table.concat, never by
    -- string ".." in a loop (Lua strings are immutable, so per-char concat is
    -- O(n^2)). Two buffers mirror the original prefix/code_tail split:
    --   parts -> finalized output BEFORE the current code segment
    --   seg   -> the current contiguous code segment (the old `code_tail`), held
    --            as chunks; concatenated to a string only when a :: needs to scan
    --            its operand or when a string/comment/cast ends the segment.
    -- Summed over the whole input this is O(n).
    local s = sqltext
    local n = #s

    -- Fast path: no "::" bytes at all means there is nothing to rewrite. Avoids
    -- the full scan for the overwhelmingly common case of cast-free statements.
    -- (A "::" that exists only inside a string/comment still takes the slow path
    -- but correctly produces an unchanged result.)
    if not string.find(s, "::", 1, true) then
        return sqltext
    end

    local parts = {}      -- output before the current code segment
    local seg = {}        -- chunks of the current code segment
    local i = 1
    local changed = false

    -- Move the current code segment into `parts` (it has ended), then reset it.
    local function flush_seg()
        if #seg > 0 then
            parts[#parts + 1] = table.concat(seg)
            seg = {}
        end
    end

    while i <= n do
        local c = s:sub(i, i)

        if c == "'" then
            -- single-quoted string: scan to closing ', '' is an escape
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
            flush_seg()
            parts[#parts + 1] = s:sub(i, j - 1)
            i = j

        elseif c == '"' then
            -- double-quoted identifier: '' analogue with ""
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
            flush_seg()
            parts[#parts + 1] = s:sub(i, j - 1)
            i = j

        elseif c == '-' and i + 1 <= n and s:sub(i + 1, i + 1) == '-' then
            -- line comment: scan to end of line
            local j = i + 2
            while j <= n and s:sub(j, j) ~= '\n' do
                j = j + 1
            end
            if j <= n then j = j + 1 end  -- include the newline
            flush_seg()
            parts[#parts + 1] = s:sub(i, j - 1)
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
            flush_seg()
            parts[#parts + 1] = s:sub(i, j - 1)
            i = j

        elseif c == ':' and i + 1 <= n and s:sub(i + 1, i + 1) == ':' then
            -- cast operator candidate
            local operand, new_tail = scan_backward_operand(table.concat(seg))
            if operand == nil then
                return sqltext
            end
            local tname, new_i = scan_type(s, i + 2)
            if tname == nil then
                return sqltext
            end
            -- new_tail is the part of the segment BEFORE the operand: it moves
            -- into the finalized prefix. The cast expression becomes the new
            -- current segment so a following :: chains onto it inside-out.
            parts[#parts + 1] = new_tail
            seg = { "CAST(" .. operand .. " AS " .. tname .. ")" }
            i = new_i
            changed = true

        else
            seg[#seg + 1] = c
            i = i + 1
        end
    end

    if not changed then
        return sqltext
    end
    flush_seg()
    return table.concat(parts)
end
