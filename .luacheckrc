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

    -- Cross-script module loading
    "import",        -- function(name, alias); binds the module table as global <alias>

    -- Result emission in DB-side Lua scripts
    "output",        -- function(...); emits a WITH-OUTPUT log line
    "exit",          -- function(table, colspec); terminates and returns the result set

    -- Exasol script context object
    "exa",           -- table; fields: meta, session_params, etc.

    -- Exasol NULL sentinel (not Lua nil)
    "null",
}

-- Globals created at runtime by import(name, alias) bindings
globals = {
    "preproc_module",   -- master.lua's per-rule module alias
}

-- Suppress warnings that are noise for Exasol Lua patterns
ignore = {
    "212",   -- unused argument (Exasol scripts often have required but unused params)
}
