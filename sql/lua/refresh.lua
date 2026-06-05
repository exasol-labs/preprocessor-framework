-- =============================================================================
-- REFRESH preprocessor resolver  (PREPROC.REFRESH)
-- =============================================================================
-- Role
--   Materialises the scoped rules in PREPROC.CONFIG (ROLE- and USER-scoped)
--   into the flat, per-user PREPROC.RESOLUTION table that PREPROC_RT.MY_PIPELINE
--   reads. The rebuild itself -- fingerprint, transitive role expansion, delete,
--   reinsert, stamp, commit -- lives in the shared PREPROC.REFRESH_CORE module
--   so that this script and every mutating admin script run the IDENTICAL logic.
--   This script is a thin RETURNS TABLE wrapper: it delegates the rebuild to
--   REFRESH_CORE, then reports the outcome.
--
-- Privilege model (invoker rights)
--   Exasol Lua scripts run with INVOKER rights. Reading the dictionary tables
--   EXA_DBA_ROLE_PRIVS / EXA_DBA_USERS requires SELECT ANY DICTIONARY, granted
--   to PREPROC_ADMIN. This script is therefore executable only by holders of
--   PREPROC_ADMIN (or EXECUTE ANY SCRIPT / DBA) -- never PUBLIC. A non-admin
--   caller fails at the dictionary read (inside REFRESH_CORE) with an
--   insufficient-privileges error, before any write, leaving PREPROC.RESOLUTION
--   unchanged.
--
-- Atomicity
--   Owned by REFRESH_CORE.rebuild(): the fingerprint is captured FIRST (so
--   STATUS, recomputing the identical SQL afterwards, reports "current"). Then
--   the rebuild -- DELETE all of RESOLUTION, reinsert resolved rows, stamp
--   last_refresh + fingerprint -- runs as one transaction committed once. No
--   reader can observe an emptied RESOLUTION paired with an old last_refresh,
--   nor a populated RESOLUTION paired with a stale stamp.
--
-- Result contract
--   The script is created RETURNS TABLE, so it returns its outcome row through
--   exit(result_table, column_spec) -- a two-dimensional array plus a column
--   type string. (output() only emits WITH-OUTPUT log lines and does NOT
--   populate a RETURNS TABLE result set; exit is the documented mechanism.)
--   The single returned row is (rows_inserted DECIMAL(9,0), last_refresh
--   VARCHAR) so any SQL client can observe the outcome. Result reporting stays
--   in this wrapper (not in REFRESH_CORE) because the result shape is specific
--   to PREPROC.REFRESH.
-- =============================================================================

-- rc is the alias table populated at runtime by import("PREPROC.REFRESH_CORE",
-- "rc"); luacheck cannot see import's binding, so declare it here (same pattern
-- as master.lua's preproc_module global).
-- luacheck: globals rc

-- --- Step 1+2: atomic rebuild via the shared module --------------------------
-- import() executes REFRESH_CORE's body in this script's context, binding its
-- exported rebuild() into the rc table. rebuild() performs the fingerprint
-- capture, delete/insert, stamp and single COMMIT, raising on any failure, and
-- returns the authoritative rows_inserted count. It does NOT call exit() --
-- result reporting is this wrapper's responsibility.
import("PREPROC.REFRESH_CORE", "rc")
local row_count = rc.rebuild()

-- --- Step 3: report the outcome ----------------------------------------------
-- After commit the stamped last_refresh is the persisted value; read it back so
-- any SQL client sees the exact timestamp recorded. Returned as one row via
-- exit() because the script is RETURNS TABLE.
local last_refresh = ""
local ts_ok, ts_res = pquery("SELECT last_refresh FROM PREPROC.SETTINGS")
if ts_ok and ts_res[1] ~= nil and ts_res[1][1] ~= null then
    last_refresh = tostring(ts_res[1][1])
end

exit(
    {{row_count, last_refresh}},
    "rows_inserted DECIMAL(9,0), last_refresh VARCHAR(64)"
)
