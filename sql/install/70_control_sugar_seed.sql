-- Purpose: Seed the CONTROL_SUGAR_V1 bootstrap rule into PREPROC.CONFIG on first install.
-- The WHERE NOT EXISTS guard makes this a no-op on re-application, preventing duplicate rows.
-- This rule enables PREPROC_ADMIN role holders to use the PREPROC ... sugar syntax, which
-- is dispatched by PREPROC_RT.CONTROL_SUGAR_V1 through the EXPAND phase.
--
-- rule_id: computed as MAX(rule_id) + 1 over the sub-9000 range, starting at 1 if the
-- table is empty or has no sub-9000 rows. This ensures no collision with existing operator
-- rules (which live below 9000) or demo rules (which are in the 9000-9999 range).

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
    SELECT 1 FROM PREPROC.CONFIG WHERE script_name = 'PREPROC_RT.CONTROL_SUGAR_V1'
);
