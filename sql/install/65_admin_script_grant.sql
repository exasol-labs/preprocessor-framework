-- Purpose: Grant the CREATE SCRIPT system privilege to PREPROC_ADMIN.
--
-- PREPROC.REFRESH_CORE generates the per-chain PREPROC_RT.P_<hash> profile scripts
-- (and retires/reclaims them) as part of every refresh. Exasol Lua scripts run
-- with the INVOKER's rights, so the PREPROC_ADMIN holder who triggers a refresh
-- — directly via PREPROC.REFRESH, or indirectly via the auto-refresh in RULE_ADD/
-- DROP/ENABLE/DISABLE and SET_MODE — must be able to create scripts. Owning the
-- PREPROC_RT schema (see 10_schemas.sql) is not sufficient on its own; Exasol also
-- requires the CREATE SCRIPT system privilege. Without this grant a non-DBA admin's
-- refresh fails with "insufficient privileges for creating a script".
--
-- GRANT is idempotent on Exasol (re-granting an existing privilege is a no-op).

GRANT CREATE SCRIPT TO PREPROC_ADMIN;
