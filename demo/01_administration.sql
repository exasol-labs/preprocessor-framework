-- =============================================================================
-- DEMO 01 - ADMINISTERING THE FRAMEWORK FROM SQL
-- =============================================================================
-- WHAT THIS SHOWS
--   The framework has no CLI requirement, no config file, and no restart. Its
--   entire control surface is SQL: a `PREPROC ...` command language that a DBA
--   drives from any SQL client. This file walks that surface end to end.
--
-- THE PUNCHLINE
--   `PREPROC ADD RULE ...` is not built-in syntax that Exasol understands. It is
--   the framework preprocessing ITS OWN admin language - a shipped EXPAND
--   module (`PREPROC_RT.CONTROL_SUGAR_V1`) rewrites each `PREPROC ...` statement
--   into an `EXECUTE SCRIPT PREPROC.<SCRIPT>(...)` call before the engine ever
--   sees it. The framework dogfoods the exact mechanism it sells.
--
-- PREREQUISITES
--   * `release.sql` has been deployed (empty database otherwise).
--   * You are connected as `SYS`, or as any user holding `DBA` or
--     `PREPROC_ADMIN`.
--
-- RUN IT
--   Step through statement by statement. Nothing here is destructive; the last
--   section restores every setting it changes.
--
-- ONE PRESENTER GOTCHA
--   Never put a trailing `--` comment on the same LINE as a `PREPROC ...`
--   statement. Most SQL clients send that comment as part of the statement text,
--   and while the command module strips LEADING comment lines it does not strip
--   trailing ones - the command is declined and you get a confusing syntax error.
--   Put explanatory comments on the line ABOVE. Every file in this demo does.
-- =============================================================================


-- -----------------------------------------------------------------------------
-- 1. THE DISPATCHER IS DEPLOYED BUT NOT YET ACTIVE
-- -----------------------------------------------------------------------------
-- Deploying the framework never touches Exasol's single global preprocessor
-- slot. Activation is a separate, deliberate DBA action - so a deploy can never
-- change how a single statement on the system behaves.

-- Proof the control plane is there: two schemas, one dispatcher, an admin surface.
SELECT SCHEMA_NAME, SCHEMA_OWNER
FROM   EXA_ALL_SCHEMAS
WHERE  SCHEMA_NAME IN ('PREPROC', 'PREPROC_RT')
ORDER BY 1;

SELECT SCRIPT_SCHEMA, SCRIPT_NAME
FROM   EXA_ALL_SCRIPTS
WHERE  SCRIPT_SCHEMA IN ('PREPROC', 'PREPROC_RT')
  AND  SCRIPT_NAME NOT LIKE 'P\_%' ESCAPE '\'
ORDER BY 1, 2;

-- The `PREPROC ...` command language is NOT active yet. Run this now and it is
-- just an unknown token - the engine raises a syntax error, because no
-- preprocessor is intercepting the statement:
--
--   PREPROC STATUS;
--   -- syntax error, unexpected IDENTIFIER_PART_


-- -----------------------------------------------------------------------------
-- 2. ACTIVATE THE DISPATCHER (SESSION-SCOPED FIRST)
-- -----------------------------------------------------------------------------
-- Prove it on your own session before changing the system. This is the whole
-- activation story - one statement, reversible, no restart.

ALTER SESSION SET sql_preprocessor_script = PREPROC_RT.MASTER;

-- The same statement that failed a moment ago now works. Nothing about the
-- database changed - the framework is simply intercepting your SQL now.
PREPROC STATUS;

-- System-wide activation, when you are ready, is the same statement against the
-- system. Left commented so this demo stays session-local:
--
--   ALTER SYSTEM SET sql_preprocessor_script = PREPROC_RT.MASTER;


-- -----------------------------------------------------------------------------
-- 3. THE ADMIN SURFACE
-- -----------------------------------------------------------------------------
-- Every rule the framework will dispatch lives in one table, and this is how you
-- read it. Two rules are seeded at install: the admin command language itself,
-- scoped to `PREPROC_ADMIN` and to `DBA`.

PREPROC LIST RULES;

-- What does the CURRENT user actually resolve to? `MY_PIPELINE` is a
-- definer-rights view scoped to `CURRENT_USER` - every user can read their OWN
-- pipeline, and no user can read anyone else's. Ordinary users get an honest
-- answer to "what is being done to my SQL?" without any grant on `PREPROC.*`.
SELECT * FROM PREPROC_RT.MY_PIPELINE;

