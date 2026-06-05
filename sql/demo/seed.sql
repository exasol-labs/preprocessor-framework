-- =============================================================================
-- PREPROCESSOR FRAMEWORK — DEMO CONFIGURATION SEED  (sql/demo/seed.sql)
-- =============================================================================
-- This file is the canonical, runnable documentation of the framework's config
-- model. Read it top to bottom: it walks through every concept the framework
-- exists for — ROLE vs USER scoping, the three phases, client-pattern dispatch,
-- and how PREPROC.CONFIG is resolved into per-user pipelines by PREPROC.REFRESH.
--
-- THE POINT OF THE DEMO
--   One config-driven lookup gives DIFFERENT users DIFFERENT pipelines:
--     DEMO_USER_ANALYST  -> TRANSLATE + EXPAND + REWRITE   (the full pipeline)
--     DEMO_USER_ETL      -> TRANSLATE (only under the EXAplus client) + REWRITE
--   Same table, same MASTER dispatcher, divergent behaviour — that is the
--   framework's selling point, made visible.
--
-- WHO RUNS THIS
--   An admin holding PREPROC_ADMIN (or DBA). The script writes PREPROC.CONFIG
--   and runs PREPROC.REFRESH; PREPROC.REFRESH reads the dictionary tables
--   (EXA_DBA_USERS / EXA_DBA_ROLE_PRIVS), which requires SELECT ANY DICTIONARY —
--   a privilege held by PREPROC_ADMIN. A non-admin caller fails at REFRESH.
--
-- WHAT THIS DOES NOT DO
--   It does NOT activate the preprocessor. Turning the preprocessor on for the
--   session/system stays a separate, deliberate DBA action (ALTER SESSION /
--   ALTER SYSTEM SET sql_preprocessor_script=...). Seeding config is safe and
--   reversible on its own; activation is the consequential step.
--
-- RE-RUNNING THIS FILE
--   The CONFIG inserts below are guarded with WHERE NOT EXISTS, so re-applying
--   the rules is a no-op (mirroring sql/install/30_settings_seed.sql). The
--   CREATE ROLE / CREATE USER statements, however, have NO "IF NOT EXISTS" form
--   in Exasol (see sql/install/10_schemas.sql) and this standalone script is not
--   processed by install.py's --@tolerate-exists splitter. Re-running on a DB
--   that already has the demo objects therefore raises "object already exists"
--   on those CREATE lines — that is harmless, or run sql/demo/teardown.sql first
--   for a clean slate. The --@tolerate-exists markers are kept for documentary
--   intent and so this file stays splitter-compatible if ever fed to install.py.
--
-- COLUMN MODEL (verified against sql/install/20_tables.sql)
--   PREPROC.CONFIG columns are:
--     rule_id, scope_type, scope_value, client_pattern, phase,
--     script_name, function_name, enabled, version
--   There is deliberately NO ordinal column on CONFIG. Ordering within a phase
--   is derived by PREPROC.REFRESH from rule_id ASCENDING and materialised as the
--   `ordinal` column of PREPROC.RESOLUTION. That is why the reserved rule_id
--   values below are assigned in pipeline order.
-- =============================================================================


-- -----------------------------------------------------------------------------
-- 1. DEMO ROLES
-- -----------------------------------------------------------------------------
-- Roles are the primary scoping unit. A ROLE-scoped CONFIG rule applies to every
-- user who holds that role (transitively — PREPROC.REFRESH walks the full role
-- grant closure). All demo objects carry a DEMO_ prefix so they are unmistakable
-- and safe to leave deployed or remove wholesale.

--@tolerate-exists
CREATE ROLE DEMO_ANALYST;

--@tolerate-exists
CREATE ROLE DEMO_ETL;


-- -----------------------------------------------------------------------------
-- 2. DEMO USERS
-- -----------------------------------------------------------------------------
-- Two users, each granted exactly one demo role. Because their role sets differ,
-- their resolved pipelines will differ — which is the whole demonstration.
-- Passwords are intentionally simple demo credentials; these users have no
-- system privileges beyond the PUBLIC grants on PREPROC_RT.

