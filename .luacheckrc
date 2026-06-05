-- luacheck configuration for Exasol preprocessor scripting
-- Target: Lua 5.1 (Exasol's embedded Lua interpreter)

std = "lua51"

-- Ignore line-length — SQL embedded in Lua strings is naturally long
max_line_length = false

-- Read-only globals injected by the Exasol preprocessor engine
read_globals = {
    -- SQL text manipulation
    "sqlparsing",    -- table; fields: getsqltext, setsqltext, tokenize, sql_changed

    -- Statement execution inside preprocessor
    "pquery",        -- function(sql, ...) → {status, rows, ...}
    "query",         -- alias for pquery in some Exasol contexts

    -- Exasol script context object
    "exa",           -- table; fields: meta, session_params, etc.

    -- Exasol NULL sentinel (not Lua nil)
    "null",
}

-- sqlparsing sub-fields (prevents "accessing undefined field" warnings)
globals = {}

-- Suppress warnings that are noise for Exasol Lua patterns
ignore = {
    "212",   -- unused argument (Exasol scripts often have required but unused params)
}
