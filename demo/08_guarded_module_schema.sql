-- =============================================================================
-- DEMO 08 - PUTTING A WHOLE MODULE IN A SCHEMA NOT EVERYONE CAN SEE
-- =============================================================================
-- WHAT THIS SHOWS
--   File 06 made the point with a toy module. This does it with the real thing:
--   all fifteen objects of `confd_control` deployed into one guarded schema,
--   gated by a single `GRANT EXECUTE`, and the whole cluster-administration
--   surface delegated to a NON-DBA role.
--
-- THE RULE OF THUMB
--   `PREPROC_RT` is the right home for a preprocessor EVERYONE should be able to
--   run - `expr::type`, a trailing-comma fixer, the admin command language. It
--   has EXECUTE and SELECT to PUBLIC by framework invariant, so anything in it is
--   callable by every user, whether or not a rule offers it to them.
--   Anything else belongs in a schema you control.
--
-- WHAT THE GUARDED SCHEMA BUYS YOU, MEASURED
--   With confd_control in `PREPROC_RT`, an ordinary user CAN invoke the gateway
--   UDF directly, bypassing the preprocessor; they are stopped one layer later by
--   the CONNECTION grant. In a guarded schema they cannot even call it -
--   "insufficient privileges for calling script". One less layer to reason about,
--   and one less thing to get wrong.
--
-- PREREQUISITES
--   * Files 01-03 have been run, and `CONFD_CONNECTION` exists (see file 07).
--   * You have the module artifact `confd_control_v1.sql` - from a library
--     checkout, or from `uv run preproc module bundle confd_control`.
--   * ONE shell step first (see section 0 below).
-- =============================================================================


-- -----------------------------------------------------------------------------
-- 0. THE ONE STEP THAT IS NOT SQL
-- -----------------------------------------------------------------------------
-- The artifact hardcodes its own object names, so re-homing it is a text edit on
-- the artifact - every object it creates AND every internal cross-reference,
-- because the entry script calls its companions by fully-qualified versioned name
-- and the SQL it emits names the admin scripts literally:
--
--   sed -e 's/PREPROC_RT\./CONFD_GUARD./g' \
--       -e 's/PREPROC\.ADMIN_/CONFD_GUARD.ADMIN_/g' \
--       confd_control_v1.sql > confd_guarded.sql
--
-- 50 references, one pass. Then run `confd_guarded.sql` in EXAplus (or a GUI
-- client with parameterized SQL disabled) after creating the schema below.
--
-- THE TRADE-OFF, STATED PRECISELY: editing the artifact changes its bytes, so its
-- `sha256` no longer matches the registry and `PREPROC INSTALL MODULE` will not
-- install it. What you give up is exactly two things - the automatic artifact
-- fetch, and the verified hash. You do NOT give up provenance or the managed
-- lifecycle: section 1a registers the module so it is tracked in `INSTALLED_MODULES` and
-- participates in retire/reclaim like any registry install. This is the framework's
-- `self-deployed` deploy mode, not an unsupported hack.
--
-- The installer is deliberately schema-agnostic - `module_install.lua` opens
-- `PREPROC_RT` as compilation context only and a statement "may create its object
-- in any schema the caller can write" (ADR-057) - but the schema comes from the
-- ARTIFACT, and `MODULE_INSTALL(source, module_name, scope)` has no target-schema
-- argument. That is a deliberate contract choice, not an oversight; see section 5.

CREATE SCHEMA IF NOT EXISTS CONFD_GUARD;

-- Deliberately no PUBLIC grant. Contrast with PREPROC_RT:
SELECT OBJECT_NAME, OBJECT_TYPE, PRIVILEGE, GRANTEE
FROM   EXA_DBA_OBJ_PRIVS
WHERE  OBJECT_NAME IN ('PREPROC', 'PREPROC_RT', 'CONFD_GUARD')
ORDER BY 1, 3;

-- >> Now run confd_guarded.sql. Fifteen objects, all in CONFD_GUARD.

SELECT SCRIPT_NAME, SCRIPT_TYPE FROM EXA_ALL_SCRIPTS
WHERE  SCRIPT_SCHEMA = 'CONFD_GUARD' ORDER BY 2, 1;


-- -----------------------------------------------------------------------------
-- 1. DELEGATE IT TO A NON-DBA ROLE
-- -----------------------------------------------------------------------------
-- This is the payoff. Cluster administration normally means handing someone DBA.
-- Here it is a role with exactly two grants and nothing else.

CREATE ROLE CLUSTER_ADMIN;

CREATE USER DANA IDENTIFIED BY "Demo1234";
GRANT CREATE SESSION TO DANA;
GRANT CLUSTER_ADMIN TO DANA;

