-- =============================================================================
-- DEMO 03 - INSTALLING A MODULE FROM BUCKETFS (air-gapped, no egress)
-- =============================================================================
-- WHAT THIS SHOWS
--   The same catalog-then-install flow as file 02, against a release tarball
--   staged in BucketFS instead of GitHub. Same commands, same end state, same
--   provenance table - one different source string.
--
-- THE PUNCHLINE
--   The air-gap story is not a degraded mode. A BucketFS read is cluster-local:
--   the resolver UDF reads the bucket off the local mount, so a database with
--   zero outbound network access installs modules exactly like a connected one.
--   That is what makes this deployable in the environments Exasol actually lives
--   in.
--
-- PREREQUISITES  (the presenter does these once, outside SQL - see demo/README.md)
--   a) A library release tarball is staged in a bucket, e.g.
--        curl -k -X PUT --user w:<write-pw> \
--             -T preproc-lib-0.3.0.tar.gz \
--             https://<host>:2581/default/preproc-lib-0.3.0.tar.gz
--      Get the tarball from the library's GitHub Release, or build it from a
--      checkout with `python3 scripts/build_release.py`.
--   b) The CONNECTION below exists and PREPROC_ADMIN has ACCESS on it.
--
--   Adjust the bucket path and the read password to your cluster.
-- =============================================================================

ALTER SESSION SET sql_preprocessor_script = PREPROC_RT.MASTER;


-- -----------------------------------------------------------------------------
-- 1. THE ONE THING THE OPERATOR OWNS: A BUCKETFS CONNECTION
-- -----------------------------------------------------------------------------
-- The framework never creates this, and it never even sees its name - no
-- source string this framework consumes carries a CONNECTION name. On a
-- READ-PROTECTED bucket, a CONNECTION object is the privilege boundary for
-- BucketFS access: whoever holds ACCESS on it can read that bucket, and
-- nobody else can - including through this framework. (`default`, used
-- below, is Exasol's always-public bucket, so that boundary does not apply
-- to it here - see the note after the GRANT.)
--
-- Use the NATIVE BucketFS address `bucketfs:<service>/<bucket>`, NOT an
-- https://...:2581/... URL. The native form is read off the cluster-local mount:
-- no host, no port, no network egress.

CREATE OR REPLACE CONNECTION PREPROC_BFS
    TO 'bucketfs:bfsdefault/default'
    IDENTIFIED BY '<bucket-read-password>';

GRANT ACCESS ON CONNECTION PREPROC_BFS TO PREPROC_ADMIN;

-- `default` is Exasol's always-public bucket, so its mount is already visible
-- with or without this grant. On a READ-PROTECTED bucket, this GRANT is what
-- makes the mount appear at all - that is the case this step exists for.

-- Worth saying out loud: the resolver UDF that reads the mount this grant makes
-- visible - by location, never through the CONNECTION object itself - lives in
-- the locked `PREPROC` schema and is never granted to PUBLIC. There is no path
-- by which an ordinary user reaches BucketFS - or the network - through the
-- framework. The admin surface is the only door.


-- -----------------------------------------------------------------------------
-- 2. BROWSE THE STAGED CATALOG
-- -----------------------------------------------------------------------------
-- Identical command to file 02, with an explicit source. BucketFS auto-extracts
-- an uploaded archive on arrival; you still name the archive exactly as you
-- uploaded it and the resolver finds the extracted content behind that name.

PREPROC CATALOG MODULES FROM 'bucketfs:bfsdefault/default/preproc-lib-0.3.0.tar.gz';

-- One thing to point at in this result:
--
--  * `cast_shorthand` shows INSTALLED = TRUE. The catalog is source-agnostic but
--    installation-aware: it knows you already have that module from file 02, even
--    though you installed it from a completely different source.
--
-- Compare against the HTTPS catalog from file 02. The tarball names an exact
-- release; the default HTTPS source follows the mutable `latest` tag. They may be
-- the same release today and diverge tomorrow, which is the point - an air-gapped
-- cluster runs a deliberately chosen release, and the provenance table records
-- which one each module actually came from either way.


-- -----------------------------------------------------------------------------
-- 3. INSTALL FROM THE TARBALL
-- -----------------------------------------------------------------------------
-- The `FROM '<source>'` clause is the ONLY difference from file 02. Everything
-- downstream - sha256 verification, script creation, rule registration,
-- provenance, refresh - is the same code path.

PREPROC INSTALL MODULE trailing_comma
    FROM 'bucketfs:bfsdefault/default/preproc-lib-0.3.0.tar.gz'
    FOR ROLE DBA;

-- A trailing comma before FROM is a syntax error in every SQL dialect, and the
-- single most common thing a human does while editing a select list. Now it just
-- works:
SELECT ORDER_ID,
       REGION,
FROM   VALUES (1,'1299.50','EMEA'), (2,'87.25','APAC'), (3,'540.00','EMEA')
         AS T(ORDER_ID, AMOUNT_TXT, REGION)
ORDER BY 1;

-- Both modules are in the TRANSLATE phase, and both run - TRANSLATE is run-all,
-- threading the text through each module in turn. Trailing comma AND cast
-- shorthand in one statement:
SELECT ORDER_ID,
       AMOUNT_TXT::DECIMAL(10,2) AS AMOUNT,
FROM   VALUES (1,'1299.50','EMEA'), (2,'87.25','APAC'), (3,'540.00','EMEA')
         AS T(ORDER_ID, AMOUNT_TXT, REGION)
ORDER BY 1;


-- -----------------------------------------------------------------------------
-- 4. PROVENANCE RECORDS THE TRANSPORT
-- -----------------------------------------------------------------------------
-- One table, both install paths, distinguishable. "Where did this transformation
-- come from" has a real answer for every row.

SELECT NAME, VERSION, SOURCE_URL, LIBRARY_VERSION, STATUS
FROM   PREPROC_RT.INSTALLED_MODULES
ORDER BY NAME, VERSION;

PREPROC LIST RULES;


-- -----------------------------------------------------------------------------
-- 5. FOR COMPLETENESS: THE OTHER TWO PATHS
-- -----------------------------------------------------------------------------
-- Everything above ran from a SQL client. The framework also ships an operator
-- CLI for teams that prefer a toolchain, and a fully offline bundle for clusters
-- where even BucketFS staging is inconvenient:
--
--   uv run preproc module list
--   uv run preproc module add trailing_comma --scope ROLE:DBA
--   uv run preproc module sync desired-state.toml   -- declarative convergence
--   uv run preproc module bundle trailing_comma --output offline.sql
--
-- `bundle` emits one self-contained .sql to hand-carry and run directly - no
-- CLI and no database connection needed on the far side. All paths converge on
-- the identical end state, so dispatch behaves the same regardless of how a
-- module arrived.


-- =============================================================================
-- WHERE WE ARE
--   Two modules installed by two different transports, both fully accounted for.
--   No cluster on earth is excluded from the module ecosystem by its network
--   policy.
--
-- NEXT: 04_users_roles_refresh.sql - who gets which transformation, and the one
--       operational rule you must internalise.
-- =============================================================================
