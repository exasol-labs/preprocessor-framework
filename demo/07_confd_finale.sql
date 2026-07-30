-- =============================================================================
-- DEMO 07 - THE FINALE: CLUSTER ADMINISTRATION AS SQL  (the confd_control module)
-- =============================================================================
-- WHAT THIS SHOWS
--   Everything in files 01-06 used deliberately small examples. This is the same
--   machinery carrying something that would otherwise be a product: a full
--   administrative command surface for the Exasol cluster, delivered as ONE
--   module install.
--
-- THE PUNCHLINE
--   Exasol cluster administration normally lives outside SQL - ConfD's XML-RPC
--   interface, the Admin UI, `confd_client` on a container shell. `confd_control`
--   moves it INTO SQL, and it needed no change to the framework to do it. Fifteen
--   objects, one rule, one command. `CONTROL DATABASE SHOW INFO` is not Exasol
--   syntax; after this install, it is.
--
--   And the reads are ordinary table expressions. `SELECT ... FROM (CONTROL ...)`
--   means backup state, storage layout, log events, and license data are joinable,
--   filterable, and persistable with the SQL you already write. That is the part
--   that tends to change the room.
--
-- PREREQUISITES
--   * Files 01-03 have been run (in particular the BucketFS CONNECTION from 03,
--     if you install from the staged tarball).
--   * You hold `DBA` - the module's rules are DBA-scoped by design.
--   * A `CONFD_CONNECTION` pointing at ConfD's XML-RPC endpoint, with a ConfD
--     user authorised for the jobs. See demo/README.md section ConfD setup; it is a
--     five-minute one-off, and the demo is much better with it.
-- =============================================================================

ALTER SESSION SET sql_preprocessor_script = PREPROC_RT.MASTER;


-- -----------------------------------------------------------------------------
-- 1. THE CONFD ENDPOINT
-- -----------------------------------------------------------------------------
-- The gateway reaches ConfD through a CONNECTION object, so the credential is a
-- managed database object rather than anything embedded in a script. Same pattern
-- as the BucketFS connection in file 03: the CONNECTION is the privilege
-- boundary.
--
-- Port 20003 on the first active node for a normal deployment; a Docker
-- single-node image exposes ConfD's XML-RPC on 443 instead (check
-- `XMLRPCPort` in /exa/etc/EXAConf).

CREATE OR REPLACE CONNECTION CONFD_CONNECTION
    TO 'https://<first-node-host>:20003'
    USER '<confd_user>' IDENTIFIED BY '<password>';


-- -----------------------------------------------------------------------------
-- 2. LOOK BEFORE YOU INSTALL
-- -----------------------------------------------------------------------------
-- The catalog is the review surface, and this is where the OBJECT_COUNT column
-- earns its place. Two modules in this library create one object each.
-- `confd_control` creates fifteen - and names every one of them, before you
-- install anything.

PREPROC CATALOG MODULES FROM 'bucketfs:PREPROC_BFS/preproc-lib-0.3.0.tar.gz';

-- Read the OBJECTS column for confd_control and note where things land - this is
-- file 06's lesson applied by a real module, not by a demo:
--
--   PREPROC_RT (PUBLIC EXECUTE)   CONFD_CONTROL_V1        the entry module
--                                 CONFD_QUERY_V1          read gateways, called
--                                 LOG_EVENTS_V1           inline as table exprs
--                                 SESSION_LOG_V1
--                                 REMOTE_VOLUME_QUERY_V1
--                                 CONFD_JOB_V1 ...          the generic gateways
--
--   PREPROC (no PUBLIC grant)     ADMIN_CONFD_JOB_V1      every privileged,
--                                 ADMIN_VIRTUAL_RESTORE_V1  state-changing step
--                                 ADMIN_BUCKETFS_FILE_V1
--                                 ADMIN_LICENSE_V1
--                                 ADMIN_REMOTE_VOLUME_V1
--
-- The things a user must be able to call sit in the PUBLIC schema; everything
-- that changes cluster state sits behind the admin boundary. The catalog lets a
-- reviewer verify that split before granting the install.


