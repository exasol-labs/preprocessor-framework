-- =============================================================================
-- DEMO 99 - TEARDOWN
-- =============================================================================
-- Removes everything files 01-07 created and leaves the framework installed and
-- clean - the state a fresh `release.sql` deploy produces, plus nothing.
--
-- Run as SYS or a DBA. Step through it; several sections need you to substitute
-- ids or object names the queries report.
--
-- !! ORDER MATTERS - DO NOT SKIP SECTION 1. Drop the RULES before the scripts they
-- point at. A rule that survives its script breaks the framework's admin surface
-- (`PREPROC REFRESH` and friends fail with `attempt to call a nil value (field
-- 'rebuild')`) for anyone whose pipeline includes it, because the failed module
-- import also breaks the import the admin scripts rely on. Ordinary SQL keeps
-- working. See file 01 section 7 for the recovery recipe - in short: turn the
-- preprocessor off for your session, drop the dangling rule, refresh.
-- =============================================================================

ALTER SESSION SET sql_preprocessor_script = PREPROC_RT.MASTER;


-- -----------------------------------------------------------------------------
-- 1. DROP THE DEMO RULES
-- -----------------------------------------------------------------------------
-- Everything except the two seeded CONTROL_SUGAR_V1 rules (ids 1 and 2) came from
-- this demo. List them, then drop each id.

SELECT RULE_ID, SCOPE_TYPE, SCOPE_VALUE, PHASE, SCRIPT_NAME
FROM   PREPROC.CONFIG
WHERE  SCRIPT_NAME <> 'PREPROC_RT.CONTROL_SUGAR_V1'
ORDER BY RULE_ID;

-- For each RULE_ID above:
--   PREPROC DROP RULE <id> NOREFRESH;
--
-- ...then refresh once:
PREPROC REFRESH;

-- Should be back to just the two seeded rules:
PREPROC LIST RULES;


-- -----------------------------------------------------------------------------
-- 2. RETIRE AND RECLAIM THE INSTALLED MODULES
-- -----------------------------------------------------------------------------
-- Provenance is deliberate about lifecycle: `retire` marks a version dead,
-- `reclaim` physically drops its objects - and reclaim refuses while any enabled
-- rule or live profile still references the module. Section 1 above is what makes
-- these calls succeed.

EXECUTE SCRIPT PREPROC.MODULE_RETIRE('confd_control', 1);
EXECUTE SCRIPT PREPROC.MODULE_RETIRE('trailing_comma', 2);
EXECUTE SCRIPT PREPROC.MODULE_RETIRE('cast_shorthand', 1);

EXECUTE SCRIPT PREPROC.MODULE_RECLAIM('confd_control', 1);    -- drops all 15 objects
EXECUTE SCRIPT PREPROC.MODULE_RECLAIM('trailing_comma', 2);
EXECUTE SCRIPT PREPROC.MODULE_RECLAIM('cast_shorthand', 1);

SELECT NAME, VERSION, STATUS FROM PREPROC_RT.INSTALLED_MODULES ORDER BY 1;


-- -----------------------------------------------------------------------------
-- 3. DROP THE HAND-WRITTEN PREPROCESSORS
-- -----------------------------------------------------------------------------
-- These were never modules, so nothing tracked them - plain DROP.

DROP SCRIPT IF EXISTS PREPROC_RT.DEMO_TOP_TABLES_V1;
DROP SCRIPT IF EXISTS FIN_GUARD.SHOW_PAYROLL_V1;


-- -----------------------------------------------------------------------------
-- 4. DROP THE DEMO SCHEMAS
-- -----------------------------------------------------------------------------
-- Only two, and only from file 06. Everything else in this demo carries its sample
-- rows inline as VALUES-derived tables, so there is nothing else to clean up.
-- File 06 needs real objects because privileges are the thing being demonstrated -
-- you cannot GRANT SELECT on a subselect.

DROP SCHEMA IF EXISTS FIN_GUARD CASCADE;
DROP SCHEMA IF EXISTS FIN       CASCADE;

-- File 08, if you ran it:
DROP SCHEMA IF EXISTS CONFD_GUARD CASCADE;


-- -----------------------------------------------------------------------------
-- 5. DROP THE DEMO USERS AND ROLES
-- -----------------------------------------------------------------------------

DROP USER IF EXISTS DEMO_ANA     CASCADE;
DROP USER IF EXISTS DEMO_BOB     CASCADE;
DROP USER IF EXISTS DEMO_FIN_ANA CASCADE;

DROP ROLE IF EXISTS DEMO_ANALYST CASCADE;
DROP ROLE IF EXISTS DEMO_ETL     CASCADE;
DROP ROLE IF EXISTS DEMO_FIN     CASCADE;


-- -----------------------------------------------------------------------------
-- 6. DROP THE CONNECTIONS
-- -----------------------------------------------------------------------------

DROP CONNECTION IF EXISTS PREPROC_BFS;
DROP CONNECTION IF EXISTS CONFD_CONNECTION;


-- -----------------------------------------------------------------------------
-- 7. REFRESH AND CONFIRM
-- -----------------------------------------------------------------------------
-- Dropping users and roles changed the resolution inputs, so this is the refresh
-- contract from file 04 one last time.

PREPROC REFRESH;
-- CURRENT
PREPROC STATUS;

-- The two seeded rules only
PREPROC LIST RULES;
SELECT * FROM PREPROC.RESOLUTION ORDER BY 1; -- SYS and any DBA holders only

-- Retired profile scripts are reclaimed by a later refresh once no session could
-- still be pinned to them - non-NULL ORPHANED_AT rows are expected here and will
-- disappear on their own.
SELECT PROFILE_HASH, CREATED_AT, ORPHANED_AT FROM PREPROC.PROFILES ORDER BY CREATED_AT;


-- -----------------------------------------------------------------------------
-- 8. DEACTIVATE (only if you activated system-wide)
-- -----------------------------------------------------------------------------
-- The session-scoped activation this demo used dies with the session; nothing to
-- undo. If you ran the system-wide form:
--
--   ALTER SYSTEM SET sql_preprocessor_script = NULL;
--
-- The framework itself stays deployed. Removing it entirely is a DROP of the two
-- schemas and the role - deliberately not scripted here.


-- -----------------------------------------------------------------------------
-- 9. BUCKETFS
-- -----------------------------------------------------------------------------
-- The staged tarball is still in the bucket. Remove it outside SQL if you want to:
--
--   curl -k -X DELETE --user w:<write-pw> \
--        https://<host>:2581/default/preproc-lib-0.3.0.tar.gz
--
-- ...or, now that confd_control is gone, note that it could have done this
-- itself:  CONTROL BUCKETFS DELETE FILE '...' FROM BUCKET 'default' IN SERVICE 'bfsdefault'
-- =============================================================================
