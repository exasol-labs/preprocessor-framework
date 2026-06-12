-- Purpose: Seed the CONTROL_SUGAR_V1 bootstrap rules into PREPROC.CONFIG on first install.
-- Two ROLE-scoped EXPAND rules are seeded so the `PREPROC ...` command sugar works out of the
-- box for both delegated preprocessor admins (PREPROC_ADMIN) and database admins (DBA).
--
-- Why DBA as well as PREPROC_ADMIN: SYS (and other superusers) hold DBA but CANNOT be granted
-- PREPROC_ADMIN ("cannot grant roles to SYS"). Because resolution walks the role-grant graph,
-- scoping the sugar to DBA lets SYS and every DBA holder use the `PREPROC ...` commands with no
-- extra grant. The sugar confers no authority beyond the EXECUTE SCRIPT PREPROC.* calls these
-- roles can already run — it is pure ergonomics over the admin control scripts.
--
-- Each INSERT carries a (scope_type, scope_value, script_name) NOT EXISTS guard, so the file is
-- idempotent: re-application inserts only the rows that are missing and never duplicates.
--
-- rule_id: MAX(rule_id) + 1 over the sub-9000 operator range (1 if empty/no sub-9000 rows),
-- avoiding the 9000-9999 demo band. The two statements run sequentially, so the DBA row takes
-- the next id after the PREPROC_ADMIN row on a clean install.

INSERT INTO PREPROC.CONFIG
    (rule_id, scope_type, scope_value, client_pattern, phase, script_name, function_name, enabled, version)
SELECT
    (SELECT COALESCE(MAX(rule_id), 0) + 1 FROM PREPROC.CONFIG WHERE rule_id < 9000),
    'ROLE',
    'PREPROC_ADMIN',
    NULL,
    'EXPAND',
    'PREPROC_RT.CONTROL_SUGAR_V1',
    'expand',
    TRUE,
    1
WHERE NOT EXISTS (
    SELECT 1 FROM PREPROC.CONFIG
    WHERE script_name = 'PREPROC_RT.CONTROL_SUGAR_V1'
      AND scope_type = 'ROLE'
      AND scope_value = 'PREPROC_ADMIN'
);

INSERT INTO PREPROC.CONFIG
    (rule_id, scope_type, scope_value, client_pattern, phase, script_name, function_name, enabled, version)
SELECT
    (SELECT COALESCE(MAX(rule_id), 0) + 1 FROM PREPROC.CONFIG WHERE rule_id < 9000),
    'ROLE',
    'DBA',
    NULL,
    'EXPAND',
    'PREPROC_RT.CONTROL_SUGAR_V1',
    'expand',
    TRUE,
    1
WHERE NOT EXISTS (
    SELECT 1 FROM PREPROC.CONFIG
    WHERE script_name = 'PREPROC_RT.CONTROL_SUGAR_V1'
      AND scope_type = 'ROLE'
      AND scope_value = 'DBA'
);
