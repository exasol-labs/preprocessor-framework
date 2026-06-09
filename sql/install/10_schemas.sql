-- Purpose: Create the PREPROC_ADMIN role and the two foundational schemas.
--   PREPROC   — admin schema; owned by PREPROC_ADMIN; holds config, tables, and views.
--   PREPROC_RT — runtime schema; owned by PREPROC_ADMIN; PUBLIC-accessible; holds
--                the MASTER script, the privilege-bridge view, and the generated
--                PREPROC_RT.P_<hash> profile scripts.
-- This file is the DBA-auditable source of truth for schema-level objects.
-- Applied idempotently: schemas use CREATE SCHEMA IF NOT EXISTS.
-- CREATE ROLE has no IF NOT EXISTS in Exasol; use --@tolerate-exists to skip "already exists" errors.

--@tolerate-exists
CREATE ROLE PREPROC_ADMIN;

CREATE SCHEMA IF NOT EXISTS PREPROC;

CREATE SCHEMA IF NOT EXISTS PREPROC_RT;

-- Both schemas are owned by PREPROC_ADMIN. PREPROC_RT ownership is load-bearing:
-- PREPROC.REFRESH_CORE generates the per-chain PREPROC_RT.P_<hash> profile scripts
-- during a refresh, and Exasol Lua scripts run with the INVOKER's rights — so a
-- PREPROC_ADMIN holder triggering a refresh (e.g. via RULE_ADD's auto-refresh or
-- PREPROC REFRESH) must own PREPROC_RT to create/replace/drop scripts there. The
-- companion CREATE SCRIPT system privilege is granted in 65_admin_script_grant.sql.
-- Re-running CHANGE OWNER to the same owner is a no-op on Exasol 2025.x (verified
-- live): idempotency rests on server behaviour rather than an IF NOT EXISTS guard.
ALTER SCHEMA PREPROC CHANGE OWNER PREPROC_ADMIN;

ALTER SCHEMA PREPROC_RT CHANGE OWNER PREPROC_ADMIN;
