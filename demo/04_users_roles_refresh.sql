-- =============================================================================
-- DEMO 04 - USERS, ROLES, AND THE REFRESH CONTRACT
-- =============================================================================
-- WHAT THIS SHOWS
--   How a transformation is targeted at an audience - by role, by user, through
--   nested roles - and the one operational rule an administrator has to
--   internalise: WHICH changes refresh themselves and which do not.
--
-- THE PUNCHLINE
--   The framework decouples "what a transformation is" from "who gets it".
--   Installing code and granting an audience are separate acts, and the audience
--   is expressed in the vocabulary the DBA already uses - roles and users.
--
-- THE RULE TO REMEMBER
--   * Changing a RULE auto-refreshes.        (the framework saw it happen)
--   * Changing a GRANT does not.             (the framework was not involved)
--   Resolution is built from rules x the live role-grant graph. The framework
--   cannot hook `GRANT`, so after any role or user change you refresh - manually
--   or on a schedule. `PREPROC STATUS` is the signal, and it is honest.
--
-- PREREQUISITES
--   * Files 01-03 have been run (cast_shorthand and trailing_comma installed).
--   * TWO SQL client sessions. Session A is the admin (you). Session B is opened
--     as a demo user where marked.
--
-- NOTE ON RULE IDS
--   The `rule_id` values below depend on how many rules you have added. Run
--   `PREPROC LIST RULES` and substitute the ids you actually see.
-- =============================================================================

ALTER SESSION SET sql_preprocessor_script = PREPROC_RT.MASTER;


-- -----------------------------------------------------------------------------
-- 1. AN AUDIENCE: THREE ROLES, THREE USERS
-- -----------------------------------------------------------------------------
-- Ordinary Exasol roles and users. The framework introduces no parallel
-- principal model - this is the point. Nothing here is framework-specific.

CREATE ROLE DEMO_ANALYST;
CREATE ROLE DEMO_ETL;
CREATE ROLE DEMO_FIN;        -- used in file 06

CREATE USER DEMO_ANA     IDENTIFIED BY "Demo1234";   -- an analyst
CREATE USER DEMO_BOB     IDENTIFIED BY "Demo1234";   -- starts with no roles at all
CREATE USER DEMO_FIN_ANA IDENTIFIED BY "Demo1234";   -- an analyst who also sees finance

-- CREATE SESSION is all they need: the demo statements carry their own data
-- inline, so there is no table to grant.
GRANT CREATE SESSION TO DEMO_ANA, DEMO_BOB, DEMO_FIN_ANA;

GRANT DEMO_ANALYST TO DEMO_ANA;
GRANT DEMO_ANALYST TO DEMO_FIN_ANA;
GRANT DEMO_FIN     TO DEMO_FIN_ANA;

-- Creating users changed the inputs to resolution. The framework says so:
-- STALE
PREPROC STATUS;
PREPROC REFRESH;
-- CURRENT
PREPROC STATUS;


-- -----------------------------------------------------------------------------
-- 2. GIVE A ROLE ACCESS TO AN INSTALLED MODULE
-- -----------------------------------------------------------------------------
-- `cast_shorthand` is already deployed (file 02) and currently bound to DBA.
-- Handing it to a second audience is one rule - no redeploy, no second copy of
-- the code, no version bump.

PREPROC ADD RULE FOR ROLE DEMO_ANALYST PHASE TRANSLATE
    SCRIPT PREPROC_RT.CAST_SHORTHAND_V1 FUNCTION cast_shorthand;

-- A RULE change, so this auto-refreshed. Both analysts resolved it immediately.
-- As admin you can inspect any user's resolved pipeline:
SELECT USER_NAME, PHASE, SCRIPT_NAME, FUNCTION_NAME, ORDINAL, PROFILE_HASH
FROM   PREPROC.RESOLUTION
ORDER BY USER_NAME, PHASE, ORDINAL;

-- Note `PROFILE_HASH`: users with the same chain share one generated profile
-- script; users with different chains get different ones. It is a content hash
-- of the ordered chain, which is why it is also a clean answer to "do these two
-- users see the same SQL rewriting?"


-- =============================================================================
-- >> SESSION B - connect as DEMO_ANA / Demo1234, then run this block
-- =============================================================================
--
--   ALTER SESSION SET sql_preprocessor_script = PREPROC_RT.MASTER;
--
--   -- Every user can read their OWN pipeline. No grant on PREPROC.* required,
--   -- and no way to see anyone else's.
--   SELECT * FROM PREPROC_RT.MY_PIPELINE;
--
--   -- The transformation the analyst was granted:
--   SELECT ORDER_ID, AMOUNT_TXT::DECIMAL(10,2) AS AMOUNT
--   FROM   VALUES (1,'1299.50'), (2,'87.25') AS T(ORDER_ID, AMOUNT_TXT);
--
--   -- And one they were NOT granted - trailing_comma is bound to DBA only, so
--   -- this is still a plain syntax error for this user:
--   SELECT ORDER_ID, REGION,
--   FROM   VALUES (1,'EMEA'), (2,'APAC') AS T(ORDER_ID, REGION);
--
--   -- The admin surface is not reachable from here either - and note HOW it is
--   -- unreachable. The command language is itself a rule scoped to PREPROC_ADMIN
--   -- and DBA, neither of which this user holds, so it never expands: they get a
--   -- syntax error, not a privileges error. The admin language is invisible, not
--   -- merely forbidden. (And the script behind it enforces PREPROC_ADMIN anyway.)
--   PREPROC LIST RULES;
--
-- =============================================================================


