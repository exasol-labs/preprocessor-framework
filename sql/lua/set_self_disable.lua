-- =============================================================================
-- SET_SELF_DISABLE preprocessor self-disable toggle  (PREPROC.SET_SELF_DISABLE)
-- =============================================================================
-- Role
--   Sets the single PREPROC.SETTINGS.self_disable_allowed flag. This is a pure
--   settings change with NO resolution impact, so the script deliberately does
--   NOT refresh. It updates ONLY self_disable_allowed: mode, last_refresh, and
--   state_fingerprint are untouched, so the stamped refresh state is preserved
--   and STATUS does not spuriously flip to stale.
--
-- Parameters (declared in the CREATE header added by install.py / bundle.py)
--   value  BOOLEAN  the new self_disable_allowed flag
--
-- Result contract
--   Created RETURNS TABLE: the single returned row is the new flag value.
-- =============================================================================

-- luacheck: read globals value

-- --- Step 1: update only the self_disable_allowed column ----------------------
-- A single-column UPDATE leaves mode, last_refresh, and state_fingerprint as
-- they were; committed on its own since there is no refresh to own the commit.
local upd_ok, upd_res = pquery(
    "UPDATE PREPROC.SETTINGS SET self_disable_allowed = :value",
    {value = value}
)
if not upd_ok then
    error(
        "PREPROC.SET_SELF_DISABLE: update failed: " .. tostring(upd_res.error_message)
    )
end

local commit_ok, commit_res = pquery("COMMIT")
if not commit_ok then
    error(
        "PREPROC.SET_SELF_DISABLE: commit failed: " .. tostring(commit_res.error_message)
    )
end

-- --- Step 2: report the new value ---------------------------------------------
exit({{value}}, "self_disable_allowed BOOLEAN")
