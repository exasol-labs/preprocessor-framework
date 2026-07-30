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
--     rule_id, scope_type, scope_value, client_pattern, phase, script_name,
--     function_name, enabled
--   emitted via exit(result_table, column_spec). The rows are returned in
--   rule_id-ascending order. An empty CONFIG returns an empty result set (no
--   error).
-- =============================================================================

-- --- Step 1: read all rules ordered by rule_id --------------------------------
local sel_ok, sel_res = pquery(
    [[SELECT rule_id, scope_type, scope_value, client_pattern, phase,
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
    rows[i] = {r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8]}
end

-- --- Step 3: return the listing -----------------------------------------------
exit(
    rows,
    "rule_id DECIMAL(18,0), scope_type VARCHAR(4), scope_value VARCHAR(128),"
        .. " client_pattern VARCHAR(256), phase VARCHAR(32),"
        .. " script_name VARCHAR(128), function_name VARCHAR(128), enabled BOOLEAN"
)
