-- Purpose: Grant SELECT ANY DICTIONARY to PREPROC_ADMIN so the invoker-rights
-- PREPROC.REFRESH and PREPROC.STATUS scripts can read EXA_DBA_ROLE_PRIVS and
-- EXA_DBA_USERS when called by a PREPROC_ADMIN holder.
-- GRANT is idempotent on Exasol (re-granting an existing privilege is a no-op).

GRANT SELECT ANY DICTIONARY TO PREPROC_ADMIN;