-- -----------------------------------------------------------------------------
-- 3. GIVE A SINGLE USER ACCESS
-- -----------------------------------------------------------------------------
-- Roles are the normal unit, but a USER-scoped rule is there for the one-off:
-- a migration engineer who needs a dialect shim nobody else should get, a service
-- account with a specific quirk.

PREPROC ADD RULE FOR USER DEMO_BOB PHASE TRANSLATE
    SCRIPT PREPROC_RT.ERGONOMICS_TRAILING_COMMA_V2 FUNCTION trailing_comma;

SELECT USER_NAME, PHASE, SCRIPT_NAME FROM PREPROC.RESOLUTION
WHERE  USER_NAME LIKE 'DEMO_%' ORDER BY 1, 2;

-- DEMO_BOB holds no roles at all and still resolves exactly one rule. Scoping is
-- a union of the role closure and the user-scoped rules.


-- -----------------------------------------------------------------------------
-- 4. THE REFRESH CONTRACT - THE BEAT NOT TO SKIP
-- -----------------------------------------------------------------------------
-- Now grant DEMO_BOB the analyst role. This is a pure GRANT: no rule changed, so
-- the framework had no way to observe it.

GRANT DEMO_ANALYST TO DEMO_BOB;

-- STALE. The materialised resolution no longer matches the live grant graph.
PREPROC STATUS;

-- And DEMO_BOB does NOT yet have the analyst's transformation - still just the
-- one user-scoped rule:
SELECT USER_NAME, PHASE, SCRIPT_NAME FROM PREPROC.RESOLUTION
WHERE  USER_NAME = 'DEMO_BOB' ORDER BY 2;

-- This is deliberate, not a gap. Resolution is materialised precisely so that
-- dispatch never walks the role-grant graph per statement - that is where the
-- per-statement cost would go. The price is an explicit refresh.

PREPROC REFRESH;
-- CURRENT
PREPROC STATUS;

-- Now the role-derived rule is there too:
SELECT USER_NAME, PHASE, SCRIPT_NAME FROM PREPROC.RESOLUTION
WHERE  USER_NAME = 'DEMO_BOB' ORDER BY 2, 3;

-- IN PRODUCTION: schedule it. Any SQL-capable scheduler will do - the framework
-- deliberately ships the entry point, not a scheduler.
--
--   EXECUTE SCRIPT PREPROC.REFRESH;
--
-- Use `PREPROC.STATUS` as the "do I need to?" probe so a five-minute cron is
-- nearly free. Both are the scripted forms on purpose: they work whether or not
-- the preprocessor is active.


-- -----------------------------------------------------------------------------
-- 5. ROLE NESTING RESOLVES TRANSITIVELY
-- -----------------------------------------------------------------------------
-- Rules follow the full role-grant closure, at arbitrary depth. Model your
-- audiences the way you already model access.

GRANT DEMO_ANALYST TO DEMO_ETL;   -- ETL now inherits everything the analyst gets
GRANT DEMO_ETL TO DEMO_BOB;
-- Grants again, so: explicit refresh
PREPROC REFRESH;

SELECT USER_NAME, PHASE, SCRIPT_NAME FROM PREPROC.RESOLUTION
WHERE  USER_NAME LIKE 'DEMO_%' ORDER BY 1, 2, 3;

-- Duplicates collapse. DEMO_BOB now reaches cast_shorthand via DEMO_ANALYST AND
-- via DEMO_ETL -> DEMO_ANALYST, and it appears exactly once. The lowest rule_id
-- wins the ordinal, so dispatch order stays deterministic.
--
-- A rule scoped to ROLE PUBLIC applies to every user, unconditionally - the
-- lever for a cluster-wide transformation.


-- -----------------------------------------------------------------------------
-- 6. NARROWING BY CLIENT APPLICATION
-- -----------------------------------------------------------------------------
-- Role and user answer "who". A rule can also narrow on "from where" - the client
-- application the session connected with. Some transformations only make sense
-- when a human is typing and should never fire inside a scheduled job.
--
-- First, find out what your tools actually call themselves. Run this in each one:
SELECT CLIENT, DRIVER FROM EXA_ALL_SESSIONS WHERE SESSION_ID = CURRENT_SESSION;

