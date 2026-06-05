-- 30_settings_seed.sql
-- Purpose: Seed PREPROC.SETTINGS with a single default row on first install.
-- The WHERE NOT EXISTS guard makes this a no-op on re-application, preventing duplicate rows.
-- Default: mode = 'convenience', self_disable_allowed = FALSE, last_refresh = NULL.

INSERT INTO PREPROC.SETTINGS (mode, self_disable_allowed, last_refresh)
SELECT 'convenience', FALSE, NULL
WHERE NOT EXISTS (SELECT 1 FROM PREPROC.SETTINGS);
