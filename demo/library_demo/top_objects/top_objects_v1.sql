--/
CREATE OR REPLACE LUA SCRIPT PREPROC_RT.TOP_OBJECTS_V1 AS
-- =============================================================================
-- TOP_OBJECTS_V1  (PREPROC_RT.TOP_OBJECTS_V1)
-- =============================================================================
-- EXPAND-phase module adding `SHOW TOP [n] OBJECTS`, which expands to a ranked
-- query over EXA_ALL_OBJECT_SIZES. Defaults to 5 rows.
--
-- EXPAND contract: return nil to decline (the dispatcher keeps scanning the
-- remaining EXPAND rules), or a single SQL statement to claim the statement,
-- which is terminal for the phase. No defensive pcall - MASTER's dispatch-level
-- pcall is the framework's only error boundary.
--
-- The expansion reads a dictionary view, so it returns exactly the objects the
-- CALLING user is allowed to see: the rewrite carries no privileges of its own.
--
-- gsub returns two values; every result here is captured into a single local
-- before use.
-- =============================================================================

-- luacheck: globals top_objects

-- Strip leading full-line comments, surrounding whitespace, and one optional
-- trailing semicolon. SQL clients routinely submit a comment block together with
-- the statement that follows it, and the preprocessor sees that raw text.
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

-- top_objects(sqltext) -> the ranked query, or nil when this is not our command.
function top_objects(sqltext)
    if type(sqltext) ~= "string" then
        return nil
    end
    local n = string.upper(normalise(sqltext)):match("^SHOW%s+TOP%s+(%d*)%s*OBJECTS$")
    if n == nil then
        return nil
    end
    if n == "" then
        n = "5"
    end
    return "SELECT ROOT_NAME AS SCHEMA_NAME, OBJECT_NAME, OBJECT_TYPE,"
        .. " RAW_OBJECT_SIZE AS RAW_BYTES FROM EXA_ALL_OBJECT_SIZES"
        .. " WHERE OBJECT_TYPE <> 'SCHEMA' AND OBJECT_NAME NOT LIKE 'P!_%' ESCAPE '!'"
        .. " ORDER BY RAW_OBJECT_SIZE DESC LIMIT " .. n
end
/
