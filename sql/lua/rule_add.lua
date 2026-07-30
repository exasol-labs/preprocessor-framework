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
--   scope_type      VARCHAR(4)    'ROLE' or 'USER'
--   scope_value     VARCHAR(128)  the role or user name the rule scopes to
--   phase           VARCHAR(32)   'TRANSLATE', 'EXPAND', or 'REWRITE'
--   script_name     VARCHAR(128)  fully-qualified SCHEMA.NAME of the rule module
--   function_name   VARCHAR(128)  entry function within that module
--   client_pattern  VARCHAR(256)  client-app substring, or SQL NULL for apply-always
--   norefresh       BOOLEAN       TRUE to skip the auto-refresh (caller refreshes)
--   The parameters are directly accessible as Lua variables of the same name.
--   This list is deliberately identical to PREPROC.RULE_ENSURE's, so the two
--   scripts differ only in insert-vs-upsert semantics.
--
-- rule_id allocation (operator space, below the reserved demo range)
--   rule_id is allocated as MAX(rule_id) + 1 over the existing operator-space
--   rows (rule_id < 9000). The 9000-9999 band is reserved for demo/seed rules
--   and is NEVER allocated here. If operator space is exhausted (the candidate
--   would reach 9000) the script errors rather than colliding with the reserved
--   band. An empty CONFIG yields rule_id 1.
--
-- Validation order (the invariant that keeps a rejected call side-effect free)
--   phase, then scope_type, then script existence, then client_pattern — all
--   BEFORE the rule_id allocation and the INSERT, so no DML runs until every
--   input is accepted. Each raises via error() naming the offending value. No
--   COMMIT has occurred at that point, so CONFIG is unchanged on any rejection.
--   client_pattern is checked by the SHARED validator in PREPROC.REFRESH_CORE,
--   so this script and RULE_ENSURE reject exactly the same patterns.
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
-- luacheck: read globals scope_type scope_value phase script_name function_name client_pattern norefresh

local RESERVED_DEMO_FLOOR = 9000

local VALID_PHASES = {TRANSLATE = true, EXPAND = true, REWRITE = true}
local VALID_SCOPE_TYPES = {ROLE = true, USER = true}

-- Load the shared module up front, not at the refresh site, because Step 4 needs
-- its validator BEFORE any DML. import() only binds the module's functions and
-- performs no work of its own, so this is behaviour-neutral -- including on the
-- norefresh path, which loads the module and never calls rebuild().
import("PREPROC.REFRESH_CORE", "rc")

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

-- --- Step 4: validate the client_pattern --------------------------------------
-- The shared guard in PREPROC.REFRESH_CORE, so RULE_ADD and RULE_ENSURE reject
-- exactly the same patterns: NULL is accepted (apply to all clients), an empty or
-- whitespace-only pattern is rejected because it would match every client, and an
-- over-length pattern is rejected rather than truncated. Last of the validations
-- and still ahead of every write.
rc.validate_client_pattern(client_pattern)

-- A SQL NULL parameter may arrive as Lua nil or the Exasol `null` sentinel; treat
-- both as "no pattern" so the INSERT is issued in the correct shape.
local cp_is_null = (client_pattern == nil or client_pattern == null)

-- --- Step 5: allocate a non-colliding rule_id in operator space ---------------
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

-- --- Step 6: insert the enabled rule ------------------------------------------
-- enabled = TRUE and version = 1 for a freshly added rule. Named binding
-- throughout (positional ? placeholders silently fail under Exasol Lua).
-- client_pattern is written as a NULL literal when absent (bind-safe) and bound
-- otherwise, the same two shapes RULE_ENSURE issues; the bound value is the
-- ORIGINAL parameter, so the pattern is persisted byte-for-byte with no folding
-- or trimming.
local ins_ok, ins_res
if cp_is_null then
    ins_ok, ins_res = pquery(
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
else
    ins_ok, ins_res = pquery(
        [[INSERT INTO PREPROC.CONFIG
            (rule_id, scope_type, scope_value, client_pattern,
             phase, script_name, function_name, enabled, version)
          VALUES
            (:rule_id, :scope_type, :scope_value, :cp,
             :phase, :script_name, :function_name, TRUE, 1)]],
        {
            rule_id = rule_id,
            scope_type = scope_type,
            scope_value = scope_value,
            cp = client_pattern,
            phase = phase,
            script_name = script_name,
            function_name = function_name,
        }
    )
end
if not ins_ok then
    error("PREPROC.RULE_ADD: insert failed: " .. tostring(ins_res.error_message))
end

-- --- Step 7: refresh (or commit-only under norefresh) -------------------------
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
    rc.rebuild()
end

-- --- Step 8: report the allocated rule_id -------------------------------------
exit({{rule_id}}, "rule_id DECIMAL(18,0)")
