-- =============================================================================
-- MODULE_RETIRE module version retirement  (PREPROC.MODULE_RETIRE)
-- =============================================================================
-- Role
--   Marks one module version retired in PREPROC.MODULES: sets status = 'retired'
--   and stamps retired_at. This is the RETIRE step of the never-mutate-a-live-
--   module update flow (deploy V+1 -> repoint the rule -> retire V). Retirement
--   is a PROVENANCE-ONLY state change: it does NOT drop the PREPROC_RT.<NAME>_V<N>
--   script and does NOT touch CONFIG / RESOLUTION, so it issues no REFRESH_CORE
--   rebuild. The physical DROP is deferred to the separately-invoked, guarded
--   PREPROC.MODULE_RECLAIM (never same flow as retirement, mirroring the profile
--   never-same-refresh-drop invariant, ADR-022): a convenience-mode session
--   pinned to a profile that baked <NAME>_V<N> may still import it until that
--   session's next statement re-resolves.
--
-- Parameters (declared in the CREATE header added by install.py / bundle.py; bare
-- names, no SQL types)
--   name     VARCHAR(128)  module name
--   version  DECIMAL(9,0)  the version to retire
--
-- Validation precedes any write (the missing-provenance rejection)
--   The (name, version) row MUST exist. Retiring a version with no provenance row
--   raises via error() BEFORE any UPDATE, so PREPROC.MODULES is unchanged. This is
--   the enforcement point behind "an update that names a <NAME>_V<N> with no
--   provenance row MUST fail before mutating anything": the update flow's retire
--   step refuses to proceed.
--
-- Idempotency (already-retired is a no-op, never re-stamped)
--   The UPDATE is gated on status = 'deployed', so retiring an already-retired
--   version matches zero rows and leaves the original retired_at intact — a repeat
--   retire is safe and never re-stamps the timestamp, mirroring how REFRESH_CORE
--   never re-stamps an already-orphaned profile.
--
-- Result contract
--   Created RETURNS TABLE: (name, version, status, retired_at). status is the
--   post-condition ('retired'); retired_at is the stamp as text, or NULL if the
--   row somehow carries none.
-- =============================================================================

-- The CREATE header declares the typed parameters; luacheck cannot see it.
-- luacheck: read globals name version

-- --- Step 1: validate the provenance row exists (reject before any write) ------
local chk_ok, chk_res = pquery(
    "SELECT status FROM PREPROC.MODULES WHERE name = :n AND version = :v",
    {n = name, v = version}
)
if not chk_ok then
    error("PREPROC.MODULE_RETIRE: provenance read failed: " .. tostring(chk_res.error_message))
end
if chk_res[1] == nil then
    error(
        "PREPROC.MODULE_RETIRE: no provenance row for ("
            .. tostring(name) .. ", " .. tostring(version) .. ")"
    )
end

-- --- Step 2: mark retired (deployed -> retired only; already-retired is a no-op)
local upd_ok, upd_res = pquery(
    [[UPDATE PREPROC.MODULES
        SET status = 'retired', retired_at = CURRENT_TIMESTAMP
      WHERE name = :n AND version = :v AND status = 'deployed']],
    {n = name, v = version}
)
if not upd_ok then
    error("PREPROC.MODULE_RETIRE: update failed: " .. tostring(upd_res.error_message))
end

-- --- Step 3: commit -----------------------------------------------------------
local commit_ok, commit_res = pquery("COMMIT")
if not commit_ok then
    error("PREPROC.MODULE_RETIRE: commit failed: " .. tostring(commit_res.error_message))
end

-- --- Step 4: report the final state -------------------------------------------
local out_ok, out_res = pquery(
    "SELECT status, retired_at FROM PREPROC.MODULES WHERE name = :n AND version = :v",
    {n = name, v = version}
)
if not out_ok then
    error("PREPROC.MODULE_RETIRE: result read failed: " .. tostring(out_res.error_message))
end
local retired_at = null
if out_res[1][2] ~= null then
    retired_at = tostring(out_res[1][2])
end

exit(
    {{name, version, out_res[1][1], retired_at}},
    "name VARCHAR(128), version DECIMAL(9,0), status VARCHAR(32), retired_at VARCHAR(64)"
)