-- Measured values: 'DbVisualizer 24.1.5', 'DBeaver 23.3.0', 'EXAplus',
-- 'PyExasol 2.2.1'. The pattern is a literal SUBSTRING of that string - not a
-- wildcard, not a regular expression - so 'DbVisualizer' matches the first only.
--
-- The clause is optional and goes after FUNCTION, before any NOREFRESH:
PREPROC ADD RULE FOR ROLE DEMO_ANALYST PHASE TRANSLATE
    SCRIPT PREPROC_RT.CAST_SHORTHAND_V1 FUNCTION cast_shorthand
    CLIENT 'DbVisualizer';

-- LIST RULES reports it, so a client-scoped rule is never invisible:
PREPROC LIST RULES;

-- The operand is NOT identifier-folded. It keeps its casing and any embedded
-- spaces, because it is matched against a self-reported application name rather
-- than an Exasol identifier - 'DBVISUALIZER' would match nothing at all.
--
-- Two things worth internalising:
--
--   * RESOLUTION is client-AGNOSTIC. The rule resolves for every user in scope
--     regardless of their client; the match happens at DISPATCH time, per
--     statement, inside MASTER. Check PREPROC.RESOLUTION and you will see the
--     rule present with its pattern, in sessions where it will never fire.
--
--   * It is SCOPING, not security. The client name is whatever the connecting
--     tool chooses to report, so a pattern keeps conveniences out of batch jobs.
--     It must never be what withholds a transformation from someone who should
--     not have it - that is ROLE/USER scoping and the schema grants in file 06.
--
-- Cost: MASTER only looks the client up when at least one resolved rule carries a
-- pattern. Sessions whose rules are all NULL-pattern never pay for it.
--
-- NULL means "all clients" - which is what every rule above this section has.
-- An empty pattern is refused rather than silently meaning "all": the sugar
-- declines CLIENT '' outright, and a whitespace-only pattern is rejected by the
-- admin script with an explanatory error. Drop the rule again with the id LIST
-- RULES reported:
--   PREPROC DROP RULE <id>;


-- -----------------------------------------------------------------------------
-- 7. BATCHING: NOREFRESH
-- -----------------------------------------------------------------------------
-- Each mutating command auto-refreshes, which is wrong for a deployment script
-- applying twenty rules. Defer and refresh once.

PREPROC ADD RULE FOR ROLE DEMO_ETL PHASE TRANSLATE
    SCRIPT PREPROC_RT.ERGONOMICS_TRAILING_COMMA_V2 FUNCTION trailing_comma NOREFRESH;

-- Still CURRENT: the staleness fingerprint tracks users and grants, not rules.
-- Do NOT use STATUS to detect un-refreshed RULE edits - that is exactly why the
-- sugar refreshes for you by default and NOREFRESH is opt-in.
PREPROC STATUS;

PREPROC REFRESH;


-- -----------------------------------------------------------------------------
-- 8. DISABLE WITHOUT DELETING
-- -----------------------------------------------------------------------------
-- The revocation path that keeps the audit trail. The row stays, the code stays,
-- the transformation stops. Reversible in one statement - which is what you want
-- at 03:00 when a rewrite is suspected in an incident.

-- Pick the rule_id of the DEMO_ETL trailing_comma rule from this output
PREPROC LIST RULES;
-- PREPROC DISABLE RULE <id>;
-- PREPROC ENABLE  RULE <id>;


-- -----------------------------------------------------------------------------
-- 9. WHAT HAPPENS TO SESSIONS THAT ARE ALREADY OPEN
-- -----------------------------------------------------------------------------
-- Ask this before a customer does.
--
--   * A session whose user has NO rules stays on the dispatcher (the install
--     default `self_disable_allowed = FALSE`) and therefore picks up new rules on
--     its next statement after a refresh. Session B above needed no reconnect.
--
--   * A session whose user HAS rules is pinned to its baked profile script. When
--     a refresh changes that user's chain, the old profile is retired - its body
--     is replaced with the dispatcher's own body - so the pinned session
--     re-resolves on its next statement and repoints itself. Self-healing, no
--     reconnect, and no untransformed gap.
--
--   * The one exception: with `PREPROC SET SELF_DISABLE TRUE`, a no-rule session
--     clears its preprocessor entirely after its first statement and will not see
--     newly granted rules until it reconnects. That setting buys ~0 ms/stmt for
--     users who have no rules; the trade-off is exactly this. It ships OFF.
--
-- Verify the first two live: leave Session B connected, add a rule for
-- DEMO_ANALYST here, and re-run the query in Session B without reconnecting.

SELECT PROFILE_HASH, CREATED_AT, ORPHANED_AT FROM PREPROC.PROFILES
ORDER BY CREATED_AT;


-- =============================================================================
-- WHERE WE ARE
--   Different users provably running different pipelines, targeted with roles you
--   already manage, with one honest operational obligation: refresh after grants.
--
-- NEXT: 05_own_preprocessor.sql - stop installing other people's modules and
--       write your own in fifteen lines.
-- =============================================================================
