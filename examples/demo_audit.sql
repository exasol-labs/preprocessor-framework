-- =============================================================================
-- EXAMPLE preprocessor: DEMO_AUDIT_V1  (REWRITE phase)
-- =============================================================================
-- Appends an inert  /* PREPROC_AUDIT user=<CURRENT_USER> */  comment to every
-- statement. The result set is unchanged; it only demonstrates that a REWRITE
-- rule fired and for which user. A demonstration module, not an audit-logging
-- feature (it writes to no table).
--
-- Prerequisite: the framework is installed (run release.sql) and the
-- preprocessor is active for your session:
--   ALTER SESSION SET sql_preprocessor_script = PREPROC_RT.MASTER;
-- =============================================================================

OPEN SCHEMA PREPROC_RT;

--/
CREATE OR REPLACE LUA SCRIPT PREPROC_RT.DEMO_AUDIT_V1() AS
-- =============================================================================
-- DEMO_AUDIT_V1 preprocessor module  (PREPROC_RT.DEMO_AUDIT_V1)
-- =============================================================================
-- Canonical authoring reference for REWRITE-phase modules. Read demo_ping_v1
-- for the full authoring contract; this file applies that contract to a
-- behaviour-annotating REWRITE rule.
--
-- REWRITE-phase return contract
--   On REWRITE the dispatcher runs every matching rule and threads the returned
--   string forward (run-all threading), not first-match-terminal. This module
--   ALWAYS applies: it returns the annotated statement string on every path and
--   never returns nil.
--
-- What it does (and explicitly does not do)
--   It appends an SQL comment  /* PREPROC_AUDIT user=<CURRENT_USER> */  to the
--   statement so the demo can show, at a glance, that a REWRITE rule fired and
--   for which user. The result is a single valid Exasol statement that runs
--   identically in meaning to the original. This is a DEMONSTRATION module, not
--   an audit-logging feature: it writes nothing to any table (per-statement
--   audit writes are out of scope -- transaction-conflict hazard).
--
-- User identity derived inside the module
--   The user identity is the CURRENT_USER the framework runs under, obtained via
--   pquery here -- never parsed from the submitted statement text.
--
-- No defensive pcall
--   MASTER wraps this call in pcall (fail-open at the rule level). Reading
--   pquery's ok flag is ordinary control flow; a genuine throw is left to
--   propagate so MASTER's pcall can skip the rule visibly.
-- =============================================================================

-- luacheck: globals tag

-- tag(sqltext) -> the statement with an appended
-- /* PREPROC_AUDIT user=<CURRENT_USER> */ comment. Always returns a string.
function tag(sqltext)
    local user = "UNKNOWN"
    local ok, result = pquery("SELECT CURRENT_USER")
    if ok and result ~= nil and result[1] ~= nil and result[1][1] ~= null then
        user = tostring(result[1][1])
    end
    return sqltext .. " /* PREPROC_AUDIT user=" .. user .. " */"
end
/



-- -----------------------------------------------------------------------------
-- Register this module with the framework
-- -----------------------------------------------------------------------------
-- A module does nothing until a PREPROC.CONFIG rule points at it and a REFRESH
-- resolves that rule. The PREPROC command sugar below does both (RULE_ADD
-- auto-refreshes). It requires the preprocessor to be active for your session
-- and that you hold PREPROC_ADMIN. Edit the scope: FOR ROLE <role> applies to
-- every holder of that role; FOR USER <user> applies to a single user.

PREPROC ADD RULE FOR ROLE PREPROC_ADMIN PHASE REWRITE SCRIPT PREPROC_RT.DEMO_AUDIT_V1 FUNCTION tag;

-- If the preprocessor is not active yet, call the admin script directly instead:
--   EXECUTE SCRIPT PREPROC.RULE_ADD('ROLE', 'PREPROC_ADMIN', 'REWRITE', 'PREPROC_RT.DEMO_AUDIT_V1', 'tag', NULL);