-- Provenance of every module ever installed (empty for now - files 02/03 fill it).
SELECT NAME, VERSION, SOURCE_URL, LIBRARY_VERSION, STATUS
FROM   PREPROC_RT.INSTALLED_MODULES
ORDER BY NAME, VERSION;


-- -----------------------------------------------------------------------------
-- 4. SUGAR AND SCRIPT ARE THE SAME THING
-- -----------------------------------------------------------------------------
-- Each `PREPROC ...` command maps to exactly one admin script call. The sugar is
-- ergonomics, never a second code path - which matters when you script the
-- framework from a tool that cannot rely on the preprocessor being active.
-- These two statements are identical:

PREPROC STATUS;
EXECUTE SCRIPT PREPROC.STATUS();

PREPROC LIST RULES;
EXECUTE SCRIPT PREPROC.RULE_LIST();

-- The scripted form is the one to use in cron, CI, and bootstrap scripts: it
-- works whether or not the preprocessor is currently active, and it does not
-- depend on the caller resolving the sugar rule.


-- -----------------------------------------------------------------------------
-- 5. STATUS AND REFRESH - THE TWO WORDS THAT MATTER OPERATIONALLY
-- -----------------------------------------------------------------------------
-- The framework does NOT read its rule table per statement. `PREPROC.REFRESH`
-- flattens rules x the live role-grant graph into a per-user resolution table,
-- and dispatch reads only that. So the cost of a rule change is paid once, at
-- refresh, not on every query.
--
-- `PREPROC STATUS` tells you whether that materialised resolution still matches
-- the live database. File 04 covers the behaviour in depth; here is the surface:

-- CURRENT / STALE / NEVER_REFRESHED
PREPROC STATUS;

-- Rebuild resolution from rules + the live role-grant graph
PREPROC REFRESH;


-- -----------------------------------------------------------------------------
-- 6. MODE - THE PERFORMANCE POSTURE
-- -----------------------------------------------------------------------------
-- `convenience` (the install default): a session pays the full lookup on its
--   FIRST statement, then repoints itself to a generated profile script with its
--   own rule chain baked in - ~7.3 ms/stmt becomes ~0.5 ms/stmt.
-- `strict`: no session ever repoints itself; every statement pays the full
--   lookup. Choose it when "a session must never rewrite its own preprocessor
--   setting" is a policy you have to be able to state.

SELECT MODE, SELF_DISABLE_ALLOWED, GROUP_SYS_WITH_DBA FROM PREPROC.SETTINGS;

PREPROC SET MODE strict;

-- ...and back to the install default
PREPROC SET MODE convenience;

-- Those profile scripts are generated, retired, and reclaimed automatically by
-- the refresh path. Operators never create or drop them - but you can watch the
-- lifecycle. A non-NULL `orphaned_at` is a retired profile awaiting reclaim; it
-- is deliberately never dropped on the refresh that orphaned it, so a
-- long-running session pinned to it can never be stranded.
SELECT PROFILE_HASH, CREATED_AT, ORPHANED_AT FROM PREPROC.PROFILES ORDER BY CREATED_AT;


-- -----------------------------------------------------------------------------
-- 7. FAIL-OPEN - THE PROMISE THAT MAKES THIS DEPLOYABLE
-- -----------------------------------------------------------------------------
-- This is the beat to spend time on with an ops audience. The framework occupies
-- the one global preprocessor slot, so the obvious objection is: "if it breaks,
-- does my database stop accepting SQL?" No. Every module call is wrapped in a
-- dispatch-level pcall. A broken module is SKIPPED, never fatal.
--
-- First, note what the framework will NOT let you do. Registering a rule against
-- a script that does not exist is rejected up front - you cannot typo your way
-- into a dead rule:
--
--   PREPROC ADD RULE FOR ROLE DBA PHASE EXPAND SCRIPT PREPROC_RT.NOPE_V1 FUNCTION expand;
--   -- PREPROC.RULE_ADD: script not found: PREPROC_RT.NOPE_V1
--
-- So let us break it the way it actually breaks in production: with a module that
-- passes registration and then throws at dispatch time.

--/
CREATE OR REPLACE LUA SCRIPT PREPROC_RT.DEMO_BROKEN_V1 AS
function expand(sqltext)
    error("this module is deliberately broken")
