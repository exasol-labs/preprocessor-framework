-- =============================================================================
-- PREPROCESSOR FRAMEWORK — DEMO CONFIGURATION TEARDOWN  (sql/demo/teardown.sql)
-- =============================================================================
-- Removes exactly what sql/demo/seed.sql created — the demo CONFIG rows (the
-- reserved 9000-range rule_ids), the demo users, and the demo roles — then runs
-- PREPROC.REFRESH so PREPROC.RESOLUTION returns to its pre-demo state.
--
-- WHO RUNS THIS
--   An admin holding PREPROC_ADMIN (or DBA): it writes PREPROC.CONFIG, drops
--   users/roles, and runs PREPROC.REFRESH (which needs SELECT ANY DICTIONARY).
--
-- IDEMPOTENCY (a hard requirement of the spec)
--   Re-running this file after the demo objects are already gone — or after only
--   a partial seed — MUST complete without error and leave no demo objects.
--   - The CONFIG DELETE is naturally idempotent (deleting 0 rows is fine).
--   - DROP USER / DROP ROLE have NO "IF EXISTS" form in Exasol, and this is a
--     standalone script (not processed by install.py's --@tolerate-exists
--     splitter), so a bare DROP of a missing object would raise an error and
--     break idempotency. The drops are therefore performed inside a transient
--     Lua script that checks the dictionary first and only drops what exists.
--     The script drops itself at the end, leaving no residue.
-- =============================================================================


-- -----------------------------------------------------------------------------
-- 1. Remove the demo CONFIG rows (reserved 9000-range).
-- -----------------------------------------------------------------------------
-- Idempotent by construction: deletes the demo rule_ids if present, no-op if not.
DELETE FROM PREPROC.CONFIG WHERE rule_id IN (9001, 9002, 9003, 9004, 9005);
COMMIT;


-- -----------------------------------------------------------------------------
-- 2. Drop the demo users and roles, existence-checked for idempotency.
-- -----------------------------------------------------------------------------
-- A transient Lua script in PREPROC_RT performs the drops. It checks the
-- dictionary (EXA_DBA_USERS / EXA_DBA_ROLES) before each DROP so missing objects
-- are silently skipped, then drops itself so nothing is left behind. CASCADE on
-- DROP USER clears any owned objects defensively (the demo users own none).
CREATE OR REPLACE LUA SCRIPT PREPROC_RT.DEMO_TEARDOWN_DROP AS
    local function drop_user_if_exists(name)
        local ok, res = pquery(
            "SELECT 1 FROM EXA_DBA_USERS WHERE USER_NAME = :n", {n = name})
        if ok and res[1] ~= nil then
            pquery("DROP USER " .. name .. " CASCADE")
        end
    end

    local function drop_role_if_exists(name)
        local ok, res = pquery(
            "SELECT 1 FROM EXA_DBA_ROLES WHERE ROLE_NAME = :n", {n = name})
        if ok and res[1] ~= nil then
            pquery("DROP ROLE " .. name)
        end
    end

    -- Users first: a role cannot be dropped while still granted to a user.
    drop_user_if_exists("DEMO_USER_ANALYST")
    drop_user_if_exists("DEMO_USER_ETL")

    drop_role_if_exists("DEMO_ANALYST")
    drop_role_if_exists("DEMO_ETL")

    pquery("COMMIT")
/

EXECUTE SCRIPT PREPROC_RT.DEMO_TEARDOWN_DROP;

DROP SCRIPT PREPROC_RT.DEMO_TEARDOWN_DROP;


-- -----------------------------------------------------------------------------
-- 3. Rebuild resolution so RESOLUTION no longer contains demo-derived rows.
-- -----------------------------------------------------------------------------
EXECUTE SCRIPT PREPROC.REFRESH;
