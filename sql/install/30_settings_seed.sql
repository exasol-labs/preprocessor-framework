-- Purpose: Seed PREPROC.SETTINGS with a single default row on first install.
-- The WHERE NOT EXISTS guard makes this a no-op on re-application, preventing duplicate rows.
-- Default: mode = 'convenience', self_disable_allowed = FALSE, last_refresh = NULL,
-- group_sys_with_dba = TRUE (grouped — SYS follows the DBA/PUBLIC/nested role closure).

INSERT INTO PREPROC.SETTINGS (mode, self_disable_allowed, last_refresh, group_sys_with_dba)
SELECT 'convenience', FALSE, NULL, TRUE
WHERE NOT EXISTS (SELECT 1 FROM PREPROC.SETTINGS);