-- Grant 1: run the module's objects.
GRANT EXECUTE ON SCHEMA CONFD_GUARD TO CLUSTER_ADMIN;

-- Grant 2: reach ConfD. The scripts run with the INVOKER's rights, so DANA needs
-- the connection in her own name - she is not borrowing anyone's.
GRANT ACCESS ON CONNECTION CONFD_CONNECTION TO CLUSTER_ADMIN;

-- And the rule, pointing at the guarded schema. Note this is the ordinary ADD
-- RULE from file 02 - a rule takes any schema-qualified name.
PREPROC ADD RULE FOR ROLE CLUSTER_ADMIN PHASE EXPAND
    SCRIPT CONFD_GUARD.CONFD_CONTROL_V1 FUNCTION confd_control;

-- Also create a user with nothing, for the contrast.
CREATE USER JOE IDENTIFIED BY "Demo1234";
GRANT CREATE SESSION TO JOE;

PREPROC REFRESH;


-- -----------------------------------------------------------------------------
-- 1a. KEEP IT GOVERNED - REGISTER THE HAND-DEPLOYED MODULE
-- -----------------------------------------------------------------------------
-- Do not skip this. A hand-deployed module does not have to be an untracked one.
-- `MODULE_REGISTER` is the same call `MODULE_INSTALL` makes internally, and it
-- accepts a fully-qualified `script_name` in any schema plus `deploy_mode =
-- 'self-deployed'` - which is precisely "the code was deployed by something other
-- than the framework; record it and manage its lifecycle".
--
-- `sha256` is NULL, correctly: nothing verified these bytes, and the provenance row
-- should say so rather than carry a hash that does not match the registry.
--
-- The `objects` argument is the inventory, `TYPE|FULLY.QUALIFIED_NAME` per line.
-- Derive it from the catalog so it cannot drift from what actually landed:
--
--   SELECT LISTAGG(CASE WHEN SCRIPT_TYPE = 'UDF'
--                       THEN 'PYTHON3 SCALAR SCRIPT' ELSE 'LUA SCRIPT' END
--                  || '|CONFD_GUARD.' || SCRIPT_NAME, CHR(10))
--   FROM EXA_ALL_SCRIPTS WHERE SCRIPT_SCHEMA = 'CONFD_GUARD';

-- EXECUTE SCRIPT PREPROC.MODULE_REGISTER(
--     'confd_control', 1,
--     'exasol-labs/preprocessor-library', 'v0.3.0',
--     NULL,                                    -- sha256: not verified, say so
--     'CONFD_GUARD.CONFD_CONTROL_V1', 'confd_control', 'EXPAND',
--     'self-deployed',
--     'hand-deployed into CONFD_GUARD from preproc-lib-0.3.0.tar.gz',
--     '0.3.0',
--     '<the inventory from the query above>');

-- Now it is tracked exactly like a registry install - note DEPLOY_MODE and that
-- SCRIPT_NAME records where it actually lives:
SELECT NAME, VERSION, SCRIPT_NAME, DEPLOY_MODE, STATUS, LIBRARY_VERSION
FROM   PREPROC_RT.INSTALLED_MODULES ORDER BY NAME;

-- And the lifecycle guard works on it. Verified: with the rule still live,
-- MODULE_RECLAIM refuses with `blocked_by_rule`; drop the rule first and it drops
-- all fifteen guarded objects and clears the provenance row.
--
--   EXECUTE SCRIPT PREPROC.MODULE_RETIRE('confd_control', 1);
--   EXECUTE SCRIPT PREPROC.MODULE_RECLAIM('confd_control', 1);   -- blocked_by_rule
--   PREPROC DROP RULE <id>;
--   EXECUTE SCRIPT PREPROC.MODULE_RECLAIM('confd_control', 1);   -- reclaimed


