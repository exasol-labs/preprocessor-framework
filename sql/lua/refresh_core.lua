-- =============================================================================
-- REFRESH_CORE shared resolution rebuild  (PREPROC.REFRESH_CORE)
-- =============================================================================
-- Role
--   The single source of truth for the resolution rebuild. Materialises the
--   scoped rules in PREPROC.CONFIG (ROLE- and USER-scoped) into the flat,
--   per-user PREPROC.RESOLUTION table that PREPROC_RT.MY_PIPELINE reads, then
--   stamps PREPROC.SETTINGS and commits once. Transitive nested-role expansion
--   is performed in native Exasol SQL (CONNECT BY); this module only
--   orchestrates: it issues the SQL, owns the transaction, and commits.
--
--   Factored out of refresh.lua so that PREPROC.REFRESH and every mutating
--   admin script (RULE_ADD/DROP/ENABLE/DISABLE, SET_MODE) run the IDENTICAL
--   rebuild instead of duplicating the SQL or nesting EXECUTE SCRIPT
--   PREPROC.REFRESH (fragile under Exasol). Callers import this module and
--   invoke rebuild(); they own their own result reporting (exit()).
--
-- Import-loading contract
--   This script is deployed as a plain importable LUA SCRIPT (no RETURNS TABLE).
--   A caller loads it and runs the rebuild via:
--
--     import("PREPROC.REFRESH_CORE", "rc")
--     local rows_inserted = rc.rebuild()
--
--   import() executes this body in the CALLER'S context, so the Exasol globals
--   pquery / error / null are bound. The exported function MUST be declared at
--   top level with plain global function syntax (function rebuild() ... end) so
--   that import() finds it in the module table; a local function would be
--   invisible to the caller.
--
-- Privilege model (invoker rights)
--   Exasol Lua scripts run with INVOKER rights. Reading EXA_DBA_ROLE_PRIVS /
--   EXA_DBA_USERS requires SELECT ANY DICTIONARY, granted to PREPROC_ADMIN. The
--   fingerprint read happens FIRST, so a non-admin caller lacking the privilege
--   fails before any write, leaving PREPROC.RESOLUTION unchanged.
--
-- Resolution model (additive UNION, SQL-privilege style)
--   A user's resolved pipeline is the UNION (superset) of every enabled
--   ROLE-derived rule (via the transitive role closure) and every enabled
--   USER-scoped rule that applies to that user. There is no override or
--   exclusion. Identical (phase, script, function) rows reaching a user by
--   multiple paths are deduplicated, the lowest contributing rule_id winning
--   the ordinal. Ordering within a phase is pure rule_id ascending; scope type
--   is ignored.
--
-- Atomicity (the invariant this module exists to centralise)
--   The fingerprint is captured FIRST (so STATUS, recomputing the identical SQL
--   afterwards, reports "current"). Then the rebuild -- DELETE all of
--   RESOLUTION, reinsert resolved rows, stamp last_refresh + fingerprint -- runs
--   as one transaction committed once at the end. No reader can observe an
--   emptied RESOLUTION paired with an old last_refresh, nor a populated
--   RESOLUTION paired with a stale stamp.
--
-- Return contract
--   rebuild() returns rows_inserted (a number), the authoritative count of rows
--   materialised into RESOLUTION. It does NOT call exit(): result reporting (and
--   reading last_refresh back) belongs to each caller, because the result shape
--   differs per caller (REFRESH returns rows_inserted + last_refresh; an admin
--   script returns its own outcome). On any failure rebuild() raises via error()
--   so the caller's transaction is left uncommitted up to that point.
-- =============================================================================

-- luacheck: globals rebuild

-- State fingerprint over the resolution inputs (the user universe and the full
-- role-grant graph). Shared verbatim with PREPROC.STATUS so writer and reader
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