end
/

PREPROC ADD RULE FOR ROLE DBA PHASE EXPAND SCRIPT PREPROC_RT.DEMO_BROKEN_V1 FUNCTION expand;

-- The rule is live and in your pipeline:
SELECT * FROM PREPROC_RT.MY_PIPELINE;

-- Now every statement you submit invokes a module that raises an error. Watch
-- what happens to your SQL:
SELECT 'the database still answers' AS PROOF;
SELECT COUNT(*) AS TABLES_VISIBLE FROM EXA_ALL_TABLES;
PREPROC LIST RULES;

-- Nothing. The dispatcher caught it, skipped that one rule, and carried on -
-- and the framework's own command language kept working alongside the broken
-- module, because the skip is per rule, not per session.
--
-- Clean up, IN THIS ORDER - rule first, then the script (see the warning below).
-- Use the rule_id the ADD reported (3 on a fresh install; PREPROC LIST RULES
-- above shows it if you are not sure).
PREPROC DROP RULE 3;

DROP SCRIPT PREPROC_RT.DEMO_BROKEN_V1;

PREPROC LIST RULES;

-- !! THE ONE ORDERING RULE THAT MATTERS: never drop a script while an enabled rule
-- still points at it.
--
-- A module that THROWS is handled cleanly, as you just saw. A module that has
-- VANISHED is not equivalent: the failed `import` inside dispatch also breaks the
-- `import` the framework's own admin scripts use, so while the dangling rule
-- exists, `PREPROC REFRESH` / `ADD RULE` / `DROP RULE` / `INSTALL MODULE` all fail
-- with `attempt to call a nil value (field 'rebuild')` for anyone whose pipeline
-- includes that rule. Ordinary user SQL keeps working - the fail-open contract
-- holds for statements - but the control surface locks itself out.
--
-- RECOVERY, if you ever land in it: turn the preprocessor off for your session,
-- which takes the broken import out of the path, then drop the dangling rule.
--
--   ALTER SESSION SET sql_preprocessor_script = '';
--   SELECT c.RULE_ID, c.SCRIPT_NAME FROM PREPROC.CONFIG c
--   WHERE NOT EXISTS (SELECT 1 FROM EXA_ALL_SCRIPTS s
--                     WHERE c.SCRIPT_NAME = s.SCRIPT_SCHEMA || '.' || s.SCRIPT_NAME);
--   EXECUTE SCRIPT PREPROC.RULE_DROP(<id>, NULL);
--   ALTER SESSION SET sql_preprocessor_script = PREPROC_RT.MASTER;
--
-- This is the second reason the scripted forms exist alongside the sugar, and it
-- is why the framework's own module lifecycle is retire-then-RECLAIM:
-- `PREPROC.MODULE_RECLAIM` refuses to drop a version while any enabled rule or
-- live profile still references it. Follow the same discipline by hand.
--
-- One more thing worth stating, because it cuts both ways: fail-open is right for
-- TRANSLATE and EXPAND, where a skipped module means "no new syntax" and the
-- engine raises a loud error. It deserves thought for REWRITE, where a skipped
-- module means the ORIGINAL statement runs - if you use REWRITE to inject a
-- policy, a skip is a policy bypass. The framework is explicit about this rather
-- than pretending the trade-off does not exist.


-- -----------------------------------------------------------------------------
-- 8. BATCHING CHANGES - NOREFRESH
-- -----------------------------------------------------------------------------
-- Every mutating command auto-refreshes, which is what you want interactively
-- and not what you want when applying twenty rules from a deployment script.
-- Append NOREFRESH to defer, then refresh once.
--
-- (Shown here as a pattern; file 04 runs it for real.)
--
--   PREPROC ADD RULE FOR ROLE A PHASE EXPAND SCRIPT PREPROC_RT.M_V1 FUNCTION expand NOREFRESH
--   PREPROC ADD RULE FOR ROLE B PHASE EXPAND SCRIPT PREPROC_RT.M_V1 FUNCTION expand NOREFRESH
--   PREPROC REFRESH


-- =============================================================================
-- WHERE WE ARE
--   A live, empty pipeline, administered entirely from SQL, with a fail-open
--   guarantee you can demonstrate rather than promise.
--
-- NEXT: 02_module_from_github.sql - get a transformation without writing one.
-- =============================================================================
