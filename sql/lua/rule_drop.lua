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