-- Transitive role-to-user closure JOIN enabled CONFIG, UNIONed with USER-scoped
-- rules, deduplicated to one row per (user, client_pattern, phase, script,
-- function) keeping the lowest contributing rule_id, with a per-(user, phase)
-- ordinal in rule_id-ascending order.
--
-- Closure semantics: in EXA_DBA_ROLE_PRIVS, GRANTED_ROLE is granted to GRANTEE.
-- Walking PRIOR GRANTEE = GRANTED_ROLE descends the grant chain to arbitrary
-- nesting depth. There is deliberately NO START WITH: Exasol forbids IN
-- subqueries outside WHERE clauses, so every row roots a chain and the full
-- (origin_role, transitive_grantee) closure is produced; the JOIN against
-- enabled ROLE-scoped CONFIG then keeps only the origins that matter. Joining
-- EXA_DBA_USERS on USER_NAME = GRANTEE keeps only the users at the leaves
-- (other roles in the chain are intermediate, not output). CONNECT BY NOCYCLE
-- is defensive: a role-grant cycle would otherwise abort the whole refresh.
local RESOLUTION_SQL = [[
    WITH scoped_rules AS (
        SELECT
            closure.grantee      AS user_name,
            cfg.client_pattern   AS client_pattern,
            cfg.phase            AS phase,
            cfg.script_name      AS script_name,
            cfg.function_name    AS function_name,
            cfg.rule_id          AS rule_id
        FROM (
            SELECT CONNECT_BY_ROOT GRANTED_ROLE AS origin_role,
                   GRANTEE                      AS grantee
            FROM EXA_DBA_ROLE_PRIVS
            CONNECT BY NOCYCLE PRIOR GRANTEE = GRANTED_ROLE
        ) closure
        JOIN PREPROC.CONFIG cfg
            ON cfg.scope_type = 'ROLE'
           AND cfg.enabled = TRUE
           AND cfg.scope_value = closure.origin_role
        JOIN EXA_DBA_USERS u
            ON u.user_name = closure.grantee
        UNION ALL
        SELECT
            cfg.scope_value      AS user_name,
            cfg.client_pattern   AS client_pattern,
            cfg.phase            AS phase,
            cfg.script_name      AS script_name,
            cfg.function_name    AS function_name,
            cfg.rule_id          AS rule_id
        FROM PREPROC.CONFIG cfg
        WHERE cfg.scope_type = 'USER' AND cfg.enabled = TRUE
    ),
    deduped AS (
        SELECT
            user_name,
            client_pattern,
            phase,
            script_name,
            function_name,
            MIN(rule_id) AS min_rule_id
        FROM scoped_rules
        GROUP BY user_name, client_pattern, phase, script_name, function_name
    )
    SELECT
        user_name,
        client_pattern,
        phase,
        script_name,
        function_name,
        ROW_NUMBER() OVER (
            PARTITION BY user_name, phase
            ORDER BY min_rule_id ASC
        ) AS ordinal
    FROM deduped
]]

-- rebuild() -> rows_inserted
--   Performs the atomic resolution rebuild and returns the authoritative count
--   of rows materialised into PREPROC.RESOLUTION. Raises via error() on any
--   failure; never calls exit() (the caller owns result reporting). The step
--   ordering below IS the atomicity contract -- fingerprint first, then the
--   single-commit delete/insert/stamp -- and must not be reordered.
function rebuild()
    -- --- Step 1: capture the fingerprint of the current input state ----------
    -- Run before the rebuild so the stamped fingerprint describes exactly the
    -- dictionary state the resolution was built from. A failure here (e.g. a
    -- non-admin caller lacking SELECT ANY DICTIONARY) surfaces before any write.
    local fp_ok, fp_res = pquery(FINGERPRINT_SQL)
    if not fp_ok then
        error("PREPROC.REFRESH_CORE: fingerprint computation failed: " .. tostring(fp_res.error_message))
    end
    local fingerprint = fp_res[1][1]
    if fingerprint == null then
        fingerprint = ""
    end

    -- --- Step 2: atomic rebuild (single transaction, committed once) ---------
    local del_ok, del_res = pquery("DELETE FROM PREPROC.RESOLUTION")
    if not del_ok then
        error("PREPROC.REFRESH_CORE: clearing RESOLUTION failed: " .. tostring(del_res.error_message))
    end

    local ins_ok, ins_res = pquery(
        [[INSERT INTO PREPROC.RESOLUTION
            (user_name, client_pattern, phase, script_name, function_name, ordinal)
        ]] .. RESOLUTION_SQL
    )
    if not ins_ok then
        error("PREPROC.REFRESH_CORE: inserting resolved rows failed: " .. tostring(ins_res.error_message))
    end
    local row_count = ins_res.rows_inserted

    -- Count back the materialised rows as the reported outcome. rows_inserted is
    -- the authoritative count; the COUNT(*) is defence-in-depth should the driver
    -- not populate rows_inserted for an INSERT...SELECT.
    if row_count == nil then
        local cnt_ok, cnt_res = pquery("SELECT COUNT(*) AS c FROM PREPROC.RESOLUTION")
        if not cnt_ok then
            error("PREPROC.REFRESH_CORE: counting resolved rows failed: " .. tostring(cnt_res.error_message))
        end
        row_count = cnt_res[1][1]
    end

    local stamp_ok, stamp_res = pquery(
        "UPDATE PREPROC.SETTINGS SET last_refresh = CURRENT_TIMESTAMP, state_fingerprint = :fp",
        {fp = fingerprint}
    )
    if not stamp_ok then
        error("PREPROC.REFRESH_CORE: stamping SETTINGS failed: " .. tostring(stamp_res.error_message))
    end

    -- Exasol Lua scripts have no commit() global; COMMIT is issued as SQL.
    local commit_ok, commit_res = pquery("COMMIT")
    if not commit_ok then
        error("PREPROC.REFRESH_CORE: commit failed: " .. tostring(commit_res.error_message))
    end

    return row_count
end
