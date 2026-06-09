-- Purpose: Create the privilege-bridge view MY_PIPELINE in the runtime schema.
-- PREPROC_RT.MY_PIPELINE runs with definer (owner) rights, so it can read PREPROC.*
-- tables that invoker-rights Lua scripts cannot access directly.
-- The CURRENT_USER filter scopes each user to their own pipeline rows.
-- ORDER BY phase, ordinal guarantees deterministic dispatch ordering for the MASTER script.

CREATE OR REPLACE VIEW PREPROC_RT.MY_PIPELINE AS
SELECT
    s.self_disable_allowed,
    s.mode,
    r.phase,
    r.client_pattern,
    r.script_name,
    r.function_name,
    r.ordinal,
    r.profile_hash
FROM PREPROC.SETTINGS s
LEFT JOIN PREPROC.RESOLUTION r
    ON r.user_name = CURRENT_USER
ORDER BY r.phase, r.ordinal;
