-- 50_grants.sql
-- Purpose: Grant schema-level EXECUTE and SELECT on PREPROC_RT to PUBLIC.
-- Schema-level grants cover all current and future objects in the schema.
-- This is a hard framework invariant: without these grants every user is locked out
-- once the preprocessor is activated in enforced mode.

GRANT EXECUTE ON SCHEMA PREPROC_RT TO PUBLIC;

GRANT SELECT ON SCHEMA PREPROC_RT TO PUBLIC;
