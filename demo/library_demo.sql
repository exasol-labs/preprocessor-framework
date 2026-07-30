-- ===========================================================================
-- DEMO 2 of 2 - THE MODULE LIBRARY
-- Narration, timings, and the shell steps for beat [6]: library_demo.md
--
-- Assumes demo 1 was watched, not that it was run. Needs a clean `release.sql`
-- install, the preprocessor active, and a DBA/SYS connection.
-- Beat [3] needs a BucketFS-staged tarball; beat [6] is mostly shell, not SQL.
-- ===========================================================================

ALTER SESSION SET sql_preprocessor_script = PREPROC_RT.MASTER;


-- [1] WHAT IS IN THE LIBRARY ----------------------------------- 0:00

-- No source given: resolves the public Exasol Labs library at its `latest`
-- release. Read-only - it touches no rule, no setting, no resolution.
PREPROC CATALOG MODULES;

-- The columns are the review surface for an install:
--   NAME / VERSION / PHASE    what it is, and where in the pipeline it runs
--   DEPLOY_MODE               library-deployed = the library ships the code;
--                             self-deployed    = the module's own project does
--   INSTALLED                 TRUE only for a currently deployed version
--   OBJECT_COUNT / OBJECTS    every object the install would create, named
--                             up front. Blast radius before you run anything.


-- [2] INSTALL ONE ---------------------------------------------- 1:00

-- One statement: resolve the index, verify the artifact's sha256, create the
-- script, register the rule, write provenance, refresh. Any failure aborts
-- BEFORE the first write - a bad download never half-installs.
--
-- `FOR ROLE DBA` is the AUDIENCE, not the permission. Install permission is
-- PREPROC_ADMIN, enforced by the admin script regardless of this clause.
PREPROC INSTALL MODULE cast_shorthand FOR ROLE DBA;

-- PostgreSQL cast shorthand is a hard syntax error in Exasol. Not any more:
SELECT ORDER_ID, AMOUNT_TXT::DECIMAL(10,2) AS AMOUNT
FROM   VALUES (1,'1299.50'), (2,'87.25') AS T(ORDER_ID, AMOUNT_TXT);

-- Provenance is a queryable table, not a log line: what is running here, from
-- where, at which version, verified against which hash.
SELECT NAME, VERSION, SOURCE_REPO, SOURCE_REF, LIBRARY_VERSION,
       SUBSTR(SHA256, 1, 16) AS SHA256_HEAD, STATUS
FROM   PREPROC_RT.INSTALLED_MODULES ORDER BY NAME;

-- Pinning: name an index explicitly for a fixed release, a fork, or your own
-- private library. The default is a convenience, not a dependency.
--   PREPROC CATALOG MODULES FROM 'https://raw.githubusercontent.com/exasol-labs/preprocessor-library/v0.3.0/registry/index.json';


-- [3] INSTALL WITHOUT INTERNET --------------------------------- 2:15

-- One CONNECTION is the privilege boundary for the bucket. Use the NATIVE
-- address form - it is read off the cluster-local mount, no network egress.
CREATE OR REPLACE CONNECTION PREPROC_BFS
    TO 'bucketfs:bfsdefault/default' IDENTIFIED BY '<bucket-read-password>';

GRANT ACCESS ON CONNECTION PREPROC_BFS TO PREPROC_ADMIN;

-- Same catalog command, one different source string.
PREPROC CATALOG MODULES FROM 'bucketfs:PREPROC_BFS/preproc-lib-0.3.0.tar.gz';

-- Same install command, one different source string.
PREPROC INSTALL MODULE trailing_comma
    FROM 'bucketfs:PREPROC_BFS/preproc-lib-0.3.0.tar.gz'
    FOR ROLE DBA;

-- A trailing comma before FROM: a syntax error everywhere, and the single most
-- common thing a human does while editing a select list.
SELECT ORDER_ID, AMOUNT_TXT::DECIMAL(10,2) AS AMOUNT,
FROM   VALUES (1,'1299.50'), (2,'87.25') AS T(ORDER_ID, AMOUNT_TXT);

-- Both modules are TRANSLATE-phase and both ran on that statement: TRANSLATE is
-- run-all, threading the text through each in turn.

-- One table, both transports, distinguishable:
SELECT NAME, VERSION, SOURCE_URL, LIBRARY_VERSION
FROM   PREPROC_RT.INSTALLED_MODULES ORDER BY NAME;


-- [4] WHO GETS IT ---------------------------------------------- 3:30

-- Installing deployed code and bound ONE audience. Retargeting is a rule change,
-- not a reinstall - the code does not move. (Demo 1 covers roles in depth.)
PREPROC LIST RULES;

--   PREPROC ADD RULE FOR ROLE ANALYSTS PHASE TRANSLATE SCRIPT PREPROC_RT.CAST_SHORTHAND_V1 FUNCTION cast_shorthand
--   PREPROC DISABLE RULE <id>      -- keep the row and the code, stop applying it
--   PREPROC ENABLE  RULE <id>


-- [5] TAKING ONE BACK OUT -------------------------------------- 4:00

-- Retire marks a version dead; reclaim physically drops its objects - and
-- refuses while any enabled rule or live profile still references it.
EXECUTE SCRIPT PREPROC.MODULE_RETIRE('trailing_comma', 2);

-- Blocked, on purpose: the rule is still there.
EXECUTE SCRIPT PREPROC.MODULE_RECLAIM('trailing_comma', 2);

-- Drop the rule first (use the trailing_comma rule id from LIST RULES above).
PREPROC DROP RULE 4;

EXECUTE SCRIPT PREPROC.MODULE_RECLAIM('trailing_comma', 2);

SELECT NAME, VERSION, STATUS FROM PREPROC_RT.INSTALLED_MODULES ORDER BY NAME;


-- [6] PUBLISH YOUR OWN ----------------------------------------- 4:45
--
-- Mostly shell and files, not SQL - the steps and the worked example are in
-- library_demo.md section Beat [6]. The finished module is in demo/library_demo/top_objects/,
-- and it is the `SHOW TOP [n] OBJECTS` preprocessor from demo 1, packaged.
--
-- After `preproc module add top_objects --registry <your checkout>`, the command
-- you hand-wrote in the first video is an installable module:

--   SHOW TOP 3 OBJECTS;

-- ...and it is tracked like any other, with the local checkout recorded as the
-- source it came from:

--   SELECT NAME, VERSION, SOURCE_URL, DEPLOY_MODE, STATUS
--   FROM PREPROC_RT.INSTALLED_MODULES WHERE NAME = 'top_objects';


-- [7] CLOSE ---------------------------------------------------- 7:30

PREPROC CATALOG MODULES;

PREPROC LIST RULES;