--@tolerate-exists
CREATE USER DEMO_USER_ANALYST IDENTIFIED BY "DemoAnalyst1!";
GRANT DEMO_ANALYST TO DEMO_USER_ANALYST;

--@tolerate-exists
CREATE USER DEMO_USER_ETL IDENTIFIED BY "DemoEtl1!";
GRANT DEMO_ETL TO DEMO_USER_ETL;


-- -----------------------------------------------------------------------------
-- 3. CONFIG RULES  (reserved demo rule_id range: 9000–9999)
-- -----------------------------------------------------------------------------
-- The 9000+ range is reserved for the demo so it can never collide with operator
-- rules (which live below it). teardown.sql deletes exactly this range.
--
-- Every rule sets enabled = TRUE (REFRESH only resolves enabled rules) and
-- version = 1 (both columns are NOT NULL in CONFIG). Each INSERT is guarded with
-- WHERE NOT EXISTS on its rule_id so re-running the seed never duplicates a rule.
--
-- rule_id ordering encodes pipeline order within a phase: the lowest rule_id for
-- a given (user, phase) becomes ordinal 1 in RESOLUTION.

-- .............................................................................
-- Rule 9001 — ROLE / DEMO_ANALYST / TRANSLATE
-- Scope: ROLE. Applies to every holder of DEMO_ANALYST (here: DEMO_USER_ANALYST).
-- Phase: TRANSLATE — runs first, before EXPAND and REWRITE.
-- Client: NULL — applies under any client (EXAplus, JDBC, ODBC, websocket, ...).
-- Observe: an analyst submitting "--!dialect:tsql SELECT TOP 5 ..." gets the
--   T-SQL translated to Exasol SQL (TOP n -> LIMIT n) before it runs. Unmarked
--   statements pass through DEMO_TRANSLATE_V1 unchanged (fail-closed).
INSERT INTO PREPROC.CONFIG
    (rule_id, scope_type, scope_value, client_pattern, phase, script_name, function_name, enabled, version)
SELECT 9001, 'ROLE', 'DEMO_ANALYST', NULL, 'TRANSLATE', 'PREPROC_RT.DEMO_TRANSLATE_V1', 'translate', TRUE, 1
WHERE NOT EXISTS (SELECT 1 FROM PREPROC.CONFIG WHERE rule_id = 9001);

-- .............................................................................
-- Rule 9002 — ROLE / DEMO_ANALYST / EXPAND
-- Scope: ROLE (DEMO_ANALYST). Phase: EXPAND — runs after TRANSLATE.
-- Client: NULL — any client.
-- Observe: DEMO_PING_V1 is the original demo module; it leaves ordinary SQL
--   untouched and only reacts to its own ping marker, so it is a safe, visible
--   middle stage that proves the EXPAND phase is wired into the analyst pipeline.
INSERT INTO PREPROC.CONFIG
    (rule_id, scope_type, scope_value, client_pattern, phase, script_name, function_name, enabled, version)
SELECT 9002, 'ROLE', 'DEMO_ANALYST', NULL, 'EXPAND', 'PREPROC_RT.DEMO_PING_V1', 'ping', TRUE, 1
WHERE NOT EXISTS (SELECT 1 FROM PREPROC.CONFIG WHERE rule_id = 9002);

-- .............................................................................
-- Rule 9003 — ROLE / DEMO_ANALYST / REWRITE
-- Scope: ROLE (DEMO_ANALYST). Phase: REWRITE — runs last.
-- Client: NULL — any client.
-- Observe: DEMO_AUDIT_V1 appends "/* PREPROC_AUDIT user=<CURRENT_USER> */" to the
--   final statement. The result set is identical; only an audit comment is added.
--   This gives the analyst the FULL three-phase pipeline.
INSERT INTO PREPROC.CONFIG
    (rule_id, scope_type, scope_value, client_pattern, phase, script_name, function_name, enabled, version)
SELECT 9003, 'ROLE', 'DEMO_ANALYST', NULL, 'REWRITE', 'PREPROC_RT.DEMO_AUDIT_V1', 'tag', TRUE, 1
WHERE NOT EXISTS (SELECT 1 FROM PREPROC.CONFIG WHERE rule_id = 9003);

