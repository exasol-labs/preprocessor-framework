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

-- Purpose: Expose module provenance to operators through the same definer-rights
-- bridge pattern as MY_PIPELINE. INSTALLED_MODULES runs with definer (owner)
-- rights, so it reads the locked PREPROC.MODULES admin table that invoker-rights
-- callers cannot touch directly, and it is covered by the schema-level PUBLIC
-- SELECT grant on PREPROC_RT (ADR-002), so any non-admin user can read it.
-- Unlike MY_PIPELINE there is no CURRENT_USER filter: provenance is global and
-- every user sees every deployed/retired module version. ORDER BY name, version
-- makes the listing deterministic.

CREATE OR REPLACE VIEW PREPROC_RT.INSTALLED_MODULES AS
SELECT
    name,
    version,
    source_repo,
    source_ref,
    source_url,
    library_version,
    sha256,
    script_name,
    function_name,
    phase,
    deploy_mode,
    status,
    installed_at,
    retired_at
FROM PREPROC.MODULES
ORDER BY name, version;
