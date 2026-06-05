-- =============================================================================
-- STATUS preprocessor staleness probe  (PREPROC.STATUS)
-- =============================================================================
-- Role
--   Read-only drift detector for the materialised resolution. Recomputes the
--   state fingerprint over the live dictionary (user universe + role-grant
--   graph) and compares it against the value PREPROC.REFRESH stamped into
--   PREPROC.SETTINGS, returning a current/stale verdict plus the recorded
--   last_refresh. A delegated periodic refresh (cron/CI) uses this as its
--   "do I need to run" signal. This script NEVER writes -- not to
--   PREPROC.RESOLUTION, not to PREPROC.SETTINGS.
--
-- Privilege model (invoker rights)
--   Identical to PREPROC.REFRESH. Exasol Lua scripts run with INVOKER rights;
--   the fingerprint recomputation reads EXA_DBA_USERS / EXA_DBA_ROLE_PRIVS,
--   which require SELECT ANY DICTIONARY -- granted to PREPROC_ADMIN, never
--   PUBLIC. A non-admin caller fails at the dictionary read with an
--   insufficient-privileges error before any verdict is returned.
--
-- Staleness model
--   - last_refresh IS NULL  -> never refreshed -> stale, verdict NEVER_REFRESHED.
--   - recomputed fingerprint != stored fingerprint -> stale, verdict STALE.
--   - otherwise -> current, verdict CURRENT.
--   The fingerprint is normalised to "" when null the same way PREPROC.REFRESH
--   normalises it before stamping, so an empty user/grant universe (HASH_MD5 of
--   a NULL LISTAGG) compares equal writer-to-reader instead of spuriously stale.
--
-- Result contract
--   Created RETURNS TABLE, so the single outcome row is returned through
--   exit(result_table, column_spec) -- a two-dimensional array plus a column
--   type string -- exactly as PREPROC.REFRESH does.
-- =============================================================================

-- State fingerprint over the resolution inputs (the user universe and the full
-- role-grant graph). Shared verbatim with PREPROC.REFRESH so writer and reader
-- compute it identically: an MD5 over the '|'-joined, sorted identifiers.
local FINGERPRINT_SQL = [[
    SELECT HASH_MD5(
        LISTAGG(ident, '|') WITHIN GROUP (ORDER BY ident)
    ) AS fp
    FROM (
        SELECT USER_NAME AS ident FROM EXA_DBA_USERS
        UNION ALL
        SELECT GRANTED_ROLE || ':' || GRANTEE AS ident FROM EXA_DBA_ROLE_PRIVS
    ) t
]]

-- --- Step 1: read the recorded refresh state --------------------------------
local set_ok, set_res = pquery("SELECT last_refresh, state_fingerprint FROM PREPROC.SETTINGS")
if not set_ok then
    error("PREPROC.STATUS: reading SETTINGS failed: " .. tostring(set_res.error_message))
end

local last_refresh = set_res[1][1]
local stored_fp = set_res[1][2]

-- --- Step 2: never refreshed short-circuit -----------------------------------
-- A NULL last_refresh means PREPROC.REFRESH has never stamped the state; the
-- resolution is by definition stale. Return before touching the dictionary so
-- the verdict is reported even on a brand-new install.
if last_refresh == null then
    exit(
        {{true, "NEVER_REFRESHED", null}},
        "is_stale BOOLEAN, verdict VARCHAR(32), last_refresh VARCHAR(64)"
    )
end

-- --- Step 3: recompute the current fingerprint -------------------------------
-- A failure here (e.g. a non-admin caller lacking SELECT ANY DICTIONARY)
-- surfaces as an error before any verdict is returned.
local fp_ok, fp_res = pquery(FINGERPRINT_SQL)
if not fp_ok then
    error("PREPROC.STATUS: fingerprint computation failed: " .. tostring(fp_res.error_message))
end
local current_fp = fp_res[1][1]

-- --- Step 4: compare and decide ----------------------------------------------
-- Normalise both sides to "" when null, mirroring PREPROC.REFRESH's stamp-time
-- coalescing, so an empty universe compares equal rather than spuriously stale.
if current_fp == null then
    current_fp = ""
end
if stored_fp == null then
    stored_fp = ""
end

local is_stale = current_fp ~= stored_fp
local verdict
if is_stale then
    verdict = "STALE"
else
    verdict = "CURRENT"
end

-- --- Step 5: report the verdict ----------------------------------------------
exit(
    {{is_stale, verdict, tostring(last_refresh)}},
    "is_stale BOOLEAN, verdict VARCHAR(32), last_refresh VARCHAR(64)"
)
