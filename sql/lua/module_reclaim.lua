-- =============================================================================
-- MODULE_RECLAIM guarded module version reclaim  (PREPROC.MODULE_RECLAIM)
-- =============================================================================
-- Role
--   The RECLAIM step of the module lifecycle: the physical
--   DROP SCRIPT PREPROC_RT.<NAME>_V<N> plus the removal of the version's
--   provenance row. It is GUARDED and is NEVER the same flow as retirement
--   (PREPROC.MODULE_RETIRE only marks status = 'retired'; reclaim is a separate,
--   later admin call), mirroring the profile never-same-refresh-drop invariant
--   (ADR-022): a version is dropped only after it has been retired AND is provably
--   unreachable. Reclaim touches only PREPROC.MODULES (delete) and the catalog
--   (drop); it changes no CONFIG rule (there is provably none — see guard 1), so
--   it issues no REFRESH_CORE rebuild.
--
-- The reclaim safety argument (why dropping <NAME>_V<N> cannot strand a caller)
--   A caller can end up importing PREPROC_RT.<NAME>_V<N> by exactly two paths, and
--   both are closed by the two guards below before any DROP:
--
--     GUARD 1 — no ENABLED CONFIG rule names the script.
--       MASTER dispatches the CURRENT pipeline resolved from enabled CONFIG rules
--       (PREPROC.RESOLUTION is built from enabled rows only). If no enabled rule
--       names <NAME>_V<N>, MASTER never imports it for any live dispatch. A
--       DISABLED rule cannot dispatch (it is absent from RESOLUTION and produces
--       no profile), so it does not block — the guard checks enabled = TRUE, the
--       exact reachability condition.
--
--     GUARD 2 — no NON-ORPHANED (live) PROFILES manifest bakes the script.
--       A convenience-mode session may be pinned to a baked profile
--       PREPROC_RT.P_<hash> that statically imports <NAME>_V<N>. A LIVE (non-
--       orphaned) profile is the only kind that still imports its baked scripts,
--       so a live profile baking <NAME>_V<N> blocks the drop. An ORPHANED profile
--       does NOT block: REFRESH_CORE's RETIRE step replaced its body with the
--       MASTER stub (MASTER's own body), which imports NOTHING statically — it
--       re-runs the live resolve -> dispatch -> swap for the session's next
--       statement. With guard 1 already holding (no enabled rule names V<N>), that
--       re-resolve targets the current version, never V<N>. So even while the
--       orphaned P_<hash> script (and sessions pinned to it) still physically
--       exist, dropping V<N> is safe. This is the generational safety inherited
--       from the profile lifecycle: a V<N>-baking profile must first be orphaned
--       (converted to a stub by a refresh) before this guard passes.
--
--   Ordering consequence: the update flow MUST repoint the rule off V<N> and
--   refresh (which orphans the V<N>-baking profile) BEFORE reclaiming V<N>. If it
--   has not, guard 1 (rule still enabled) or guard 2 (profile still live) blocks
--   the drop and reclaim is a safe no-op that leaves the retired row intact.
--
-- Manifest match (guard 2) is literal, not LIKE
--   PROFILES.manifest is the '||'-joined LISTAGG of
--   'phase|ordinal|script_name|function_name|client_pattern' entries, so a
--   script_name always appears flanked by '|'. The needle '|'||script_name||'|'
--   is matched with INSTR (wildcard-free): a LIKE would treat the '_' characters
--   in the qualified script name as single-char wildcards and over-match (V1 vs
--   V12, etc.). The flanking '|' prevents a prefix false-match.
--
-- Which scripts this drops (PREPROC_RT-owned artifacts only)
--   The framework only ever deploys module artifacts into PREPROC_RT, so reclaim
--   drops only a script whose parsed schema is PREPROC_RT and which is present in
--   the catalog. A self-deployed module's script lives in its own subsystem and is
--   NEVER dropped here; its provenance row is still removed (reclamation recorded)
--   so the record does not leak. A script already absent is the normal idempotent
--   case and still reclaims the row.
--
-- Drop-then-verify-then-delete (mirrors REFRESH_CORE reclaim)
--   The provenance row is the sole record that a retired version still needs
--   reclaiming, so it is deleted ONLY after the DROP is confirmed by the catalog.
--   A DROP that leaves the script present (e.g. a transient lock) leaves the row
--   intact so the next reclaim retries, rather than stranding an untracked script.
--   DROP SCRIPT implicitly commits, but the only pending work before it is reads,
--   so nothing partial is committed.
--
-- Parameters (declared in the CREATE header added by install.py / bundle.py; bare
-- names, no SQL types)
--   name     VARCHAR(128)  module name
--   version  DECIMAL(9,0)  the retired version to reclaim
--
-- Result contract
--   Created RETURNS TABLE: (name, version, dropped, reclaimed, reason).
--     dropped   — a physical DROP SCRIPT was executed and confirmed.
--     reclaimed — the provenance row was removed.
--     reason    — 'reclaimed' | 'blocked_by_rule' | 'blocked_by_profile' |
--                 'not_retired' | 'not_found' | 'drop_failed'.
-- =============================================================================

-- The CREATE header declares the typed parameters; luacheck cannot see it.
-- luacheck: read globals name version

local RESULT_SPEC =
    "name VARCHAR(128), version DECIMAL(9,0), dropped BOOLEAN, reclaimed BOOLEAN, reason VARCHAR(64)"

-- --- Step 1: read the provenance row ------------------------------------------
local row_ok, row_res = pquery(
    "SELECT script_name, status FROM PREPROC.MODULES WHERE name = :n AND version = :v",
    {n = name, v = version}
)
if not row_ok then
    error("PREPROC.MODULE_RECLAIM: provenance read failed: " .. tostring(row_res.error_message))
end
if row_res[1] == nil then
    -- Nothing recorded (already reclaimed or never registered): idempotent no-op.
    exit({{name, version, false, false, "not_found"}}, RESULT_SPEC)
end
local script_name = tostring(row_res[1][1])
local status = tostring(row_res[1][2])

-- --- Step 2: only a retired version is reclaimable ----------------------------
-- A deployed (live) version must be retired first; reclaim never drops a live one.
if status ~= "retired" then
    exit({{name, version, false, false, "not_retired"}}, RESULT_SPEC)
end

-- --- Step 3: GUARD 1 — no enabled CONFIG rule names the script -----------------
local rule_ok, rule_res = pquery(
    "SELECT COUNT(*) AS c FROM PREPROC.CONFIG WHERE enabled = TRUE AND script_name = :sn",
    {sn = script_name}
)
if not rule_ok then
    error("PREPROC.MODULE_RECLAIM: rule check failed: " .. tostring(rule_res.error_message))
end
if (tonumber(rule_res[1][1]) or 0) ~= 0 then
    exit({{name, version, false, false, "blocked_by_rule"}}, RESULT_SPEC)
end

-- --- Step 4: GUARD 2 — no live (non-orphaned) profile bakes the script ---------
local needle = "|" .. script_name .. "|"
local prof_ok, prof_res = pquery(
    [[SELECT COUNT(*) AS c FROM PREPROC.PROFILES
        WHERE orphaned_at IS NULL AND INSTR(manifest, :needle) > 0]],
    {needle = needle}
)
if not prof_ok then
    error("PREPROC.MODULE_RECLAIM: profile check failed: " .. tostring(prof_res.error_message))
end
if (tonumber(prof_res[1][1]) or 0) ~= 0 then
    exit({{name, version, false, false, "blocked_by_profile"}}, RESULT_SPEC)
end

-- --- Step 5: drop the PREPROC_RT artifact when the framework owns it -----------
-- Parse SCHEMA.NAME on the first '.' (schema names carry no dot); only a
-- PREPROC_RT-owned, catalog-present script is dropped.
local dropped = false
local dot = string.find(script_name, ".", 1, true)
if dot ~= nil then
    local schema = string.upper(string.sub(script_name, 1, dot - 1))
    local sname = string.upper(string.sub(script_name, dot + 1))
    if schema == "PREPROC_RT" and sname ~= "" then
        local exists_ok, exists_res = pquery(
            [[SELECT COUNT(*) AS c FROM EXA_ALL_SCRIPTS
                WHERE SCRIPT_SCHEMA = 'PREPROC_RT' AND SCRIPT_NAME = :sn]],
            {sn = sname}
        )
        if not exists_ok then
            error("PREPROC.MODULE_RECLAIM: catalog check failed: "
                .. tostring(exists_res.error_message))
        end
        if (tonumber(exists_res[1][1]) or 0) ~= 0 then
            -- DROP, then confirm absence before touching provenance. sname was
            -- matched against a real catalog SCRIPT_NAME above (parameterised), so
            -- it is a valid identifier and cannot inject through this concatenation.
            pquery("DROP SCRIPT PREPROC_RT." .. sname)
            local still_ok, still_res = pquery(
                [[SELECT COUNT(*) AS c FROM EXA_ALL_SCRIPTS
                    WHERE SCRIPT_SCHEMA = 'PREPROC_RT' AND SCRIPT_NAME = :sn]],
                {sn = sname}
            )
            if not still_ok then
                error("PREPROC.MODULE_RECLAIM: drop verification failed: "
                    .. tostring(still_res.error_message))
            end
            if (tonumber(still_res[1][1]) or 0) ~= 0 then
                -- Drop did not take effect; leave the provenance row for retry.
                exit({{name, version, false, false, "drop_failed"}}, RESULT_SPEC)
            end
            dropped = true
        end
    end
end

-- --- Step 6: remove the provenance row (reclamation recorded by deletion) ------
local del_ok, del_res = pquery(
    "DELETE FROM PREPROC.MODULES WHERE name = :n AND version = :v",
    {n = name, v = version}
)
if not del_ok then
    error("PREPROC.MODULE_RECLAIM: provenance delete failed: " .. tostring(del_res.error_message))
end

local commit_ok, commit_res = pquery("COMMIT")
if not commit_ok then
    error("PREPROC.MODULE_RECLAIM: commit failed: " .. tostring(commit_res.error_message))
end

exit({{name, version, dropped, true, "reclaimed"}}, RESULT_SPEC)