-- -----------------------------------------------------------------------------
-- 3. ONE STATEMENT
-- -----------------------------------------------------------------------------
-- Fifteen objects, in file order, with the artifact's sha256 verified first; then
-- ONE DBA-scoped EXPAND rule on the entry script, one provenance row, one
-- refresh. The composite module is retired and reclaimed as a single unit too -
-- it is one lifecycle object, not fifteen loose scripts.

PREPROC INSTALL MODULE confd_control
    FROM 'bucketfs:PREPROC_BFS/preproc-lib-0.3.0.tar.gz'
    FOR ROLE DBA;

-- Installing from the bucket is deliberate: it shows the big module reaching an
-- air-gapped cluster. The HTTPS default works identically now that the library
-- publishes it -
--   PREPROC INSTALL MODULE confd_control FOR ROLE DBA;

SELECT NAME, VERSION, PHASE, DEPLOY_MODE, STATUS, OBJECTS
FROM   PREPROC_RT.INSTALLED_MODULES
WHERE  NAME = 'confd_control';

-- Exactly ONE new rule for those fifteen objects:
PREPROC LIST RULES;


-- -----------------------------------------------------------------------------
-- 4. CLUSTER ADMINISTRATION, TYPED AS SQL
-- -----------------------------------------------------------------------------
-- None of the following is Exasol syntax. All of it works now.
--
-- !! PRESENTER NOTE: unlike the framework's own `PREPROC ...` module, confd_control
-- does NOT strip leading comment lines - a `CONTROL ...` statement submitted with a
-- comment block glued to the front of it is declined and you get
-- `syntax error, unexpected IDENTIFIER_PART_`. If your client sends preceding
-- comments along with the statement, select just the `CONTROL ...` line and execute
-- that. (File 05 section 2 shows the three-line fix a module author applies to avoid
-- this; it is exactly why the framework's own command module has it.)

CONTROL DATABASE SHOW INFO;
CONTROL BUCKETFS SHOW SERVICES;
CONTROL BUCKETFS SHOW SERVICE 'bfsdefault';
CONTROL STORAGE SHOW VOLUMES;
CONTROL DATABASE SHOW BACKUPS;
CONTROL DATABASE SHOW LICENSE;


-- -----------------------------------------------------------------------------
-- 5. THE BEAT THAT LANDS: READS ARE TABLE EXPRESSIONS
-- -----------------------------------------------------------------------------
-- Every `SHOW ...` rewrites to an inline-capable UDF call, so it composes with
-- ordinary SQL. Cluster state stops being something you read off a screen and
-- starts being something you query.

-- Filter it:
SELECT FIELD, VALUE_TEXT
FROM   (CONTROL DATABASE SHOW INFO)
WHERE  FIELD IN ('state', 'config.version', 'config.num_active_nodes')
ORDER BY 1;

-- Aggregate and pivot it like any other rowset:
SELECT COUNT(*)                                                    AS FIELDS_RETURNED,
       MAX(CASE WHEN FIELD = 'config.version' THEN VALUE_TEXT END) AS DB_VERSION,
       MAX(CASE WHEN FIELD = 'state'          THEN VALUE_TEXT END) AS DB_STATE
FROM   (CONTROL DATABASE SHOW INFO);

-- And JOIN live cluster state against what you expect it to be - a config-drift
-- check, in one statement, creating nothing. This is the moment monitoring stops
-- being a screen you read and becomes a query you run:
SELECT e.FIELD, e.EXPECTED, i.VALUE_TEXT AS ACTUAL,
       CASE WHEN i.VALUE_TEXT = e.EXPECTED THEN 'ok' ELSE 'DRIFT' END AS VERDICT
FROM   VALUES ('state','running'), ('config.num_active_nodes','1'),
              ('config.name','EXPECTED_NAME')
         AS e(FIELD, EXPECTED)
LEFT JOIN (CONTROL DATABASE SHOW INFO) i ON i.FIELD = e.FIELD
ORDER BY 1;

-- Persisting it is then just CREATE TABLE AS SELECT, if you want history - but note
-- only ONE `CONTROL ...` read can appear per statement, so build multi-read reports
-- by materialising or by running them separately.

-- Typed rows, not just key/value. Log events come back as TS / NODE / PRIORITY /
-- MESSAGE, so they are filterable and aggregatable:
SELECT PRIORITY, COUNT(*) AS EVENTS
FROM   (CONTROL DATABASE SHOW LOG EVENTS FOR SERVICE 'ConfD' WITHIN '30m')
GROUP BY PRIORITY
ORDER BY 2 DESC;

-- And the one that usually gets an audible reaction - your own session's SQL log,
-- as a result set. Behind this: an ephemeral private bucket created and cleaned up
-- by the module, a ConfD debug_collect, and extraction of just your session's
-- lines. From the outside it is a table.
SELECT LINE_NO, LOG_TEXT
FROM   (CONTROL DATABASE SHOW SESSION LOG FOR CURRENT_SESSION)
ORDER BY LINE_NO
LIMIT 20;


-- -----------------------------------------------------------------------------
-- 6. THE WIDER SURFACE (documented; these mutate cluster state)
-- -----------------------------------------------------------------------------
-- Not for a live demo, but worth showing on the slide - this is the shape of an
-- administrative DSL delivered as a preprocessor module:
--
--   CONTROL DATABASE START BACKUP TO VOLUME ArchiveVolume1 WITH LEVEL 0 AND EXPIRATION '1w'
--   CONTROL DATABASE ADD BACKUP SCHEDULE 'nightly' TO VOLUME ArchiveVolume1 AT '0 2 * * *' ENABLED
--   CONTROL DATABASE START VIRTUAL RESTORE FROM BACKUP '...' FOR SCHEMA 'RETAIL' USING CONNECTION vr_jdbc AND EXA CONNECTION vr_exa
--   CONTROL DATABASE INSTALL LICENSE FROM BUCKETFS FILE 'licenses/exasol.license' IN BUCKET 'default'
--   CONTROL BUCKETFS ADD BUCKET 'mybucket' TO SERVICE 'bfsdefault' PUBLIC
--   CONTROL BUCKETFS UPLOAD FILE '<presigned-url>' TO BUCKET 'default' IN SERVICE 'bfsdefault' AS 'jars/mylib.jar'
--   CONTROL STORAGE ADD REMOTE VOLUME 'archive1' TYPE 's3' URL 'https://...' USING CONNECTION s3_creds
--
-- Mutating commands go through the `PREPROC.ADMIN_*_V1` scripts in the locked
-- schema and RAISE on failure - a failed ConfD job is a loud script error, never
-- an empty result set that reads like success.
--
-- Full reference: docs/confd-admin.md in the confd-via-sql repository.


-- -----------------------------------------------------------------------------
-- 7. WHY THIS IS THE RIGHT CLOSING ARGUMENT
-- -----------------------------------------------------------------------------
-- Trace what the framework actually contributed:
--
--   * It owns Exasol's single preprocessor slot, so confd_control did not have to
--     fight for it - and neither will the next add-on. Two, five, twenty syntax
--     providers coexist because MASTER multiplexes them.
--   * It made a fifteen-object subsystem installable in one statement, with the
--     object inventory declared and reviewable up front, from GitHub or from a
--     bucket on an air-gapped cluster.
--   * It scoped the whole surface to `ROLE DBA` with one rule, and left the
--     privileged internals behind a schema with no PUBLIC grant.
--   * It kept the fail-open contract: this module can be broken, missing, or
--     revoked, and ordinary SQL on this cluster keeps working.
--   * Every bit of it is queryable - which rules exist, which modules are
--     installed, where they came from, what each user resolves to.
--
-- None of that is specific to ConfD. It is what the next thing gets for free.


-- =============================================================================
-- CLEAN UP: 99_teardown.sql removes everything this demo created.
-- =============================================================================