-- =============================================================================
-- >> SESSION B - connect as DANA / Demo1234.  NOT a DBA. Holds two grants.
-- =============================================================================
--
--   ALTER SESSION SET sql_preprocessor_script = PREPROC_RT.MASTER;
--
--   SELECT PHASE, SCRIPT_NAME FROM PREPROC_RT.MY_PIPELINE;
--   -- EXPAND | CONFD_GUARD.CONFD_CONTROL_V1
--
--   -- the full surface, for a user who holds no DBA:
--   CONTROL DATABASE SHOW INFO;
--   CONTROL BUCKETFS SHOW SERVICES;
--   CONTROL STORAGE SHOW VOLUMES;
--   CONTROL DATABASE SHOW LICENSE;
--   SELECT FIELD, VALUE_TEXT FROM (CONTROL DATABASE SHOW INFO) WHERE FIELD = 'state';
--
-- =============================================================================
-- >> SESSION C - connect as JOE / Demo1234.  No roles at all.
-- =============================================================================
--
--   ALTER SESSION SET sql_preprocessor_script = PREPROC_RT.MASTER;
--
--   -- 1. The grammar does not exist for him: no rule resolves, so this is a
--   --    SYNTAX error. He cannot discover that a CONTROL surface exists.
--   CONTROL DATABASE SHOW INFO;
--
--   -- 2. Try to bypass the preprocessor and call the gateway UDF by name.
--   --    THIS is the line that justifies the guarded schema:
--   SELECT CONFD_GUARD.CONFD_QUERY_V1('CONFD_CONNECTION','db_info','{"db_name":""}');
--   -- insufficient privileges for calling script
--   --
--   -- In PREPROC_RT the same call SUCCEEDS as far as invoking the UDF, and only
--   -- fails one layer deeper on the CONNECTION grant. Here it never runs at all.
--
--   -- 3. And the admin scripts are equally out of reach:
--   EXECUTE SCRIPT CONFD_GUARD.ADMIN_CONFD_JOB_V1('db_info','{"db_name":""}');
--   -- insufficient privileges for executing a script
--
-- =============================================================================


-- -----------------------------------------------------------------------------
-- 2. REVOCATION IS IMMEDIATE
-- -----------------------------------------------------------------------------
-- No refresh, no reconnect. The dispatcher imports the module at statement time
-- under the caller's rights, so a grant change lands on the next statement - even
-- in a session already pinned to a profile.

REVOKE EXECUTE ON SCHEMA CONFD_GUARD FROM CLUSTER_ADMIN;

-- >> In SESSION B (DANA), re-run - straight back to a syntax error:
--   CONTROL DATABASE SHOW INFO;

GRANT EXECUTE ON SCHEMA CONFD_GUARD TO CLUSTER_ADMIN;

-- >> In SESSION B again: working, same session.


-- -----------------------------------------------------------------------------
-- 3. WHERE TO PUT WHAT
-- -----------------------------------------------------------------------------
-- The whole lesson in one table.
--
--   PREPROC_RT      EXECUTE + SELECT to PUBLIC, by framework invariant.
--   (public)        Put here: preprocessors every user should be able to run.
--                   `expr::type`, trailing commas, dialect shims, the PREPROC
--                   command language itself. Assume anything here is callable by
--                   everyone regardless of what rules say.
--
--   PREPROC         No PUBLIC grant. The framework's own admin surface and the
--   (framework)     BucketFS/network resolver UDF. Not yours to extend.
--
--   <your schema>   You choose the grants. Put here: anything that touches
--   (guarded)       sensitive data, performs a privileged action, or should be
--                   available to some roles and not others. Costs one CREATE
--                   SCHEMA and one GRANT.
--
-- A rule takes any schema-qualified name, so the framework does not care which
-- of these you pick - which is exactly why the choice is yours to get right.


-- -----------------------------------------------------------------------------
-- 4. THE ONE NUANCE WORTH KNOWING
-- -----------------------------------------------------------------------------
-- If you re-home only the `PREPROC_RT` half and leave the five `ADMIN_*_V1`
-- scripts in `PREPROC`, you get a PARTIAL surface for a non-DBA:
--
--   * inline read commands work - they rewrite to a UDF SELECT in your schema
--     (`SELECT ... FROM (CONTROL DATABASE SHOW INFO)`);
--   * commands that route through `PREPROC.ADMIN_*_V1` fail with "insufficient
--     privileges for executing a script", because `PREPROC` is locked.
--
-- Verified: with only the UDF half moved, DANA could run
-- `SELECT ... FROM (CONTROL DATABASE SHOW INFO)` but not `CONTROL BUCKETFS SHOW
-- SERVICES`. That may be exactly the split you want - reads delegated, mutations
-- DBA-only - but decide it deliberately rather than discovering it. Moving all
-- fifteen objects (the second `sed` expression in section 0) gives the full surface.


