-- =============================================================================
-- SET_SYS_GROUPING preprocessor SYS-grouping toggle setter (PREPROC.SET_SYS_GROUPING)
-- =============================================================================
-- Role
--   Sets the single PREPROC.SETTINGS.group_sys_with_dba value. The toggle
--   governs whether the SYS account is grouped with DBA during resolution
--   (TRUE, the seeded default) or isolated as a raw-SQL break-glass recovery
--   user excluded from the entire ROLE-grant closure (FALSE). A non-boolean
--   value is rejected BEFORE any UPDATE, so a bad call leaves SETTINGS exactly
--   as it was.
--
-- Parameters (declared in the CREATE header added by install.py / bundle.py)
--   value      BOOLEAN  TRUE (grouped) or FALSE (isolated)
--   norefresh  BOOLEAN  TRUE to skip the auto-refresh (caller refreshes)
--
-- Validation order (the invariant that keeps a rejected call side-effect free)
--   The value is validated first and raises via error() naming the bad value.
--   No UPDATE runs on rejection, so SETTINGS is unchanged.
--
-- Refresh / atomicity
--   On success the UPDATE is followed by rc.rebuild() (REFRESH_CORE), which
--   COMMITs the change with the rebuilt RESOLUTION. Under norefresh the UPDATE
--   is committed on its own.
--
-- Result contract
--   Created RETURNS TABLE: the single returned row is the new value.
-- =============================================================================

-- luacheck: globals rc
-- luacheck: read globals value norefresh

-- --- Step 1: validate the value is a genuine boolean --------------------------
if not (value == true or value == false) then
    error(
        "PREPROC.SET_SYS_GROUPING: invalid value: " .. tostring(value)
            .. " (expected TRUE or FALSE)"
    )
end

-- --- Step 2: update the single SETTINGS row -----------------------------------
local upd_ok, upd_res = pquery(
    "UPDATE PREPROC.SETTINGS SET group_sys_with_dba = :value",
    {value = value}
)
if not upd_ok then
    error(
        "PREPROC.SET_SYS_GROUPING: update failed: " .. tostring(upd_res.error_message)
    )
end

-- --- Step 3: refresh (or commit-only under norefresh) -------------------------
if norefresh == true then
    local commit_ok, commit_res = pquery("COMMIT")
    if not commit_ok then
        error(
            "PREPROC.SET_SYS_GROUPING: commit failed: "
                .. tostring(commit_res.error_message)
        )
    end
else
    import("PREPROC.REFRESH_CORE", "rc")
    rc.rebuild()
end

-- --- Step 4: report the new value ----------------------------------------------
exit({{value}}, "group_sys_with_dba BOOLEAN")
