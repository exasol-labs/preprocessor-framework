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