-- .............................................................................
-- Rule 9004 — ROLE / DEMO_ETL / TRANSLATE  (CLIENT-SCOPED)
-- Scope: ROLE (DEMO_ETL). Phase: TRANSLATE.
-- Client: 'EXAplus' — this rule resolves ONLY when the connecting client string
--   matches the EXAplus pattern. The same statement run from JDBC/ODBC/websocket
--   does NOT get the TRANSLATE stage. This is the client-aware dispatch demo:
--   identical user, identical SQL, different pipeline depending on the client.
-- Observe: DEMO_USER_ETL gets dialect translation in EXAplus but not elsewhere.
INSERT INTO PREPROC.CONFIG
    (rule_id, scope_type, scope_value, client_pattern, phase, script_name, function_name, enabled, version)
SELECT 9004, 'ROLE', 'DEMO_ETL', 'EXAplus', 'TRANSLATE', 'PREPROC_RT.DEMO_TRANSLATE_V1', 'translate', TRUE, 1
WHERE NOT EXISTS (SELECT 1 FROM PREPROC.CONFIG WHERE rule_id = 9004);

-- .............................................................................
-- Rule 9005 — ROLE / DEMO_ETL / REWRITE
-- Scope: ROLE (DEMO_ETL). Phase: REWRITE. Client: NULL — any client.
-- Observe: DEMO_USER_ETL always gets the audit REWRITE, but has NO EXPAND rule
--   and only a client-scoped TRANSLATE. Compared to the analyst, the ETL user's
--   resolved pipeline is visibly shorter and conditionally shaped — proving that
--   scope (role + client) drives the resolved pipeline, not the user identity
--   alone.
INSERT INTO PREPROC.CONFIG
    (rule_id, scope_type, scope_value, client_pattern, phase, script_name, function_name, enabled, version)
SELECT 9005, 'ROLE', 'DEMO_ETL', NULL, 'REWRITE', 'PREPROC_RT.DEMO_AUDIT_V1', 'tag', TRUE, 1
WHERE NOT EXISTS (SELECT 1 FROM PREPROC.CONFIG WHERE rule_id = 9005);

COMMIT;


-- -----------------------------------------------------------------------------
-- 4. RESOLVE
-- -----------------------------------------------------------------------------
-- PREPROC.REFRESH materialises the scoped CONFIG rules above into the flat,
-- per-user PREPROC.RESOLUTION table (and stamps SETTINGS). Until this runs,
-- PREPROC_RT.MY_PIPELINE shows nothing for the demo users. After it runs, each
-- demo user can read their own resolved pipeline.
EXECUTE SCRIPT PREPROC.REFRESH;


-- =============================================================================
-- 5. INSPECT THE RESULT  (commented — run these to see the demo)
-- =============================================================================
-- As each demo user, MY_PIPELINE shows only that user's resolved rules (it
-- filters on CURRENT_USER):
--
--   -- (connect as DEMO_USER_ANALYST first)
--   -- SELECT * FROM PREPROC_RT.MY_PIPELINE;
--   --   expect TRANSLATE, EXPAND, REWRITE rows
--
--   -- (connect as DEMO_USER_ETL first)
--   -- SELECT * FROM PREPROC_RT.MY_PIPELINE;
--   --   expect REWRITE always, and TRANSLATE only via the EXAplus client_pattern
--
-- As the admin, inspect the materialised resolution directly. NOTE: RESOLUTION
-- is keyed by user_name (it has no scope_value column — scope is a CONFIG-side
-- concept that REFRESH has already resolved away into per-user rows):
--
--   -- SELECT * FROM PREPROC.RESOLUTION
--   --  WHERE user_name IN ('DEMO_USER_ANALYST', 'DEMO_USER_ETL')
--   --  ORDER BY user_name, phase, ordinal;
--
-- To see the source rules (this is where scope_value lives):
--
--   -- SELECT * FROM PREPROC.CONFIG
--   --  WHERE scope_value IN ('DEMO_ANALYST', 'DEMO_ETL')
--   --  ORDER BY rule_id;
-- =============================================================================
