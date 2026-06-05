-- Purpose: Create the PREPROC_ADMIN role and the two foundational schemas.
--   PREPROC   — admin schema; owned by PREPROC_ADMIN; holds config, tables, and views.
--   PREPROC_RT — runtime schema; PUBLIC-accessible; holds the MASTER script and privilege-bridge views.
-- This file is the DBA-auditable source of truth for schema-level objects.
-- Applied idempotently: schemas use CREATE SCHEMA IF NOT EXISTS.
-- CREATE ROLE has no IF NOT EXISTS in Exasol; use --@tolerate-exists to skip "already exists" errors.

--@tolerate-exists
CREATE ROLE PREPROC_ADMIN;

CREATE SCHEMA IF NOT EXISTS PREPROC;

CREATE SCHEMA IF NOT EXISTS PREPROC_RT;

-- Re-running CHANGE OWNER to the same owner is a no-op on Exasol 2025.x
-- (verified live): idempotency here rests on server behaviour rather than an
-- IF NOT EXISTS guard. Safe to re-apply; no action is taken if the owner is
-- already PREPROC_ADMIN.
ALTER SCHEMA PREPROC CHANGE OWNER PREPROC_ADMIN;