-- -----------------------------------------------------------------------------
-- 5. WHY THE LIBRARY CONTRACT KEEPS THE PREPROC SCHEMAS
-- -----------------------------------------------------------------------------
-- The obvious wish is an installer parameter -
--
--   PREPROC INSTALL MODULE confd_control ... IN SCHEMA CONFD_GUARD;
--
-- with artifacts no longer schema-qualifying what they create, so an `OPEN SCHEMA`
-- decides placement. It is mechanically feasible; measured on 2026.1:
--
--   * `OPEN SCHEMA X` + an UNQUALIFIED `CREATE ... SCRIPT FOO_V1` lands in X;
--   * `exa.meta.script_schema` IS available inside a module dispatched by MASTER
--     and reports the MODULE's schema, so a module could self-locate.
--
-- It is nevertheless NOT the direction, and the reason is worth understanding
-- because it is the same reason this file's approach is sound.
--
-- Position-independence would force every companion reference to be BUILT AT
-- RUNTIME (`import(HOME .. ".CONFD_QUERY_V1", "q")`), because two things measured
-- here cannot be unqualified: an unqualified `import()` FAILS inside preprocessor
-- dispatch, and SQL a module EMITS is compiled in the END USER's session, whose
-- open schema is arbitrary. So the contract would trade explicit, greppable,
-- reviewable names for string-built ones - turning late binding of a CONSTANT into
-- late binding of a VARIABLE, in a system where a dangling reference is expensive:
-- a bad name in emitted SQL breaks the caller's own statement (fail-open cannot
-- help - see the hazard note in section 0), and a failed `import()` can lock the operator
-- out of the admin surface entirely.
--
-- Keeping library artifacts fully qualified means an auditor reads `module.toml`
-- and knows exactly what lands where, and the names are verifiable by inspection
-- rather than by execution.
--
-- The escape hatch for the minority case is this file: edit the artifact, deploy it
-- where you want, and REGISTER it (section 1a) so it stays governed. What that costs is
-- the automatic fetch and the verified hash - not provenance, and not the
-- lifecycle. `docs/module-authoring.md` names this mode explicitly and its worked
-- example is `SEMANTIC_VIEWS_RT.ENTRY_V3`, deliberately NOT in `PREPROC_RT`.
--
-- Design discussion: https://github.com/exasol-labs/preprocessor-framework/issues/11
--
-- ONE HAZARD TO KNOW ABOUT WHEN YOU RE-HOME BY HAND (section 0), because the framework
-- cannot catch it for you. Script bodies are OPAQUE to Exasol at creation time -
-- it resolves an `import` or an embedded query only when the script RUNS. Both of
-- these CREATE successfully today, hardcoded schema or not:
--
--   CREATE OR REPLACE LUA SCRIPT X_V1 AS
--   function expand(s) import("PREPROC_RT.MISSING_V1", "m") return m.f(s) end
--
--   CREATE OR REPLACE LUA SCRIPT Y_V1 AS
--   function expand(s) return "SELECT PREPROC_RT.NO_SUCH_UDF_V1('x')" end
--
-- (A VIEW naming that same missing UDF IS rejected at creation - the late binding
-- is specific to script bodies. `src/preproc/module/validate.py` documents this.)
--
-- So if your `sed` misses a reference, nothing complains until a user runs the
-- command - and note what that failure looks like: a module that emits SQL naming
-- a missing object turns the CALLER'S OWN statement into
-- `function or script ... not found`. MASTER's fail-open does not apply, because the
-- module did not throw; it returned a valid string that happens to name a missing
-- object. Fail-open covers a module that ERRORS, never one that produces
-- unresolvable SQL. A conformant EXPAND module limits the damage to its own
-- commands (it returns nil for everything else); a REWRITE module would not.
--
-- Practical check after re-homing - confirm the whole inventory actually landed,
-- and that nothing still points at the old schema:
--
--   SELECT SCRIPT_TYPE, COUNT(*) FROM EXA_ALL_SCRIPTS
--   WHERE SCRIPT_SCHEMA = 'CONFD_GUARD' GROUP BY 1;
--   -- expect exactly: SCRIPTING 6  (entry + 5 ADMIN_*),  UDF 9  = 15 objects
--
--   SELECT SCRIPT_NAME FROM EXA_ALL_SCRIPTS
--   WHERE SCRIPT_SCHEMA = 'CONFD_GUARD'
--     AND (SCRIPT_TEXT LIKE '%PREPROC_RT.%' OR SCRIPT_TEXT LIKE '%PREPROC.ADMIN\_%' ESCAPE '\');
--   -- expect 0 rows: anything here is a reference the sed missed
--
-- Then actually run one command of each kind - an inline read and one routed
-- through an ADMIN_* script (see section 4) - before you hand the role to anyone.


-- -----------------------------------------------------------------------------
-- 6. TEARDOWN
-- -----------------------------------------------------------------------------
-- Rules before scripts (file 01 section 7).

PREPROC LIST RULES;
-- PREPROC DROP RULE <the CONFD_GUARD rule id>;

DROP USER IF EXISTS DANA CASCADE;
DROP USER IF EXISTS JOE  CASCADE;
DROP ROLE IF EXISTS CLUSTER_ADMIN CASCADE;
DROP SCHEMA IF EXISTS CONFD_GUARD CASCADE;

PREPROC REFRESH;
