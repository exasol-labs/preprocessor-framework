-- =============================================================================
-- DEMO 02 - INSTALLING A MODULE FROM THE COMMUNITY LIBRARY (over HTTPS)
-- =============================================================================
-- WHAT THIS SHOWS
--   Browsing and installing a published preprocessor from SQL, with no CLI, no
--   Python toolchain, no file to hand-carry, and no DDL to write. Two statements
--   take you from "nothing installed" to "the engine accepts new syntax".
--
-- THE PUNCHLINE
--   Exasol gives you exactly ONE preprocessor slot. The framework owns it and
--   multiplexes it, so a syntax transformation becomes an installable,
--   versioned, provenance-tracked artifact instead of a fork of somebody's
--   monolithic script.
--
-- PREREQUISITES
--   * 01_administration.sql has been run (the preprocessor is active for your
--     session; re-run the ALTER SESSION below if you reconnected).
--   * The cluster has outbound HTTPS access to raw.githubusercontent.com.
--     No egress? That is exactly what file 03 is for.
-- =============================================================================

ALTER SESSION SET sql_preprocessor_script = PREPROC_RT.MASTER;


-- -----------------------------------------------------------------------------
-- 1. BROWSE THE LIBRARY BEFORE INSTALLING ANYTHING
-- -----------------------------------------------------------------------------
-- With no source given, the framework resolves the public Exasol Labs library at
-- its `latest` release. `CATALOG` is strictly read-only - it touches no rule, no
-- setting, no resolution.

PREPROC CATALOG MODULES;

-- Read the result carefully, because it is the review surface for an install:
--   NAME / VERSION / PHASE  - what it is and where in the pipeline it runs
--   DEPLOY_MODE             - `library-deployed` (the library ships the code) vs
--                             `self-deployed` (the module's own project installs it)
--   INSTALLED               - TRUE only for a currently deployed version
--   OBJECT_COUNT / OBJECTS  - EVERY object the install would create, declared up
--                             front. You see the full blast radius before you
--                             run anything.
--
-- Pinning: to install from a specific release, a fork, or your own private
-- library, name the index explicitly. The default is a convenience, not a
-- hardcoded dependency.
--
--   PREPROC CATALOG MODULES FROM 'https://raw.githubusercontent.com/exasol-labs/preprocessor-library/v0.2.1/registry/index.json';


-- -----------------------------------------------------------------------------
-- 2. SOMETHING TO TRANSFORM
-- -----------------------------------------------------------------------------
-- Sample rows inline, as a VALUES-derived table. This demo deliberately creates no
-- schema and no table: the transformations are about SQL TEXT, so anything a
-- transformed statement needs should be visible in the statement itself. It also
-- means nothing to clean up, and nothing to grant when files 04 and 06 hand these
-- statements to other users.
--
-- Amounts arriving as text is the migration reality this module exists for.
--
-- PostgreSQL-style cast shorthand is a hard syntax error in Exasol. Run it now and
-- see the failure - this is the "before" shot:
--
--   SELECT ORDER_ID, AMOUNT_TXT::DECIMAL(10,2)
--   FROM   VALUES (1,'1299.50','EMEA') AS T(ORDER_ID, AMOUNT_TXT, REGION);
--   -- syntax error, unexpected DOUBLE_COLON_


-- -----------------------------------------------------------------------------
-- 3. INSTALL IT
-- -----------------------------------------------------------------------------
-- One statement: resolve the library index, verify the artifact's sha256, create
-- the script, register the activation rule, write the provenance row, refresh.
-- Any failure in that chain aborts BEFORE the first write - a bad download never
-- leaves a half-installed module behind.
--
-- `FOR ROLE DBA` is the audience, not the permission. It says "users holding DBA
-- get this transformation". Install permission is a separate thing entirely:
-- PREPROC_ADMIN (or DBA), enforced by the admin script itself.

PREPROC INSTALL MODULE cast_shorthand FOR ROLE DBA;

-- The `::` syntax now works. Nothing was changed in the engine, in your client,
-- or in the query - the statement was rewritten to CAST(...) before compilation.
SELECT ORDER_ID,
       AMOUNT_TXT::DECIMAL(10,2) AS AMOUNT
FROM   VALUES (1,'1299.50','EMEA'), (2,'87.25','APAC'), (3,'540.00','EMEA')
         AS T(ORDER_ID, AMOUNT_TXT, REGION)
ORDER BY 1;

-- NOTE FOR THE PRESENTER: cast_shorthand v1 does not rewrite a *string-literal*
-- operand (`'42'::DECIMAL` is returned unchanged). It fails CLOSED - the
-- original text reaches the engine and raises a normal syntax error rather than
-- producing half-rewritten SQL. Demo with a column or a numeric literal.


-- -----------------------------------------------------------------------------
-- 4. WHAT THE INSTALL RECORDED
-- -----------------------------------------------------------------------------
-- Provenance is not a log line - it is a queryable, PUBLIC-readable table. For
-- an audit, "which transformations are running on this cluster, from where, at
-- which version, verified against which hash" is one SELECT.

SELECT NAME, VERSION, SOURCE_REPO, SOURCE_REF, LIBRARY_VERSION,
       SUBSTR(SHA256, 1, 16) AS SHA256_HEAD, STATUS, INSTALLED_AT
FROM   PREPROC_RT.INSTALLED_MODULES
ORDER BY NAME, VERSION;

-- Note SOURCE_REF: you asked for `latest` and the framework recorded the
-- CONCRETE release it resolved to. The convenience of a moving tag without
-- losing the ability to say exactly what you are running.

-- And the rule the install registered on your behalf - an ordinary rule, no
-- different from one you would add by hand:
PREPROC LIST RULES;


-- -----------------------------------------------------------------------------
-- 5. THE MODULE AND ITS AUDIENCE ARE INDEPENDENT
-- -----------------------------------------------------------------------------
-- Installing deployed code and bound ONE audience to it. Widening or narrowing
-- that audience later is a rule change, not a reinstall - no code moves.
--
--   PREPROC ADD RULE FOR ROLE ANALYSTS PHASE TRANSLATE SCRIPT PREPROC_RT.CAST_SHORTHAND_V1 FUNCTION cast_shorthand
--   PREPROC DISABLE RULE 3     -- keep the row and the code, stop applying it
--   PREPROC ENABLE  RULE 3
--
-- File 04 does this for real against demo users and roles.


-- =============================================================================
-- WHERE WE ARE
--   A published transformation, installed from SQL in one statement, with
--   verified provenance and an audience you can retarget at will.
--
-- NEXT: 03_module_from_bucketfs.sql - the same thing on a cluster with no
--       internet access.
-- =============================================================================
