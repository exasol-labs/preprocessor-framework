-- =============================================================================
-- SET_MODE preprocessor mode setter  (PREPROC.SET_MODE)
-- =============================================================================
-- Role
--   Sets the single PREPROC.SETTINGS.mode value. The mode governs the operator
--   surface posture (the settings_seed seeds 'convenience'); the recognised
--   values are 'convenience' and 'strict'. An unrecognised value is rejected
--   BEFORE any UPDATE, so a bad call leaves SETTINGS exactly as it was.
--
-- Parameters (declared in the CREATE header added by install.py / bundle.py)
--   mode       VARCHAR(32)  'convenience' or 'strict'
--   norefresh  BOOLEAN      TRUE to skip the auto-refresh (caller refreshes)
--
-- Validation order (the invariant that keeps a rejected call side-effect free)
--   The mode value is validated first and raises via error() naming the bad
--   value. No UPDATE runs on rejection, so SETTINGS is unchanged.
--
-- Refresh / atomicity
--   On success the UPDATE is followed by rc.rebuild() (REFRESH_CORE), which
--   COMMITs the change with the rebuilt RESOLUTION. Under norefresh the UPDATE
--   is committed on its own.
--
-- Result contract
--   Created RETURNS TABLE: the single returned row is the new mode.
-- =============================================================================

-- luacheck: globals rc
-- luacheck: read globals mode norefresh

local VALID_MODES = {convenience = true, strict = true}

-- --- Step 1: validate the mode value ------------------------------------------
if not VALID_MODES[mode] then
    error(
        "PREPROC.SET_MODE: invalid mode: " .. tostring(mode)
            .. " (expected convenience or strict)"
    )
end

-- --- Step 2: update the single SETTINGS row -----------------------------------
local upd_ok, upd_res = pquery(
    "UPDATE PREPROC.SETTINGS SET mode = :mode",
    {mode = mode}
)
if not upd_ok then
    error("PREPROC.SET_MODE: update failed: " .. tostring(upd_res.error_message))
end

-- --- Step 3: refresh (or commit-only under norefresh) -------------------------
if norefresh == true then
    local commit_ok, commit_res = pquery("COMMIT")
    if not commit_ok then
        error(
            "PREPROC.SET_MODE: commit failed: " .. tostring(commit_res.error_message)
        )
    end
else
    import("PREPROC.REFRESH_CORE", "rc")
    rc.rebuild()
end

-- --- Step 4: report the new mode ----------------------------------------------
exit({{mode}}, "mode VARCHAR(32)")
